"""Tests for live streaming captions (apple_live helper + LiveSession).

Run with the venv python from the project root:
    ~/.meetingscribe/venv/bin/python tools/test_live_captions.py

Streams a real recorded WAV through the helper in 100 ms chunks — exactly
how the recorder feeds it — and checks the event stream contract.
"""

import json
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import live_captions  # noqa: E402
import swift_helpers  # noqa: E402

WAV = Path(__file__).resolve().parent.parent / \
    "recordings" / "Demo meeting (synthesized voices) — 20260610-000001" / "mic.wav"


def _stream_wav_through(binary):
    w = wave.open(str(WAV), "rb")
    rate, ch = w.getframerate(), w.getnchannels()
    proc = subprocess.Popen(
        [binary, "en-US", str(rate), str(ch)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    lines = []
    t = threading.Thread(target=lambda: lines.extend(
        ln.decode().strip() for ln in proc.stdout))
    t.start()
    chunk = rate // 10  # 100 ms — recorder-sized chunks
    while True:
        data = w.readframes(chunk)
        if not data:
            break
        proc.stdin.write(data)
    proc.stdin.close()
    proc.wait(timeout=120)
    t.join(timeout=10)
    return [json.loads(ln) for ln in lines if ln]


def test_helper_stream():
    binary = swift_helpers.ensure_binary(
        Path(__file__).resolve().parent / "apple_live.swift", "apple_live", min_macos=(26, 0))
    assert binary, "apple_live helper unavailable (needs macOS 26+, arm64)"
    events = _stream_wav_through(binary)
    finals = [e for e in events if e["t"] == "final"]
    partials = [e for e in events if e["t"] == "partial"]
    assert events and events[-1]["t"] == "done", "missing done event"
    assert finals, "no final captions from a WAV with speech"
    assert partials, "no partial captions — volatile results not flowing"
    starts = [e["start"] for e in finals]
    assert starts == sorted(starts), f"final timestamps not monotonic: {starts}"
    assert all(e["end"] >= e["start"] for e in finals), "end < start"
    assert all(e["text"].strip() for e in finals), "empty final text"
    text = " ".join(e["text"] for e in finals).lower()
    assert "tracking dashboard" in text, f"unexpected transcription: {text[:120]}"
    print(f"PASS helper_stream ({len(partials)} partials, {len(finals)} finals)")


def test_live_session():
    session = live_captions.LiveSession(locale="en-US", context_strings=["Shariq"])
    assert session.enabled, "LiveSession should be enabled on this machine"
    tap = session.tap("mic")
    assert tap is not None
    w = wave.open(str(WAV), "rb")
    rate, ch = w.getframerate(), w.getnchannels()
    chunk = 1024  # recorder chunk size
    while True:
        data = w.readframes(chunk)
        if not data:
            break
        tap(data, ch, rate)
        time.sleep(0.002)  # keep the queue from saturating instantly
    session.stop()
    deadline = time.time() + 60
    snap = session.snapshot()
    while time.time() < deadline:
        snap = session.snapshot()
        if snap["turns"] and not snap["partials"]:
            feed = session._feeds.get("mic")
            if feed and feed.proc.poll() is not None:
                break
        time.sleep(0.5)
    assert snap["turns"], "no caption turns from LiveSession"
    assert all(t["who"] == "You" for t in snap["turns"]), "mic track must label as You"
    since = snap["turns"][0]["seq"]
    partial_snap = session.snapshot(since=since)
    assert all(t["seq"] > since for t in partial_snap["turns"]), "since filter broken"
    session.discard()
    print(f"PASS live_session ({len(snap['turns'])} turns, dropped={snap['dropped']})")


def main():
    started = time.time()
    assert WAV.exists(), f"demo WAV missing: {WAV}"
    test_helper_stream()
    test_live_session()
    print(f"ALL PASS in {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()
