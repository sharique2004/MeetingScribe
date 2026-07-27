"""Ask this meeting — grounded Q&A over one meeting's transcript.

Same engine story as summarize.py: the preferred path is the user's own
`claude` CLI (no API key), and config "summary_engine": "apple" routes to
the on-device Apple Intelligence model instead. Only the transcript TEXT is
ever sent, never audio.

Two things make this different from summarizing:

1. The whole transcript is sent whenever it fits, and the budget is set so
   that it fits for every meeting the owner has. Only past that — a very long
   meeting, or the much smaller Apple on-device budget — do we degrade by
   RETRIEVAL: an evenly spaced skeleton of the meeting (so the model still
   sees the whole arc) plus the turns that actually match the question, with
   their neighbours for context, joined by explicit "…" gap markers. Nothing
   is invented and the model is told the excerpt is partial. That retrieval
   is LEXICAL, so it is a lossy last resort, never a latency optimisation:
   a question whose words do not appear in its own evidence ("did she
   apologise" vs a turn that says "sorry") retrieves none of it.

2. Every answer must be SEEKABLE. The model returns citations as
   {"t": seconds, "quote": ...}; _validate_citations() snaps each one onto a
   real turn and drops anything it cannot place, so the UI can never be
   handed a timestamp that seeks nowhere. The QUOTE leads that placement —
   the two audio tracks are diarized separately, so on real meetings ~49% of
   turns have another turn starting within a couple of seconds and a
   nearest-by-time snap lands on the wrong one about as often as not. A quote
   that belongs to no turn the model was shown is DROPPED, never quietly
   replaced with the words of whichever turn the clock landed on: a missing
   citation is recoverable, a fabricated one is not.
"""

import atexit
import heapq
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

import local_llm
import summarize
from summarize import NeedsClaudeError  # re-exported: the route catches it

log = logging.getLogger("meetingscribe.ask")

# Char budget for the transcript excerpt.
#
# This was briefly cut to 13,000 as a LATENCY setting (docs/ASK_LATENCY.md
# measured 22.2s -> 12.5s on one question) and it measurably broke answers.
# Retrieval here is purely LEXICAL, so a question whose words do not appear in
# the evidence retrieves nothing: "Did she apologise for anything, and what
# exactly did she say?" yields the terms {anything, apologise, exactly}, every
# turn containing "sorry" scores 0.0, and the apology at 32:46 of that meeting
# was simply never shown to the model — which then answered, correctly for what
# it could see, "the transcript excerpt doesn't show her apologising."
#
# Measured over 14 real questions on 9 of the owner's own meetings, with the
# gold evidence turns picked by content rather than by this file's scorer:
#
#     budget                          evidence recall   questions fully covered
#     13,000 + a "wide" regex path    28/60  =  46.7%   6/14
#     120,000                         60/60  = 100.0%   14/14
#
# So the budget is a QUALITY setting first, and it is back where it was. Every
# meeting the owner has fits inside it whole (the longest is 110 minutes and
# 74,894 chars), so the retrieval below is now the rare path — a meeting past
# roughly two hours of dense speech, plus the Apple one, which has always had
# to retrieve.
#
# What that costs, measured rather than quoted from the doc above. Paired runs,
# 13,000 vs 120,000, same question, same machine, 3 rounds each, medians.
# Meetings are named by the corpus pseudonyms in docs/DIARIZATION_AUDIT_ADDENDUM.md
# §0 (tools/eval_diarization.py prints the same labels):
#
#     meeting             first words        complete
#     Call E (34 min)     5.7s -> 7.9s     9.7s -> 11.8s
#     Room W (110 min)    4.7s -> 14.6s    9.5s -> 20.9s
#       (276 turns, 74,894 chars, a 75,179-char prompt whole)
#
# It is NOT one number, and it is not "about two seconds": that was one
# mid-sized meeting read as if it were all of them. The cost scales with the
# transcript, and on the LONGEST meeting the owner has it is about ten seconds
# to the first word (12.1-15.2s across those runs, against 4.3-6.4s truncated)
# and about eleven to a complete answer (17.0-21.4s against 9.0-11.0s). What
# the doc's 22.2s predicted was roughly right for a meeting this size; what
# streaming and the empty-MCP sandbox removed was the FIXED overhead, not the
# per-character cost, so a short meeting got fast and a long one did not.
#
# Ten seconds on the owner's one 110-minute meeting is still worth paying —
# every other meeting is under 52,000 chars, and the alternative is answering
# "she never apologised" about a call in which she said "I am so sorry" twice.
# But anyone re-litigating this budget should re-litigate it against ten
# seconds on the longest meeting, not two on a middling one.
MAX_PROMPT_CHARS = 120000

# The Apple on-device budget, and the two smaller ones it falls back to.
#
# This was 5,500 while the Claude budget was 120,000, and on the same gold set
# used above (14 questions over 5 of the owner's meetings, 41 evidence turns
# picked by reading the transcripts rather than by this file's scorer) 5,500
# recovered 16 of 41. So the on-device engine answered from a third of the
# evidence and then said, in as many words, "she did not apologize for
# anything" about a call in which she says "I'm so sorry" at 32:46.
#
# 11,000 is what the hardware actually allows, measured rather than guessed.
# local_llm.MAX_PROMPT_CHARS is an ADVISORY 9,000, derived from a 4096-token
# context at ~4 chars/token; the real ceiling is content-dependent, because
# what costs tokens is characters AND the model's own output. Probed on the
# owner's meetings with the real instructions, schema and max_tokens=700:
#
#     meeting                largest prompt accepted   first refusal
#     Room W (276 turns)             13,973              14,974
#     Call H                         12,983              13,972
#     Call D                         13,965              (none ≤14k)
#     Call E                         11,962              12,995
#     Call B (187 turns)             11,427              11,939
#
# Call B is the floor and it is not a clean one: it also refused once
# at 10,432 and then accepted 10,934, because a long answer spends the same
# context the prompt does. A single number therefore cannot be both generous
# and safe — so this is a LADDER, and _ask_apple() steps down it whenever the
# model says the request did not fit. The user sees a smaller excerpt, never
# "The request was too long for the on-device model."
#
# Recall on that gold set, measured at each rung:
#
#     5,500 (was)   16/41 = 39.0%    3/14 questions fully covered
#     8,000         20/41 = 48.8%    4/14
#     11,000 (now)  21/41 = 51.2%    4/14
#     120,000       41/41 =  100%   14/14   (the Claude path, for scale)
#
# Half the evidence is still missing, and no budget this model can hold will
# fix that — which is why _APPLE_PARTIAL_RULE and the coverage note in
# answer_question() make the gap VISIBLE instead of letting the answer assert
# absence from a fraction of the meeting.
APPLE_PROMPT_CHARS = 11000
APPLE_PROMPT_LADDER = (APPLE_PROMPT_CHARS, 8000, 5500)

