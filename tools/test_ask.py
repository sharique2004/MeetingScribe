#!/usr/bin/env python3
"""Regression tests for ask.py citation validation — the seekability contract.

WHY THIS FILE EXISTS
--------------------
Nothing in the suite imported ask.py. Its citations are the only part of an
answer the user can VERIFY, so a citation that points at the wrong line is worse
than no citation at all: it looks like evidence.

_validate_citations() promises two things about every citation it lets through:

    * the "t" is the start of a real turn, so the player can seek to it;
    * the "quote" is text that is genuinely in THAT turn.

Both can be kept while still attributing a sentence to the wrong speaker, and
that is the defect under test here.

THE CRITICAL BUG (resolution by nearest time instead of by quote)
-----------------------------------------------------------------
Turns overlap. In the fixtures below two turns start at 0.00 (the mic and the
call-audio track), and two more pairs start 0.30 s apart — all far inside
CITATION_TOLERANCE_S (2.0 s). When the model returns a "t" that is within
tolerance of MORE THAN ONE turn, the timestamp cannot decide which one it meant.
The quote can, and it is the thing the model actually copied.

Resolving such a citation by nearest-time picks a turn essentially at random,
and then the "pin the quote to what was really said" step overwrites the model's
correct quote with the WRONG turn's text. The user is shown a sentence the
speaker never said, with a working seek button under it.

TWO CORPORA
-----------
test/synthetic/   THE BASELINE, committed and privacy-free. Its citation fixture
                  is written by tools/make_synthetic_fixtures.py with the SAME
                  overlap structure as the frozen demo meeting — two turns at
                  exactly 0.00 and two pairs 0.30 s apart — so the ambiguity
                  under test is real and not invented. Always runs.
test/fixtures/    OPTIONAL, local only, gitignored. Adds the frozen demo
                  meeting. Skipped with a message when absent.

No LLM is called, no network, no subprocess: every check drives the pure
validation helpers directly. Nothing under recordings/ or ~/.meetingscribe/ is
read or written.

    ~/.meetingscribe/venv/bin/python tools/test_ask.py

Check functions are named check_* and all work sits behind __main__, so a stray
`pytest tools/` collects and runs nothing.
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import ask  # noqa: E402

sys.path.insert(0, str(REPO / "tools"))
from test_diarization import fixture_dir, report, run  # noqa: E402


# ---------------------------------------------------------------------------
# One spec per corpus. The STRUCTURE is identical — two turns starting at the
# same instant, two pairs 0.30 s apart, and one long turn far from everything
# else — only the words differ, because the quotes have to be verbatim from the
# fixture they are checked against.
#
# synthetic (tools/make_synthetic_fixtures.py, script_ask_demo):
#   i  start-end        text
#   0   0.00- 1.02  Hello there, everyone.
#   1   0.00- 1.26  Morning to you all.                  <- identical start
#   2   1.02- 3.96  I am dialling in from the other office today.
#   3   1.32- 4.20  Thanks for joining the quarterly planning review.
#   4   3.96- 9.90  I agree with the pilot idea, …       <- 0.30 s apart, and
#   5   4.26-13.08  I wanted to start with the budget review …   int() differs
#   6  13.14-29.34  I actually have the numbers right here. …
#   7  29.52-41.82  That is a great question. …
#   8  41.88-48.24  I will draft the proposal …
#
# local: the frozen demo meeting, same timings, different words.
# ---------------------------------------------------------------------------

SYNTHETIC_SPEC = {
    "slug": "synthetic-ask-demo-call-20200101-000020",
    "min_turns": 9,
    # t, quote (verbatim from `want`), want, why
    "ambiguous": [
        (4.10, "I wanted to start with the budget review", 5,
         "turn 4 starts 3.96 and turn 5 starts 4.26: nearest-time picks 4, and "
         "they even seek to different seconds (3 vs 4)"),
        (4.10, "I agree with the pilot idea", 4,
         "the mirror image — the same ambiguous t, quoted from the other turn. "
         "A fix that always prefers the later turn fails here"),
        (0.00, "Morning to you all", 1,
         "two turns start at exactly 0.00; only the quote can tell them apart"),
        (0.00, "Hello there, everyone", 0, "the mirror image at 0.00"),
        (1.10, "Thanks for joining the quarterly planning review", 3,
         "turn 2 starts 1.02, turn 3 starts 1.32; nearest-time picks 2"),
        (1.10, "I am dialling in from the other office", 2, "the mirror image"),
    ],
    "survives_quote": "I wanted to start with the budget review",
    "survives_turn": 5,
    "clear_turn": 6,
    "clear": [
        ({"t": 13.14, "quote": "Revenue grew 12% compared to last quarter"},
         "exact start, verbatim quote"),
        ({"t": 13, "quote": "Revenue grew 12% compared to last quarter"},
         "start rounded to whole seconds, as the prompt asks for"),
        ({"t": 13.14}, "no quote at all"),
        ({"t": 20.0, "quote": "customer acquisition cost went up as well"},
         "a moment INSIDE the turn snaps back to its start"),
        ({"t": 13.14, "quote": "the revenue was up twelve percent on the quarter"},
         "a paraphrase must not drag it elsewhere"),
    ],
    "real_quote": "Revenue grew 12% compared to last quarter",
    "fake_quote": "Revenue tripled and we are hiring forty people next month",
}

LOCAL_SPEC = {
    "slug": "demo-meeting-synthesized-voices-20260610-000001",
    "min_turns": 9,
    "ambiguous": [
        (4.10, "I wanted to start with the budget review", 5,
         "turn 4 starts 3.96 and turn 5 starts 4.26: nearest-time picks 4, and "
         "they even seek to different seconds (3 vs 4)"),
        (4.10, "I agree with the experiment idea", 4,
         "the mirror image — the same ambiguous t, quoted from the other turn. "
         "A fix that always prefers the later turn fails here"),
        (0.00, "Good morning, everyone", 1,
         "two turns start at exactly 0.00; only the quote can tell them apart"),
        (0.00, "Hello, team", 0, "the mirror image at 0.00"),
        (1.10, "Thanks for joining the Quarterly Planning Call", 3,
         "turn 2 starts 1.02, turn 3 starts 1.32; nearest-time picks 2"),
        (1.10, "This is Shariq speaking from my side", 2, "the mirror image"),
    ],
    "survives_quote": "I wanted to start with the budget review",
    "survives_turn": 5,
    "clear_turn": 6,
    "clear": [
        ({"t": 13.14, "quote": "Revenue grew 12% compared to last quarter"},
         "exact start, verbatim quote"),
        ({"t": 13, "quote": "Revenue grew 12% compared to last quarter"},
         "start rounded to whole seconds, as the prompt asks for"),
        ({"t": 13.14}, "no quote at all"),
        ({"t": 20.0, "quote": "customer acquisition cost went up as well"},
         "a moment INSIDE the turn snaps back to its start"),
        ({"t": 13.14, "quote": "the revenue was up twelve percent on the quarter"},
         "a paraphrase must not drag it elsewhere"),
    ],
    "real_quote": "Revenue grew 12% compared to last quarter",
    "fake_quote": "Revenue tripled and we are hiring forty people next month",
}

SPECS = {"synthetic": SYNTHETIC_SPEC, "local": LOCAL_SPEC}


def spec_for(corpus):
    try:
        return SPECS[corpus.name]
    except KeyError:  # pragma: no cover - a new corpus needs its own quotes
        raise SystemExit(f"no citation spec for corpus {corpus.name!r}")


def demo_entries(corpus):
    spec = spec_for(corpus)
    meta = json.loads(
        (fixture_dir(corpus, spec["slug"]) / "meeting.json").read_text(encoding="utf-8"))
    entries = ask._turn_entries(meta)
    if len(entries) < spec["min_turns"]:
        raise SystemExit(f"{spec['slug']} changed shape: {len(entries)} turns")
    return entries


def by_index(entries, i):
    return next(e for e in entries if e["i"] == i)


def ambiguous_at(entries, t):
    """The turns a timestamp cannot choose between — starts within tolerance."""
    return [e for e in entries
            if abs(e["start"] - t) <= ask.CITATION_TOLERANCE_S]


def placed_index(entries, citation):
    """The turn index _place() resolved a citation to, or None if it dropped it.

    _place returns either the entry or an (entry, placed_by_quote) pair
    depending on the revision; the turn it chose is the contract, the shape of
    the return value is not.
    """
    placed = ask._place(citation, entries)
    if placed is None:
        return None
    entry = placed[0] if isinstance(placed, tuple) else placed
    return None if entry is None else entry["i"]


# -------------------------------------------------------------------- checks --

def check_ambiguous_timestamp_resolves_by_quote(fail, corpus):
    """THE BUG: within tolerance of two turns, the QUOTE decides — not the clock.

    Each row is a real overlap in the fixture. "nearest" is the turn a pure
    nearest-time rule picks; "want" is the turn the model actually quoted. Where
    the two differ, resolving by time attributes the quote to the wrong speaker.
    """
    entries = demo_entries(corpus)
    spec = spec_for(corpus)
    for t, quote, want, why in spec["ambiguous"]:
        candidates = ambiguous_at(entries, t)
        if len(candidates) < 2:
            fail(f"test premise broken: t={t} is no longer ambiguous "
                 f"({len(candidates)} turn(s) within {ask.CITATION_TOLERANCE_S}s)")
            continue
        nearest = min(entries, key=lambda e: abs(e["start"] - t))["i"]
        got = placed_index(entries, {"t": t, "quote": quote})
        mark = "ok " if got == want else "BAD"
        print(f"    {mark} t={t:5.2f} quote={quote[:34]!r:38s} "
              f"-> turn {got} (nearest-time would say {nearest}, want {want})")
        if got != want:
            fail(f"citation t={t} quoting turn {want} was placed on turn {got} "
                 f"— {why}")

    # And the consequence the user sees: the quote must survive intact, not be
    # overwritten with the other turn's words.
    quote = spec["survives_quote"]
    home = spec["survives_turn"]
    kept = ask._validate_citations([{"t": 4.10, "quote": quote}], entries)
    print(f"    validated -> {json.dumps(kept)}")
    if len(kept) != 1:
        fail(f"an ambiguous but quotable citation was dropped: {kept}")
    elif ask._norm(quote) not in ask._norm(kept[0]["quote"]):
        fail(f"the model's correct quote {quote!r} was overwritten with "
             f"{kept[0]['quote']!r} — it was placed on the wrong turn, then "
             "'pinned' to that turn's text")
    elif kept[0]["t"] != int(by_index(entries, home)["start"]):
        fail(f"the citation seeks to {kept[0]['t']}s, but the quoted turn "
             f"starts at {by_index(entries, home)['start']}s")


def check_unambiguous_timestamp_still_wins(fail, corpus):
    """The fix must not over-rotate: a clear timestamp is still authoritative.

    A citation whose "t" matches exactly one turn must land there whether the
    quote is verbatim, paraphrased, or missing entirely — otherwise a slightly
    reworded quote would start dragging citations across the meeting.
    """
    entries = demo_entries(corpus)
    spec = spec_for(corpus)
    home = spec["clear_turn"]
    for citation, why in spec["clear"]:
        others = [e for e in ambiguous_at(entries, float(citation["t"]))
                  if e["i"] != home]
        got = placed_index(entries, citation)
        mark = "ok " if got == home else "BAD"
        print(f"    {mark} {why:52s} -> turn {got}")
        if others:
            fail(f"test premise broken: t={citation['t']} is ambiguous with "
                 f"turns {[e['i'] for e in others]}")
        if got != home:
            fail(f"unambiguous citation ({why}) landed on turn {got}, not {home}")


def check_unplaceable_citations_are_dropped(fail, corpus):
    """A timestamp that matches no turn, and a quote that matches no text, is
    a hallucination — the UI must never be handed a seek that goes nowhere."""
    entries = demo_entries(corpus)
    end = max(e["end"] for e in entries)
    cases = [
        ({"t": 9999, "quote": "we agreed to acquire the Frankfurt office"},
         "a time past the end of the meeting, quoting nothing"),
        ({"t": end + 30, "quote": "and that was the last thing anyone said"},
         "just past the end"),
        ({"t": -5, "quote": "before the meeting even started, apparently"},
         "a negative time"),
        ({"quote": "no timestamp and nothing that matches the transcript"},
         "no timestamp at all, quote matches nothing"),
        ({"t": None, "quote": "still nothing that was ever said in this call"},
         "an explicit null timestamp"),
        ({"t": "not a number", "quote": "unparseable time, unmatched quote"},
         "an unparseable timestamp"),
        ({"t": 9999, "quote": "yes"}, "a quote too short to identify anything"),
        ({"t": 9999}, "neither a usable time nor a quote"),
        ({}, "an empty citation object"),
    ]
    for citation, why in cases:
        got = placed_index(entries, citation)
        mark = "ok " if got is None else "BAD"
        print(f"    {mark} {why:52s} -> {got}")
        if got is not None:
            fail(f"an unplaceable citation ({why}) was placed on turn {got}: "
                 f"{citation!r}")

    # Non-citations must not crash or leak through either.
    for raw, why in [(None, "citations missing"), ("[]", "a string"),
                     ([None, 3, "x", []], "junk list items"),
                     ({"t": 0}, "a dict instead of a list")]:
        out = ask._validate_citations(raw, entries)
        print(f"    {'ok ' if out == [] else 'BAD'} {why:52s} -> {out}")
        if out != []:
            fail(f"_validate_citations({why}) returned {out!r}, expected []")


def check_a_meeting_with_no_turns_does_not_crash(fail, corpus):
    """A silent or failed recording has zero turns. Asking about it must answer,
    not 500.

    _place() calls min(entries, …) on the turn list; an empty one raises
    ValueError, which propagates out of answer_question() and turns the whole
    /api/ask request into a server error. The user's question disappears.
    """
    for raw in ([{"t": 5, "quote": "anything at all that was said here"}],
                [{"t": 0, "quote": "hello"}],
                [{"quote": "no timestamp either"}]):
        try:
            out = ask._validate_citations(raw, [])
        except Exception as exc:
            fail(f"_validate_citations({raw!r}, []) raised "
                 f"{type(exc).__name__}: {exc} — a meeting with no turns must "
                 "yield no citations, not an exception")
            print(f"    BAD {str(raw)[:56]:58s} -> {type(exc).__name__}")
            continue
        print(f"    {'ok ' if out == [] else 'BAD'} {str(raw)[:56]:58s} -> {out}")
        if out != []:
            fail(f"a meeting with no turns produced citations: {out!r}")


def check_fabricated_quote_never_reaches_the_user(fail, corpus):
    """Whatever survives, the quote is always text that was really said.

    And a fabricated citation must not displace a correct one that resolves to
    the same turn: the correct quote wins, it is not overwritten by the later,
    invented one.
    """
    entries = demo_entries(corpus)
    spec = spec_for(corpus)
    home = spec["clear_turn"]
    real, fake = spec["real_quote"], spec["fake_quote"]

    kept = ask._validate_citations(
        [{"t": 13.14, "quote": real}, {"t": 13.14, "quote": fake}], entries)
    print(f"    correct first  -> {json.dumps(kept)}")
    if len(kept) != 1:
        fail(f"two citations on the same turn produced {len(kept)} entries: {kept}")
    if kept and ask._norm(real) not in ask._norm(kept[0]["quote"]):
        fail(f"a fabricated quote displaced the correct one: {kept[0]['quote']!r}")

    kept = ask._validate_citations(
        [{"t": 13.14, "quote": fake}, {"t": 13.14, "quote": real}], entries)
    print(f"    fabricated first -> {json.dumps(kept)[:120]}")
    if len(kept) != 1:
        fail(f"two citations on the same turn produced {len(kept)} entries: {kept}")
    if kept and ask._norm(fake) in ask._norm(kept[0]["quote"]):
        fail(f"an invented sentence was shown to the user as a quote: "
             f"{kept[0]['quote']!r}")
    if kept and ask._norm(kept[0]["quote"]) not in ask._norm(by_index(entries, home)["text"]):
        fail("the surviving quote is not text from the turn it points at: "
             f"{kept[0]['quote']!r}")


def check_every_kept_citation_is_seekable(fail, corpus):
    """The invariant, swept over the whole frozen transcript.

    For every turn, and for a spread of plausible model behaviours (exact start,
    rounded start, a moment inside, a nearby-but-wrong time, a verbatim quote, a
    truncated quote, an invented quote), every citation that survives must point
    at a real turn start AND carry text that is genuinely in a turn starting
    there.
    """
    entries = demo_entries(corpus)
    starts = {int(e["start"]) for e in entries}
    raw = []
    for e in entries:
        mid = (e["start"] + e["end"]) / 2.0
        head = " ".join(e["text"].split()[:8])
        raw += [
            {"t": e["start"], "quote": e["text"]},
            {"t": int(e["start"]), "quote": head},
            {"t": mid, "quote": head},
            {"t": e["start"] + 1.5, "quote": head},
            {"t": e["start"], "quote": "a sentence nobody in this call uttered"},
            {"t": 5000 + e["i"], "quote": head},
        ]
    kept = ask._validate_citations(raw, entries)
    print(f"    {len(raw)} raw citations over {len(entries)} turns -> "
          f"{len(kept)} kept (cap {ask.MAX_CITATIONS})")
    if len(kept) > ask.MAX_CITATIONS:
        fail(f"MAX_CITATIONS is {ask.MAX_CITATIONS} but {len(kept)} were kept")

    # A DUPLICATE CITATION IS ABOUT TURNS, NOT CLOCKS.
    #
    # This used to assert that no two kept citations share a "t", and that was
    # wrong in a way that cost the user evidence. "t" is int(turn start), so two
    # GENUINELY DIFFERENT turns that begin inside the same second have the same
    # "t" — not a corner case: the mic and call-audio tracks are diarized
    # separately, and turns 0 and 1 of both fixtures start at 0.00. Under the
    # old assertion the only way to stay green was to dedup on the truncated
    # second, which silently drops the second turn's evidence; ask.py says so at
    # its KNOWN COST comment and names this assertion as what blocks the fix.
    #
    # The real invariant is that no TURN is cited twice, which still catches the
    # duplicate that matters (the same line of the meeting shown to the user as
    # two separate pieces of evidence) while letting two different turns share a
    # second. A kept citation's turn is recovered by placing it again: its quote
    # is verbatim in its own turn, and resolving by quote is the contract the
    # first check in this file enforces.
    homes = []
    for c in kept:
        turn = placed_index(entries, c)
        if turn is None:
            fail(f"a citation that survived validation cannot be placed at all: "
                 f"{c!r} — it would seek nowhere")
            continue
        homes.append(turn)
    if len(set(homes)) != len(homes):
        twice = sorted({i for i in homes if homes.count(i) > 1})
        fail(f"the same turn was cited twice (turn(s) {twice}): {kept}")
    shared = len(homes) - len({c["t"] for c in kept})
    print(f"    {len(homes)} citations on {len(set(homes))} distinct turns; "
          f"{shared} share a truncated 't' with another "
          f"(>0 once ask.py dedups on turn identity instead of int(start))")

    for c in kept:
        if c["t"] not in starts:
            fail(f"citation t={c['t']} is not the start of any turn "
                 f"(starts: {sorted(starts)})")
            continue
        homes = [e for e in entries if int(e["start"]) == c["t"]]
        if not any(ask._norm(c["quote"]) in ask._norm(e["text"]) for e in homes):
            fail(f"citation t={c['t']} carries {c['quote'][:60]!r}, which is in "
                 f"none of the turn(s) starting there: "
                 f"{[e['text'][:40] for e in homes]}")
    if not kept:
        fail("the sweep kept no citations at all — the validator is dropping "
             "even exact, verbatim ones")


CHECKS = [
    ("an ambiguous timestamp resolves by quote", check_ambiguous_timestamp_resolves_by_quote),
    ("an unambiguous timestamp still wins", check_unambiguous_timestamp_still_wins),
    ("unplaceable citations are dropped", check_unplaceable_citations_are_dropped),
    ("a meeting with no turns does not crash", check_a_meeting_with_no_turns_does_not_crash),
    ("a fabricated quote never reaches the user", check_fabricated_quote_never_reaches_the_user),
    ("every kept citation is seekable", check_every_kept_citation_is_seekable),
]


def main():
    failures = []
    ran = run(CHECKS, failures)
    return report(failures, ran)


if __name__ == "__main__":
    sys.exit(main())
