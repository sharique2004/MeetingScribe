"""Real names on speaker labels, read out of the conversation itself.

People say who they are and who they are talking to all the time: "Hi, I'm
Marcus from engineering", "Thanks, Priya", "Priya, what do you think?". This
module reads the finished transcript with the on-device model, finds those
moments, and writes the names onto clusters that would otherwise stay
"Speaker 1 / Speaker 2". No new permission, no window scraping, nothing
leaving the Mac on the default engine.

It runs AFTER voice_profiles.apply_recognition, so a remembered voice always
beats an inference, and it uses the same seam: defaults-only, never a name a
human typed, never the "you" key.

THE ONE RULE: EVIDENCE OR NOTHING.
---------------------------------
Every name that lands here carries an anchored quote proving it, and the
quote is what decides who gets the name. The model is used for one job only:
noticing that a portion of the transcript contains a naming moment, and
copying the words that show it. Everything downstream is arithmetic:

  1. The model's quote is matched back to the turns that were actually in its
     prompt (ask._anchor_citation). The model's own timestamp is discarded as
     a guess and the winning turn supplies the real one. A quote that matches
     nothing is DROPPED, never relocated. An identity claim with no anchor is
     exactly the claim you want thrown away.
  2. The anchored TURN tells us which cluster spoke those words. The model is
     never asked which speaker it means, so it can never mislabel one.
  3. The turn's own text is re-read in Python for the naming pattern. The
     model's "kind" is logged but not trusted; the regex decides.
  4. The spelling that gets stored is the TRANSCRIPT's, not the model's, so a
     model that "corrects" Sharique to Shariq cannot rename anybody.

INTRODUCTION AND ADDRESS ARE NOT THE SAME CLAIM.
-----------------------------------------------
A self-introduction ("I'm Marcus") names the SPEAKER of the turn. That is a
one-step lookup and it is safe.

A direct address ("Thanks, Priya") names a DIFFERENT person, and guessing
which one is how two people get silently swapped. The usual heuristic is
adjacency ("it must be whoever spoke last"), and it is wrong often enough to
be dangerous: "Thanks, Priya" looks backwards, "Priya, what do you think?"
looks forwards, and a three-way conversation offers no reliable signal for
either. So THIS MODULE NEVER GUESSES THE DIRECTION. An address claim resolves
only when the arithmetic leaves no choice:

    target = every cluster in the meeting - the cluster that spoke the line

If that set has exactly one member, the address is unambiguous and the name
lands on it. If it has two or more, the claim is DROPPED. In practice that
means address works on two-party meetings (which is most of them) and stays
out of the way in a room, where self-introductions carry the load instead.

The address form is also verified against the transcript, not inferred: the
name has to appear in a vocative position in the anchored turn ("Priya, ...",
"thanks Priya", "..., Priya?"), so a name that is merely being talked ABOUT
("I spoke to Priya yesterday") never counts as an address.

FAILURE IS FREE.
----------------
Names are exactly the content Apple's guardrail is touchy about, so a refused
portion is expected, not exceptional: it is logged and skipped, and the rest
of the meeting still gets read. The whole entry point is wrapped, so a crash,
a guardrail, an unavailable model or a bug in here costs nobody a transcript.
"""

import json
import logging
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import ask
import local_llm
import summarize
import tech_vocabulary
import voice_profiles

log = logging.getLogger("meetingscribe.names")

# One portion per model call. A little under ask.APPLE_SWEEP_CHARS because
# these instructions are longer than the sweep's.
PORTION_CHARS = 6000
# Concurrency. local_llm caps every caller at MAX_INFLIGHT=3; two workers
# leaves room for whatever else the app is doing on the Neural Engine.
WORKERS = 2
# A guard on absurdly long meetings, not a policy. When it bites, the MIDDLE
# is dropped: introductions cluster in the first minutes, so the head is the
# part that must survive.
MAX_PORTIONS = 12
HEAD_SHARE = 0.75
# Claude sees the meeting in one call; this bounds what that call costs.
CLAUDE_MAX_CHARS = 120_000
CLAUDE_TIMEOUT_S = 180

MAX_NAME_CHARS = 40

# One naming pass at a time per process. A pass is a burst of concurrent
# on-device calls, and two bursts at once is what ask.py's sweep lock exists
# to prevent.
_PASS_LOCK = threading.Lock()


# ------------------------------------------------------------ what the model does --

