"""Record ~3 seconds with whichever backend MEETINGSCRIBE_BACKEND selects,
then report what was captured. Exercises the same code path macOS uses when
run with MEETINGSCRIBE_BACKEND=sounddevice.

--require-track KEY (repeatable) makes the run FAIL unless that track exists
and holds >2 s at its reported rate — without it a dead system tap would pass
on the mic track alone. --seconds N extends the capture window.
Pair with MEETINGSCRIBE_SYSTEM_SOURCE=tap|blackhole to pin the system source.
"""

import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio_recorder  # noqa: E402

require_tracks = []
seconds = 3
args = sys.argv[1:]
while args:
    arg = args.pop(0)
    if arg == "--require-track" and args:
        require_tracks.append(args.pop(0))
    elif arg == "--seconds" and args:
        seconds = int(args.pop(0))
    else:
        sys.exit(f"usage: {sys.argv[0]} [--require-track KEY]... [--seconds N]")

print("backend:", audio_recorder.BACKEND)
rec = audio_recorder.MeetingRecorder()
out = Path(tempfile.mkdtemp(prefix="msc-rectest-"))
info = rec.start(out, "test")
print("devices:", {k: v["device"] for k, v in info["tracks"].items()})
print("warnings:", info["warnings"])
time.sleep(seconds)
print("status:", rec.status()["tracks"])
result = rec.stop()
print("stopped:", {k: (v["seconds"], v["rate"]) for k, v in result["tracks"].items()})
if result["warnings"]:
    print("stop warnings:", result["warnings"])

track_ok = {}
for key, tr in result["tracks"].items():
    p = out / tr["file"]
    with wave.open(str(p)) as wf:
        frames, rate, ch = wf.getnframes(), wf.getframerate(), wf.getnchannels()
    ok = frames > 2 * rate and rate == tr["rate"]  # ≥~2s at the reported rate
    track_ok[key] = ok
    print(f"{key}: {frames} frames @ {rate} Hz, {ch}ch -> {'OK' if ok else 'TOO SHORT'}")

passed = result["duration"] > 2
for key in require_tracks:
    if not track_ok.get(key):
        print(f"REQUIRED TRACK '{key}': MISSING OR TOO SHORT")
        passed = False
print("PASS" if passed else "FAIL")
sys.exit(0 if passed else 1)
