"""Smoke test: run the processing pipeline on the synthesized demo meeting.

Also holds the offline unit checks for echo removal (test_drop_echo_* below).
Those touch no recording, no model and no network, so they are safe for pytest
to collect out of this file; everything that reads a recording stays behind
main().

Two things this test is careful about:

* It never touches the real recording. ``pipeline.process_meeting`` writes back
  into the directory it reads (meeting.json, analysis.json, analysis.npz), so
  running it in place would destroy the before-state and invalidate the
  analysis.npz cache this test is meant to validate against. We copy the
  meeting into a temp directory and run there.
* It asserts the golden result instead of only printing it.

Usage:
    python tools/test_pipeline.py                 # the demo meeting
    python tools/test_pipeline.py <meeting-id>    # any recording (suffix match)
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import (  # noqa: E402
    _far_end_cover,
    _merge_spans,
    drop_echo,
    process_meeting,
)

RECORDINGS = Path(__file__).resolve().parents[1] / "recordings"

# The demo meeting lives in a directory whose name is
# "Demo meeting (synthesized voices) — 20260610-000001"; only the timestamp
# suffix is stable, so resolve by suffix rather than hardcoding the title.
DEMO_ID = "20260610-000001"

# Golden regression values for the demo meeting. If a diarization change moves
# these, that is the change to justify — not the numbers to edit away.
GOLDEN_SPEAKERS = {"you": "You", "s1": "Speaker 1", "s2": "Speaker 2"}
GOLDEN_WARNINGS = []
# 44 as of 2026-08-03: the default ASR backend moved to Parakeet, whose
# sentence segmentation cuts the demo's system track into different spans
# than Apple Speech did, so build_windows() emits 44 windows where the Apple
# transcript yielded 39. The cluster outcome is unchanged (2 remote voices,
# golden speaker map above). The FROZEN fixture in test/fixtures/ still
# carries the 39-window Apple-era cache, and tools/eval_diarization.py keeps
# gating that at 39 — this constant covers a FRESH reprocess, which now runs
# Parakeet.
GOLDEN_EMBEDDING_WINDOWS = {"system": 44}


def _seg(start, text, rate=0.5):
    """One recognizer-shaped segment: text plus per-word timestamps.

    `rate` is seconds per word. It matters for the echo tests below because
    drop_echo compares against far-end words that END within ECHO_SLACK_S of
    the mic segment, so a fast far-end sentence can sit entirely inside the
    comparison window while occupying none of the mic segment's own span.
    That combination is exactly the one the cover gate exists to judge, and
    it is what the real answer-the-far-end segments look like.
    """
    words = text.split()
    return {
        "start": start,
        "end": start + rate * len(words),
        "text": text,
        "words": [
            {"w": w, "s": start + rate * i, "e": start + rate * (i + 1)}
            for i, w in enumerate(words)
        ],
    }


def test_drop_echo_without_system_speech_is_a_noop():
    """An empty system transcript must cost the mic track nothing.

    The echo pass is gated on both tracks carrying speech rather than on the
    meeting's declared mode, so that a hybrid meeting (in-person room, remote
    participants, both tracks live) stops publishing the remote voice twice.
    That gate is only safe because of the invariant asserted here: with no
    system speech there is nothing to match against, so nothing is removed.
    In-person meetings run the mic track through this function now, and
    in-person the mic track IS the transcript, so a false drop is not a
    duplicate removed, it is a person's words deleted.
    """
    mic = [
        _seg(0.0, "so where did we land on the pricing question"),
        # A genuine repeat. Nothing may key off self-similarity in the mic track.
        _seg(6.0, "so where did we land on the pricing question"),
        _seg(12.0, "yeah"),
    ]
    kept, dropped, trimmed = drop_echo(mic, [])
    assert dropped == 0, f"dropped {dropped} segment(s) with no system track"
    assert trimmed == 0, f"trimmed {trimmed} segment(s) with no system track"
    assert kept == mic, "mic transcript changed with no system track"
    assert all(k is m for k, m in zip(kept, mic)), "segments must pass through untouched"


def test_drop_echo_catches_misheard_echo():
    """Echo the recognizer heard WRONG is still echo.

    The word comparison is all-or-nothing per word, so an echo transcribed as
    "the dead line slipt" for "the deadline slipped" scores 0.46 and used to be
    published as something the user said. Characters score the same pair 0.96.
    The second segment is the control: real user speech in the same window,
    sharing a word with the far end, has to survive.
    """
    system = [
        _seg(
            0.0,
            "the deadline slipped because the contractor rewrote the parser "
            "and nobody told the client until the launch review",
        )
    ]
    echoed = _seg(0.3, "the dead line slipt because the contract or rewrote the parcer")
    genuine = _seg(6.0, "can you send me the design doc when you get a chance")

    kept, dropped, trimmed = drop_echo([echoed, genuine], system)
    assert dropped == 1, f"expected the misheard echo to be dropped, dropped {dropped}"
    assert [k["text"] for k in kept] == [genuine["text"]], (
        f"wrong segment survived: {[k['text'] for k in kept]}"
    )
    assert trimmed == 0, f"expected a whole-segment drop, trimmed {trimmed}"


def test_drop_echo_keeps_short_replies_that_reuse_the_far_end_words():
    """A short answer that borrows the far end's words is conversation, not echo.

    Both segments below are verbatim from real meetings on this machine, and
    both scored above ECHO_DROP_RATIO on characters, so an ungated character
    path deleted them. Neither can be acoustic echo: the far end had stopped
    talking, and each is the ordinary answer to the question it had just
    finished asking. Correlating the two tracks over those two segments in the
    original recordings gives 0.09 and 0.02, so the microphone was not
    carrying the far-end signal at all.

    The timings here reproduce that shape rather than approximating it. The
    far end finishes just before the mic segment starts, so its words are
    still inside the comparison window (drop_echo looks back past the segment
    start by ECHO_SLACK_S) while covering none of the segment itself. The real
    segments measured 0.00 and 0.06 cover; these measure 0.00.

    This pins the cover gate, ECHO_CHAR_MIN_COVER. Text alone cannot separate
    an answer from an echo, because an answer that reuses the question's words
    really does match it; only the clock can, and cover is the clock.
    """
    system = [_seg(0.0, "yeah can you hear it in tv awesome", rate=0.18)]
    reply = _seg(1.5, "Yeah, I can hear you.")
    kept, dropped, trimmed = drop_echo([reply], system)
    assert dropped == 0, f"deleted a genuine short reply (dropped {dropped})"
    assert [k["text"] for k in kept] == [reply["text"]]

    system = [_seg(0.0, "and you used to live in dubai right", rate=0.18)]
    answer = _seg(1.5, "Yeah, I lived in Dubai.")
    kept, dropped, trimmed = drop_echo([answer], system)
    assert dropped == 0, f"deleted a genuine short answer (dropped {dropped})"
    assert [k["text"] for k in kept] == [answer["text"]]


def test_drop_echo_keeps_speech_the_far_end_was_silent_through():
    """Same words, same window, different clock: only one of them is echo.

    This is the character path's whole problem in two assertions. The mic text
    is identical in both halves and so is the far-end sentence it matches
    (0.96 on characters either way). The only difference is when the user
    said it. In the first half the far end is talking underneath, which is
    what acoustic echo requires and cannot happen any other way. In the second
    the far end has stopped, so whatever the microphone picked up came from
    the room, not from the speakers, and it has to be published even though it
    matches almost letter for letter.

    Length cannot be what saves the second one: at eleven tokens it is well
    clear of ECHO_CHAR_MIN_TOKENS. Only cover separates them.
    """
    system = [_seg(0.0, "the deadline slipped because the contractor rewrote the parser", rate=0.16)]
    misheard = "the dead line slipt because the contract or rewrote the parcer"

    over_the_far_end = _seg(0.1, misheard, rate=0.16)
    kept, dropped, trimmed = drop_echo([over_the_far_end], system)
    assert dropped == 1, f"echo under live far-end speech survived (dropped {dropped})"

    after_the_far_end = _seg(1.5, misheard, rate=0.16)
    kept, dropped, trimmed = drop_echo([after_the_far_end], system)
    assert dropped == 0, f"deleted speech the far end was silent through (dropped {dropped})"
    assert [k["text"] for k in kept] == [after_the_far_end["text"]]


def test_drop_echo_keeps_four_word_matches_whatever_the_far_end_is_doing():
    """Four words is below where the character test has any measured power.

    Scored against far-end windows 600 s away that cannot be echo of them,
    and counting only the windows where the far end was talking (so the cover
    gate is satisfied and out of the way), four-token mic segments fire on
    16.7% of real windows and 9.1% of unrelated ones. Five-token segments fire
    on 38.1% and 2.7%. Four words is a coin toss with extra steps, so the
    character path does not get a vote there.

    The segment below is real, and the far end really was talking over it, so
    this is very likely a genuine echo that now stays in the transcript. That
    is the deliberate side to err on: a duplicated line is cosmetic, and a
    deleted one is gone.
    """
    system = [_seg(0.0, "im so excited")]
    reply = _seg(0.1, "I am so excited.")
    kept, dropped, trimmed = drop_echo([reply], system)
    assert dropped == 0, f"character path voted on a four-word segment (dropped {dropped})"
    assert [k["text"] for k in kept] == [reply["text"]]


def test_cover_gate_does_not_reach_the_word_path():
    """The gate belongs to the character fallback and nothing else.

    A verbatim repeat of the far end is still a drop even with the far end
    silent through it, because the word path decides that one and the word
    path is unchanged. It can afford to be: it demands near-total agreement
    word for word, which is a far harder thing to hit by accident than a
    letter score. Pinning it here so that widening the gate later has to be a
    decision rather than an accident.
    """
    system = [_seg(0.0, "we should ship the parser rewrite next tuesday", rate=0.18)]
    verbatim = _seg(1.5, "we should ship the parser rewrite next tuesday")
    kept, dropped, trimmed = drop_echo([verbatim], system)
    assert dropped == 1, f"word-path echo survived the cover gate (dropped {dropped})"
    assert kept == []


def test_far_end_cover_is_an_occupancy_fraction():
    """Overlapping far-end words must be merged before they are counted.

    Recognizers hand back words whose spans overlap, so summing durations
    would let a busy stretch of far-end speech score past 1.0 and turn the
    gate into a no-op. Merging first is what makes the number mean "this
    fraction of the segment had far-end sound in it".
    """
    spans = _merge_spans([(0.0, 1.0), (0.5, 2.0), (1.8, 3.0), (5.0, 6.0)])
    assert spans == [[0.0, 3.0], [5.0, 6.0]], spans
    ends = [s[1] for s in spans]

    assert _far_end_cover(0.0, 3.0, spans, ends) == 1.0
    assert _far_end_cover(3.0, 5.0, spans, ends) == 0.0
    assert abs(_far_end_cover(2.0, 6.0, spans, ends) - 0.5) < 1e-9
    assert _far_end_cover(4.0, 4.0, spans, ends) == 0.0, "zero-length span must not divide by zero"
    assert _far_end_cover(0.0, 10.0, [], []) == 0.0, "no far-end speech is zero cover"


def resolve_meeting_dir(meeting_id):
    """Find the recording directory for a meeting id.

    Directory names are "<title> — <timestamp>", so an exact-name match is
    tried first, then a suffix match on the timestamp, then a substring match.
    """
    if not RECORDINGS.is_dir():
        raise SystemExit(f"no recordings directory at {RECORDINGS}")
    candidates = sorted(d for d in RECORDINGS.iterdir() if d.is_dir())

    exact = [d for d in candidates if d.name == meeting_id]
    suffix = [d for d in candidates if d.name.endswith(meeting_id)]
    substring = [d for d in candidates if meeting_id in d.name]
    for matches in (exact, suffix, substring):
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = "\n  ".join(d.name for d in matches)
            raise SystemExit(f"{meeting_id!r} is ambiguous:\n  {names}")
    names = "\n  ".join(d.name for d in candidates) or "(none)"
    raise SystemExit(f"no recording matching {meeting_id!r}. Available:\n  {names}")


def embedding_window_counts(meeting_dir):
    """{track: n_windows} from the analysis.npz the run just wrote."""
    npz_path = meeting_dir / "analysis.npz"
    if not npz_path.exists():
        return {}
    import numpy as np

    with np.load(npz_path) as npz:
        return {
            key[: -len("_embeddings")]: int(npz[key].shape[0])
            for key in npz.files
            if key.endswith("_embeddings")
        }


def report(meta, windows):
    print("STATUS:", meta["status"])
    print("LANGUAGES:", meta.get("languages"))
    print("SPEAKERS:", json.dumps(meta["speakers"]))
    print("WARNINGS:", meta["warnings"])
    print("EMBEDDING WINDOWS:", windows)
    print("TURNS:")
    for t in meta["turns"]:
        name = meta["speakers"].get(t["speaker"], t["speaker"])
        print(f"  [{t['start']:6.1f}-{t['end']:6.1f}] {name}: {t['text']}")
    print("STATS:")
    for key, st in meta["stats"]["per_speaker"].items():
        name = meta["speakers"].get(key, key)
        top = ", ".join(f"{w}x{c}" for w, c in list(st["fillers"].items())[:3]) or "-"
        print(
            f"  {name}: {st['seconds']}s ({round(st['share']*100)}%), {st['words']} words, "
            f"{st['wpm']} wpm, {st['questions']} questions, fillers: {top}"
        )


def check_golden(meta, windows):
    """Assert the demo meeting's golden result. Returns nothing; raises on drift."""
    assert meta["status"] == "done", f"status was {meta['status']!r}, expected 'done'"
    # GOLDEN_EMBEDDING_WINDOWS is a claim about Parakeet's segmentation (44
    # windows; Apple Speech cut this track into 39). Pin the backend so a
    # machine that silently fell down the ladder fails HERE, with the reason,
    # instead of on a mysterious window count below.
    backend = (meta.get("processing") or {}).get("backend")
    assert backend == "parakeet", (
        f"expected the parakeet backend to have produced this run, got "
        f"{backend!r} — the window-count golden below is only valid for it"
    )
    assert meta["speakers"] == GOLDEN_SPEAKERS, (
        f"speaker map drifted:\n  got      {meta['speakers']}\n"
        f"  expected {GOLDEN_SPEAKERS}"
    )
    assert meta["warnings"] == GOLDEN_WARNINGS, (
        f"warnings drifted:\n  got      {meta['warnings']}\n"
        f"  expected {GOLDEN_WARNINGS}"
    )
    assert windows == GOLDEN_EMBEDDING_WINDOWS, (
        f"embedding window counts drifted:\n  got      {windows}\n"
        f"  expected {GOLDEN_EMBEDDING_WINDOWS}"
    )
    assert meta["turns"], "no turns were produced"