CLAIM_SCHEMA = {
    "type": "object", "name": "NamesInPortion", "properties": [
        {"name": "claims", "type": "array", "max": 4,
         "items": {"type": "object", "name": "NameClaim", "properties": [
             {"name": "name", "type": "string",
              "description": "the person's name, spelled EXACTLY as the line spells it"},
             {"name": "kind", "type": "string",
              "description": "\"introduction\" when the speaker is naming themselves, "
                             "\"address\" when the speaker is saying the name of the "
                             "person they are talking to"},
             {"name": "quote", "type": "string",
              "description": "a short fragment copied word for word from the one line "
                             "that shows this, including the name"},
             {"name": "confidence", "type": "string",
              "description": "\"high\" when the line plainly says it, \"medium\" when it "
                             "is likely, \"low\" when it is a guess"},
         ]},
         "description": "every moment in THIS portion where someone introduces "
                        "themselves or is spoken to by name; empty when there are none"},
    ],
}

INSTRUCTIONS = (
    "You are reading a meeting transcript to find the moments where a person in "
    "the meeting is named out loud. There are only two kinds:\n"
    "- introduction: a speaker names THEMSELVES. \"I'm Marcus\", \"my name is "
    "Priya\", \"Marcus here\", \"hi, this is Dana\".\n"
    "- address: a speaker says the name of the person they are TALKING TO. "
    "\"Thanks, Priya\", \"Priya, what do you think?\", \"over to you, Marcus\".\n"
    "Report nothing else. A name that belongs to a company, a product, a place, "
    "or to somebody who is being talked ABOUT rather than talked TO is not a "
    "claim: \"I spoke to Priya yesterday\" names nobody in this meeting.\n"
    "For each claim, copy a short fragment WORD FOR WORD out of the single line "
    "that shows it. The fragment must appear in that line exactly as written, "
    "and must contain the name. Never invent, complete, correct or translate a "
    "name; spell it exactly as the line spells it.\n"
    "The names above are made up, to show you the shape of the two kinds. They "
    "are not in this meeting: never report a name that appears only in these "
    "instructions.\n"
    "\"Speaker 1\", \"Speaker 2\", \"Remote 1\" and \"You\" are the labels this "
    "transcript uses when it does not know who somebody is. They are not names "
    "and are never a claim.\n"
    "Most portions contain no claim at all. An empty list is the normal answer, "
    "and it is much better than a guess."
)

_ROSTER_RULE = (
    "\nThe people in this meeting are, from its calendar invite: {names}. "
    "Those are the ONLY names that can be correct. If a line names somebody "
    "else, it is not one of these people and there is no claim to report."
)


# ------------------------------------------------------------------ name shapes --

# What a person's name may look like once the transcript has spelled it: one
# or two capitalised tokens, each with a lower-case letter somewhere in it.
# Apostrophes and hyphens are in ("D'nika", "O'Brien", "Anne-Marie"); an
# initialism is not ("IPC, yeah" and "SF?" both sit in vocative position in
# this corpus and neither is a person).
_NAME_TOKEN = r"[A-Z][A-Za-z'’.\-]*[a-z][A-Za-z'’.\-]*"
_NAME_RE = re.compile(rf"^{_NAME_TOKEN}(?: {_NAME_TOKEN})?$")

