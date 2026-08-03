#!/usr/bin/env python3
"""Regression tests for speaker_names: real names read off the conversation.

Everything here is SYNTHETIC: hand-written turns, a stubbed local_llm.generate,
no audio, no model, no real meeting. The contract under test, in the order the
guarantees matter:

  1. EVIDENCE OR NOTHING. A claim whose quote is not in the transcript is
     dropped, not relocated, and no name is ever applied without an anchored
     quote recorded on the meeting.
  2. INTRODUCTION AND ADDRESS ARE DIFFERENT CLAIMS. "I'm Marcus" names the
     speaker; "Thanks, Priya" names somebody else, and the direction is never
     guessed. An address resolves only when subtracting the speaker leaves
     exactly one cluster, and is dropped otherwise.
  3. The model does not decide anything. Its "kind" is ignored, its spelling
     is ignored, and a name it invents cannot survive because the words have
     to be in the turn.
  4. Human-typed names are untouchable, "you" is never renamed, and a name
     already on the meeting is never given to a second speaker.
  5. A calendar roster, when there is one, is a hard candidate list.
  6. Confidence-gated: one "high" claim, or two portions agreeing; a lone
     "medium" is held back and contradictions cancel out.
  7. FAIL SILENT. A guardrail refusal, a crash or an unavailable model leaves
     the transcript exactly as it was.

Run:  ~/.meetingscribe/venv/bin/python tools/test_speaker_names.py
"""
import json
import logging
import re
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import local_llm  # noqa: E402
import speaker_names as sn  # noqa: E402
import summarize  # noqa: E402

# The module logs an exception on the fail-silent path, which is the correct
# behaviour and pure noise in a test run.
logging.getLogger("meetingscribe").setLevel(logging.CRITICAL)

CFG = {"speaker_names": True}
CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


# --- the harness -------------------------------------------------------------

def turns(*rows):
    """(speaker, start, text) triples -> pipeline-shaped turns."""
    return [{"speaker": s, "track": "system", "start": float(t),
             "end": float(t) + 4.0, "text": text}
            for s, t, text in rows]


def claim(name, quote, kind="introduction", confidence="high"):
    return {"name": name, "quote": quote, "kind": kind, "confidence": confidence}


