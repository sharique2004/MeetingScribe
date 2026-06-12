"""Shared paths and user configuration for MeetingScribe."""

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
RECORDINGS_DIR = BASE_DIR / "recordings"
MODELS_DIR = BASE_DIR / "models"
CONFIG_PATH = BASE_DIR / "config.json"

# Keep HuggingFace downloads quiet and self-contained on Windows.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# Keep all model downloads (Whisper, MLX, speaker embeddings) in models/.
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
    # macOS: switch the sound output to a Multi-Output Device (speakers +
    # BlackHole) while recording, and switch back afterwards.
    "auto_route_macos": True,
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


for _d in (RECORDINGS_DIR, MODELS_DIR):
    _d.mkdir(exist_ok=True)