# Words that survive the capitalisation test and are still not people. The
# recognizer capitalises sentence openings, and "I'm Sorry" is a sentence, not
# an introduction. Fillers earn their place here the same way: "Um, ..." and
# "Uh, ..." open a third of the turns in this corpus, and a vocative test that
# does not know they are noise reads every one of them as somebody's name.
_NOT_A_NAME = {
    "a", "absolutely", "actually", "afternoon", "again", "all", "almost",
    "already", "also", "always", "an", "and", "any", "anyway", "april",
    "august", "back", "bad", "because", "been", "before", "better", "both",
    "busy", "but", "bye", "certainly", "cool", "curious", "december", "did",
    "does", "doing", "done", "down", "early", "east", "east", "either",
    "employee", "engineer", "enough", "even", "evening", "every", "exactly",
    "excited", "february", "fine", "first", "for", "friday", "from", "genuinely",
    "glad", "going", "good", "gonna", "got", "great", "guys", "happy", "have",
    "he", "hello", "here", "hey", "hi", "his", "honestly", "hoping", "how",
    "however", "i", "if", "in", "interested", "into", "is", "it", "january",
    "july", "june", "just", "kind", "kinda", "last", "later", "like", "little",
    "looking", "maybe", "march", "may", "me", "monday", "morning", "much", "my",
    "never", "new", "next", "nice", "no", "north", "not", "november", "now",
    "october", "of", "off", "ok", "okay", "on", "one", "only", "or", "other",
    "our", "out", "over", "perfect", "please", "pretty", "probably", "quite",
    "ready", "really", "right", "same", "saturday", "september", "she", "so",
    "some", "sorry", "south", "speaker", "still", "sure", "sunday", "super",
    "team", "thank", "thanks", "that", "the", "their", "them", "then", "there",
    "these", "they", "thinking", "this", "those", "thursday", "to", "today",
    "tomorrow", "too", "totally", "trying", "tuesday", "under", "up", "us",
    "very", "wednesday", "week", "welcome", "well", "we", "west", "what", "when",
    "where", "which", "while", "who", "why", "will", "with", "working", "would",
    "yeah", "year", "yes", "yesterday", "yet", "you", "your", "yours", "yep",
    # fillers, backchannels and hedges: the loudest false-positive class in
    # real speech, because they sit exactly where a vocative sits
    "ah", "agreed", "alright", "amazing", "anyway", "anyways", "apparently",
    "awesome", "basically", "brilliant", "clearly", "correct", "definitely",
    "er", "erm", "excellent", "frankly", "hmm", "hopefully", "huh",
    "interesting", "literally", "mhm", "mm", "nah", "noted", "nope",
    "obviously", "oh", "seriously", "surely", "uh", "understood", "um", "wow",
    "yup",
    # sentence adverbs and imperatives. "Typically, ...", "Look, ..." and
    # "Go, go, go" all park a capitalised word in front of a comma
    "additionally", "bro", "buddy", "congrats", "congratulations", "come",
    "dude", "essentially", "eventually", "finally", "firstly", "fortunately",
    "generally", "go", "ideally", "imagine", "importantly", "initially",
    "listen", "look", "luckily", "man", "mate", "meanwhile", "nonetheless",
    "otherwise", "personally", "recently", "regardless", "remember", "secondly",
    "similarly", "specifically", "stop", "technically", "therefore", "thirdly",
    "typically", "ultimately", "unfortunately", "usually", "wait",
    # titles and collective address
    "dr", "everybody", "everyone", "folks", "madam", "mr", "mrs", "ms",
    "prof", "professor", "sir",
    # assistants and platforms that get spoken to like people
    "alexa", "google", "siri",
}

# Product and company names the recognizer is already biased towards. A brand
# in a vocative slot ("Salesforce, what do you think" is rare; "…, Intel." is
# not) is the one false positive no grammar can catch, and this list already
# exists and is already maintained. Taken from the BUILT-IN list only: the
# user's own `vocabulary` setting is documented as a place to put colleagues'
# names, so excluding it would block exactly the people this module is for.
_PRODUCT_NAMES = {word.casefold()
                  for entry in tech_vocabulary.DEFAULT_VOCABULARY
                  for word in str(entry).split()}

# "I'm", "That's", "It's" pass the capitalised-token test and are contractions,
# not people. Two letters or fewer after an apostrophe at the end of a token is
# a contraction; "D'nika" and "O'Brien" carry a whole name after theirs.
_CONTRACTION_RE = re.compile(r"['’](?:s|m|d|t|ll|ve|re)$", re.I)

# "I'm not Marcus" introduces nobody. Checked against what comes BEFORE the
# match, so a negation later in the sentence does not cancel a real one.
_NEGATED_RE = re.compile(r"\b(?:not|never|n['’]t)\b")


def _plausible(token):
    """Could this single token be a person's name?"""
    # Trailing punctuation travels with the word ("Mr.", "Marcus'") and must
    # not be what lets a stoplisted word through.
    folded = token.casefold().strip(".'’-")
    return (2 <= len(token) <= 20
            and folded not in _NOT_A_NAME
            and folded not in _PRODUCT_NAMES
            and not _CONTRACTION_RE.search(token))


def _clean_name(name):
    """The model's name field, trimmed to something we would put on screen."""
    name = " ".join(str(name or "").split())[:MAX_NAME_CHARS]
    parts = name.split()
    if not 1 <= len(parts) <= 2:
        return ""
    return name


def _canonical_name(text, name):
    """The TRANSCRIPT's spelling of `name` inside `text`, or "".

    The model's spelling is never the one that ships: it routinely tidies
    "Sharique" into "Shariq" and lower-cases what it copies. Finding the name
    in the turn again does three jobs at once: it proves the name is really
    in the evidence, it hands back the transcript's own capitalisation, and it
    rejects a word the recognizer wrote in lower case, which is how "I'm good"
    stops being a person called Good.
    """
    name = _clean_name(name)
    if not name or not all(_plausible(w) for w in name.split()):
        return ""
    pattern = r"\b" + r"\s+".join(re.escape(w) for w in name.split()) + r"\b"
    for match in re.finditer(pattern, text, re.I):
        found = " ".join(match.group(0).split())
        if _NAME_RE.match(found) and all(_plausible(w) for w in found.split()):
            return found
    return ""