class Model:
    """Stands in for the on-device model. `script` is one list of claims per
    PORTION, in portion order; an entry may instead be an exception to raise,
    which is how a guardrail refusal is simulated.

    Replies are keyed off the portion number in the prompt, never off call
    order: portions are read concurrently, so call order is a race and a test
    that depended on it would fail one run in five.
    """

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, instructions, prompt, schema, **kw):
        with self._lock:
            self.calls.append({"instructions": instructions, "prompt": prompt})
        match = re.match(r"PORTION (\d+) ", prompt)
        index = int(match.group(1)) - 1 if match else 0
        reply = self.script[min(index, len(self.script) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return {"claims": list(reply)}


@contextmanager
def one_turn_per_portion():
    """Split the transcript one turn per portion, so a test can script what
    each portion's reader says without depending on line lengths."""
    real = sn.PORTION_CHARS
    sn.PORTION_CHARS = 1
    try:
        yield
    finally:
        sn.PORTION_CHARS = real


def run(model, meeting_turns, speakers, meta=None, cfg=None):
    """One inference pass with the model stubbed out. -> (applied, meta)."""
    meta = dict(meta or {})
    real_generate, real_available, real_engine = (
        local_llm.generate, local_llm.available, summarize._pick_engine)
    local_llm.generate = model
    local_llm.available = lambda force=False: (True, None)
    summarize._pick_engine = lambda: "apple"
    try:
        applied = sn.apply_inferred_names(meta, meeting_turns, speakers,
                                          cfg if cfg is not None else CFG)
    finally:
        local_llm.generate = real_generate
        local_llm.available = real_available
        summarize._pick_engine = real_engine
    return applied, meta


# --- 1. evidence or nothing ---------------------------------------------------

@check
def a_quote_that_is_not_in_the_transcript_is_dropped():
    rows = turns(("you", 0, "Good morning everyone, shall we start?"),
                 ("s1", 6, "Yes let us get going, I have a hard stop at three."))
    speakers = {"you": "You", "s1": "Speaker 1"}
    # Plausible, well-formed, high confidence, and invented. Nothing in the
    # transcript says it, so there is nothing to anchor to.
    model = Model([claim("Marcus", "Hi everyone, I'm Marcus from platform")])
    applied, meta = run(model, rows, speakers)
    assert applied == {}, applied
    assert speakers["s1"] == "Speaker 1"
    assert "speaker_names" not in meta


@check
def an_applied_name_carries_its_quote_and_timestamp():
    rows = turns(("you", 0, "Thanks for making the time."),
                 ("s1", 12, "Of course. Hi, I'm Marcus, I run the platform team."))
    speakers = {"you": "You", "s1": "Speaker 1"}
    model = Model([claim("Marcus", "I'm Marcus")])
    applied, meta = run(model, rows, speakers)
    assert applied == {"s1": "Marcus"}, applied
    assert speakers["s1"] == "Marcus"
    evidence = meta["speaker_names"]["s1"]
    assert evidence["name"] == "Marcus"
    assert evidence["t"] == 12, evidence
    assert "Marcus" in evidence["quote"], evidence
    assert evidence["said_by"] == "s1", evidence
    assert evidence["kind"] == "introduction"
    assert evidence["confidence"] == "high"
    assert evidence["roster"] is False


# --- 2. introduction vs address ----------------------------------------------

@check
def a_self_introduction_names_the_speaker_of_that_turn():
    rows = turns(("s1", 0, "Hello, my name is Priya and I look after billing."),
                 ("s2", 9, "Great to meet you. Shall we start with the numbers?"))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Priya", "my name is Priya")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {"s1": "Priya"}, applied
    assert speakers["s2"] == "Speaker 2", "the wrong speaker was named"


@check
def an_address_names_the_other_speaker_not_the_one_talking():
    # THE DIRECTION TEST. "you" says the name; the name belongs to s1.
    rows = turns(("s1", 0, "So we shipped the migration on Tuesday night."),
                 ("you", 20, "Thanks, Katy. That is a big one off the list."))
    speakers = {"you": "You", "s1": "Speaker 1"}
    model = Model([claim("Katy", "Thanks, Katy", kind="address")])
    applied, meta = run(model, rows, speakers)
    assert applied == {"s1": "Katy"}, applied
    assert speakers["you"] == "You", "the local user must never be renamed"
    evidence = meta["speaker_names"]["s1"]
    assert evidence["kind"] == "address"
    # The evidence names the OTHER speaker, and says so.
    assert evidence["said_by"] == "you", evidence
    assert "Katy" in evidence["quote"], evidence


@check
def the_stored_quote_is_the_sentence_that_carries_the_name():
    # A long turn whose model fragment lands on a sentence with no name in it.
    # What gets stored has to be the sentence that actually proves the name,
    # or the evidence proves nothing when a human reads it back.
    rows = turns(("s1", 0, "We shipped on Tuesday and the rollback held. "
                           "Hi everyone, I'm Marcus. "
                           "Anyway, the numbers are on slide four."),
                 ("s2", 30, "Understood."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Marcus", "the numbers are on slide four")])
    applied, meta = run(model, rows, speakers)
    assert applied == {"s1": "Marcus"}, applied
    quote = meta["speaker_names"]["s1"]["quote"]
    assert "I'm Marcus" in quote, quote
    assert "slide four" not in quote, quote


@check
def an_address_is_dropped_when_more_than_one_speaker_could_be_meant():
    # Three clusters: subtracting the speaker leaves two, so there is no
    # arithmetic answer and guessing is exactly what this must not do.
    rows = turns(("s1", 0, "I pushed the fix this morning."),
                 ("s2", 8, "And I reviewed it."),
                 ("s3", 14, "Thanks, Priya, that unblocks the release."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2", "s3": "Speaker 3"}
    model = Model([claim("Priya", "Thanks, Priya", kind="address")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {}, f"an ambiguous address was resolved anyway: {applied}"
    assert speakers == {"s1": "Speaker 1", "s2": "Speaker 2", "s3": "Speaker 3"}


@check
def an_address_aimed_at_the_local_user_names_nobody():
    rows = turns(("you", 0, "Here is where we got to on the rollout."),
                 ("s1", 30, "Thanks, Sharique. That all makes sense."))
    speakers = {"you": "You", "s1": "Speaker 1"}
    model = Model([claim("Sharique", "Thanks, Sharique", kind="address")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {}, applied
    assert speakers == {"you": "You", "s1": "Speaker 1"}


@check
def a_person_talked_about_is_not_a_person_talked_to():
    rows = turns(("you", 0, "Any news on the contract?"),
                 ("s1", 5, "I spoke to Priya yesterday and she is still waiting."))
    speakers = {"you": "You", "s1": "Speaker 1"}
    # The model is wrong on purpose: it calls a third-party mention an address.
    model = Model([claim("Priya", "I spoke to Priya yesterday", kind="address")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {}, f"a mention was treated as an address: {applied}"


@check
def a_name_inside_a_reported_question_is_not_an_address():
    # "I asked Priya what she thought" contains the exact string a vocative
    # question would ("Priya what"), and means the opposite.
    rows = turns(("s1", 0, "I asked Priya what she thought of the offer."),
                 ("s2", 8, "And?"))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Priya", "asked Priya what she thought", kind="address")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {}, f"a reported question read as a vocative: {applied}"


@check
def a_vocative_may_sit_behind_a_filler_word():
    rows = turns(("s1", 0, "That is where we landed on pricing."),
                 ("s2", 9, "So Priya, what do you make of that number?"))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Priya", "So Priya, what do you make of that",
                         kind="address")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {"s1": "Priya"}, applied


@check
def someone_asking_where_a_person_is_does_not_introduce_them():
    rows = turns(("s1", 0, "Quick one before we start, is Marcus here yet?"),
                 ("s2", 8, "He is dialling in now."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Marcus", "is Marcus here yet")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {}, applied


@check
def a_destination_is_not_a_hand_off():
    # Measured on the real corpus: "over to X" matched a business park and put
    # it on a speaker label. A hand-off gives the floor to a person, and says
    # so with "you".
    rows = turns(("s1", 0, "So he's taking you over to Apple Park for lunch."),
                 ("s2", 9, "That sounds good."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Apple Park", "over to Apple Park", kind="address")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {}, f"a place was treated as a person: {applied}"

    # The real hand-off still works.
    rows = turns(("s1", 0, "That is the architecture. Over to you, Priya."),
                 ("s2", 9, "Thanks."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Priya", "Over to you, Priya.", kind="address")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {"s2": "Priya"}, applied


@check
def a_list_of_names_is_not_an_address():
    rows = turns(("s1", 0, "On the panel we had Bob, Priya."),
                 ("s2", 7, "Right, that is the full set."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Priya", "we had Bob, Priya", kind="address")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {}, f"a list read as a vocative: {applied}"


# --- 3. the model decides nothing --------------------------------------------

@check
def the_models_claim_type_is_not_trusted():
    # It says "address" over a line that plainly introduces its own speaker.
    # The transcript wins, so the name lands on the speaker, not the other one.
    rows = turns(("s1", 0, "Hi, I'm Marcus, I will be taking you through this."),
                 ("s2", 9, "Sounds good."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Marcus", "I'm Marcus", kind="address")])
    applied, meta = run(model, rows, speakers)
    assert applied == {"s1": "Marcus"}, applied
    assert meta["speaker_names"]["s1"]["kind"] == "introduction"


@check
def the_transcripts_spelling_wins_over_the_models():
    rows = turns(("s1", 0, "Hi there, I'm Sharique, thanks for joining."),
                 ("s2", 8, "No problem at all."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    # Lower case out of the model, capitalised on screen: the name that ships
    # is the one the transcript spells.
    model = Model([claim("sharique", "I'm Sharique")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {"s1": "Sharique"}, f"model spelling leaked through: {applied}"

    # And a model that "corrects" the spelling to something the turn does not
    # contain cannot impose it either: the name is recovered from the words it
    # copied, so what ships is still the transcript's spelling.
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Shariq", "I'm Sharique")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {"s1": "Sharique"}, f"model spelling leaked through: {applied}"


@check
def an_ordinary_word_is_not_a_name():
    rows = turns(("s1", 0, "I'm good thanks, and I'm sorry about the delay."),
                 ("s2", 8, "No trouble."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Good", "I'm good thanks"), claim("Sorry", "I'm sorry")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {}, applied


@check
def a_filler_a_contraction_and_a_product_are_not_people():
    rows = turns(("s1", 0, "Um, so that is where Salesforce came in."),
                 ("s2", 8, "That's fair. Uh, what did they quote?"))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    for bad, quote in (("Um", "Um, so that is where"),
                       ("Salesforce", "where Salesforce came in"),
                       ("That's", "That's fair"),
                       ("Uh", "Uh, what did they quote")):
        speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
        model = Model([claim(bad, quote, kind="address")])
        applied, _meta = run(model, rows, speakers)
        assert applied == {}, f"{bad!r} was treated as a person: {applied}"


@check
def a_negated_introduction_is_not_an_introduction():
    rows = turns(("s1", 0, "No, I'm not Marcus, he is on the other call."),
                 ("s2", 8, "Apologies, my mistake."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Marcus", "I'm not Marcus")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {}, applied


@check
def a_label_where_the_name_should_be_falls_back_to_the_quote():
    """The on-device model's commonest failure on the real corpus: it copies
    the right line and then writes the transcript's LABEL in the name field.
    The name is in the words it copied, so it comes from there."""
    rows = turns(("s1", 0, "We shipped the migration on Tuesday night."),
                 ("you", 20, "Hey, Zach. That is a big one off the list."))
    speakers = {"you": "You", "s1": "Speaker 1"}
    model = Model([claim("You", "Hey, Zach.", kind="address")])
    applied, meta = run(model, rows, speakers)
    assert applied == {"s1": "Zach"}, applied
    assert "Zach" in meta["speaker_names"]["s1"]["quote"]


@check
def the_quote_fallback_needs_a_verbatim_anchor():
    # A paraphrase can anchor by word overlap, and a turn reached that way is
    # not evidence of anything: mining it for a name is exactly the guess this
    # module refuses to make.
    rows = turns(("s1", 0, "Thanks so much for all of that detail, Zach."),
                 ("you", 20, "No problem at all."))
    speakers = {"you": "You", "s1": "Speaker 1"}
    model = Model([claim("Speaker 1", "thanks for all that detail", kind="address")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {}, f"a paraphrase was mined for a name: {applied}"


@check
def a_quote_naming_two_people_is_ambiguous():
    rows = turns(("s1", 0, "Right, let us begin."),
                 ("you", 9, "Hey, Zach. Hi, Priya. Good to see you both."))
    speakers = {"you": "You", "s1": "Speaker 1"}
    model = Model([claim("You", "Hey, Zach. Hi, Priya.", kind="address")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {}, f"an ambiguous fragment produced a name: {applied}"


# --- 4. the seam: defaults only, no duplicates -------------------------------

@check
def a_human_typed_name_is_never_overwritten():
    rows = turns(("s1", 0, "Hi, I'm Marcus from the platform team."),
                 ("s2", 8, "Welcome."))
    speakers = {"s1": "Katy", "s2": "Speaker 2"}   # the user typed "Katy"
    model = Model([claim("Marcus", "I'm Marcus")])
    applied, meta = run(model, rows, speakers)
    assert applied == {}, applied
    assert speakers["s1"] == "Katy"
    assert "speaker_names" not in meta


@check
def a_name_already_on_the_meeting_is_not_given_to_a_second_speaker():
    rows = turns(("s1", 0, "Hi, I'm Priya."),
                 ("s2", 6, "Hello."))
    speakers = {"s1": "Speaker 1", "s2": "Priya"}   # already a Priya here
    model = Model([claim("Priya", "I'm Priya")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {}, f"two Priyas on one meeting: {applied}"
    assert speakers["s1"] == "Speaker 1"


@check
def a_formulaic_name_is_never_applied():
    rows = turns(("s1", 0, "Hi, I'm Speaker Two on this recording."),
                 ("s2", 8, "Understood."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Speaker Two", "I'm Speaker Two")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {}, applied


# --- 5. the calendar roster --------------------------------------------------

@check
def a_roster_is_a_hard_candidate_list():
    rows = turns(("s1", 0, "Hi, I'm Marcus, standing in for Dana today."),
                 ("s2", 9, "Thanks for covering."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    meta = {"calendar_event": {"names": ["Dana Giedd", "Sharique Khatri"]}}
    model = Model([claim("Marcus", "I'm Marcus")])
    applied, _meta = run(model, rows, speakers, meta=meta)
    assert applied == {}, f"a name off the roster was applied: {applied}"

    # And the same pass accepts a name that IS on it.
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    rows = turns(("s1", 0, "Hi, I'm Dana, I look after the account."),
                 ("s2", 9, "Great."))
    model = Model([claim("Dana", "I'm Dana")])
    applied, _meta2 = run(model, rows, speakers, meta=meta)
    assert applied == {"s1": "Dana"}, applied
    assert _meta2["speaker_names"]["s1"]["roster"] is True
    # The roster reaches the model too, not just the filter.
    assert "Dana Giedd" in model.calls[0]["instructions"]


# --- 6. confidence and agreement ---------------------------------------------

@check
def a_lone_medium_claim_is_held_back_and_two_portions_carry_it():
    with one_turn_per_portion():
        rows = turns(("s1", 0, "Hi, I'm Marcus, good to meet you all today."),
                     ("s2", 9, "Likewise, shall we get straight into the agenda."),
                     ("s1", 20, "Sure. As I said, I'm Marcus and I own the API."),
                     ("s2", 32, "Perfect, that is the part I wanted to hear about."))
        speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}

        # One portion, medium: not enough.
        one = Model([claim("Marcus", "I'm Marcus", confidence="medium")], [])
        applied, _meta = run(one, rows, dict(speakers))
        assert applied == {}, f"a lone medium claim was applied: {applied}"

        # Two portions agreeing at medium: enough.
        two = Model([claim("Marcus", "I'm Marcus", confidence="medium")],
                    [], [claim("Marcus", "I'm Marcus and I own the API",
                               confidence="medium")], [])
        applied, meta = run(two, rows, dict(speakers))
        assert applied == {"s1": "Marcus"}, applied
        assert meta["speaker_names"]["s1"]["portions"] >= 2, meta["speaker_names"]


@check
def contradictory_claims_cancel_out():
    rows = turns(("s1", 0, "Hi, I'm Marcus. Or as some people call me Priya."),
                 ("s2", 10, "Right."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Marcus", "I'm Marcus"),
                   claim("Priya", "call me Priya")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {}, f"a tie was resolved instead of dropped: {applied}"


@check
def one_name_cannot_land_on_two_speakers():
    rows = turns(("s1", 0, "Hi, I'm Marcus."),
                 ("s2", 6, "And I'm Marcus as well, confusingly."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Marcus", "I'm Marcus"),
                   claim("Marcus", "I'm Marcus as well")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {}, f"the same name went on two clusters: {applied}"


# --- 7. fail silent -----------------------------------------------------------

@check
def a_declined_portion_does_not_sink_the_pass():
    with one_turn_per_portion():
        rows = turns(("s1", 0, "Right, let us look at last quarter's numbers now."),
                     ("s2", 9, "The revenue line is the one I want to talk about."),
                     ("s1", 20, "Sorry, I should say, I'm Marcus and I am new here."),
                     ("s2", 32, "Welcome aboard, that is good to know finally."))
        speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
        model = Model(local_llm.LocalLLMError("declined", code="guardrail"),
                      [],
                      [claim("Marcus", "I'm Marcus")],
                      [])
        applied, _meta = run(model, rows, speakers)
        assert applied == {"s1": "Marcus"}, applied
        assert len(model.calls) == 4, f"the pass stopped at the refusal: {len(model.calls)}"


@check
def a_model_that_crashes_costs_nobody_a_transcript():
    rows = turns(("s1", 0, "Hi, I'm Marcus."), ("s2", 6, "Hello."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}

    def explode(*a, **kw):
        raise RuntimeError("the helper died")

    applied, meta = run(explode, rows, speakers)
    assert applied == {}, applied
    assert speakers == {"s1": "Speaker 1", "s2": "Speaker 2"}
    assert "speaker_names" not in meta


@check
def an_unavailable_model_is_a_no_op():
    rows = turns(("s1", 0, "Hi, I'm Marcus."), ("s2", 6, "Hello."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    real_available, real_engine = local_llm.available, summarize._pick_engine
    local_llm.available = lambda force=False: (False, "device_not_eligible")
    summarize._pick_engine = lambda: "apple"
    try:
        applied = sn.apply_inferred_names({}, rows, speakers, CFG)
    finally:
        local_llm.available = real_available
        summarize._pick_engine = real_engine
    assert applied == {}, applied
    assert speakers["s1"] == "Speaker 1"


@check
def the_config_switch_turns_it_off_completely():
    rows = turns(("s1", 0, "Hi, I'm Marcus."), ("s2", 6, "Hello."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Marcus", "I'm Marcus")])
    meta = {"speaker_names": {"s1": {"name": "stale"}}}
    applied, meta = run(model, rows, speakers, meta=meta,
                        cfg={"speaker_names": False})
    assert applied == {}, applied
    assert model.calls == [], "the model was called with the feature off"
    assert "speaker_names" not in meta, "stale evidence survived"


@check
def a_transcript_with_no_naming_moment_never_reaches_the_model():
    # The verifier would throw away anything the model said about these lines,
    # so the calls are skipped rather than paid for.
    rows = turns(("s1", 0, "The deploy went out at four and nothing broke."),
                 ("s2", 8, "Good. Let us close the ticket then."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Marcus", "I'm Marcus")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {} and model.calls == [], f"model was called: {model.calls}"

    # ...and one line that could carry a name is enough to make it worth asking.
    rows = turns(("s1", 0, "Hi, I'm Marcus."), ("s2", 6, "Hello."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    model = Model([claim("Marcus", "I'm Marcus")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {"s1": "Marcus"} and model.calls, applied


@check
def nothing_runs_when_every_label_is_already_a_real_name():
    rows = turns(("s1", 0, "Hi, I'm Marcus."), ("s2", 6, "Hello."))
    speakers = {"s1": "Katy", "s2": "Dana"}
    model = Model([claim("Marcus", "I'm Marcus")])
    applied, _meta = run(model, rows, speakers)
    assert applied == {} and model.calls == [], "a pointless model call was made"


# --- 8. the engine the user chose --------------------------------------------

@check
def the_claude_engine_is_honoured_and_verified_the_same_way():
    """The user's engine setting decides which model reads the transcript; the
    evidence rules are the same either side of that fork."""
    rows = turns(("s1", 0, "Hi, I'm Marcus, I run the platform team."),
                 ("s2", 8, "Good to meet you."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}

    class Proc:
        returncode = 0
        stdout = json.dumps({"result": json.dumps({"claims": [
            {"name": "Marcus", "kind": "introduction",
             "quote": "I'm Marcus", "confidence": "high"},
            # Invented: no such line, so it must not survive.
            {"name": "Dana", "kind": "address",
             "quote": "Thanks, Dana", "confidence": "high"},
        ]})})

    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["input"] = kw.get("input")
        return Proc()

    real = (summarize.find_claude, subprocess.run, summarize._pick_engine,
            sn.subprocess.run)
    summarize.find_claude = lambda: "/usr/bin/true"
    sn.subprocess.run = fake_run
    summarize._pick_engine = lambda: "claude"
    try:
        meta = {}
        applied = sn.apply_inferred_names(meta, rows, speakers, CFG)
    finally:
        summarize.find_claude, subprocess.run = real[0], real[1]
        summarize._pick_engine = real[2]
        sn.subprocess.run = real[3]
    assert applied == {"s1": "Marcus"}, applied
    assert "s2" not in meta.get("speaker_names", {}), "an invented name survived"
    assert "--strict-mcp-config" in seen["argv"], "the transcript left the sandbox"
    assert "I'm Marcus" in seen["input"]


@check
def a_missing_claude_cli_is_a_no_op_not_a_fallback():
    rows = turns(("s1", 0, "Hi, I'm Marcus."), ("s2", 6, "Hello."))
    speakers = {"s1": "Speaker 1", "s2": "Speaker 2"}
    real_find, real_engine = summarize.find_claude, summarize._pick_engine
    summarize.find_claude = lambda: None
    summarize._pick_engine = lambda: "claude"
    try:
        applied = sn.apply_inferred_names({}, rows, speakers, CFG)
    finally:
        summarize.find_claude, summarize._pick_engine = real_find, real_engine
    assert applied == {}, applied
    assert speakers["s1"] == "Speaker 1"


# --- 9. reading order ---------------------------------------------------------

@check
def a_long_meeting_is_trimmed_from_the_middle_not_the_head():
    portions = [[{"i": i}] for i in range(sn.MAX_PORTIONS * 3)]
    real_split = sn.ask._sweep_portions
    sn.ask._sweep_portions = lambda entries, limit: portions
    try:
        kept = sn._portions([])
    finally:
        sn.ask._sweep_portions = real_split
    assert len(kept) == sn.MAX_PORTIONS, len(kept)
    assert kept[0] is portions[0], "the head was dropped"
    assert kept[-1] is portions[-1], "the tail was dropped"
    head = max(1, int(sn.MAX_PORTIONS * sn.HEAD_SHARE))
    assert kept[:head] == portions[:head], "the head is not contiguous"


def main():
    failures = []
    for fn in CHECKS:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as exc:
            failures.append(f"{fn.__name__}: {exc}")
            print(f"FAIL  {fn.__name__}: {exc}")
    print("=" * 60)
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        return 1
    print(f"OK: {len(CHECKS)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
