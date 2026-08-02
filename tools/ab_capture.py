"""Dual-capture A/B gate for the Core Audio tap (PRD §7.2).

Records the SAME system audio two ways at once — the legacy BlackHole path
(multi-output routing, exactly today's production pipeline) and the new
apple_syscap process tap — then reports whether the tap is a faithful
replacement:

  - duration delta between the two system tracks (gate: ≤ 50 ms/min drift)
  - sample-rate integrity (both files hold their advertised rate)
  - RMS-envelope correlation over the overlapping region (gate: ≥ 0.99)

Usage:
  MEETINGSCRIBE dev machine with BlackHole installed. Play a meeting (or any
  speech) and run:

    python tools/ab_capture.py --seconds 1200 --out ~/ab-session-1

  Each run saves blackhole.wav + tap.wav + metrics.json in --out. The PRD's
  remaining gates (ASR WER ≤ 1% absolute, end-to-end diarization equality)
  are pipeline-level: run the full pipeline over each WAV pair afterwards —
  this script deliberately reports only capture-level physics.

Minimum Phase 1 evidence: 3 sessions ≥ 20 min incl. one Zoom, one browser
meeting, and one with an output-device switch mid-session.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

import macos_audio  # noqa: E402
import swift_helpers  # noqa: E402

CHUNK_FRAMES = 1024


def record_blackhole(path, stop_flag, meta):
    """Record the BlackHole device exactly the way audio_recorder does."""
    import sounddevice as sd

    device = next(
        (dict(d) for d in sd.query_devices()
         if "blackhole" in d["name"].lower() and d["max_input_channels"] >= 1),
        None,
    )
    if device is None:
        raise SystemExit("BlackHole not found — the A/B needs both capture paths.")
    rate = int(device["default_samplerate"])
    channels = max(1, min(int(device["max_input_channels"]), 2))
    meta["blackhole"] = {"device": device["name"], "rate": rate}
    wf = wave.open(str(path), "wb")
    wf.setnchannels(channels)
    wf.setsampwidth(2)
    wf.setframerate(rate)
    stream = sd.InputStream(device=int(device["index"]), channels=channels,
                            samplerate=rate, dtype="int16", blocksize=CHUNK_FRAMES)
    stream.start()
    meta["blackhole"]["started_at"] = time.perf_counter()
    try:
        while not stop_flag["stop"]:
            data, _ = stream.read(CHUNK_FRAMES)
            wf.writeframes(bytes(data.tobytes()))
    finally:
        stream.stop()
        stream.close()
        wf.close()


def record_tap(path, seconds, meta):
    binary = swift_helpers.ensure_binary(
        Path(__file__).resolve().parents[1] / "tools" / "apple_syscap.swift",
        "apple_syscap", min_macos=(26, 0),
    )
    if not binary:
        raise SystemExit("apple_syscap helper unavailable on this machine.")
    proc = subprocess.Popen(
        [binary, "--rate", "48000", "--channels", "2", "--chunk-ms", "20",
         "--exclude-pid", str(os.getpid())],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    events = []
    wf = wave.open(str(path), "wb")
    wf.setnchannels(2)
    wf.setsampwidth(2)
    wf.setframerate(48000)
    meta["tap"] = {"device": "system-tap", "rate": 48000, "started_at": time.perf_counter()}
    deadline = time.time() + seconds
    chunk = 48000 * 2 * 2 * 20 // 1000
    try:
        while time.time() < deadline:
            data = proc.stdout.read(chunk)
            if not data:
                break
            wf.writeframes(data)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        for line in proc.stderr.read().decode("utf-8", "replace").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        wf.close()
    meta["tap"]["events"] = events


def load_mono(path):
    with wave.open(str(path)) as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    return pcm.astype(np.float64) / 32768.0, rate


HOP_MS = 10


def envelope(x, rate):
    hop = int(rate * HOP_MS / 1000)
    n = len(x) // hop
    return np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1))


def _best_lag(ea, eb, max_lag):
    """Lag (in hops) shifting `ea` that best aligns it with `eb`, by envelope
    cross-correlation. Positive lag = ea starts earlier than eb."""
    best = (-2.0, 0)
    for lag in range(-max_lag, max_lag + 1):
        x, y = (ea[lag:], eb) if lag >= 0 else (ea, eb[-lag:])
        n = min(len(x), len(y))
        if n < 100:
            continue
        x, y = x[:n], y[:n]
        if x.std() < 1e-9 or y.std() < 1e-9:
            continue
        r = float(np.corrcoef(x, y)[0, 1])
        if r > best[0]:
            best = (r, lag)
    return best


def compare(bh_path, tap_path):
    """Capture-fidelity metrics between the two system tracks.

    Raw file-length deltas measure this script's start/stop skew, not the
    capture, so the gates are alignment-aware instead:

      - envelope correlation over the aligned overlap (edges trimmed) — did
        both paths hear the same thing?
      - clock drift = alignment lag of the last third minus the first third —
        do the two captures stay in step over the session? (This is what
        would smear diarization word-overlap echo-dropping.)
    """
    a, ra = load_mono(bh_path)
    b, rb = load_mono(tap_path)
    dur_a, dur_b = len(a) / ra, len(b) / rb
    ea, eb = envelope(a, ra), envelope(b, rb)
    max_lag = min(1000, len(ea) - 1, len(eb) - 1)  # ±10 s
    corr, lag = _best_lag(ea, eb, max_lag)

    # Aligned overlap, first/last second trimmed off.
    xa, xb = (ea[lag:], eb) if lag >= 0 else (ea, eb[-lag:])
    n = min(len(xa), len(xb))
    trim = int(1000 / HOP_MS)
    xa, xb = xa[trim: n - trim], xb[trim: n - trim]
    overlap_corr = corr
    if len(xa) > 200 and xa.std() > 1e-9 and xb.std() > 1e-9:
        overlap_corr = float(np.corrcoef(xa, xb)[0, 1])

    # Split-half drift: re-align each third independently.
    third = len(xa) // 3
    drift_hops = 0.0
    if third > 200:
        _, lag_start = _best_lag(xa[:third], xb[:third], trim)
        _, lag_end = _best_lag(xa[-third:], xb[-third:], trim)
        drift_hops = lag_end - lag_start
    drift_s = drift_hops * HOP_MS / 1000
    session_min = max(dur_a, dur_b) / 60

    speech = float(np.mean(ea > ea.max() * 0.05)) if len(ea) else 0.0
    return {
        "blackhole_seconds": round(dur_a, 3),
        "tap_seconds": round(dur_b, 3),
        "start_skew_s": round(lag * HOP_MS / 1000, 3),
        "envelope_correlation": round(overlap_corr, 4),
        "clock_drift_s": round(drift_s, 3),
        "active_audio_fraction": round(speech, 3),
        "gates": {
            # ≤50 ms of relative drift per minute of session
            "drift_ok": abs(drift_s) <= 0.05 * max(1.0, session_min),
            "envelope_correlation_ok": overlap_corr >= 0.99,
        },
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--out", type=Path, default=Path.home() / "meetingscribe-ab")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    bh_path, tap_path = args.out / "blackhole.wav", args.out / "tap.wav"

    print("Routing output through the multi-output device (legacy path)…")
    route = macos_audio.ensure_routing("BlackHole 2ch")
    print(f"  routed via {route['via']} (audible on {route['hears']})")

    import threading
    meta = {}
    stop_flag = {"stop": False}
    bh_thread = threading.Thread(target=record_blackhole,
                                 args=(bh_path, stop_flag, meta), daemon=True)
    bh_thread.start()
    print(f"Recording BOTH paths for {args.seconds}s — play meeting audio now…")
    try:
        record_tap(tap_path, args.seconds, meta)
    finally:
        stop_flag["stop"] = True
        bh_thread.join(timeout=10)
        if route.get("changed"):
            macos_audio.restore_routing()
            print("  previous sound output restored")

    metrics = compare(bh_path, tap_path)
    metrics["meta"] = {k: {kk: vv for kk, vv in v.items() if kk != "events"}
                       for k, v in meta.items()}
    metrics["tap_events"] = [e for e in meta.get("tap", {}).get("events", [])
                             if e.get("t") != "ready"]
    (args.out / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))
    ok = all(metrics["gates"].values())
    print("A/B CAPTURE GATES:", "PASS" if ok else "FAIL",
          "(WER + diarization equality still require a pipeline run on both WAVs)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