def _sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", str(text or "")) if s.strip()]


_CAPITALISED_RE = re.compile(r"\b[A-Z][A-Za-z'’.\-]*\b")


def _candidate_names(text):
    """Every one or two word capitalised phrase in `text` that could be a name.

    This is the same net _canonical_name casts, thrown by us instead of by the
    model. It exists for _has_reachable_evidence below.
    """
    tokens = [t for t in _CAPITALISED_RE.findall(text)
              if _NAME_RE.match(t) and _plausible(t)]
    pairs = [f"{a} {b}" for a, b in zip(tokens, tokens[1:])
             if re.search(rf"\b{re.escape(a)}\s+{re.escape(b)}\b", text)]
    return tokens + pairs


def _has_reachable_evidence(entries):
    """Could ANY claim survive verification against this transcript?

    A claim is only ever accepted when the anchored turn reads as an
    introduction or a vocative for a plausible name, so a transcript where no
    turn does either cannot produce a name no matter what the model says.
    Checking that here costs a few milliseconds of regex and saves a whole
    sweep of model calls on the meetings, roughly a third of this corpus, that
    were never going to yield anything. It is a necessary condition of the
    verifier, not a heuristic: what it skips, the verifier would have thrown
    away one call later.
    """
    for entry in entries:
        text = entry["text"]
        for name in _candidate_names(text):
            if _is_self_introduction(text, name) != _is_direct_address(text, name):
                return True
    return False


def _is_self_introduction(text, name):
    """Does `text` introduce its own speaker as `name`?"""
    n = re.escape(name)
    possessive = r"(?![’']s\b)"
    patterns = [
        rf"\bI\s*[’']?\s*m\s+{n}\b{possessive}",
        rf"\bI\s+am\s+{n}\b{possessive}",
        rf"\bmy\s+name\s*(?:[’']s|is)\s+{n}\b{possessive}",
        # "Marcus here" only where a turn or sentence opens: "is Marcus here?"
        # is somebody looking for him, not Marcus saying so.
        rf"^\W*{n}\s+here\b",
        rf"\bcall\s+me\s+{n}\b{possessive}",
        rf"\bI\s+go\s+by\s+{n}\b{possessive}",
    ]
    for sentence in _sentences(text):
        for pattern in patterns:
            match = re.search(pattern, sentence, re.I)
            if match and not _NEGATED_RE.search(sentence[:match.start()]):
                return True
    # "This is Dana" introduces the speaker at the START of a turn and somebody
    # ELSE in the middle of one ("...and this is Dana, our CTO"), so it counts
    # only where a phone-style opener can be.
    opener = rf"^\W*(?:hi|hey|hello|good\s+\w+)?[\s,]*this\s+is\s+{n}\b{possessive}"
    head = _sentences(text)[:1]
    return bool(head and re.search(opener, head[0], re.I))