SKELETON_SHARE = 0.25      # of the budget reserved for whole-meeting coverage
NEIGHBOUR_TURNS = 1        # turns of context kept either side of a hit
MIN_EXCERPT_CHARS = 200    # a kept turn is never trimmed shorter than this

CITATION_TOLERANCE_S = 2.0  # how far a cited "t" may sit from a real turn start
MAX_CITATIONS = 6
MAX_HISTORY_MESSAGES = 6
MAX_HISTORY_CHARS = 2000
MAX_QUESTION_CHARS = 1000
CLAUDE_TIMEOUT_S = 300

GAP_MARKER = "…"


def budget_for(question=None):
    """The transcript budget one question deserves: all of it.

    There was briefly a second, much smaller default here, with a regex opting
    "wide" questions (summarise, every, timeline, action items) into the full
    budget. It routed backwards. "Did anyone mention X", "how did it end" and
    "did she ever say Y" all took the SMALL budget, and those are precisely the
    questions that cannot be answered without the whole meeting: proving that
    something was never said means having read everything, and the end of a
    meeting is the part a truncated excerpt loses first. Worse, the routing was
    invisible — the model answered "the transcript excerpt doesn't show that"
    and the user had no way to know it had been handed a tenth of the call.
    One budget, large enough for the whole transcript, has no such failure mode.
    """
    return MAX_PROMPT_CHARS


ASK_INSTRUCTIONS = """You are answering a question about ONE meeting, for the person who attended it. You are given part or all of that meeting's transcript. Answer ONLY from the transcript.

Rules:
- If the transcript does not contain the answer, say so plainly ("The transcript doesn't cover that" / "They never said"). NEVER guess, and never use outside knowledge about the people or companies involved.
- The transcript is auto-generated: expect misheard words and names, and read for intent rather than quoting errors as fact.
- The speaker labelled "You" is the local user — the person asking you this question.
- Be direct and specific. 1-4 sentences unless the question genuinely needs a list. No preamble, no restating the question.

Return ONLY a JSON object — no markdown fences, no commentary — with exactly these fields:
{
 "answer": "your answer, in plain text",
 "citations": [{"t": 727, "quote": "a short verbatim fragment from that line"}]
}

About citations:
- "t" is the start time of the cited line IN WHOLE SECONDS. Every line is prefixed with [m:ss] — convert it: [12:07] is 727, [0:45] is 45, [104:03] is 6243.
- Cite ONLY lines that actually appear in the transcript excerpt above. Never cite a time you did not see.
- "quote" must be copied verbatim from that same line, at most 25 words.
- Give 1-4 citations for the lines that most directly support the answer, most important first. Use an empty array when the transcript does not answer the question."""

# Under pipeline.py's "mic_fallback" the call audio was never captured, so one
# microphone carried every voice and the clusters are s1..sN — there is no
# speaker labelled "You" and which one is the questioner is genuinely unknown.
# summarize.py was already fixed for this; the same false premise lives here.
# Derive the variant by substituting that one rule so the normal prompt stays
# the untouched literal above and the two can never drift apart silently.
_YOU_RULE = ('- The speaker labelled "You" is the local user — the person '
             'asking you this question.')
_NO_YOU_RULE = (
    '- The person asking you this question is NOT identified in the '
    'transcript. The call audio was never captured, so every voice — theirs '
    'included — was recorded on one microphone and split into unlabelled '
    'speakers. NO speaker is labelled "You". Never say or imply that a '
    'particular speaker is the person asking, never attribute a first-person '
    'action ("you said", "you agreed") to one, and if who did something '
    'matters but is genuinely unclear, say so plainly.')

ASK_INSTRUCTIONS_NO_LOCAL_USER = ASK_INSTRUCTIONS.replace(_YOU_RULE, _NO_YOU_RULE)
if ASK_INSTRUCTIONS_NO_LOCAL_USER == ASK_INSTRUCTIONS:  # pragma: no cover
    raise RuntimeError(
        'ASK_INSTRUCTIONS drifted from _YOU_RULE — the mic_fallback prompt '
        'would silently claim a speaker labelled "You" exists')


def _is_mic_fallback(meta):
    """pipeline.py's "mic_fallback" contract: the online call-audio track came
    back silent, so the single microphone carried BOTH sides and was clustered
    like a room mic. The speaker keys are s1..sN with no "you" key at all, and
    the pipeline deliberately does not guess which cluster is the local user.
    meta["mode"] is still "online" here, so only this flag identifies the case.
    Read straight from meta so this file's framing does not depend on another
    module's private helpers.
    """
    return (meta or {}).get("diarization_mode") == "mic_fallback"


# Apple guided-generation schema (NOT JSON Schema): "properties" is an array
# of named entries and every object carries a "name" — see tidy.TIDY_SCHEMA.
ASK_SCHEMA = {
    "type": "object", "name": "MeetingAnswer", "properties": [
        {"name": "answer", "type": "string",
         "description": "the answer to the question, from the transcript only, "
                        "or a plain statement that the transcript does not cover it"},
        {"name": "citations", "type": "array", "max": 4,
         "items": {"type": "object", "name": "Citation", "properties": [
             {"name": "t", "type": "integer",
              "description": "start time of the cited line in WHOLE SECONDS — "
                             "convert its [m:ss] prefix, e.g. [12:07] is 727"},
             {"name": "quote", "type": "string",
              "description": "a short fragment copied verbatim from that same line"},
         ]},
         "description": "the transcript lines that support the answer; empty when there are none"},
    ],
}

_APPLE_YOU = "The speaker called \"You\" is the person asking. "
_APPLE_NO_YOU = ("No speaker is labelled \"You\" and the person asking is not "
                 "identified — never claim a particular speaker is them. ")

APPLE_INSTRUCTIONS = (
    "You answer questions about one meeting using ONLY the transcript excerpt "
    "you are given. If the excerpt does not contain the answer, say so plainly "
    "instead of guessing. " + _APPLE_YOU + "Answer "
    "in one or two complete sentences — never a bare \"Yes\" or \"No\" — naming "
    "the people and specifics involved. Cite the lines you used by their [m:ss] "
    "timestamp converted to whole seconds."
)
APPLE_INSTRUCTIONS_NO_LOCAL_USER = APPLE_INSTRUCTIONS.replace(
    _APPLE_YOU, _APPLE_NO_YOU)