def main():
    test_drop_echo_without_system_speech_is_a_noop()
    test_drop_echo_catches_misheard_echo()
    test_drop_echo_keeps_short_replies_that_reuse_the_far_end_words()
    test_drop_echo_keeps_speech_the_far_end_was_silent_through()
    test_drop_echo_keeps_four_word_matches_whatever_the_far_end_is_doing()
    test_cover_gate_does_not_reach_the_word_path()
    test_far_end_cover_is_an_occupancy_fraction()
    print("OK — echo unit checks pass.")

    meeting_id = sys.argv[1] if len(sys.argv) > 1 else DEMO_ID
    source = resolve_meeting_dir(meeting_id)
    is_demo = source.name.endswith(DEMO_ID)
    print(f"MEETING: {source.name}")

    # Run against a throwaway copy: process_meeting writes meeting.json,
    # analysis.json and analysis.npz back into the directory it reads.
    with tempfile.TemporaryDirectory(prefix="meetingscribe-test-") as tmp:
        work = Path(tmp) / source.name
        shutil.copytree(source, work)
        print(f"WORKDIR: {work}")

        meta = process_meeting(work, lambda msg: print("  >", msg))
        windows = embedding_window_counts(work)
        report(meta, windows)

        if is_demo:
            check_golden(meta, windows)
            print("\nOK — golden speaker map, warnings and window counts match.")
        else:
            print("\nOK — ran to completion (golden assertions apply to the demo only).")


if __name__ == "__main__":
    main()