def _is_direct_address(text, name):
    """Is `name` spoken TO in `text`, vocative rather than merely mentioned?

    Every pattern here pins the name against something that only happens when
    you are talking to a person: the start of a sentence followed by a pause,
    a greeting or thanks with nothing in between, a hand-off, or a question
    aimed at them. "I spoke to Priya yesterday" matches none of them, which is
    the whole point.
    """
    n = re.escape(name)
    greet = (r"thanks|thank\s+you|hi|hey|hello|welcome\s+back|welcome|bye|"
             r"goodbye|good\s+morning|good\s+afternoon|good\s+evening|"
             r"congrats|congratulations|nice\s+to\s+meet\s+you|"
             r"great\s+to\s+meet\s+you|cheers|sorry")
    # "over to you, Priya" hands the floor to a person. A bare "over to X"
    # does not: measured on this corpus, "So he's taking you over to Apple
    # Park for lunch" put a business park on a speaker label. Requiring "you"
    # is what makes it a hand-off rather than a destination.
    handoff = (r"over\s+to\s+you|back\s+to\s+you|"
               r"handing\s+(?:it\s+)?(?:over\s+)?to\s+you|go\s+ahead")
    followup = (r"what|how|why|when|where|do\s+you|did\s+you|can\s+you|"
                r"could\s+you|would\s+you|are\s+you|were\s+you|any\s+thoughts|"
                r"your\s+thoughts|go\s+ahead|take\s+it\s+away|over\s+to\s+you")
    # A vocative sits at the front of what the speaker is saying, behind at
    # most a filler word. Without this anchor "I asked Priya what she thought"
    # reads as "Priya, what…" and a bystander gets somebody else's name.
    opener = (r"(?:so|and|but|ok|okay|alright|right|now|well|um|uh|yeah|yes|"
              r"no|sorry|thanks|thank\s+you|great|perfect|cool|hey|hi|hello)")
    front = rf"^\W*(?:{opener}\b)?[\s,]*"
    patterns = [
        rf"{front}{n}\s*[,?!:]",                    # "So Priya, ..."
        rf"\b(?:{greet})[,\s]+{n}\b",               # "thanks, Priya"
        rf"\b(?:{handoff})[,\s]+{n}\b",             # "over to you, Priya"
        rf"{front}{n}\s*,?\s+(?:{followup})\b",     # "Priya what do you think"
    ]
    for sentence in _sentences(text):
        for pattern in patterns:
            if re.search(pattern, sentence, re.I):
                return True
        # "..., Priya?" at the end of a sentence is an address; "we interviewed
        # Bob, Priya" is a list. The difference is whether the word before the
        # comma is itself a name, so that is what gets checked rather than
        # trusting the comma.
        tail = re.search(rf"(\S+)\s*,\s*{n}\s*[.?!]?\s*$", sentence, re.I)
        if tail and not _NAME_RE.match(tail.group(1).strip(",")):
            return True
    return False


# --------------------------------------------------------------------- reading --

def _portions(entries):
    """The transcript as portions, head-weighted when it will not all fit.

    ask._sweep_portions splits it; the only decision here is what to drop when
    a meeting is long enough to cost more model time than it is worth. The
    MIDDLE goes. Introductions happen in the first minutes and the head is
    where the names are, so trimming from the front would throw away the
    evidence this module exists to find.
    """
    portions = ask._sweep_portions(entries, PORTION_CHARS)
    if len(portions) <= MAX_PORTIONS:
        return portions
    head = max(1, int(MAX_PORTIONS * HEAD_SHARE))
    tail = MAX_PORTIONS - head
    log.info("names: %d portions, reading the first %d and last %d",
             len(portions), head, tail)
    return portions[:head] + (portions[-tail:] if tail else [])


def _prompt_instructions(roster):
    if not roster:
        return INSTRUCTIONS
    return INSTRUCTIONS + _ROSTER_RULE.format(names=", ".join(roster))


def _claims_from_portion(index, total, portion_entries, roster, progress):
    """One model call over one portion -> its verified claims.

    A refusal is not an error here. Names and personal identification are what
    the on-device guardrail is most likely to decline, so a declined portion
    is logged and the sweep carries on; over a whole meeting the surviving
    portions usually still hold the introduction.
    """
    body = "\n".join(e["line"] for e in portion_entries)
    prompt = f"PORTION {index + 1} of {total} of the transcript:\n{body}"
    try:
        raw = local_llm.generate(_prompt_instructions(roster), prompt,
                                 CLAIM_SCHEMA, max_tokens=400)
    except local_llm.LocalLLMError as exc:
        log.info("names: portion %d/%d declined or failed (%s): %s",
                 index + 1, total, exc.code, exc)
        raw = {}
    finally:
        progress(index)
    return _verify_claims(raw, portion_entries, index, roster)


def _turn_at(portion_entries, anchor):
    """The turn _anchor_citation landed on.

    It hands back a whole-second timestamp rather than the entry, and two
    turns can round into the same second, so where there is more than one
    candidate the quote itself breaks the tie.
    """
    second = int(anchor.get("t", -1))
    matches = [e for e in portion_entries if int(e["start"]) == second]
    if not matches:
        return None
    quote = ask._norm(anchor.get("quote") or "")
    if quote:
        for entry in matches:
            if quote in ask._norm(entry["text"]):
                return entry
    return matches[0]


