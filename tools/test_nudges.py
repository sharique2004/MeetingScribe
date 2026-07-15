"""Decision-table tests for the nudge engine (fake clock, fake signals).

Run with the venv python from the project root:
    ~/.meetingscribe/venv/bin/python tools/test_nudges.py

Also exercises macos_audio.mic_in_use() for real (smoke only — its value
depends on what's using the mic right now).
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nudge  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make(clock, tmp, event=None, mic=False, app=None):
    holder = {"event": event, "mic": mic, "app": app}
    engine = nudge.NudgeEngine(
        calendar_event=lambda: holder["event"],
        mic_in_use=lambda: holder["mic"],
        meeting_app=lambda: holder["app"],
        now=clock,
        state_path=Path(tmp) / "nudges.json",
    )
    return engine, holder


def test_calendar_once():
    clock = Clock()
    with tempfile.TemporaryDirectory() as tmp:
        engine, holder = make(clock, tmp, event={"title": "Weekly sync", "start": 123.0})
        n = engine.evaluate(recording=False)
        assert n and n["kind"] == "calendar" and "Weekly sync" in n["title"]
        # Stable while unanswered
        assert engine.evaluate(recording=False)["id"] == n["id"]
        # Accept consumes it; the same event never nudges again
        assert engine.take(n["id"])["meeting_title"] == "Weekly sync"
        assert engine.evaluate(recording=False) is None
        clock.t += 9999
        assert engine.evaluate(recording=False) is None
        # ...but a different event does
        holder["event"] = {"title": "Standup", "start": 456.0}
        n2 = engine.evaluate(recording=False)
        assert n2 and "Standup" in n2["title"] and n2["id"] != n["id"]
        # Snooze (ack) also blocks it forever
        assert engine.ack(n2["id"])
        assert engine.evaluate(recording=False) is None
    print("PASS calendar_once")


def test_calendar_state_persists():
    clock = Clock()
    with tempfile.TemporaryDirectory() as tmp:
        engine, _ = make(clock, tmp, event={"title": "Weekly sync", "start": 123.0})
        n = engine.evaluate(recording=False)
        engine.take(n["id"])
        # Fresh engine, same state file: still won't re-nudge
        engine2, _ = make(clock, tmp, event={"title": "Weekly sync", "start": 123.0})
        assert engine2.evaluate(recording=False) is None
    print("PASS calendar_state_persists")


def test_call_persistence_and_cooldown():
    clock = Clock()
    with tempfile.TemporaryDirectory() as tmp:
        engine, holder = make(clock, tmp, mic=True, app="Zoom")
        assert engine.evaluate(recording=False) is None, "must not fire instantly"
        clock.t += 5
        assert engine.evaluate(recording=False) is None, "still under persist window"
        clock.t += 6  # 11s busy
        n = engine.evaluate(recording=False)
        assert n and n["kind"] == "call" and "Zoom" in n["title"]
        engine.take(n["id"])
        # Cooldown: still busy, no new nudge within the hour
        clock.t += 120
        assert engine.evaluate(recording=False) is None
        clock.t += nudge.CALL_COOLDOWN_S
        n2 = engine.evaluate(recording=False)
        assert n2 and n2["kind"] == "call"
        # Mic released -> persistence resets
        engine.ack(n2["id"])
        holder["mic"] = False
        clock.t += nudge.CALL_COOLDOWN_S
        assert engine.evaluate(recording=False) is None
        holder["mic"] = True
        clock.t += 2
        assert engine.evaluate(recording=False) is None, "persistence must restart"
    print("PASS call_persistence_and_cooldown")


def test_recording_suppresses_everything():
    clock = Clock()
    with tempfile.TemporaryDirectory() as tmp:
        engine, _ = make(clock, tmp,
                         event={"title": "Sync", "start": 1.0}, mic=True, app="Zoom")
        clock.t += 100
        assert engine.evaluate(recording=True) is None
        # And the mic timer didn't accumulate while we were recording
        n = engine.evaluate(recording=False)
        assert n and n["kind"] == "calendar", "calendar may fire, call must not yet"
    print("PASS recording_suppresses_everything")


def test_pending_expiry():
    clock = Clock()
    with tempfile.TemporaryDirectory() as tmp:
        engine, holder = make(clock, tmp, event={"title": "Sync", "start": 1.0})
        n = engine.evaluate(recording=False)
        assert n is not None
        clock.t += nudge.PENDING_TTL_S + 1
        holder["event"] = None
        assert engine.evaluate(recording=False) is None, "expired pending must clear"
        assert engine.take(n["id"]) is None, "expired nudge can't be accepted"
    print("PASS pending_expiry")


def test_mic_in_use_smoke():
    try:
        import macos_audio
        value = macos_audio.mic_in_use()
        assert value in (True, False)
        print(f"PASS mic_in_use_smoke (currently {value})")
    except ImportError:
        print("SKIP mic_in_use_smoke (not macOS)")


def main():
    test_calendar_once()
    test_calendar_state_persists()
    test_call_persistence_and_cooldown()
    test_recording_suppresses_everything()
    test_pending_expiry()
    test_mic_in_use_smoke()
    print("ALL PASS")


if __name__ == "__main__":
    main()