# Appended to whichever of the two above is in use when the excerpt is only
# part of the meeting — which, on this engine, is nearly always. Without it the
# model reads a third of a call and reports the other two thirds as absence:
# "She did not apologize for anything", measured on a meeting whose transcript
# contains "I'm so sorry for our 1st interview". Being wrong is not the only
# failure here; sounding certain while wrong is the one the user cannot catch.
_APPLE_PARTIAL_RULE = (
    "\n\nIMPORTANT: what follows is only PART of this meeting — an evenly "
    "spaced sample of it plus the parts that match the question, with \""
    + GAP_MARKER + "\" marking what was left out. So you CANNOT tell whether "
    "something was never said; you can only tell whether it is in front of "
    "you. If the answer is not in this excerpt, say that the excerpt does not "
    "show it — never that it did not happen, and never that nobody said it."
)

_PARTIAL_NOTE = (
    "NOTE: this meeting is too long to include in full, so this is a SELECTION "
    "of it — an evenly spaced sample of the whole meeting plus the parts that "
    "match the question. \"" + GAP_MARKER + "\" marks transcript that was left "
    "out. Answer from what you can see; if the answer is probably in a part you "
    "cannot see, say the transcript excerpt doesn't show it."
)


# --------------------------------------------------------------- transcript --

def _turn_entries(meta):
    """[{i, start, end, text, line}] — one per turn, "[m:ss] Speaker: text"."""
    speakers = meta.get("speakers") or {}
    entries = []
    for i, turn in enumerate(meta.get("turns") or []):
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(turn.get("start") or 0.0)
            end = float(turn.get("end") or start)
        except (TypeError, ValueError):
            continue
        name = speakers.get(turn.get("speaker"), turn.get("speaker") or "Speaker")
        entries.append({
            "i": i,
            "start": start,
            "end": max(end, start),
            "text": text,
            "line": f"[{summarize._fmt_time(start)}] {name}: {text}",
        })
    return entries


# ---------------------------------------------------------------- retrieval --

_STOPWORDS = set(
    "a about all also am an and any are as at be been being but by can could "
    "did do does doing done for from get got had has have how i if in into is "
    "it its just like me my not of on one or our out over said say says she he "
    "so some than that the their them then there these they this those to too "
    "up us was we were what when where which who whom why will with would you "
    "your yours".split()
)


def _terms(text):
    return {
        w for w in re.findall(r"[a-z0-9']+", str(text).lower())
        if len(w) > 2 and w not in _STOPWORDS
    }


def _score(entry, terms):
    """How well one turn matches the question — matched distinct terms, with
    a small bonus for repeats so a turn that is really about the topic wins."""
    if not terms:
        return 0.0
    words = re.findall(r"[a-z0-9']+", entry["text"].lower())
    if not words:
        return 0.0
    counts = {}
    for w in words:
        if w in terms:
            counts[w] = counts.get(w, 0) + 1
    if not counts:
        return 0.0
    return len(counts) + 0.1 * min(10, sum(counts.values()) - len(counts))


def _cost(entry):
    return len(entry["line"]) + 1


def _trim(entry, room):
    """A copy of `entry` whose rendered line fits `room` chars, or None when
    not even a usable fragment fits.

    A turn longer than the whole budget used to be dropped outright, which on
    a meeting of long monologues could leave NOTHING for the model to read.
    The timestamp and speaker prefix are kept so the fragment is still a real,
    citable line; the untruncated "text" is kept for citation matching, since
    it is genuinely what that speaker said.
    """
    if _cost(entry) <= room:
        return entry
    prefix = entry["line"][:len(entry["line"]) - len(entry["text"])]
    fits = room - 1 - len(prefix) - len(GAP_MARKER)
    if fits < 1:
        return None
    return dict(entry, line=prefix + entry["text"][:fits].rstrip() + GAP_MARKER)


def _spread(n, want):
    """`want` positions spread evenly over range(n) — both ends included."""
    want = max(1, min(int(want), n))
    if want == 1:
        return [0]
    return sorted({round(k * (n - 1) / (want - 1)) for k in range(want)})


def _skeleton_indices(entries, budget):
    """The biggest evenly spaced sample of `entries` that fits `budget`.

    Sizing the sample is what makes the coverage whole-meeting: the estimate
    from the average turn cost is only an estimate, so if the turns it lands on
    are dearer than average it is shrunk — evenly, still spanning the meeting —
    until it fits, instead of being truncated at whichever turn ran out of room
    and leaving the rest of the meeting unrepresented.
    """
    if not entries:
        return []
    avg = max(1.0, sum(_cost(e) for e in entries) / len(entries))
    want = max(1, int(budget / avg))
    for _ in range(8):  # converges in 2-3; the bound is belt and braces
        picks = _spread(len(entries), want)
        total = sum(_cost(entries[i]) for i in picks)
        if total <= budget or want <= 1:
            return picks
        # Scale down by how far over we are, and always by at least one, so
        # this cannot stall on a sample whose cost barely exceeds the budget.
        want = max(1, min(want - 1, int(want * budget / total)))
    return _spread(len(entries), want)