def _verify_claims(raw, portion_entries, portion_index, roster):
    """Model output -> the claims that survive the transcript. Never raises."""
    out = []
    if not isinstance(raw, dict):
        return out
    for claim in (raw.get("claims") or [])[:8]:
        if not isinstance(claim, dict):
            continue
        # The quote decides everything. _anchor_citation matches it back to the
        # turns that were in this portion's prompt (verbatim first, then word
        # overlap at 0.5) and hands back the real timestamp; a quote that
        # matches nothing gets None and the claim dies here.
        anchor = ask._anchor_citation({"quote": claim.get("quote")}, portion_entries)
        if not anchor:
            continue
        turn = _turn_at(portion_entries, anchor)
        if turn is None:
            continue
        name = _canonical_name(turn["text"], claim.get("name"))
        if not name:
            # MEASURED, not hypothesised: on this corpus the 3B on-device
            # model fills `name` with the transcript's own label far more
            # often than with a person. Shown the line "Hey, Zach." it
            # returned {"kind": "address", "quote": "Hey, Zach.", "name":
            # "You"} - the right evidence with the label of the person being
            # addressed where the name should be. The name it could not
            # produce is sitting in the words it copied, so it is taken from
            # there instead, under the same rule as everything else: from the
            # transcript, in a verified naming position, or not at all.
            name = _name_from_quote(turn["text"], anchor.get("quote"))
        if not name or voice_profiles.is_default_name(name):
            continue
        if roster and not _in_roster(name, roster):
            continue
        # The MODEL's kind is not consulted. What the anchored turn actually
        # says is: an introduction has to read like one, an address has to be
        # vocative, and a name that is only mentioned is neither.
        intro = _is_self_introduction(turn["text"], name)
        address = _is_direct_address(turn["text"], name)
        if intro == address:      # neither form, or both at once: not decidable
            continue
        out.append({
            "name": name,
            "kind": "introduction" if intro else "address",
            "model_kind": str(claim.get("kind") or "").strip().lower(),
            "confidence": str(claim.get("confidence") or "").strip().lower(),
            "quote": _evidence_quote(turn["text"], name, anchor.get("quote")),
            "t": int(anchor["t"]),
            "speaker": turn.get("key"),
            "portion": portion_index,
        })
    return out


def _name_from_quote(text, verbatim_quote):
    """The person named inside the fragment the model copied, or "".

    Only ever called on a VERBATIM anchor, which is the whole safety of it:
    _anchor_citation hands back a quote string only when those exact words
    are in that exact turn, so this reads a real fragment of a real line
    rather than mining a turn that a paraphrase happened to land near. The
    fragment must offer exactly one candidate, in a verified naming position,
    or the claim dies as ambiguous.
    """
    if not verbatim_quote:
        return ""
    found = {}
    for cand in _candidate_names(verbatim_quote):
        if _is_self_introduction(text, cand) != _is_direct_address(text, cand):
            found[cand.casefold()] = cand
    if not found:
        return ""
    # "Amitav" and "Amitav Das" are one candidate seen twice, not two.
    longest = max(found.values(), key=len)
    parts = set(longest.casefold().split())
    if all(set(c.casefold().split()) <= parts for c in found.values()):
        return longest
    return ""


def _evidence_quote(text, name, model_quote):
    """The words a human should be shown to check this name.

    NOT simply what the model copied. The model quotes whatever caught its
    eye, and on a five minute turn that is often a sentence next to the one
    that names somebody: shown on its own it proves nothing. What proves the
    name is the sentence the verifier matched, so that is what gets stored,
    and the model's fragment is only the fallback.
    """
    for sentence in _sentences(text):
        if _canonical_name(sentence, name) and (
                _is_self_introduction(sentence, name)
                or _is_direct_address(sentence, name)):
            return " ".join(sentence.split())[:200]
    return (str(model_quote or "") or text)[:200]


def _in_roster(name, roster):
    folded = {r.casefold() for r in roster}
    for entry in roster:
        folded.update(part.casefold() for part in entry.split())
    return all(part.casefold() in folded for part in name.split())


def _roster_names(meta):
    """The calendar invite's attendee names, or []. A hallucinated name is the
    worst outcome this module can produce, so when the meeting came with a
    roster the candidates are pinned to it."""
    names = ((meta.get("calendar_event") or {}).get("names")) or []
    out = []
    for raw in names:
        cleaned = " ".join(str(raw or "").split())[:MAX_NAME_CHARS]
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out[:12]


def _entries(turns, speakers):
    """ask._turn_entries over turns that are not on `meta` yet.

    The pipeline calls this module between building the turns and publishing
    them, so the entries are built from the live objects rather than from
    meeting.json. Same shape, same "[m:ss] Speaker 1: text" line, so the
    anchoring machinery is the identical code path the Ask sweep uses.

    One field is added: the CLUSTER KEY of the turn. ask's entries carry the
    display name inside `line` and nothing else, and the display name is
    exactly what this module is about to change, so the key has to travel
    separately. `i` indexes back into `turns`, which is where it comes from.
    """
    entries = ask._turn_entries({"turns": turns, "speakers": speakers})
    for entry in entries:
        try:
            entry["key"] = turns[entry["i"]]["speaker"]
        except (IndexError, KeyError, TypeError):
            entry["key"] = None
    return [e for e in entries if e["key"]]


