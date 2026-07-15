"""Tests for the on-device LLM stack (local_llm + summarize + tidy + screener).

Run with the venv python from the project root:
    ~/.meetingscribe/venv/bin/python tools/test_local_llm.py

Needs macOS 26+ with Apple Intelligence enabled — the point is to exercise
the real on-device model end to end. Uses a synthetic meeting written into a
temp dir; never touches real recordings.
"""

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import local_llm  # noqa: E402
import summarize  # noqa: E402
import tidy  # noqa: E402

from contextlib import contextmanager  # noqa: E402


@contextmanager
def _force_engine(engine):
    """Pin summarize's engine choice for one test."""
    original = summarize.load_config
    summarize.load_config = lambda: dict(original(), summary_engine=engine)
    try:
        yield
    finally:
        summarize.load_config = original


def make_meeting(tmp, turns, speakers=None):
    d = Path(tmp) / "20260101-120000"
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": "20260101-120000",
        "title": "Roadmap sync",
        "created": "2026-01-01T12:00:00",
        "mode": "online",
        "status": "done",
        "duration": max(float(t["end"]) for t in turns),
        "speakers": speakers or {"you": "You", "s1": "Speaker 1"},
        "turns": turns,
        "stats": {},
    }
    (d / "meeting.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return d


def t(speaker, track, start, end, text):
    return {"speaker": speaker, "track": track, "start": start, "end": end, "text": text}


def test_available():
    ok, reason = local_llm.available(force=True)
    assert ok, f"on-device model unavailable: {reason} — {local_llm.reason_message(reason)}"
    print("PASS available")


def test_summarize_short():
    turns = [
        t("you", "mic", 0, 6, "Thanks for joining. Today we need to lock the launch date and decide who owns QA."),
        t("s1", "system", 7, 15, "I think we should move the launch to Friday the twelfth. The build is stable."),
        t("you", "mic", 16, 22, "Okay, decided, launch moves to Friday the twelfth. Can you update the release notes by Wednesday?"),
        t("s1", "system", 23, 28, "Yes, I will update the release notes by Wednesday."),
        t("you", "mic", 29, 34, "Great. I still do not know who owns QA — let us figure that out next week."),
    ]
    with tempfile.TemporaryDirectory() as tmp, _force_engine("apple"):
        d = make_meeting(tmp, turns)
        summary = summarize.summarize_meeting(d, progress_cb=lambda m: None)
        meta = json.loads((d / "meeting.json").read_text())
    for key in ("tldr", "key_points", "decisions", "action_items",
                "follow_ups", "open_questions", "follow_up_email"):
        assert key in summary, f"summary missing {key}"
    assert summary["tldr"], "empty tldr"
    assert meta.get("summary") == summary, "summary not persisted"
    text = json.dumps(summary).lower()
    assert "friday" in text, f"expected the launch decision to appear, got: {summary['tldr']!r}"
    print("PASS summarize_short —", summary["tldr"][:80], "…")


def test_summarize_claude_engine():
    """The default engine: the user's own Claude via the CLI."""
    if summarize.find_claude() is None:
        print("SKIP summarize_claude_engine (claude CLI not installed)")
        return
    turns = [
        t("you", "mic", 0, 6, "Hey Priya, thanks for making time. I want to close on the launch date."),
        t("s1", "system", 7, 15, "Of course. I think we should move the launch to Friday the twelfth — the build is stable."),
        t("you", "mic", 16, 22, "Agreed, Friday the twelfth it is. Can you update the release notes by Wednesday?"),
        t("s1", "system", 23, 28, "Yes, I'll have the release notes done by Wednesday."),
    ]
    with tempfile.TemporaryDirectory() as tmp, _force_engine("claude"):
        d = make_meeting(tmp, turns)
        summary = summarize.summarize_meeting(d, progress_cb=lambda m: None)
    assert summary.get("engine") == "claude", summary.get("engine")
    assert summary["tldr"], "empty tldr"
    text = json.dumps(summary).lower()
    assert "friday" in text and "priya" in text, f"expected specifics, got {summary['tldr']!r}"
    assert "best regards, you" not in text, "email signed as the literal 'You'"
    print("PASS summarize_claude_engine —", summary["tldr"][:80], "…")


def test_summarize_chunked():
    # A meeting long enough to force the map-reduce path (> MAX_CHUNK_CHARS).
    topics = [
        ("the database migration", "postgres", "we will move the orders table first"),
        ("the mobile app rewrite", "react native", "the login screen ships this sprint"),
        ("the hiring plan", "two backend engineers", "interviews start next month"),
        ("the pricing change", "usage based billing", "finance signs off on tuesday"),
        ("the incident from last week", "a bad deploy", "we are adding a canary stage"),
        ("the customer feedback themes", "slow exports", "exports get a progress bar"),
    ]
    turns, clock = [], 0.0
    for round_i in range(30):
        topic, detail, outcome = topics[round_i % len(topics)]
        turns.append(t("you", "mic", clock, clock + 24,
                       f"Let us talk about {topic}. My main concern is {detail} and I want us to "
                       f"agree on a concrete next step before we move on, because last time this "
                       f"slipped through and nobody followed up on it for two whole weeks."))
        clock += 25
        turns.append(t("s1", "system", clock, clock + 24,
                       f"Agreed. On {topic} the plan is that {outcome}. I can own that and report "
                       f"back in the Thursday standup with numbers, assuming nothing else blows "
                       f"up before then and we get the staging environment back."))
        clock += 25
    with tempfile.TemporaryDirectory() as tmp, _force_engine("apple"):
        d = make_meeting(tmp, turns)
        total = sum(len(x["text"]) for x in turns)
        assert total > summarize.MAX_CHUNK_CHARS, "test transcript too short to exercise chunking"
        messages = []
        summary = summarize.summarize_meeting(d, progress_cb=messages.append)
    assert any("reading the meeting" in m.lower() for m in messages), f"expected map-phase progress, got {messages}"
    assert summary["tldr"], "empty tldr on chunked path"
    print(f"PASS summarize_chunked ({total} chars, {len(messages)} progress msgs)")


def test_tidy_echo():
    # "you" echoes the remote speaker: turn 1 is a pure echo copy of turn 0,
    # turn 3 is part echo part genuine.
    turns = [
        t("s1", "system", 0.0, 4.0, "The quarterly numbers look strong across every region."),
        t("you", "mic", 1.2, 5.0, "The quarterly numbers look strong across every region."),
        t("s1", "system", 6.0, 9.0, "Marketing wants to double the budget next quarter."),
        t("you", "mic", 7.1, 12.0, "Marketing wants to double the budget next quarter. I think that is reasonable given the growth."),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        d = make_meeting(tmp, turns)
        result = tidy.tidy_meeting(d, progress_cb=lambda m: None)
        meta = json.loads((d / "meeting.json").read_text())
        assert (d / "meeting.pretidy.json").exists(), "no pretidy backup written"
    kept_text = " ".join(x["text"] for x in meta["turns"]).lower()
    assert "reasonable given the growth" in kept_text, "genuine mic speech was lost"
    assert result["dropped_turns"] + result["trimmed_turns"] >= 1, \
        f"expected at least one echo fix, got {result}"
    print("PASS tidy_echo —", result)


def test_tidy_windows():
    # Windowing math only (no model): ids must stay global and cover all turns.
    turns = [t("you", "mic", i * 5.0, i * 5.0 + 4, f"turn number {i} with some words in it") for i in range(100)]
    windows = tidy._windows(turns)
    assert len(windows) > 1, "expected multiple windows for 100 turns"
    seen = {e["id"] for w in windows for e in w}
    assert seen == set(range(100)), "windows do not cover all turns"
    assert windows[1][0]["id"] < windows[0][-1]["id"] + 1, "windows do not overlap"
    print(f"PASS tidy_windows ({len(windows)} windows)")


def test_merge_window_ops():
    ops = tidy._merge_window_ops([
        {"merge_speakers": [{"fold": "s2", "into": "s1"}],
         "drop_turns": [3, 4], "trim_turns": [{"id": 7, "keep": "hello"}],
         "rename_speakers": [{"label": "s1", "name": "Jess"}]},
        {"merge_speakers": [{"fold": "s2", "into": "s3"}],  # conflict: first wins
         "drop_turns": [4, 9], "trim_turns": [], "rename_speakers": []},
    ])
    assert ops["merge_speakers"] == {"s2": "s1"}
    assert ops["drop_turns"] == [3, 4, 9]
    assert ops["trim_turns"] == {"7": "hello"}
    assert ops["rename_speakers"] == {"s1": "Jess"}
    print("PASS merge_window_ops")


def main():
    started = time.time()
    test_available()
    test_tidy_windows()
    test_merge_window_ops()
    test_summarize_short()
    test_tidy_echo()
    test_summarize_chunked()
    test_summarize_claude_engine()
    print(f"ALL PASS in {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()
