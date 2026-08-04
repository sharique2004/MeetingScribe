"""Neural speaker turns — pyannote community-1 on the Neural Engine.

This module runs MeetingScribe's second diarization engine: FluidAudio's
OFFLINE CoreML port of pyannote community-1 (powerset segmentation +
WeSpeaker embeddings + PLDA/VBx clustering), wrapped in the
`fluid-diarizer` Swift helper (native/fluiddiarizer). Fully on-device:
audio never leaves the Mac; the ~22 MB of CoreML models download once from
HuggingFace into MODELS_DIR and are ordinary files the user can delete.

DIVISION OF LABOUR, decided by measurement (native/diarization-ab, offline
re-test 2026-08-03, results/fluidaudio-offline-t*.json):

  * The classic engine (diarization.py: ECAPA windows + GEN3 fold cascade)
    KEEPS deciding HOW MANY voices a track holds. On the 21 truth-backed
    real fixtures it scores 21/21; the neural pipeline's own auto count
    peaked at 19/21 (threshold 0.5) and merged Room T's two people at every
    threshold. Counts are where the shipped cascade has receipts.
  * THIS engine, forced to that count, decides WHO SPEAKS WHEN. Powerset
    segmentation reads the raw waveform at 16 ms hops, handles overlap, and
    VBx assigns identity per frame — against the classic path's 2-second
    windows snapped to ASR segment spans. On the golden Demo fixture the
    two engines agree on 95% of windows and the disagreements are turn
    boundaries, which is exactly where frame-level segmentation is the
    better witness.

The neural pass is refinement, not a dependency: every failure — missing
binary, missing models, no network on first run, timeout, count mismatch —
falls back to the classic assignment that shipped yesterday, with a log
line and nothing else. config "diarization_engine" picks: "auto" (the
hybrid above, default), "classic" (never run the neural pass).
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import swift_helpers
from config import BASE_DIR, MODELS_DIR

log = logging.getLogger("meetingscribe.diarization_neural")

# Stamped into analysis.npz beside cached turns. BUMP whenever the pinned
# FluidAudio revision, the models, or the CLI's config presets change — a
# cached turn set from an older engine must re-derive, never silently mix.
NEURAL_VERSION = 1

# The vendor-recommended clustering threshold for meeting audio; with the
# speaker count pinned by the classic engine it only steers the AHC warm
# start, but 0.7 is also what the A/B swept as the best auto point.
THRESHOLD = 0.7

_PACKAGE_DIR = BASE_DIR / "native" / "fluiddiarizer"
_BINARY_NAME = "fluid-diarizer"
MODELS_SUBDIR = MODELS_DIR / "fluid-diarization"

_RESOLVED = None
_RESOLVE_LOCK = threading.Lock()


def _build_from_source():
    """Developer mode: `swift build -c release` the package and cache the
    product in ~/.meetingscribe/bin. Returns the path or None. The build is
    attempted once per process — SPM failures are slow to repeat."""
    if not (_PACKAGE_DIR / "Package.swift").exists():
        return None
    if shutil.which("swift") is None:
        return None
    out = swift_helpers.BIN_DIR / _BINARY_NAME
    built = _PACKAGE_DIR / ".build" / "release" / _BINARY_NAME
    try:
        if not built.exists():
            log.info("building %s from %s…", _BINARY_NAME, _PACKAGE_DIR)
            subprocess.run(["swift", "build", "-c", "release"],
                           cwd=str(_PACKAGE_DIR), check=True,
                           capture_output=True, text=True, timeout=1200)
        if not out.exists() or out.stat().st_mtime < built.stat().st_mtime:
            # Copy-then-rename so a concurrent reader can only ever exec the
            # old binary or the complete new one, never a half-written file.
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(f".tmp-{os.getpid()}")
            shutil.copy2(built, tmp)
            os.chmod(tmp, 0o755)
            os.replace(tmp, out)
        return str(out)
    except (subprocess.SubprocessError, OSError) as exc:
        detail = getattr(exc, "stderr", "") or exc
        log.warning("could not build %s: %s", _BINARY_NAME, detail)
        return None


def binary():
    """Path to fluid-diarizer, or None. Prebuilt (packaged app) first, then
    the ~/.meetingscribe/bin cache, then compile-from-source (dev checkout).
    Resolution is cached for the process — this sits on the reprocess path —
    and serialized, because two meetings can process concurrently and a
    double `swift build` is both slow and a write race."""
    global _RESOLVED
    if _RESOLVED is not None:
        return _RESOLVED or None
    with _RESOLVE_LOCK:
        if _RESOLVED is not None:
            return _RESOLVED or None
        if sys.platform != "darwin":
            _RESOLVED = False
            return None
        found = swift_helpers.find_binary(_BINARY_NAME) or _build_from_source()
        _RESOLVED = found or False
        return found


def available():
    return binary() is not None


def diarize_turns(wav_path, num_speakers=None, threshold=THRESHOLD,
                  offset=0.0, timeout=None):
    """Run the neural engine over one track WAV.

    Returns a list of (start, end, speaker_idx) turns on the MEETING
    timeline (`offset` — the track's start_offset — is added to the
    file-local times the engine reports), speaker_idx renumbered by first
    appearance. Raises on any failure; callers fall back to classic.
    """
    exe = binary()
    if exe is None:
        raise RuntimeError("fluid-diarizer unavailable")
    wav_path = Path(wav_path)
    if timeout is None:
        # Inference measures 150-410x realtime on this hardware, so
        # audio_s/10 alone is a ~15-40x margin. The 600 s floor exists for
        # the FIRST run, which also downloads ~22 MB of models and compiles
        # them for the ANE.
        try:
            import soundfile as sf

            audio_s = float(sf.info(str(wav_path)).duration)
        except Exception:
            audio_s = 3600.0
        timeout = max(600.0, audio_s / 10.0)

    MODELS_SUBDIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".json", prefix="ms-turns-",
                                     delete=False) as tmp:
        out_path = Path(tmp.name)
    try:
        cmd = [exe, str(wav_path), "--out", str(out_path),
               "--threshold", str(threshold),
               "--models-dir", str(MODELS_SUBDIR)]
        if num_speakers:
            cmd += ["--num-speakers", str(int(num_speakers))]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise RuntimeError(tail[-1] if tail else f"exit {proc.returncode}")
        data = json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        out_path.unlink(missing_ok=True)

    turns = [(float(s["start"]) + float(offset),
              float(s["end"]) + float(offset),
              int(s["speaker_idx"]))
             for s in data.get("segments", [])]
    turns.sort(key=lambda t: t[0])
    return turns


# A neural cluster carrying less total speech than this is chunk-boundary
# confetti, not a voice. Measured on the offline sweep (OFFLINE_RETEST.md):
# every phantom cluster on the real corpus carries 1.7–1.9 s; the smallest
# genuine cluster anywhere is Demo's 17.7 s (and the known backchannel-only
# real speaker in the synthetic corpus carries ~12.8 s). 5.0 sits between
# the bands with >2x margin on both sides.
CONFETTI_MIN_S = 5.0


def fold_confetti(turns, min_s=CONFETTI_MIN_S):
    """Fold sub-`min_s` clusters into their temporally nearest real voice,
    then renumber by first appearance. Returns the cleaned turn list."""
    if not turns:
        return []
    total = {}
    for s, e, lab in turns:
        total[lab] = total.get(lab, 0.0) + (e - s)
    real = {lab for lab, sec in total.items() if sec >= min_s}
    if not real:  # a very short recording: keep the biggest voice only
        real = {max(total, key=total.get)}
    kept = [(s, e, lab) for s, e, lab in turns if lab in real]
    out = []
    for s, e, lab in turns:
        if lab not in real:
            mid = (s + e) / 2.0
            lab = min(kept, key=lambda t: min(abs(mid - t[0]), abs(mid - t[1])))[2]
        out.append((s, e, lab))
    mapping = {}
    renumbered = []
    for s, e, lab in out:
        if lab not in mapping:
            mapping[lab] = len(mapping)
        renumbered.append((s, e, mapping[lab]))
    return renumbered
