"""Record ~3 seconds with whichever backend MEETINGSCRIBE_BACKEND selects,
then report what was captured. Exercises the same code path macOS uses when
run with MEETINGSCRIBE_BACKEND=sounddevice."""

import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio_recorder  # noqa: E402

print("backend:", audio_recorder.BACKEND)
rec = audio_recorder.MeetingRecorder()
out = Path(tempfile.mkdtemp(prefix="msc-rectest-"))
info = rec.start(out, "test")
print("devices:", {k: v["device"] for k, v in info["tracks"].items()})
print("warnings:", info["warnings"])
time.sleep(3)
print("status:", rec.status()["tracks"])
result = rec.stop()
print("stopped:", {k: (v["seconds"], v["rate"]) for k, v in result["tracks"].items()})
for key, tr in result["tracks"].items():
    p = out / tr["file"]
    with wave.open(str(p)) as wf:
        frames, rate, ch = wf.getnframes(), wf.getframerate(), wf.getnchannels()
    ok = frames > 2 * rate  # at least ~2s of audio written
    print(f"{key}: {frames} frames @ {rate} Hz, {ch}ch -> {'OK' if ok else 'TOO SHORT'}")
print("PASS" if result["duration"] > 2 else "FAIL")