# ------------------------------------------------------------------- resolving --

_CONF_RANK = {"high": 3, "medium": 2, "low": 1}


def _rank(confidence):
    return _CONF_RANK.get(str(confidence or "").strip().lower(), 1)


def _target_key(claim, all_keys):
    """Which cluster this claim names, or None.

    An introduction names the speaker of its own turn: one step, no guessing.

    An address names somebody else, and the direction is NEVER inferred. If
    subtracting the speaker from the meeting leaves exactly one cluster, the
    address can only mean that one and it is safe. If it leaves two or more,
    the claim is dropped rather than pointed at whoever happened to speak
    next: a wrong address silently swaps two people, and no name at all is
    the better outcome.
    """
    speaker = claim.get("speaker")
    if not speaker:
        return None
    if claim["kind"] == "introduction":
        return speaker
    others = [k for k in all_keys if k != speaker]
    return others[0] if len(others) == 1 else None


def _resolve(claims, speakers):
    """Verified claims -> {key: evidence} for the names that may be applied.

    Three gates, in order:
      confidence   a single "high" claim, or two portions agreeing at "medium".
      agreement    the same cluster claimed as two different people, or the
                   same person claimed on two clusters, cancels out unless one
                   side has strictly more support. A tie drops BOTH.
      collision    a name already on the speaker map (a human typed it, or
                   voice profiles recognised it) is never duplicated.
    """
    all_keys = list(speakers)
    grouped = {}
    for claim in claims:
        key = _target_key(claim, all_keys)
        if not key or key == "you":
            continue
        if not voice_profiles.is_default_name(speakers.get(key)):
            continue          # a human typed this one; nothing to fill in
        slot = grouped.setdefault((key, claim["name"].casefold()), {
            "key": key, "name": claim["name"], "portions": set(),
            "best": claim, "rank": 0,
        })
        slot["portions"].add(claim["portion"])
        rank = _rank(claim["confidence"])
        if rank > slot["rank"] or (rank == slot["rank"]
                                   and claim["t"] < slot["best"]["t"]):
            slot["best"] = claim
        slot["rank"] = max(slot["rank"], rank)

    accepted = []
    for slot in grouped.values():
        support = len(slot["portions"])
        if slot["rank"] >= 3 or (slot["rank"] >= 2 and support >= 2):
            slot["support"] = support
            accepted.append(slot)
        else:
            log.info("names: %s -> %r held back (confidence %r, %d portion(s))",
                     slot["key"], slot["name"], slot["best"]["confidence"], support)

    accepted = _drop_contradictions(accepted, lambda s: s["key"])
    accepted = _drop_contradictions(accepted, lambda s: s["name"].casefold())

    taken = {voice_profiles._fold(v) for v in speakers.values()}
    out = {}
    for slot in sorted(accepted, key=lambda s: (-s["support"], -s["rank"],
                                                s["best"]["t"])):
        folded = slot["name"].casefold()
        if folded in taken:
            log.info("names: %r is already on this meeting, not reusing it", slot["name"])
            continue
        taken.add(folded)
        best = slot["best"]
        out[slot["key"]] = {
            "name": slot["name"],
            "quote": best["quote"],
            "t": best["t"],
            "kind": best["kind"],
            # WHO said the evidence. Equal to the key on an introduction, the
            # other person on an address. Without it an audit cannot tell the
            # two apart, and the address case is the one worth checking.
            "said_by": best["speaker"],
            "confidence": best["confidence"],
            "portions": slot["support"],
        }
    return out


def _drop_contradictions(accepted, key_of):
    """Keep one slot per `key_of` value: the best supported, or none.

    Two portions saying different things about the same cluster is the signal
    that the evidence is bad, not an invitation to pick a side. Only a strict
    margin resolves it; equal support means both go.
    """
    buckets = {}
    for slot in accepted:
        buckets.setdefault(key_of(slot), []).append(slot)
    out = []
    for group in buckets.values():
        if len(group) == 1:
            out.append(group[0])
            continue
        group.sort(key=lambda s: (s["support"], s["rank"]), reverse=True)
        first, second = group[0], group[1]
        if (first["support"], first["rank"]) > (second["support"], second["rank"]):
            out.append(first)
        else:
            log.info("names: contradictory claims %s, dropping all of them",
                     [(s["key"], s["name"]) for s in group])
    return out