def select_entries(entries, question, budget):
    """-> (kept_entries, partial). Whole transcript when it fits, otherwise a
    skeleton of the meeting plus the turns matching the question."""
    cost = _cost
    if sum(cost(e) for e in entries) <= budget:
        return entries, False

    keep, used = set(), 0
    trimmed = {}   # idx -> shortened copy, for turns too long to keep whole

    # 1. Skeleton — evenly spaced turns across the whole meeting, so the model
    #    always has the shape of the conversation even for a narrow question.
    #
    #    This used to walk a fixed stride and BREAK at the first turn that did
    #    not fit, which is the same as stopping partway through the meeting:
    #    the stride is sized from the AVERAGE turn cost, so a few longer-than-
    #    average turns early on exhausted the share before the walk ever
    #    reached the second half. Measured on the 111-minute meeting, the model
    #    was shown 6 of the 147 turns after the 55-minute mark, and questions
    #    about how it ended were answered from its opening. So: choose a sample
    #    SIZE that actually fits, then skip (never stop at) any single turn too
    #    long to take. Coverage spans the whole meeting either way.
    #
    #    Both ENDS are taken first, out of their own halves of that share.
    #    _spread() puts index 0 and n-1 in the sample by construction, but the
    #    fit test below could still skip either: the first turn if it alone is
    #    dearer than the share, the last because by the time the walk reaches
    #    it the share is nearly spent. Those are the two turns the sample can
    #    least afford to lose — "how did the call end?" is answered from the
    #    end of the call, and dropping the last turn is the exact regression
    #    build_prompt() below already had to be fixed for. So pin them, and
    #    trim rather than drop one too long to take whole: a fragment of the
    #    closing turn is still the closing turn, with a real time to cite.
    skeleton_budget = budget * SKELETON_SHARE
    ends = list(dict.fromkeys([0, len(entries) - 1]))
    for n, idx in enumerate(ends):
        entry = entries[idx]
        # This end's share of the skeleton, or a usable fragment when that is
        # smaller — but never so much of a small budget that the OTHER end is
        # then unplaceable, which would just move the dropped turn.
        floor = min(MIN_EXCERPT_CHARS, (budget - used) // (len(ends) - n))
        room = max(floor, int(skeleton_budget * (n + 1) / len(ends)) - used)
        if cost(entry) > room:
            entry = _trim(entry, room)
            if entry is None or used + cost(entry) > budget:
                continue
            trimmed[idx] = entry
        keep.add(idx)
        used += cost(entry)

    for idx in _skeleton_indices(entries, skeleton_budget):
        if idx in keep or used + cost(entries[idx]) > skeleton_budget:
            continue
        keep.add(idx)
        used += cost(entries[idx])

    # 2. The turns that actually match the question, best first — EVIDENCE
    #    FIRST, CONTEXT SECOND. Every match is taken on its own before any
    #    match is given its neighbours, so one long turn near the top of the
    #    ranking cannot spend the room the other matches needed. Measured on a
    #    real 49-minute call ("what did we agree the next steps would be?"),
    #    the top-ranked match was a single 7,762-char monologue — taken with
    #    its neighbours it was two thirds of the excerpt, and the four other
    #    turns that actually say "next steps" were dropped for want of room.
    #
    #    The half-the-room cap therefore applies to EVERY match, not only to
    #    one too long to fit. It used to fire only when a turn overflowed the
    #    room left, so a monologue that DID fit simply took the lot — and the
    #    bigger the budget, the more it took. Measured on that same call
    #    with "What salary did we end up discussing?" (whose weak term "end"
    #    is what puts a 7,762-char monologue at the top of the ranking): at a
    #    budget of 8,000 that turn was trimmed to 3,799 and 34 turns were
    #    shown, 2 of the 5 salary turns among them; at 9,000 it fitted whole,
    #    took 86% of the excerpt, and the model was shown 11 turns and NONE of
    #    the salary. More budget, less evidence. Half the room, always, is
    #    monotonic: the same question now retrieves at 9,000 what it did at
    #    8,000, and more.
    terms = _terms(question)
    hits = [n for score, _, n in
            sorted(((_score(e, terms), -n, n) for n, e in enumerate(entries)),
                   reverse=True)
            if score > 0]

    for idx in hits:
        if idx in keep:
            continue
        # Dropping a long match loses the best evidence there is — a long
        # monologue is exactly where the answer tends to live — so keep the
        # front of it instead, never more than half the room still open.
        entry = entries[idx]
        room = max(MIN_EXCERPT_CHARS, (budget - used) // 2)
        if cost(entry) > room:
            entry = _trim(entry, room)
            if entry is None or used + cost(entry) > budget:
                continue  # a smaller match may still fit
            trimmed[idx] = entry
        keep.add(idx)
        used += cost(entry)

    for idx in hits:  # and now the neighbours, in the same order of preference
        for j in range(idx - NEIGHBOUR_TURNS, idx + NEIGHBOUR_TURNS + 1):
            if not 0 <= j < len(entries) or j in keep:
                continue
            if used + cost(entries[j]) > budget:
                continue
            keep.add(j)
            used += cost(entries[j])

    # 3. Spend anything left widening coverage, always into the BIGGEST hole
    #    still open — which halves that hole, so the leftover spreads over the
    #    whole meeting instead of piling onto its opening minutes. Filling
    #    earliest-first (what this used to do) compounded step 1: the front of
    #    the meeting arrived contiguous and the end stayed invisible, so "how
    #    did it end" was answered from the beginning of the call.
    holes = []  # a max-heap of (-length, lo, hi) over half-open unkept runs

    def open_hole(lo, hi):
        if hi > lo:
            heapq.heappush(holes, (lo - hi, lo, hi))

    previous = -1
    for idx in sorted(keep) + [len(entries)]:
        open_hole(previous + 1, idx)
        previous = idx
    while holes:
        _, lo, hi = heapq.heappop(holes)
        mid = (lo + hi) // 2
        if used + cost(entries[mid]) <= budget:
            keep.add(mid)
            used += cost(entries[mid])
        # Either way `mid` is now settled, so both halves are strictly smaller
        # runs and this terminates; a turn too long for the room left simply
        # steps aside for whatever else still fits.
        open_hole(lo, mid)
        open_hole(mid + 1, hi)

    # 4. Never hand the model an empty excerpt. When every single turn is
    #    longer than the budget (a lecture, a long monologue) all three steps
    #    above keep nothing, and answering from nothing is the one failure the
    #    model cannot recover from — it has no way to know it saw no transcript.
    if not keep:
        best = max(range(len(entries)),
                   key=lambda n: (_score(entries[n], terms), -n))
        return [_trim(entries[best], budget) or entries[best]], True

    return [trimmed.get(i, entries[i]) for i in sorted(keep)], True


def _render(entries, kept):
    """Kept turns as text, with a marker wherever turns were skipped."""
    kept_by_id = {e["i"]: e for e in kept}
    out, gap = [], False
    for entry in entries:
        if entry["i"] in kept_by_id:
            if gap:
                out.append(GAP_MARKER)
                gap = False
            # the kept copy, which may be a trimmed version of this turn
            out.append(kept_by_id[entry["i"]]["line"])
        else:
            gap = True
    if gap:
        out.append(GAP_MARKER)
    return "\n".join(out)


# ------------------------------------------------------------------ history --

def _history_text(history, budget=MAX_HISTORY_CHARS):
    """The recent back-and-forth, so follow-ups like "and what about him?"
    still make sense. Newest messages win the budget."""
    messages = []
    for item in (history if isinstance(history, list) else [])[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        role = "Question" if str(item.get("role")) == "user" else "Your earlier answer"
        messages.append(f"{role}: {text[:600]}")
    out, used = [], 0
    for line in reversed(messages):
        if used + len(line) > budget:
            break
        out.insert(0, line)
        used += len(line) + 1
    return "\n".join(out)


# ---------------------------------------------------------------- citations --

def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


# Below this a quote is too short to identify a turn safely — "yes", "okay",
# "the numbers" are said all over a meeting — so the matchers below refuse to
# place anything on one. One constant, because _checkable() has to mean exactly
# "the matchers really did test this quote against the transcript".
MIN_QUOTE_CHARS = 12


def _checkable(quote):
    """Did the model supply a quote the matchers below could actually test?

    A missing or very short quote is not evidence of anything: they decline to
    place it, so their silence says nothing about whether it was invented.
    """
    return len(_norm(quote)) >= MIN_QUOTE_CHARS


def _nearest(candidates, t):
    return min(candidates, key=lambda e: (abs(e["start"] - t), e["i"]))


def _quote_in(quote, candidates, t):
    """The candidate turn that actually contains `quote`, or None.

    Exactly one is the answer we want. Two candidates can genuinely contain
    the same words (an overlap between the two tracks, or a repeated phrase),
    and there the cited time is the honest tie-break — either way the quote
    really is that turn's words.
    """
    norm_quote = _norm(quote)
    if len(norm_quote) < MIN_QUOTE_CHARS:
        return None
    hits = [e for e in candidates if norm_quote in _norm(e["text"])]
    if not hits:
        return None
    return hits[0] if len(hits) == 1 else _nearest(hits, t)


def _quote_match(quote, entries, near=None):
    """The turn a quote was copied from, searching the whole transcript.
    `near` is the cited time when there is one: it only breaks ties, never
    outranks the words themselves."""
    norm_quote = _norm(quote)
    if len(norm_quote) < MIN_QUOTE_CHARS:
        return None
    hits = [e for e in entries if norm_quote in _norm(e["text"])]
    if hits:
        return hits[0] if len(hits) == 1 or near is None else _nearest(hits, near)
    # Partial recall: the model paraphrased slightly. Require a strong,
    # unambiguous word overlap before trusting it.
    words = [w for w in norm_quote.split() if len(w) > 2]
    if len(words) < 4:
        return None
    best, best_ratio = [], 0.0
    for entry in entries:
        turn_words = set(_norm(entry["text"]).split())
        ratio = sum(1 for w in words if w in turn_words) / len(words)
        if ratio > best_ratio:
            best, best_ratio = [entry], ratio
        elif ratio == best_ratio:
            best.append(entry)
    if best_ratio < 0.8:
        return None
    return best[0] if len(best) == 1 or near is None else _nearest(best, near)


def _time_placement_ok(quote, candidates, entry):
    """The quote matched nothing. May the clock alone still place this citation?

    Placing by time is honest when there is no quote to contradict it — the
    model gave a bare timestamp, we snap it to the turn it means, and
    _validate_citations shows that turn's own words. It is NOT honest when the
    model did supply a quote and no turn in the meeting contains it, because
    the user is then shown a substituted, verbatim-looking sentence the model
    never wrote, on a turn nothing but the clock chose. A missing citation is
    recoverable; a fabricated one is not.

    Two things can still vouch for such a quote, and both must hold:

    * THE TIMESTAMP MUST BE UNAMBIGUOUS. With more than one candidate turn,
      nearest-by-time is the coin flip this module exists to stop (the two
      tracks are diarized separately; ~49% of turns have another starting
      within CITATION_TOLERANCE_S). Where exactly one turn can be meant, the
      seek is right by construction and the pinned text really is what was
      said at that moment.
    * THE QUOTE MUST BE ABOUT THAT TURN. A paraphrase the fuzzy matcher just
      missed still shares the turn's content words ("the revenue was up twelve
      percent on the quarter" against a turn saying "Revenue grew 12% compared
      to last quarter"). A fabrication shares none of them. This is a
      deliberately weak bar — ONE content word in common — so it separates
      invention from imprecision without pretending to judge phrasing.
    """
    if not _checkable(quote):
        return True
    return len(candidates) == 1 and bool(_terms(quote) & _terms(entry["text"]))


def _place(citation, entries):
    """Resolve one raw citation onto a real turn. -> (entry, by_quote), or
    None to drop it.

    THE QUOTE LEADS. The two audio tracks are diarized separately, so on real
    meetings about half of all turns have another turn starting within
    CITATION_TOLERANCE_S of them; picking the nearest start is close to a coin
    flip, and _validate_citations would then overwrite the model's genuine
    quote with the wrong turn's words — a line nobody said, pointing at the
    wrong audio. So the timestamp only narrows the field to the turns it could
    plausibly mean, and the quote picks the winner. Nearest-by-time is the last
    resort, for when the quote places nothing at all — and _time_placement_ok()
    decides when that resort is honest rather than invented.
    """
    quote = citation.get("quote")
    try:
        t = float(citation.get("t"))
    except (TypeError, ValueError):
        t = None

    if t is not None and t >= 0:
        # every turn that timestamp could mean: starting close to it, or
        # running across it (a moment quoted from inside a long turn)
        candidates = [e for e in entries
                      if abs(e["start"] - t) <= CITATION_TOLERANCE_S
                      or e["start"] <= t <= e["end"]]
        entry = _quote_in(quote, candidates, t)
        if entry is not None:
            return entry, True
        # None of them said it — the timestamp is simply wrong, so trust the
        # words and look for them across the whole meeting.
        entry = _quote_match(quote, entries, near=t)
        if entry is not None:
            return entry, True
        if candidates:  # nothing to go on but the closest start
            nearest = _nearest(candidates, t)
            if _time_placement_ok(quote, candidates, nearest):
                return nearest, False
            log.info("dropped citation t=%r: quoted %r, which is nowhere in "
                     "this meeting", citation.get("t"), str(quote)[:60])
            return None

    # No usable time, but the quote can still identify the turn.
    entry = _quote_match(quote, entries)
    return (entry, True) if entry is not None else None


def _validate_citations(raw, entries):
    """Keep only citations the UI can actually seek to.

    Every surviving "t" is the start of a real turn, and every "quote" is text
    that is genuinely in that turn (the model's fragment when it really came
    from there, otherwise an excerpt of the turn itself).
    """
    kept, seen = [], set()
    for citation in (raw if isinstance(raw, list) else []):
        if not isinstance(citation, dict):
            continue
        placed = _place(citation, entries)
        if placed is None:
            log.info("dropped unplaceable citation %r", citation)
            continue
        entry, by_quote = placed
        t = int(entry["start"])
        # Dedup on the TURN, not on its truncated start second. This was the
        # long-standing KNOWN COST here: "t" is int(turn start), so two
        # GENUINELY DIFFERENT turns beginning inside the same second shared a
        # key and the later one's evidence was silently thrown away. Not a
        # corner case — the mic and call-audio tracks are diarized separately,
        # and turns 0 and 1 of the demo transcript both start at 0.00.
        #
        # It stayed unpaid because tools/test_ask.py asserted that no two kept
        # citations may share a "t". That assertion now checks the invariant
        # that was actually meant — no TURN cited twice — so the fix lands.
        # Two citations CAN now carry the same "t"; they are different lines
        # of the meeting that happen to start in the same second, the UI draws
        # one chip each, and both seek to that moment, which is the truth.
        if entry["i"] in seen:
            continue
        seen.add(entry["i"])
        quote = str(citation.get("quote") or "").strip()[:300]
        # Pinning is for citations placed by TIME ALONE: there the fragment is
        # not known to belong to the turn we picked, and showing it would put
        # words in that speaker's mouth. A citation that placed itself on its
        # quote is already verbatim in its turn, so this rewrites nothing —
        # bar the fuzzy partial-recall path, where the model paraphrased and a
        # paraphrase must not be displayed as a verbatim quote either.
        #
        # What reaches here by time alone is now only what _time_placement_ok()
        # vouched for: a bare timestamp, or one unambiguous turn the quote is
        # demonstrably about. A quote belonging to no turn in the meeting no
        # longer arrives to be quietly swapped for one.
        if not quote or _norm(quote) not in _norm(entry["text"]):
            quote = entry["text"][:240]  # pin the quote to what was really said
        log.debug("citation t=%r placed on turn %d (%s)",
                  citation.get("t"), entry["i"], "quote" if by_quote else "time")
        kept.append({"t": t, "quote": quote})
        if len(kept) >= MAX_CITATIONS:
            break
    return kept


# -------------------------------------------------------------------- claude --

# summarize._summarize_claude can't be called directly — it hard-codes the
# summary prompt — so this is the sibling wrapper, sharing its helpers
# (find_claude, the setup help text, NeedsClaudeError, _extract_json) and its
# signed-out detection. The two differ only where Q&A needs it to: the answer
# is STREAMED back here, because a summary runs once in the background while
# an answer is watched.
_SIGNED_OUT_RE = re.compile(r"log ?in|logged out|authent|credential|api key|/login", re.I)

_SANDBOX = None
_SANDBOX_LOCK = threading.Lock()


def _sandbox():
    """(cwd, mcp_config) for the CLI subprocess — an EMPTY working directory
    and a config declaring no MCP servers.

    Two reasons, one change. Security: the transcript is untrusted text, and a
    subprocess with tools and MCP servers attached is a prompt injection away
    from acting on it — with `--tools ""`, `--strict-mcp-config` and an empty
    config there is nothing to act with, and an empty cwd means no CLAUDE.md,
    settings or project files are picked up either. Speed: the user's MCP
    servers are otherwise initialised on every single question, which measured
    ~1s and, worse, an occasional multi-second spike (docs/ASK_LATENCY.md).
    """
    global _SANDBOX
    with _SANDBOX_LOCK:  # two questions can be in flight at once
        if _SANDBOX is None:
            root = Path(tempfile.mkdtemp(prefix="meetingscribe-ask-"))
            config = root / "no-mcp.json"
            config.write_text('{"mcpServers": {}}', encoding="utf-8")
            cwd = root / "cwd"
            cwd.mkdir()
            atexit.register(shutil.rmtree, str(root), True)
            _SANDBOX = (str(cwd), str(config))
        return _SANDBOX


def _ask_model():
    """The model Ask runs on. Left unset, the CLI inherits the user's default
    (which may be Opus in 1M-context mode — slow for a bounded transcript Q&A).
    Pin a fast model instead: Haiku answers this kind of grounded question in a
    fraction of the time. Overridable via config "ask_model"."""
    return str(summarize.load_config().get("ask_model") or "haiku")


def _claude_argv(exe, config):
    """NOTE: `--tools` and `--mcp-config` are VARIADIC — a positional prompt
    placed after them is swallowed as another value and the CLI exits 1 with
    empty stdout. The prompt goes in on stdin, so there is no positional to
    swallow; keep it that way."""
    return [exe, "-p",
            "--output-format", "stream-json", "--verbose",
            "--include-partial-messages",
            "--model", _ask_model(),
            "--tools", "", "--strict-mcp-config", "--mcp-config", config]


def _event(line):
    """One line of --output-format stream-json, or None if it isn't one."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        return json.loads(line)
    except ValueError:
        return None


def _kill_tree(proc):
    """SIGKILL the process group `proc` leads, falling back to the process.

    Only worth doing because of start_new_session=True at the Popen: without
    it the group is OURS, and killpg would take down the Flask server.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass  # already gone, or no group of its own after all
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _stream_claude(argv, prompt, cwd, on_delta):
    """Run the CLI, feeding text back to `on_delta` as it is written.
    -> (reply, detail, failed).

    The prompt goes in on a file rather than a pipe, and stderr comes back on
    one, so a big transcript can never deadlock against a full pipe buffer
    while we are reading stdout line by line.
    """
    chunks, result = [], None
    with tempfile.TemporaryFile("w+", encoding="utf-8") as stdin, \
            tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as stderr:
        stdin.write(prompt)
        stdin.flush()
        stdin.seek(0)
        # start_new_session puts the CLI in a process group of its own, so the
        # timeout below can kill the WHOLE tree. proc.kill() alone kills the
        # `claude` wrapper and leaves its node child holding the write end of
        # our stdout pipe: the `for line in proc.stdout` loop below never sees
        # EOF, and the watchdog that was supposed to bound the call bounds
        # nothing — the request hangs until the user gives up on it.
        proc = subprocess.Popen(argv, cwd=cwd, stdin=stdin, stdout=subprocess.PIPE,
                                stderr=stderr, text=True, start_new_session=True)
        timed_out = threading.Event()

        def give_up():
            timed_out.set()
            _kill_tree(proc)

        watchdog = threading.Timer(CLAUDE_TIMEOUT_S, give_up)
        watchdog.start()
        try:
            for line in proc.stdout:
                event = _event(line)
                if event is None:
                    continue
                if event.get("type") == "result":
                    result = event
                    continue
                inner = event.get("event") or {}
                if inner.get("type") != "content_block_delta":
                    continue
                delta = inner.get("delta") or {}
                text = delta.get("text") or ""
                if delta.get("type") == "text_delta" and text:
                    chunks.append(text)
                    if on_delta is not None:
                        try:
                            on_delta(text)
                        except Exception:  # a broken listener must not lose the answer
                            log.exception("ask on_delta callback failed")
            proc.wait()
        finally:
            watchdog.cancel()
            proc.stdout.close()
        if timed_out.is_set():
            raise RuntimeError("Claude took too long to answer — please try again.")
        stderr.seek(0)
        err = stderr.read().strip()

    reply = "".join(chunks)
    if isinstance(result, dict):
        failed = bool(result.get("is_error")) or proc.returncode != 0
        # "result" is the finished answer; the deltas are the same text, and
        # stand in only if the CLI ever stops sending it.
        reply = str(result.get("result") or reply)
    else:
        failed = proc.returncode != 0 or not reply
    return reply, (err or reply), failed


def _run_claude(prompt, progress_cb, on_delta=None):
    """Ask the CLI and parse its JSON answer. `on_delta` receives the answer
    text in pieces as it arrives, so a caller that can stream shows the first
    words in ~4s instead of a blank screen for the whole call."""
    exe = summarize.find_claude()
    if exe is None:
        raise NeedsClaudeError(summarize._CLAUDE_SETUP_HELP)
    cwd, config = _sandbox()
    progress_cb("Asking your Claude account…")
    reply, detail, failed = _stream_claude(_claude_argv(exe, config), prompt,
                                           cwd, on_delta)
    if failed:
        detail = detail.strip()
        if _SIGNED_OUT_RE.search(detail):
            raise NeedsClaudeError(
                "Your Claude account is signed out. Open Terminal, run "
                "`claude`, and sign in — then ask again.")
        last = detail.splitlines()[-1] if detail else "unknown error"
        raise RuntimeError(f"Claude could not answer: {last}")
    try:
        parsed = summarize._extract_json(reply)
    except ValueError:
        return _salvage(reply)
    return parsed if isinstance(parsed, dict) else {}


def _salvage(reply):
    """Unparseable reply -> the best answer we can still honestly give.

    Plain prose is a perfectly good answer (just uncited). Truncated JSON gets
    its "answer" field rescued; anything else is an error, never a wall of
    braces shown to the user.
    """
    text = str(reply).strip()
    if not text:
        raise RuntimeError("Claude returned an empty answer — please try again.")
    looks_json = text.lstrip("`json \n").startswith("{")
    if looks_json:
        match = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if match:
            try:
                return {"answer": json.loads(f'"{match.group(1)}"'), "citations": []}
            except ValueError:
                pass
        raise RuntimeError("Claude's reply was cut off — please ask again.")
    return {"answer": text, "citations": []}


# ------------------------------------------------------------------- prompt --

def _header(meta, partial):
    title = meta.get("title") or "Untitled meeting"
    parts = [f'Transcript of the meeting "{title}".', summarize._speaker_note(meta)]
    # Naming the Mac's account holder as the speaker labelled "You" is only
    # true when such a speaker exists. Under the mic fallback it does not, and
    # asserting it invites the model to pin the questioner on whichever voice
    # happens to be Speaker 1 — the instructions say so instead.
    me = None if _is_mic_fallback(meta) else summarize._local_user_name()
    if me:
        parts.append(f'The local user — the speaker labelled "You" — is {me}.')
    if partial:
        parts.append(_PARTIAL_NOTE)
    return " ".join(p for p in parts if p)


def build_prompt(meta, entries, question, history, budget):
    """-> (source_text, kept_entries, partial). The transcript the model
    actually sees, and whether it is only part of the meeting.

    The whole prompt — header, history, transcript and question — is what has
    to fit `budget`, so everything but the transcript is measured first (using
    the longer "partial" header, since we don't know yet whether we'll need it).
    """
    # The transcript is the point, so the history and the question never eat
    # more than a fraction of the budget — and if the header alone is big
    # (many speakers), the history is dropped rather than the transcript.
    question = question[:max(120, budget // 12)]
    fixed = len(_header(meta, True)) + len(question) + 80
    history_text = _history_text(history, max(0, min(MAX_HISTORY_CHARS,
                                                    budget // 6, budget - fixed - 400)))
    room = max(200, budget - fixed - len(history_text))
    kept, partial = select_entries(entries, question, room)

    def assemble(kept):
        blocks = [_header(meta, partial)]
        if history_text:
            blocks.append("Earlier in this conversation:\n" + history_text)
        blocks.append("TRANSCRIPT:\n" + _render(entries, kept))
        blocks.append("QUESTION: " + question)
        return "\n\n".join(blocks)

    # The "…" gap markers only exist once rendered, so the assembled prompt can
    # land a little over budget. This used to be settled by handing back the
    # LAST kept turns, which silently undid the whole-meeting coverage above:
    # the final turn of the meeting is the one the skeleton is most careful to
    # include, and it was the first one given away. Measured on a real 33-minute
    # call, select_entries kept turn 141 of 141 and the assembler then dropped
    # every turn past 135, so "how did the call end?" was answered without the
    # end of the call. Ask for a smaller excerpt instead: what comes back is
    # smaller but still spans the meeting.
    source = assemble(kept)
    for _ in range(4):
        if len(source) <= budget:
            break
        room = max(200, room - (len(source) - budget) - 64)
        kept, partial = select_entries(entries, question, room)
        source = assemble(kept)
    # Last resort, and it shows: drop turns from the MIDDLE, never off an end.
    # Taking them off the end was the same regression in a second place — the
    # loop above exists because handing back the final turns undid the
    # whole-meeting coverage, and select_entries now pins both ends for the
    # same reason, so this must not hand one of them straight back. It stops
    # at two turns rather than eating into them; by then the overshoot is only
    # the rendered "…" markers, which shrink along with the turns.
    while len(source) > budget and len(kept) > 2:
        mid = len(kept) // 2   # rebuilt, never popped: `kept` can BE `entries`
        kept = kept[:mid] + kept[mid + 1:]
        partial = True
        source = assemble(kept)
    return source, kept, partial


# Does the answer claim something DID NOT happen? This cannot tell a finding
# ("he did not want to relocate") from a claim about the whole meeting ("they
# never discussed salary"), and it does not try: over-triggering costs nothing,
# because the sharper note below is true of EVERY partial answer — it only
# reads oddly against a positive one. Missing a real absence claim is the
# failure that matters, so the pattern errs towards catching them.
_ABSENCE_RE = re.compile(
    r"\b(?:never|nothing|nobody|no one|none)\b"
    r"|\bno (?:mention|record|indication|evidence|discussion|answer)\b"
    r"|\b(?:did|does|do|was|were|is|are|has|have|had)(?:n't|\s+not)\b"
    r"|\bdoesn'?t (?:show|cover|contain|mention)\b",
    re.I)


def _coverage_note(entries, kept, answer, absence=False):
    """The sentence that tells the user how much of the meeting was read.

    The instructions ask the model to own its blind spot, and on the Apple
    path it frequently will not: asked "did she apologise?" about a call whose
    transcript contains "I'm so sorry for our 1st interview", the on-device
    model — shown 31% of it, not including that turn — answers "She did not
    apologize for anything." Measured, not hypothesised. That reads as a
    finding, and the user has no way to tell it is really "not in the third I
    was given".

    So this does not depend on the model complying. It states the coverage
    outright, and where the answer asserts an ABSENCE it says plainly that the
    absence is only of the part that was read. `absence` forces that reading
    for a caller that already knows the answer is a negative one — the empty
    answer, whose stand-in text ("I couldn't find an answer to that in this
    transcript") asserts absence without using any of the words _ABSENCE_RE
    above looks for.
    """
    whole = sum(_cost(e) for e in entries)
    shown = sum(_cost(e) for e in kept)
    share = max(1, min(99, round(100.0 * shown / whole))) if whole else 0
    if absence or _ABSENCE_RE.search(answer):
        return (f"(Only about {share}% of this meeting was read — it was too "
                f"long to take in full — so treat this as \"not in the part I "
                f"saw\" rather than \"it never happened\".)")
    return (f"(Answered from about {share}% of this meeting — it was too long "
            f"to read in full, so parts of it were not seen.)")


# --------------------------------------------------------------------- main --

def _ask_apple(meta, entries, question, history, fallback, progress_cb):
    """One on-device answer. -> (raw, kept, partial).

    Steps DOWN the budget ladder whenever the model says the request did not
    fit. A single fixed budget cannot be both generous and safe here: the same
    meeting at the same size is accepted or refused depending on how long the
    answer turns out to be, because prompt and output share one 4096-token
    context. Retrying smaller costs a few seconds; the alternative is handing
    the user "The request was too long for the on-device model", which they
    can do nothing about.
    """
    base = APPLE_INSTRUCTIONS_NO_LOCAL_USER if fallback else APPLE_INSTRUCTIONS
    overflow = None
    for budget in APPLE_PROMPT_LADDER:
        source, kept, partial = build_prompt(meta, entries, question, history, budget)
        try:
            raw = local_llm.generate(
                base + (_APPLE_PARTIAL_RULE if partial else ""),
                source, ASK_SCHEMA, max_tokens=700)
        except local_llm.LocalLLMError as exc:
            if exc.code != "context_overflow":
                raise RuntimeError(str(exc)) from exc
            log.info("on-device ask did not fit at %d chars (%d turn(s)) — "
                     "retrying with a smaller excerpt", len(source), len(kept))
            # The spinner has been up for several seconds by now and is about
            # to be up for several more; say why rather than look stuck.
            progress_cb("Too much to read at once — trying a shorter excerpt…")
            overflow = exc
            continue
        return (raw if isinstance(raw, dict) else {}), kept, partial
    raise RuntimeError(str(overflow))


def answer_question(meeting_dir, question, history=None, progress_cb=lambda msg: None,
                    on_delta=None):
    """Answer one question about one meeting. -> {"answer", "citations"}.

    `on_delta` is called with each piece of the answer as the model writes it
    (Claude path only). It is for showing the answer as it lands — the return
    value is still the whole thing, with the citations validated.
    """
    meeting_dir = Path(meeting_dir)
    meta = json.loads((meeting_dir / "meeting.json").read_text(encoding="utf-8"))
    question = str(question or "").strip()[:MAX_QUESTION_CHARS]
    if not question:
        raise RuntimeError("Ask a question first.")

    entries = _turn_entries(meta)
    if not entries:
        raise RuntimeError("No transcript to ask about yet.")
    fallback = _is_mic_fallback(meta)

    if summarize._pick_engine() == "claude":
        budget = budget_for(question)
        source, kept, partial = build_prompt(meta, entries, question, history, budget)
        instructions = ASK_INSTRUCTIONS_NO_LOCAL_USER if fallback else ASK_INSTRUCTIONS
        raw = _run_claude(instructions + "\n\n" + source, progress_cb, on_delta)
    else:
        ok, reason = local_llm.available()
        if not ok:
            raise RuntimeError(local_llm.reason_message(reason))
        progress_cb("Answering on this Mac…")
        raw, kept, partial = _ask_apple(meta, entries, question, history,
                                        fallback, progress_cb)

    # NOT truncated. This used to end in [:4000], which on the streaming path
    # was visible vandalism: the deltas draw the whole answer on screen, then
    # the "done" event — which app.py documents as the authority and the UI
    # therefore renders over the top — replaced it with the first 4,000 chars.
    # The user watched a long answer arrive and then lose its ending. Nothing
    # downstream needs a cap: the length is already bounded by the model (700
    # tokens on the Apple path, one CLI reply on the other), and any cap here
    # can only ever shorten something the user has already read.
    answer = str((raw or {}).get("answer") or "").strip()
    empty = not answer
    if empty:
        answer = "I couldn't find an answer to that in this transcript."
    if partial:
        # The model was shown part of the meeting; say so where it cannot be
        # missed, so "they never said that" is read as "not in what I saw".
        #
        # This used to be an `elif`, which suppressed the note on exactly the
        # answer that needs it most. "I couldn't find an answer to that in this
        # transcript" is a claim about the WHOLE meeting; when the excerpt was
        # a third of it, that is how a retrieval miss becomes a confident false
        # negative — and the user was told nothing. The empty answer is a
        # negative one by construction, so it takes the absence wording too.
        answer += "\n\n" + _coverage_note(entries, kept, answer, absence=empty)
    # Only turns the model was actually shown may be cited.
    citations = _validate_citations((raw or {}).get("citations"), kept)
    log.info("asked %s: %d turn(s) shown, %d citation(s) kept",
             meeting_dir.name, len(kept), len(citations))
    return {"answer": answer, "citations": citations}
