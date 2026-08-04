"""Shared paths and user configuration for MeetingScribe."""

import json
import os
from pathlib import Path

# Code lives here (templates, tools, the Python modules). When MeetingScribe
# runs from inside the downloaded .app bundle, this is read-only.
BASE_DIR = Path(__file__).resolve().parent

# User data (recordings, models, config) lives in a writable directory. The
# packaged app sets MEETINGSCRIBE_DATA=~/.meetingscribe so recordings survive
# app updates and the bundle stays read-only; running from a source checkout
# (no env var) keeps everything in the project folder as before.
DATA_DIR = Path(os.environ.get("MEETINGSCRIBE_DATA") or BASE_DIR)
RECORDINGS_DIR = DATA_DIR / "recordings"
MODELS_DIR = DATA_DIR / "models"
CONFIG_PATH = DATA_DIR / "config.json"

# Keep HuggingFace downloads quiet and self-contained on Windows.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# Keep all model downloads (Whisper, speaker embeddings) under DATA_DIR.
os.environ.setdefault("HF_HOME", str(MODELS_DIR / "hf"))

DEFAULTS = {
    # Whisper model: "auto", or tiny / base / small / medium / large-v3 /
    # large-v3-turbo. "auto" picks large-v3-turbo on the Apple-GPU backend
    # (fast) and "small" on CPU. Ignored by the "apple" backend.
    "whisper_model": "auto",
    # "auto" / "apple" / "mlx" / "faster":
    #   apple  - Apple SpeechAnalyzer on the Neural Engine (macOS 26+):
    #            fastest, coolest, fully on-device. The default when available.
    #   mlx    - Whisper on the Apple GPU (Apple Silicon).
    #   faster - Whisper on the CPU (works everywhere).
    "whisper_backend": "auto",
    # Force a language code like "en" or "hi", or null to auto-detect.
    "language": None,
    "compute_type": "int8",
    # Cosine-distance cutoff for deciding two voices are different people.
    # Lower = more likely to split voices apart, higher = more likely to merge.
    "diarization_threshold": 0.6,
    # Speaker attribution engine. "auto" (default): the classic ECAPA cascade
    # decides HOW MANY voices (21/21 on the real corpus), and the neural
    # engine (pyannote community-1 on the Neural Engine, ~22 MB of local
    # CoreML models) re-derives WHO SPEAKS WHEN at that count — frame-level
    # turns instead of 2-second window votes. "classic": never run the
    # neural pass (the pre-2026-08 behaviour, and the automatic fallback
    # whenever the neural engine is missing or fails).
    "diarization_engine": "auto",
    # macOS: switch the sound output to a Multi-Output Device (speakers +
    # BlackHole) while recording, and switch back afterwards.
    "auto_route_macos": True,
    # Summary + Ask engine. "claude" (default), "codex", "gemini" or
    # "copilot" run on the user's own AI CLI of that name (best quality;
    # transcript TEXT is sent to that provider's cloud, never audio — the
    # registry lives in ai_cli.py). "apple" keeps everything on-device via
    # Apple Intelligence (shallower, but 100% offline). When the chosen CLI
    # is missing or signed out, summaries fall back to Apple Intelligence
    # automatically; Ask reports what to install instead, because an
    # interactive answer that silently switched models is harder to trust.
    "summary_engine": "claude",
    # Live captions while recording (macOS 26+, on-device SpeechAnalyzer).
    "live_captions": True,
    # Voice profiles: renaming a speaker saves their voice locally, so later
    # meetings label that person by name automatically. Fully on-device
    # (voice_profiles.json under DATA_DIR); recognition never overwrites a
    # name the user typed, only the "Speaker N" defaults.
    "voice_profiles": True,
    # Max cosine distance between a meeting's voice and a saved profile to
    # accept the match. Lower = stricter (fewer, surer auto-names). Measured
    # against this machine's corpus, not chosen: voice_profiles.RECOGNIZE_MAX_
    # DIST carries the measurement and must hold the same number.
    "voice_profile_threshold": 0.45,
    # Read real names off the conversation ("Hi, I'm Marcus", "Thanks, Priya")
    # and put them on speaker labels that would otherwise say "Speaker N".
    # Runs on the engine chosen above, only on formulaic labels, never over a
    # name the user typed, and only where an anchored quote from the
    # transcript proves it.
    #
    # OFF until three things measured on the 41 meetings here are decided,
    # because each of them is a product call and none is an engineering one:
    #   1. It is not deterministic. Four passes over the same corpus produced
    #      9, 9, 12 and 11 names. A name that appears, then vanishes when the
    #      user hits Reprocess, is worse than a label that never claimed to
    #      know. The engine runs at temperature 0.2 and nothing in the gate
    #      log explains the variance.
    #   2. It spells the name the way the recognizer heard it. Of 11 names on
    #      this corpus none was the wrong PERSON, but 4 were a mangled
    #      spelling of the right one ("Katty" for Katy, "Sharik Khatri" for
    #      Sharique Khatri). Showing someone their own name misspelled may be
    #      worse than "Speaker 1".
    #   3. It costs 15 to 93 seconds of processing on meetings that reach the
    #      model, on every recording.
    # Nothing shows the evidence yet either: meta["speaker_names"] carries the
    # anchored quote behind every name, and no UI reads it.
    "speaker_names": False,
    # Extra words/names the speech recognizer should be biased toward, e.g.
    # ["Kubernetes", "Priya", "InsForge"]. Calendar attendee names are added
    # automatically per meeting.
    "vocabulary": [],
    "port": 5005,
    "open_browser": True,
}


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            pass
    return cfg


for _d in (DATA_DIR, RECORDINGS_DIR, MODELS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