# ---------------------------------------------------------------- the engines --

def _claims_apple(entries, roster, progress_cb):
    portions = _portions(entries)
    total = len(portions)
    done = {"n": 0}
    lock = threading.Lock()

    def progress(_index):
        with lock:
            done["n"] += 1
            progress_cb(f"Looking for names… {done['n']}/{total}")

    progress_cb(f"Looking for names… 0/{total}")
    with _PASS_LOCK:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            batches = pool.map(
                lambda pair: _claims_from_portion(pair[0], total, pair[1],
                                                  roster, progress),
                list(enumerate(portions)))
            return [c for batch in batches for c in batch]


_CLAUDE_FORMAT = (
    "\n\nReply with JSON only, in exactly this shape and nothing else:\n"
    '{"claims": [{"name": "...", "kind": "introduction" | "address", '
    '"quote": "...", "confidence": "high" | "medium" | "low"}]}\n'
    'Use {"claims": []} when the transcript contains no such moment.'
)


def _claims_claude(entries, roster, progress_cb):
    """The same pass through the user's own Claude, when that is the engine
    they chose. One call: it sees the whole meeting, so every claim anchors
    against every entry it was shown."""
    exe = summarize.find_claude()
    if exe is None:
        log.info("names: Claude engine selected but the CLI is not installed")
        return []
    kept, size = [], 0
    for entry in entries:
        size += len(entry["line"]) + 1
        if size > CLAUDE_MAX_CHARS:
            break
        kept.append(entry)
    body = "\n".join(e["line"] for e in kept)
    progress_cb("Looking for names…")
    try:
        # The transcript is untrusted input, so it goes through the same
        # sandbox the summary uses, on stdin, never after the variadic flags.
        with summarize.claude_sandbox() as (flags, cwd):
            proc = subprocess.run(
                [exe, "-p", "--output-format", "json",
                 "--model", summarize._summary_model()] + flags,
                input=_prompt_instructions(roster) + _CLAUDE_FORMAT
                + "\n\nTRANSCRIPT:\n" + body,
                capture_output=True, text=True, timeout=CLAUDE_TIMEOUT_S, cwd=cwd,
            )
        if proc.returncode != 0:
            log.info("names: claude exited %d", proc.returncode)
            return []
        raw = summarize._extract_json(json.loads(proc.stdout).get("result") or "")
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
        log.info("names: claude pass failed: %s", exc)
        return []
    return _verify_claims(raw, kept, 0, roster)


# --------------------------------------------------------------------- public --

def apply_inferred_names(meta, turns, speakers, cfg, progress_cb=lambda msg: None):
    """Put real names on formulaic speaker labels by reading the transcript.

    Mutates `speakers` in place and records the evidence on `meta` under
    "speaker_names" (removed, never left stale, when nothing was inferred).
    Returns the {key: name} that was applied, possibly empty.

    Never raises past a log. This is a bonus layer on a finished transcript
    and must not cost anyone one.
    """
    try:
        if not cfg.get("speaker_names", True):
            meta.pop("speaker_names", None)
            return {}
        replaceable = {k for k, v in speakers.items()
                       if voice_profiles.is_default_name(v) and k != "you"}
        if not replaceable or not turns:
            meta.pop("speaker_names", None)
            return {}

        entries = _entries(turns, speakers)
        if not entries or not _has_reachable_evidence(entries):
            log.info("names: nothing in this transcript could support a name")
            meta.pop("speaker_names", None)
            return {}
        roster = _roster_names(meta)

        if summarize._pick_engine() == "claude":
            claims = _claims_claude(entries, roster, progress_cb)
        else:
            ok, reason = local_llm.available()
            if not ok:
                log.info("names: on-device model unavailable (%s)", reason)
                meta.pop("speaker_names", None)
                return {}
            claims = _claims_apple(entries, roster, progress_cb)

        resolved = _resolve(claims, speakers)
        applied = {}
        for key, evidence in resolved.items():
            if key not in replaceable:
                continue
            speakers[key] = evidence["name"]
            evidence["roster"] = bool(roster)
            applied[key] = evidence["name"]
        if applied:
            meta["speaker_names"] = {k: resolved[k] for k in applied}
            log.info("names: inferred %s", applied)
        else:
            meta.pop("speaker_names", None)
        return applied
    except Exception:
        log.exception("names: inference failed, leaving the default labels")
        try:
            meta.pop("speaker_names", None)
        except Exception:
            pass
        return {}
