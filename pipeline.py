"""Processing pipeline: raw meeting WAVs -> speaker-labelled transcript.

Steps:
  1. Transcribe each track with faster-whisper (local, word timestamps).
  2. Label speakers. Online mode: the mic track is "You", voices on the
     system track are clustered into Speaker 1..N. In-person mode: voices on
     the mic track are clustered (everyone shares the room mic), and any
     speech on the system track becomes Remote 1..N.
  3. Drop mic-track segments that are just acoustic echo of the system audio
     (happens when people use speakers instead of headphones).
  4. Merge both tracks on one timeline, group into readable turns, compute
     conversation stats, and save everything into meeting.json.
"""

import difflib
import gc
import json
import logging
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from bisect import bisect_left, bisect_right
from pathlib import Path

import numpy as np

import diarization
import diarization_neural
import speaker_names
import stats as stats_mod
import swift_helpers
import tech_vocabulary
import voice_profiles
from config import (
    BASE_DIR,
    DATA_DIR,
    MODELS_DIR,
    ModelDownloadError,
    ensure_hf_files,
    hf_download_plan,
    load_config,
)

log = logging.getLogger("meetingscribe.pipeline")

# The process umask, read once at import (single-threaded) because reading it
# requires temporarily clearing it. _atomic_write needs it: tempfile.mkstemp
# hardcodes 0600, while the writes it replaces — Path.write_text, open(...,
# "wb") — create 0666 & ~umask. Without this the mode of meeting.json would
# quietly change depending on which writer touched it last.
_UMASK = os.umask(0)
os.umask(_UMASK)


def _atomic_write(path, write_body):
    """Replace `path` in one indivisible step. write_body(tmp_path) fills it in.

    Path.write_text and np.savez_compressed both TRUNCATE THE TARGET IN PLACE:
    for the whole duration of the write the on-disk file is a prefix of the new
    contents, and anything that stops the process there — a crash, or the
    os._exit that /api/shutdown performs — freezes it that way. meeting.json is
    the only copy of a meeting's transcript, so a truncated one is lost work;
    analysis.npz truncated mid-archive is an unreadable zip.

    Writing to a fresh file in the SAME directory and then os.replace()-ing it
    over the target removes that window entirely. os.replace is atomic within a
    filesystem: a concurrent reader sees either the entire old file or the
    entire new one, and a crash before the replace leaves the original wholly
    intact (only the temp file is orphaned, and it is cleaned up here).

    The temp name is unique per call (mkstemp), so two writers — pipeline
    thread, Flask request, a second process — can never share one. Note this is
    deliberately NOT app.py's fixed "meeting.json.tmp" name; both are atomic for
    the reader, and distinct names mean the two writers cannot destroy each
    other's temp file even though they take no common lock.
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                    prefix=path.name + ".", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        # The replacement inherits the temp file's permissions, so give it the
        # ones the target would have had: whatever it has now, or — creating it
        # for the first time — what a plain write would have produced.
        try:
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        except OSError:
            os.chmod(tmp, 0o666 & ~_UMASK)
        write_body(tmp)
        # Flush the contents to disk before publishing the name, so a power cut
        # just after the replace cannot leave the new name pointing at unwritten
        # blocks. Best effort: a filesystem that refuses fsync must not cost us
        # the write itself.
        try:
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _atomic_write_text(path, text):
    _atomic_write(path, lambda tmp: tmp.write_text(text, encoding="utf-8"))


_WHISPER = None
_WHISPER_KEY = None
_PARAKEET = None
_PARAKEET_REPO = None
# Guards the Parakeet singleton AND its use: process_meeting runs on a thread
# per meeting, and two meetings may transcribe at once (record-stop on one,
# Reprocess on another). See _get_parakeet. _transcribe_mlx and the idle
# reaper below take the same lock: everything that touches the Metal device
# or the singletons serializes here.
_PARAKEET_LOCK = threading.Lock()

# ------------------------------------------------- releasing ASR memory ----
#
# The singletons above made the engine's resident size a high-water mark: it
# starts at ~52 MB and sat at ~730 MB forever after the first meeting. The
# model WEIGHTS are not the problem — Parakeet's 2.4 GB safetensors are
# file-backed clean pages the OS reclaims for free. What never came back was
# MLX's buffer cache (~470 MB of decode scratch, measured on a 120 s decode:
# active 1270 MB / peak 2886 MB, all but ~180 MB of RSS returned by
# clear_cache) and, separately, the ECAPA embedder (see tools/embed_worker.py
# — onnxruntime's CPU arena gives nothing back on unload, exactly as torch did
# before it, so that one gets a process that exits instead).
#
# Two mechanisms, both serialized on _PARAKEET_LOCK:
#   * _release_mlx() after every decode — returns the buffer cache while the
#     weights stay warm (a second meeting re-decodes 2.0 s slower, measured).
#   * an idle timer that additionally drops the model singletons _ASR_IDLE_S
#     after the last transcription, so a machine that recorded one meeting
#     today is not still holding decode state tonight. _get_parakeet /
#     _get_whisper already reload on None; the reaper costs the NEXT meeting
#     only the model reload it already paid on its first.
_ASR_IDLE_S = 600.0
_ASR_REAPER = None
_ASR_REAPER_LOCK = threading.Lock()


def _release_mlx():
    """Return MLX's buffer cache to the OS. Safe at any point: it only frees
    buffers no live array references. No-op unless MLX is already loaded —
    a CPU-only install must not initialize Metal just to clean up after it."""
    mx = sys.modules.get("mlx.core")
    if mx is None:
        return
    try:
        log.info("MLX memory: active %d MB, cache %d MB, peak %d MB",
                 mx.get_active_memory() >> 20, mx.get_cache_memory() >> 20,
                 mx.get_peak_memory() >> 20)
        mx.clear_cache()
    except AttributeError:  # pre-0.21 mlx kept these under mx.metal
        mx.metal.clear_cache()


def _reap_asr():
    global _PARAKEET, _PARAKEET_REPO, _WHISPER, _WHISPER_KEY
    with _PARAKEET_LOCK:
        if _PARAKEET is None and _WHISPER is None:
            return
        _PARAKEET = None
        _PARAKEET_REPO = None
        _WHISPER = None
        _WHISPER_KEY = None
        gc.collect()
        _release_mlx()
    log.info("ASR models released after %.0f s idle", _ASR_IDLE_S)


def _arm_asr_reaper():
    """(Re)start the idle countdown. Called at the end of every
    transcribe_track, so the timer measures quiet time since the LAST track,
    and back-to-back meetings never pay a reload."""
    global _ASR_REAPER
    with _ASR_REAPER_LOCK:
        if _ASR_REAPER is not None:
            _ASR_REAPER.cancel()
        _ASR_REAPER = threading.Timer(_ASR_IDLE_S, _reap_asr)
        _ASR_REAPER.daemon = True
        _ASR_REAPER.start()


TURN_MERGE_GAP_S = 3.0

# Model label shown in the UI when whisper_model is "auto". Parakeet runs on
# the Apple GPU via MLX and is the accuracy pick for meeting audio (NVIDIA
# parakeet-tdt-0.6b: the strongest meeting-domain WER of anything that runs
# on this hardware, near-zero silence hallucination, native punctuation and
# word timestamps). Apple Speech runs on the Neural Engine (fastest,
# coolest); MLX whisper uses the GPU; faster-whisper the CPU.
AUTO_MODEL = {
    "parakeet": "parakeet-tdt-0.6b",
    "apple": "apple-speech",
    "mlx": "large-v3-turbo",
    "faster": "small",
}

MLX_REPOS = {
    "tiny": "mlx-community/whisper-tiny",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}

# Parakeet TDT 0.6B — v2 is English-only and the best English of the pair;
# v3 trades a sliver of English accuracy for 25 European languages. Neither
# covers hi/ja/ko/zh/ar, so those languages fall through to Apple/Whisper in
# transcribe_track's ladder.
PARAKEET_REPO_EN = "mlx-community/parakeet-tdt-0.6b-v2"
PARAKEET_REPO_MULTI = "mlx-community/parakeet-tdt-0.6b-v3"
PARAKEET_V3_LANGS = {
    "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de", "el", "hu",
    "it", "lv", "lt", "mt", "pl", "pt", "ro", "sk", "sl", "es", "sv", "ru",
    "uk",
}
# Long meetings are transcribed in overlapping chunks (the model's full
# attention window is ~24 min but memory grows with length; 2-minute chunks
# with the library's default 15 s overlap keep peak memory flat and let the
# library's token-merge stitch the seams).
PARAKEET_CHUNK_S = 120.0
PARAKEET_OVERLAP_S = 15.0
# The two files parakeet_mlx.from_pretrained asks the Hub for. model.safetensors
# is 2.4 GB of the 2.4 GB, and on a fresh install it is the single longest thing
# that happens before the user's first transcript exists.
PARAKEET_FILES = ("config.json", "model.safetensors")
PARAKEET_LABEL = "the speech model"

# Apple SpeechAnalyzer helper (macOS 26+). Source ships in tools/; the
# compiled binary is cached outside the synced project folder.
_APPLE_SRC = BASE_DIR / "tools" / "apple_transcribe.swift"
_APPLE_BIN = DATA_DIR / "bin" / "apple_transcribe"
_APPLE_TIMEOUT_S = 1800

# Map a bare language code to a default Apple locale; the helper also resolves
# equivalents, so this only needs the common cases.
_APPLE_LOCALES = {
    "en": "en-US", "hi": "hi-IN", "es": "es-ES", "fr": "fr-FR", "de": "de-DE",
    "it": "it-IT", "pt": "pt-BR", "ja": "ja-JP", "ko": "ko-KR", "zh": "zh-CN",
    "ar": "ar-SA", "ru": "ru-RU", "nl": "nl-NL",
}


def _ensure_apple_binary():
    """Return the path to the compiled Apple helper, building it on demand.

    Returns None if the platform can't support it or compilation fails — the
    caller then falls back to a Whisper backend. SpeechAnalyzer needs
    macOS 26+.
    """
    return swift_helpers.ensure_binary(_APPLE_SRC, "apple_transcribe", min_macos=(26, 0))


def _mlx_available():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return False
    try:
        import mlx_whisper  # noqa: F401

        return True
    except ImportError:
        return False


def _parakeet_lang(cfg):
    """The bare language code Parakeet would transcribe this meeting in, or
    None when the requested language is outside what it covers. Auto-detect
    (no language set) reads as English — the same assumption the Apple tier
    has always made when it defaults the locale to en-US."""
    lang = (cfg.get("language") or "en").strip().lower().split("-")[0]
    return lang if lang in PARAKEET_V3_LANGS else None


def _parakeet_available(cfg):
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return False
    if _parakeet_lang(cfg) is None:
        return False
    try:
        import parakeet_mlx  # noqa: F401

        return True
    except ImportError:
        return False


def pick_backend(cfg):
    """Pick the transcription backend.

    "parakeet" — Parakeet TDT on the Apple GPU (MLX): the most accurate on
                 meeting audio and nearly hallucination-free on silence. The
                 default when available — accuracy is what the transcript is
                 FOR, and the recording is already over when this runs.
    "apple"    — Apple SpeechAnalyzer on the Neural Engine (macOS 26+):
                 fastest and coolest, but tuned for clean dictation; on
                 meeting audio it mishears noticeably more.
    "mlx"      — Whisper large-v3-turbo on the Apple GPU: the multilingual
                 catch-all, and the one tier that honours the vocabulary via
                 initial_prompt.
    "faster"   — Whisper on the CPU (faster-whisper), the portable fallback.

    Config "whisper_backend" can force any of these.
    """
    backend = cfg.get("whisper_backend", "auto")
    if backend in ("parakeet", "apple", "mlx", "faster"):
        return backend
    # With NO language set, Parakeet reads the meeting as English — the same
    # assumption the Apple tier has always made (locale defaults to en-US).
    # So auto only puts Parakeet ahead of a tier that shared that assumption:
    # on a Mac where Apple Speech isn't available, the old default was
    # Whisper, which genuinely auto-detects, and an unset language must keep
    # that ability rather than silently anglicize a Spanish meeting.
    if _parakeet_available(cfg) and (
            cfg.get("language") or _ensure_apple_binary() is not None):
        return "parakeet"
    if _ensure_apple_binary() is not None:
        return "apple"
    if _mlx_available():
        return "mlx"
    return "faster"


def resolve_model(cfg, backend):
    # Apple Speech and Parakeet have no user-selectable model variants, so
    # whisper_model does not apply to them. The Whisper backends honour an
    # explicit model, else "auto".
    if backend == "apple":
        return AUTO_MODEL["apple"]
    if backend == "parakeet":
        lang = _parakeet_lang({"language": cfg.get("language")})
        repo = PARAKEET_REPO_EN if lang in (None, "en") else PARAKEET_REPO_MULTI
        return repo.rsplit("/", 1)[1]
    model = cfg.get("whisper_model") or "auto"
    return AUTO_MODEL.get(backend, "small") if model == "auto" else model


def _get_whisper(cfg):
    global _WHISPER, _WHISPER_KEY
    model_name = resolve_model(cfg, "faster")
    key = (model_name, cfg["compute_type"])
    if _WHISPER is None or _WHISPER_KEY != key:
        from faster_whisper import WhisperModel

        _WHISPER = WhisperModel(
            model_name,
            device="cpu",
            compute_type=cfg["compute_type"],
            download_root=str(MODELS_DIR / "whisper"),
        )
        _WHISPER_KEY = key
    return _WHISPER


def _track_duration(path):
    import soundfile as sf

    try:
        return float(sf.info(str(path)).duration)
    except Exception:
        return 0.0


# ------------------------------------------------------- reading a track ----
#
# WHY THIS IS NOT diarization.load_mono_16k.
#
# That function is three whole-track arrays alive at once. Measured on a real
# 3-hour recording in the recorder's own format (48 kHz stereo 16-bit, what
# audio_recorder.py writes), peak physical footprint of ONE call:
#
#     sf.read(dtype=float32, always_2d=True)   4.15 GB   -> 4.23 GB peak
#     data.mean(axis=1)                        2.07 GB   -> 6.30 GB peak
#     resample_poly(...)                       0.69 GB   -> 6.99 GB peak
#
# and the track is read TWICE per meeting (once to transcribe, once to embed
# voices), so a two-track meeting paid that four times over.
#
# Nothing downstream needs the stereo file or the intermediate mono copy: they
# exist only because the conversion is written as three whole-array steps. This
# reads the file a block at a time and converts each block into its final place
# in ONE preallocated output array, so the peak is the output plus one block —
# measured 0.87 GB for the same 3-hour track. The output is BITWISE identical
# to the whole-file conversion, verified sample for sample on that track and on
# 8 kHz / 16 kHz / 22.05 kHz / 44.1 kHz / 48 kHz / 96 kHz mono and stereo
# fixtures; it is a cheaper way to compute the same numbers, not an
# approximation of them.
#
# The resampler is an FIR filter, so a block converted on its own would ring at
# both ends. Each block is therefore read with _LOAD_PAD_FRAMES of real audio on
# either side and the padding discarded after conversion — far more context than
# the filter's support (10 * max(up, down) taps at the up-sampled rate, i.e. 30
# input samples for 48 kHz -> 16 kHz), which is what makes the result identical
# to converting the whole track in one go rather than merely close to it.
_LOAD_BLOCK_FRAMES = 1 << 22   # ~4.2M input frames, ~87 s at 48 kHz
_LOAD_PAD_FRAMES = 1 << 14     # overlap carried into each block and thrown away


def load_mono_16k(path):
    """Any WAV as float32 mono at 16 kHz, without ever holding it three times.

    Same result as diarization.load_mono_16k, a fraction of the memory. See the
    note above; use this everywhere in the pipeline.
    """
    import soundfile as sf
    from scipy.signal import resample_poly

    with sf.SoundFile(str(path)) as snd:
        sr, frames, channels = int(snd.samplerate), int(snd.frames), int(snd.channels)
        if frames <= 0:
            return np.zeros(0, dtype=np.float32)

        def mono(block):
            return block.mean(axis=1) if channels > 1 else block[:, 0]

        if sr == diarization.EMBED_SR:
            out = np.empty(frames, dtype=np.float32)
            done = 0
            while done < frames:
                block = snd.read(min(_LOAD_BLOCK_FRAMES, frames - done),
                                 dtype="float32", always_2d=True)
                if not len(block):
                    break
                out[done:done + len(block)] = mono(block)
                done += len(block)
            return out[:done]

        g = math.gcd(sr, diarization.EMBED_SR)
        up, down = diarization.EMBED_SR // g, sr // g
        # resample_poly returns exactly ceil(n * up / down) samples.
        total_out = -(-frames * up // down)
        out = np.empty(total_out, dtype=np.float32)
        # Whole numbers of `down` keep every block boundary on an exact output
        # sample, so the pieces butt together with no rounding drift.
        step = max(down, (_LOAD_BLOCK_FRAMES // down) * down)
        pad = max(down, (_LOAD_PAD_FRAMES // down) * down)
        pos = written = 0
        while pos < frames:
            take = min(step, frames - pos)
            left = min(pad, pos)
            right = min(pad, frames - pos - take)
            snd.seek(pos - left)
            block = snd.read(left + take + right, dtype="float32", always_2d=True)
            # A WAV whose header promises more frames than the file holds (a
            # recording the machine died in the middle of) reads short. The
            # whole-file version simply returned what was there, so this does
            # too — a corrupt track costs its tail, never an exception on a
            # path whose whole job is to salvage the meeting.
            short = len(block) < left + take + right
            piece = resample_poly(mono(block), up, down)
            del block
            lo = left * up // down
            # The tail owns whatever ceil() rounded up to; every other block
            # owns exactly its own samples.
            want = (total_out - written if pos + take >= frames
                    else take * up // down)
            want = min(want, max(0, len(piece) - lo))
            out[written:written + want] = piece[lo:lo + want]
            written += want
            pos += take
            if short:
                break
        return out[:written]


# Segments whose underlying audio is quieter than this (peak, full-scale
# float) are Whisper hallucinations — silence famously transcribes as
# "Thank you." The faster-whisper backend avoids these with its VAD filter;
# the MLX backend has none, so we check the audio ourselves.
SILENCE_PEAK = 0.004  # ≈ -48 dBFS


def _is_hallucination(seg, audio, sr=16000):
    if seg.get("no_speech_prob", 0.0) > 0.85:
        return True
    i0 = max(0, int(float(seg["start"]) * sr))
    i1 = min(len(audio), int(float(seg["end"]) * sr))
    if i1 <= i0:
        return True
    return float(np.abs(audio[i0:i1]).max()) < SILENCE_PEAK


def _vocab_prompt(cfg):
    """The vocabulary as a comma-joined Whisper initial_prompt / hotwords
    string, truncated at a comma boundary well inside Whisper's 224-token
    prompt window. None when there is nothing to bias toward."""
    strings = [str(s).strip() for s in (cfg.get("_context_strings") or [])
               if str(s).strip()]
    if not strings:
        return None
    joined = ", ".join(strings)
    if len(joined) > 700:
        joined = joined[:700].rsplit(",", 1)[0]
    return joined


def _is_vocab_echo(text, prompt):
    """Whisper sometimes transcribes its own prompt over near-silence. A
    segment whose words are nearly all vocabulary words, in bulk, is that —
    real speech about one product name never reads as a term list."""
    if not prompt:
        return False
    vocab = {w.lower() for w in re.findall(r"\w+", prompt)}
    words = [w.lower() for w in re.findall(r"\w+", text)]
    if len(words) < 4:
        return False
    hits = sum(1 for w in words if w in vocab)
    return hits / len(words) >= 0.9


def _parakeet_where():
    """Where parakeet_mlx keeps its checkpoints, as hf_hub_download kwargs.
    from_pretrained passes this same cache_dir straight to hf_hub_download, so
    a prefetch aimed here is exactly what it will find already downloaded."""
    return {"cache_dir": str(MODELS_DIR / "parakeet")}


def asr_download_size(cfg):
    """(bytes still to download, bytes in total) for the Parakeet checkpoint
    this config would use, or (None, None) when Parakeet is not the engine or
    the Hub is unreachable. (0, total) once it is on disk."""
    if not _parakeet_available(cfg):
        return None, None
    lang = _parakeet_lang(cfg)
    repo = PARAKEET_REPO_EN if lang in (None, "en") else PARAKEET_REPO_MULTI
    return hf_download_plan(repo, PARAKEET_FILES, **_parakeet_where())


def download_asr_model(cfg, progress_cb=None):
    """Fetch the Parakeet checkpoint NOW, into the folder _get_parakeet reads.

    For a caller that wants the 2.4 GB to arrive somewhere the user expects a
    wait (onboarding) instead of in the middle of their first meeting. Returns
    the bytes downloaded, raises ModelDownloadError, and is idempotent and cheap
    once the model is on disk. Returns 0 without fetching anything when this
    machine would not use Parakeet at all, because pre-downloading a checkpoint
    that will never be loaded is 2.4 GB of the user's bandwidth for nothing.

    progress_cb(done_bytes, total_bytes, label): the numeric form, for a caller
    drawing its own bar rather than printing the pipeline's one-line strings.
    """
    if not _parakeet_available(cfg):
        return 0
    lang = _parakeet_lang(cfg)
    repo = PARAKEET_REPO_EN if lang in (None, "en") else PARAKEET_REPO_MULTI
    return ensure_hf_files(repo, PARAKEET_FILES, label=PARAKEET_LABEL,
                           bytes_cb=progress_cb, **_parakeet_where())


def model_download_sizes(cfg=None):
    """What a first run still has to fetch, per model:
    [{"name", "label", "pending", "total"}], sizes in bytes (None = unknown).

    Exists so the caller can quote a real total ("2.5 GB to download") instead
    of the static "First launch sets up its environment once" that a fresh
    install stares at for ten minutes. Read-only: it costs one HEAD per file
    and downloads nothing.
    """
    cfg = dict(load_config()) if cfg is None else cfg
    out = []
    pending, total = asr_download_size(cfg)
    if total or pending:
        out.append({"name": resolve_model(cfg, "parakeet"), "label": PARAKEET_LABEL,
                    "pending": pending, "total": total})
    pending, total = diarization.embedder_download_size()
    if total or pending:
        out.append({"name": "spkrec-ecapa-voxceleb",
                    "label": diarization.ECAPA_LABEL,
                    "pending": pending, "total": total})
    return out


def _get_parakeet(repo, progress_cb=None):
    """The Parakeet model singleton. Callers MUST hold _PARAKEET_LOCK for
    their whole use of the returned model, not just this call: two meetings
    can process concurrently (each on its own request thread), the swap
    between the en/multilingual checkpoints is a torn-pair hazard, and
    parakeet-mlx documents nothing about concurrent generate() on one
    model. Serializing GPU transcription is also simply faster than two
    decodes thrashing one GPU."""
    global _PARAKEET, _PARAKEET_REPO
    if _PARAKEET is None or _PARAKEET_REPO != repo:
        from parakeet_mlx import from_pretrained

        # Fetch the checkpoint ourselves so the 2.4 GB is visible and bounded.
        # from_pretrained reports nothing while it downloads, and it wraps both
        # of its hf_hub_download calls in a bare `except Exception` that then
        # re-reads `repo` as a local directory — so a network failure there
        # surfaces as a baffling FileNotFoundError about a path nobody named,
        # instead of "your connection dropped, try again".
        ensure_hf_files(repo, PARAKEET_FILES, label=PARAKEET_LABEL,
                        progress_cb=progress_cb, **_parakeet_where())
        _PARAKEET = from_pretrained(repo, **_parakeet_where())
        _PARAKEET_REPO = repo
    return _PARAKEET


def _parakeet_words(sentence):
    """AlignedTokens -> the {"w","s","e"} word list every tier returns.
    Tokens are subword pieces; a piece starting with a space starts a new
    word (matching Whisper's leading-space word convention downstream)."""
    words = []
    for tok in sentence.tokens:
        text = tok.text
        if not text:
            continue
        if words and not text.startswith(" "):
            words[-1]["w"] += text
            words[-1]["e"] = float(tok.end)
        else:
            words.append({"w": text, "s": float(tok.start), "e": float(tok.end)})
    return words


def _transcribe_parakeet(path, label, cfg, progress_cb):
    """Parakeet TDT on the Apple GPU via MLX — the accuracy tier for meeting
    audio. Transducer decoding barely hallucinates on silence and its frame
    timestamps are tighter than Whisper's DTW alignment, which is exactly
    what word→speaker attribution wants."""
    import mlx.core as mx
    from parakeet_mlx.alignment import (
        merge_longest_common_subsequence,
        merge_longest_contiguous,
        sentences_to_result,
        tokens_to_sentences,
    )
    from parakeet_mlx.audio import get_logmel

    lang = _parakeet_lang(cfg)
    if lang is None:
        raise RuntimeError("language not covered by Parakeet")
    repo = PARAKEET_REPO_EN if lang == "en" else PARAKEET_REPO_MULTI
    model_name = repo.rsplit("/", 1)[1]
    progress_cb(f"Transcribing {label} on the Apple GPU ({model_name})…")

    # Decode the WAV ourselves (parakeet-mlx's own loader shells out to
    # ffmpeg, which most machines don't have). The model wants 16 kHz mono;
    # load_mono_16k delivers exactly that.
    audio = load_mono_16k(path)

    # One meeting transcribes at a time: the lock covers model load AND
    # generate, because a concurrent Reprocess on another meeting would
    # otherwise race the singleton (or swap the en/multilingual checkpoint
    # under this thread) — see _get_parakeet.
    with _PARAKEET_LOCK:
        model = _get_parakeet(repo, progress_cb)
        sr = model.preprocessor_config.sample_rate
        if sr != diarization.EMBED_SR:  # never true for the shipped models
            raise RuntimeError(f"unexpected Parakeet sample rate {sr}")

        # Mirrors parakeet_mlx.BaseParakeet.transcribe()'s chunked branch over
        # our own decoded audio, reusing the library's overlap token-merge.
        #
        # mx.array COPIES into MLX's own allocator, so converting the whole
        # track up front held it twice (690 MB each for a 3-hour meeting) for
        # the entire decode. The chunked branch only ever reads a 120-second
        # window, so each window is converted as it is used and released with
        # the iteration — the copy is bounded by PARAKEET_CHUNK_S instead of by
        # the length of the meeting. The short branch below is one chunk by
        # definition, so it converts once and is unchanged.
        total = len(audio)
        chunk = int(PARAKEET_CHUNK_S * sr)
        overlap = int(PARAKEET_OVERLAP_S * sr)
        if total <= chunk:
            result = model.generate(
                get_logmel(mx.array(audio), model.preprocessor_config))[0]
        else:
            all_tokens = []
            for start in range(0, total, chunk - overlap):
                end = min(start + chunk, total)
                if end - start < model.preprocessor_config.hop_length:
                    break
                piece = model.generate(
                    get_logmel(mx.array(audio[start:end]),
                               model.preprocessor_config))[0]
                offset = start / sr
                for sent in piece.sentences:
                    for tok in sent.tokens:
                        tok.start += offset
                        tok.end = tok.start + tok.duration
                if all_tokens:
                    try:
                        all_tokens = merge_longest_contiguous(
                            all_tokens, piece.tokens,
                            overlap_duration=PARAKEET_OVERLAP_S)
                    except RuntimeError:
                        all_tokens = merge_longest_common_subsequence(
                            all_tokens, piece.tokens,
                            overlap_duration=PARAKEET_OVERLAP_S)
                else:
                    all_tokens = piece.tokens
                progress_cb(f"Transcribing {label}… {min(99, int(end / total * 100))}%")
            result = sentences_to_result(tokens_to_sentences(all_tokens))
        # result is plain Python (AlignedToken floats) by here; the cache
        # holds only decode scratch. Still under the lock: a concurrent
        # decode must not watch its buffer pool vanish mid-generate.
        _release_mlx()

    out = []
    dropped = 0
    for sent in result.sentences:
        text = sent.text.strip()
        if not text or not re.search(r"\w", text):
            continue
        seg = {"start": float(sent.start), "end": float(sent.end), "text": text,
               "words": _parakeet_words(sent)}
        if _is_hallucination(seg, audio):
            dropped += 1
            continue
        out.append(seg)
    if dropped:
        log.info("%s: dropped %d silent segment(s)", label, dropped)
    return out, lang


def _transcribe_mlx(path, label, cfg, progress_cb):
    """Whisper on the Apple GPU via mlx-whisper — fast and easy on the fans."""
    import mlx_whisper

    model = resolve_model(cfg, "mlx")
    progress_cb(f"Transcribing {label} on the Apple GPU ({model})…")
    # Decode the WAV ourselves (mlx-whisper would otherwise shell out to
    # ffmpeg, which most machines don't have). Whisper wants 16 kHz mono.
    audio = load_mono_16k(path)
    prompt = _vocab_prompt(cfg)
    # Same lock as Parakeet: one GPU decode at a time (two would thrash one
    # Metal device), and the idle reaper must not clear the cache mid-decode.
    with _PARAKEET_LOCK:
        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=MLX_REPOS.get(model, model),
            language=cfg.get("language") or None,
            word_timestamps=True,
            condition_on_previous_text=False,
            hallucination_silence_threshold=2.0,
            # Bias recognition toward attendee names and the user's vocabulary —
            # proper names are the recognizer's biggest error class, and this is
            # the only knob Whisper exposes for them.
            initial_prompt=prompt,
            verbose=None,
        )
        _release_mlx()
    out = []
    dropped = 0
    for seg in result.get("segments", []):
        text = seg["text"].strip()
        if not text or not re.search(r"\w", text):
            continue
        if _is_hallucination(seg, audio) or _is_vocab_echo(text, prompt):
            dropped += 1
            continue
        words = [
            {"w": w["word"], "s": float(w["start"]), "e": float(w["end"])}
            for w in (seg.get("words") or [])
            if w.get("start") is not None
        ]
        out.append(
            {"start": float(seg["start"]), "end": float(seg["end"]), "text": text, "words": words}
        )
    if dropped:
        log.info("%s: dropped %d hallucinated segment(s) on silence", label, dropped)
    return out, result.get("language")


def _apple_locale(cfg):
    lang = (cfg.get("language") or "").strip().lower()
    if not lang:
        return "en-US"
    if "-" in lang:  # already a full locale like "en-gb"
        a, b = lang.split("-", 1)
        return f"{a}-{b.upper()}"
    return _APPLE_LOCALES.get(lang, "en-US")


def apple_locale(language):
    """Public: map a bare language code (or None) to an Apple locale id."""
    return _apple_locale({"language": language})


def _context_file(cfg):
    """Write the contextual-strings file for the Apple helpers, or None.

    Biasing recognition toward attendee names and user vocabulary fixes the
    single biggest error class in meeting transcripts: proper names.
    """
    strings = [s for s in (cfg.get("_context_strings") or []) if str(s).strip()]
    if not strings:
        return None
    import tempfile

    try:
        f = tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="ms-ctx-", delete=False)
        json.dump({"strings": [str(s)[:80] for s in strings[:100]]}, f)
        f.close()
        return f.name
    except OSError:
        return None


def _transcribe_apple(path, label, cfg, progress_cb):
    """Apple SpeechAnalyzer on the Neural Engine — fast, cool, on-device."""
    binary = _ensure_apple_binary()
    if binary is None:
        raise RuntimeError("Apple Speech helper unavailable")
    locale = _apple_locale(cfg)
    progress_cb(f"Transcribing {label} with Apple Speech ({locale})…")
    cmd = [binary, str(path), locale]
    ctx = _context_file(cfg)
    if ctx:
        cmd.append(ctx)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_APPLE_TIMEOUT_S,
        )
    finally:
        if ctx:
            Path(ctx).unlink(missing_ok=True)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"exit {proc.returncode}")
    data = json.loads(proc.stdout)
    out = []
    for seg in data.get("segments", []):
        text = (seg.get("text") or "").strip()
        if not text or not re.search(r"\w", text):
            continue
        words = [
            {"w": w["w"], "s": float(w["s"]), "e": float(w["e"])}
            for w in (seg.get("words") or [])
            if w.get("s") is not None
        ]
        out.append(
            {"start": float(seg["start"]), "end": float(seg["end"]), "text": text, "words": words}
        )
    # Same silence guard the Whisper paths get: recognizers hallucinate
    # phrases over dead air, and dead air is cheap to detect.
    if out:
        audio = None
        try:
            audio = load_mono_16k(path)
            filtered = [s for s in out if not _is_hallucination(s, audio)]
            if len(filtered) < len(out):
                log.info("%s: dropped %d silent segment(s)", label, len(out) - len(filtered))
            out = filtered
        except Exception as exc:  # guard is best-effort
            log.debug("silence guard skipped for %s: %s", label, exc)
        finally:
            # This backend transcribes from the FILE, so the waveform exists
            # only for the guard above. Dropping it here is what keeps it out
            # of the caller's frame for the rest of the meeting.
            del audio
    return out, data.get("language")


def transcribe_track(path, label, cfg, progress_cb):
    """Transcribe one WAV. Returns (segments, language). Tries the configured
    backend, then degrades gracefully
    (parakeet -> apple -> mlx -> faster-whisper)."""
    try:
        return _transcribe_ladder(path, label, cfg, progress_cb)
    finally:
        # Success or not, the models this call warmed start their idle
        # countdown now — see _arm_asr_reaper.
        _arm_asr_reaper()


def _transcribe_ladder(path, label, cfg, progress_cb):
    backend = pick_backend(cfg)
    if backend == "parakeet":
        try:
            return _transcribe_parakeet(path, label, cfg, progress_cb)
        except Exception as exc:
            log.warning("Parakeet failed (%s); trying the next engine", exc)
            progress_cb(f"Parakeet unavailable ({exc}); trying the next engine…")
            if _ensure_apple_binary() is not None:
                backend = "apple"
            else:
                backend = "mlx" if _mlx_available() else "faster"
    if backend == "apple":
        try:
            return _transcribe_apple(path, label, cfg, progress_cb)
        except Exception as exc:
            log.warning("Apple Speech failed (%s); trying Whisper", exc)
            progress_cb(f"Apple Speech unavailable ({exc}); using Whisper…")
            backend = "mlx" if _mlx_available() else "faster"
    if backend == "mlx":
        try:
            return _transcribe_mlx(path, label, cfg, progress_cb)
        except Exception as exc:
            progress_cb(f"GPU transcription failed ({exc}); falling back to CPU…")

    model = _get_whisper(cfg)
    duration = _track_duration(path)
    segments_iter, info = model.transcribe(
        str(path),
        language=cfg.get("language") or None,
        vad_filter=True,
        word_timestamps=True,
        beam_size=5,
        # The other tiers already decode each window independently; without
        # this the CPU tier alone lets one mis-heard phrase seed the next
        # window's decode (classic Whisper repetition spirals).
        condition_on_previous_text=False,
        # Vocabulary biasing without spending the text-prompt window —
        # hotwords apply to every window, not just the first.
        hotwords=_vocab_prompt(cfg),
    )
    out = []
    for seg in segments_iter:
        text = seg.text.strip()
        if not text or not re.search(r"\w", text):
            continue
        words = [
            {"w": w.word, "s": w.start, "e": w.end}
            for w in (seg.words or [])
            if w.start is not None
        ]
        out.append({"start": seg.start, "end": seg.end, "text": text, "words": words})
        if duration > 0:
            pct = min(99, int(seg.end / duration * 100))
            progress_cb(f"Transcribing {label}… {pct}%")
    return out, getattr(info, "language", None)


def _apply_offset(segments, offset):
    if not offset:
        return segments
    for seg in segments:
        seg["start"] += offset
        seg["end"] += offset
        for w in seg.get("words") or []:
            w["s"] += offset
            w["e"] += offset
    return segments


def _norm_text(text):
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


ECHO_SLACK_S = 1.5  # acoustic echo lands on the mic slightly after the system copy

# Containment bands, shared by the word path and the character path below.
ECHO_DROP_RATIO = 0.7  # at or above this, the segment is echo and goes away
ECHO_PARTIAL_RATIO = 0.35  # below this, nothing about the segment looks echoed
ECHO_MIN_TOKENS = 4  # shorter than this is an interjection, not a sentence

# The CHARACTER path needs more evidence than the word path, and both extra
# conditions below are MEASURED rather than chosen. Method for both: replay
# drop_echo over every saved meeting on this machine that has both tracks (17
# of 41), and score each mic segment twice, once against its real far-end
# window and once against a DECOY window taken 600 s away, which cannot be
# echo of it. dist/echo_review_v2.md carries the full tables.
#
# COVER is the physical condition. Echo can only exist while the far end is
# actually producing sound, so a segment the far end was silent through cannot
# be echo however well the letters line up. Measuring the fraction of each mic
# segment's own span that far-end speech occupies splits the 39 segments this
# path deletes cleanly in two: the six that turned out to be the user
# ANSWERING the far end and reusing its words ("Yeah, I lived in Dubai."
# against "you used to live in dubai") all sit at or below 0.33, and the next
# one up sits at 0.66. Nothing lands in between, so the threshold goes in the
# middle of the empty band. Reading the audio rather than the text agrees:
# correlating the mic and system energy envelopes over those same segments,
# the six score a median 0.06 (the mic was not carrying the far-end signal at
# all) against 0.86 for the segments this gate keeps deleting.
ECHO_CHAR_MIN_COVER = 0.5

# LENGTH is what is left of the old floor, and it is now set only where the
# character test has no measured power at all. Conditioned on the cover gate
# passing, the character test fires on 16.7% of real 4-word windows against
# 9.1% of decoy windows, so at four words it is barely telling the two apart;
# at five words it is 38.1% against 2.7%, and it stays that separated at every
# length above. Below this floor the word path decides alone, as it always did.
ECHO_CHAR_MIN_TOKENS = 5


def _echo_containment(mic_tokens, window_tokens):
    """Fraction of the mic segment's words that also appear, in order, in the
    system-track words around it. ~1.0 means the mic segment is pure echo."""
    if not mic_tokens or not window_tokens:
        return 0.0
    sm = difflib.SequenceMatcher(None, mic_tokens, window_tokens, autojunk=False)
    matched = sum(block.size for block in sm.get_matching_blocks())
    return matched / len(mic_tokens)


def _echo_containment_chars(mic_tokens, window_tokens):
    """Character-level twin of _echo_containment, for MISHEARD echo.

    The word comparison above is all-or-nothing per word, so an echo the
    recognizer transcribed slightly differently scores low on every word it got
    wrong and survives as something the user supposedly said. Real example, mic
    against system:

        "I'm going to take root at heart"   vs   "I'm a tech recruit at heart"

    Four of seven words differ, so word containment lands near 0.4 and the
    segment is published under the user's name. Comparing characters instead
    lets a near-miss word contribute the letters it did get right.

    Spaces are dropped before matching, so a word boundary the two transcripts
    disagree about ("a tech" against "attach") cannot break the run.

    This is deliberately the WEAKER test: letters recur far more than words do,
    so a long comparison window can align a good fraction of an unrelated
    sentence. It therefore only ever runs as a fallback, on segments the word
    path already scored as part echo (see drop_echo).
    """
    mic_chars = "".join(mic_tokens)
    window_chars = "".join(window_tokens)
    if not mic_chars or not window_chars:
        return 0.0
    sm = difflib.SequenceMatcher(None, mic_chars, window_chars, autojunk=False)
    matched = sum(block.size for block in sm.get_matching_blocks())
    return matched / len(mic_chars)


def _merge_spans(spans):
    """Sorted, non-overlapping copy of (start, end) pairs.

    Far-end words overlap each other (a recognizer will happily end one word
    after the next one starts), so their durations cannot just be summed.
    Merging first is what makes _far_end_cover a true occupancy fraction
    instead of a number that can run past 1.0.
    """
    merged = []
    for s, e in sorted(spans):
        if e <= s:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def _far_end_cover(start, end, spans, span_ends):
    """Fraction of [start, end] that far-end speech occupies, in [0, 1].

    `spans` comes from _merge_spans and `span_ends` is its end times, so the
    first span that can touch the segment is found by bisection rather than by
    walking the whole track for every mic segment.

    Note the resolution this has: when the system track carries word
    timestamps (every meeting recorded by this app does) the spans are words,
    and the gaps between far-end sentences count as silence. A transcript that
    only has segment timestamps measures whole segments instead, which reads
    high, so the gate is at its most permissive on the tracks it knows least
    about. That is the safe direction for a gate whose job is to STOP the
    character path, not to trigger it.
    """
    if end <= start or not spans:
        return 0.0
    total = 0.0
    for i in range(bisect_right(span_ends, start), len(spans)):
        s, e = spans[i]
        if s >= end:
            break
        total += min(e, end) - max(s, start)
    return total / (end - start)


def _trim_echo_words(seg, window_tokens):
    """Word-level trim for part-echo segments: strip a leading/trailing run
    of words that duplicate the system track, keep the genuine middle.

    Conservative on purpose: only trims when the echoed edge run is ≥4 words,
    the kept remainder is ≥3 words, and the segment has word timestamps.
    Returns a trimmed copy, or None when no safe trim exists.
    """
    words = seg.get("words") or []
    tokens = [_norm_text(w["w"]) for w in words]
    pairs = [(i, t) for i, t in enumerate(tokens) if t]
    if len(pairs) < 7 or not window_tokens:
        return None
    sm = difflib.SequenceMatcher(None, [t for _, t in pairs], window_tokens, autojunk=False)
    echoed = set()
    for block in sm.get_matching_blocks():
        for k in range(block.a, block.a + block.size):
            echoed.add(k)
    # Echoed prefix / suffix runs (indices into `pairs`).
    lead = 0
    while lead < len(pairs) and lead in echoed:
        lead += 1
    tail = 0
    while tail < len(pairs) - lead and (len(pairs) - 1 - tail) in echoed:
        tail += 1
    kept_n = len(pairs) - lead - tail
    if kept_n < 3 or max(lead, tail) < 4:
        return None
    kept_words = [words[pairs[i][0]] for i in range(lead, len(pairs) - tail)]
    trimmed = dict(seg)
    trimmed["words"] = kept_words
    trimmed["text"] = " ".join(w["w"].strip() for w in kept_words).strip()
    trimmed["start"] = float(kept_words[0]["s"])
    trimmed["end"] = float(kept_words[-1]["e"])
    return trimmed if trimmed["text"] else None


def drop_echo(mic_segs, sys_segs):
    """Remove mic segments that duplicate overlapping system-track speech.

    Remote voices can leak from the speakers back into the mic when the user
    is not wearing headphones; the system track is the clean copy, so the mic
    duplicate is dropped. Segments that are only PART echo (the user starts
    talking as the remote side finishes) get the echoed words trimmed off
    instead of a whole-segment keep/drop decision.

    Matching is done against ALL system-track words inside the mic segment's
    time window (not segment-by-segment): recognizers rarely put the echo and
    the original on the same segment boundaries, so per-segment comparison
    misses most duplicates that span two system segments.

    Returns (kept_segments, dropped_count, trimmed_count).
    """
    sys_words = []
    for s in sys_segs:
        words = s.get("words") or []
        if words:
            for w in words:
                sys_words.append((float(w["s"]), float(w["e"]), _norm_text(w["w"])))
        else:
            sys_words.append((float(s["start"]), float(s["end"]), _norm_text(s["text"])))
    sys_words.sort(key=lambda w: w[0])
    starts = [w[0] for w in sys_words]
    # When the far end was actually making sound, for the cover gate below.
    far_end = _merge_spans((w[0], w[1]) for w in sys_words)
    far_end_ends = [span[1] for span in far_end]

    kept, dropped, trimmed = [], 0, 0
    for m in mic_segs:
        mic_tokens = _norm_text(m["text"]).split()
        win_start = float(m["start"]) - ECHO_SLACK_S
        # Look back 30s past the window start so whole-segment entries (no
        # word timestamps) that began earlier but overlap are still seen.
        lo = bisect_left(starts, win_start - 30.0)
        hi = bisect_right(starts, float(m["end"]) + ECHO_SLACK_S)
        window_tokens = " ".join(
            w[2] for w in sys_words[lo:hi] if w[1] >= win_start
        ).split()
        ratio = _echo_containment(mic_tokens, window_tokens)
        # Short interjections ("yeah", "okay") must match completely to be
        # treated as echo; real sentences count as echo at 70% containment.
        is_sentence = len(mic_tokens) >= ECHO_MIN_TOKENS
        threshold = ECHO_DROP_RATIO if is_sentence else 0.999
        if ratio >= threshold:
            dropped += 1
        elif ratio >= ECHO_PARTIAL_RATIO:
            # Part echo by word count. Three different things look like this:
            # the user talking over the tail of a remote sentence (trim the
            # echoed edge, keep the middle), a whole sentence of echo the
            # recognizer misheard (drop it), and the user ANSWERING the far end
            # in the far end's own words (keep it, all of it).
            #
            # Characters separate the first two: a misheard echo still tracks
            # the original letter for letter. They cannot separate the third,
            # because an answer that reuses the question's words really does
            # match it. Only the clock can: an answer comes AFTER the far end
            # stops, so far-end speech covers little or none of it, while echo
            # is simultaneous with the sound that caused it by definition.
            char_ratio = 0.0
            if len(mic_tokens) >= ECHO_CHAR_MIN_TOKENS and _far_end_cover(
                float(m["start"]), float(m["end"]), far_end, far_end_ends
            ) >= ECHO_CHAR_MIN_COVER:
                char_ratio = _echo_containment_chars(mic_tokens, window_tokens)
            if char_ratio >= ECHO_DROP_RATIO:
                dropped += 1
                continue
            cut = _trim_echo_words(m, window_tokens)
            if cut is not None:
                trimmed += 1
                kept.append(cut)
            else:
                kept.append(m)
        else:
            kept.append(m)
    return kept, dropped, trimmed


# --- Self-echo: the user's OWN voice coming back on the system track -------------
# The far end sometimes sends the user's voice back: a remote participant on
# open speakers with a weak canceller, "original sound" mode, two remote
# people in one room. That copy lands on the system track a network round
# trip AFTER the mic heard it, and drop_echo above cannot see it — it only
# ever deletes MIC segments — so it was clustered and published as a phantom
# far-end "Speaker N" saying the user's words.
#
# The signature is the LAG, and it was measured (2026-08-21, scratch probe
# over the 20 saved meetings that carry both tracks, scoring every far-end
# segment of >= ECHO_MIN_TOKENS words against the mic words around it, after
# drop_echo had run):
#
#   * one call carries real self-echo: 10 far-end segments, 6-92 words each,
#     word containment 0.71-1.00, every one at a lag of 0.33-0.41 s;
#   * the far end REPLYING in the user's words ("Great talking to you too",
#     "I'm good, thank you") matches at 0.75-0.80 but at 0.97-1.21 s — a
#     person answering, not a wire echoing;
#   * leak residue drop_echo missed (the far end in the mic) matches with the
#     mic copy at or AFTER the far-end copy: lag -0.10 to +0.01 s;
#   * a decoy (mic shifted 600 s) scores zero matches at any ratio.
#
# So: containment at the word path's own bar, and the median lag of the
# matched words inside [SELF_ECHO_MIN_LAG_S, SELF_ECHO_MAX_LAG_S]. The lower
# edge is causality (an echo cannot precede its cause, and the leak residue
# sits at zero); the upper edge is placed inside the empty band between the
# slowest echo seen (0.41 s) and the fastest reply (0.97 s). Both edges are
# set by ONE call each — re-measure when a second self-echo call lands.
SELF_ECHO_MIN_LAG_S = 0.15
SELF_ECHO_MAX_LAG_S = 0.7
# How far back from a far-end segment to gather mic words for the comparison.
# Wider than SELF_ECHO_MAX_LAG_S on purpose: the lag gate, not the window,
# decides, and a narrow window would hide a slow echo instead of rejecting it.
SELF_ECHO_SEARCH_S = 1.5


def _timed_tokens(seg):
    """[(start, normalised word)] for one segment, from its word timestamps
    when it has them, else every token at the segment start."""
    words = seg.get("words") or []
    out = []
    if words:
        for w in words:
            for tok in _norm_text(w["w"]).split():
                out.append((float(w["s"]), tok))
    else:
        for tok in _norm_text(seg["text"]).split():
            out.append((float(seg["start"]), tok))
    return out


def drop_self_echo(sys_segs, mic_segs):
    """Remove system-track segments that are the user's own voice echoed back.

    Run AFTER drop_echo, on the mic segments it kept: the leaked far-end
    copies drop_echo removes would otherwise match their own originals here
    at zero lag (the lag gate rejects them anyway, but there is no reason to
    let them into the comparison).

    Returns (kept_system_segments, dropped_count).
    """
    mic_words = []
    for m in mic_segs:
        mic_words.extend(_timed_tokens(m))
    if not mic_words:
        return list(sys_segs), 0
    mic_words.sort(key=lambda w: w[0])
    mic_starts = [w[0] for w in mic_words]

    kept, dropped = [], 0
    for seg in sys_segs:
        sys_words = _timed_tokens(seg)
        toks = [w[1] for w in sys_words]
        if len(toks) < ECHO_MIN_TOKENS:
            kept.append(seg)
            continue
        start, end = float(seg["start"]), float(seg["end"])
        lo = bisect_left(mic_starts, start - SELF_ECHO_SEARCH_S)
        hi = bisect_right(mic_starts, end - SELF_ECHO_MIN_LAG_S)
        window = mic_words[lo:hi]
        win_toks = [w[1] for w in window]
        if _echo_containment(toks, win_toks) < ECHO_DROP_RATIO:
            kept.append(seg)
            continue
        # The lag of the match: far-end word time minus the mic time of the
        # same word, over every matched word, so one stray alignment cannot
        # set it. Median, so the number is a lag and not an average of lags.
        sm = difflib.SequenceMatcher(None, toks, win_toks, autojunk=False)
        lags = []
        for block in sm.get_matching_blocks():
            for k in range(block.size):
                lags.append(sys_words[block.a + k][0] - window[block.b + k][0])
        if not lags:
            kept.append(seg)
            continue
        lags.sort()
        lag = lags[len(lags) // 2]
        if SELF_ECHO_MIN_LAG_S <= lag <= SELF_ECHO_MAX_LAG_S:
            dropped += 1
        else:
            kept.append(seg)
    return kept, dropped


ECHO_WARNING_PREFIX = "Mic audio contained speaker echo"
SELF_ECHO_WARNING_PREFIX = "Call audio contained your own voice"

# EVERY warning _label_and_assemble can emit starts with this word — both the
# per-track failure notice and MIC_ONLY_WARNING are built from it below. That
# is what makes it safe to use as a "drop the previous run's labelling
# warnings" filter: the set it removes is exactly the set the labelling step
# regenerates.
DIARIZATION_WARNING_PREFIX = "Diarization"

# Warnings this pipeline generates itself. They are rebuilt from scratch on
# every run, so any older copy has to be dropped first or Reprocess stacks
# duplicates. "Removed " is the retired wording of the echo warning, still
# present in meetings processed by older builds.
#
# The two filters are deliberately different sizes, and recluster's is the
# narrow one: recluster_meeting re-runs ONLY the labelling step, so it may drop
# only DIARIZATION_WARNING_PREFIX. Stripping the whole tuple there would delete
# the echo warning — which recluster never recomputes, because echo cleanup
# happens during transcription — and the meeting would silently lose it on the
# first speaker-count change.
PIPELINE_WARNING_PREFIXES = (DIARIZATION_WARNING_PREFIX, "Removed ", ECHO_WARNING_PREFIX,
                             SELF_ECHO_WARNING_PREFIX)


def _build_turns(labelled_segments):
    """Merge consecutive same-speaker segments into readable turns."""
    labelled_segments.sort(key=lambda s: s["start"])
    turns = []
    for seg in labelled_segments:
        prev = turns[-1] if turns else None
        if (
            prev is not None
            and prev["speaker"] == seg["speaker"]
            and prev["track"] == seg["track"]
            and seg["start"] - prev["end"] <= TURN_MERGE_GAP_S
        ):
            prev["text"] = (prev["text"] + " " + seg["text"]).strip()
            prev["end"] = max(prev["end"], seg["end"])
        else:
            turns.append(
                {
                    "speaker": seg["speaker"],
                    "track": seg["track"],
                    "start": round(seg["start"], 2),
                    "end": seg["end"],
                    "text": seg["text"],
                }
            )
    for t in turns:
        t["end"] = round(t["end"], 2)
    return turns


ANALYSIS_JSON = "analysis.json"
ANALYSIS_NPZ = "analysis.npz"

# --- Keeping analysis.json and analysis.npz in step -----------------------------
# The saved voice analysis is one logical object spread over two files: the
# transcripts in analysis.json and the ECAPA windows/embeddings in analysis.npz.
# They are only meaningful TOGETHER — the embeddings index windows of the exact
# audio those transcripts describe — but they cannot be replaced in one atomic
# step, and until now nothing tied them to each other:
#
#   * a Reprocess that collected no embeddings rewrote analysis.json and left
#     analysis.npz untouched, so a NEW transcript sat beside the PREVIOUS run's
#     cache; and
#   * even when both are written, a crash between the two leaves the pair
#     mismatched.
#
# embed_version cannot see either case: it says which code computed the
# embeddings, and in both the stale cache was computed by this very build, so
# the marker matches and the staleness goes undetected.
#
# So each save stamps BOTH files with the same fresh random token. recluster
# uses the cache only when the tokens agree, which is true exactly when the npz
# it is holding is the one written for the transcripts it just loaded. Tokens
# are compared, never ordered — a token says "same save", not "newer".
#
# The token is what lets an npz this run did not rewrite simply STAY on disk:
# it is either still paired (the transcripts are unchanged, so it is carried
# over — see _carried_state_id) or plainly unpaired and ignored. Either way the
# embeddings survive, which matters because only the WAV can regenerate them.
#
# Files written before the token existed have it in NEITHER file; that pair was
# written together by the old code and is honoured as matching (see
# _load_analysis_cache), so no existing meeting is forced to re-embed.
STATE_ID_KEY = "state_id"

# --- Lost system track (online mode) -------------------------------------------
# When BlackHole is not installed, or the meeting app is not actually routed
# through it, the call audio never reaches the system track and BOTH sides of
# the conversation land on the microphone. Left undetected the whole meeting
# renders as a monologue by "You".
#
# THE SIGNAL IS ABSOLUTE, NOT RELATIVE. An earlier version of this detector
# gated on the system/mic speech RATIO, which is unsafe: a meeting where the
# user talks 22 minutes and the far end contributes 60 s of "mhm"/"right" has a
# ratio of 0.045 and would trip a 0.05 ratio gate even though NOTHING IS WRONG
# — and the punishment for a false positive is severe (the user's own mic gets
# split into several strangers and nobody is labelled "You"). The corpus agrees
# the ratio carries no usable margin: healthy online calls sit at 0.558, 0.589,
# 0.898, 0.964, 1.611, 2.034, 2.429, 2.903, 4.824 and 9.287, a spread of 17x
# with no floor to lean on, while the one genuinely broken meeting
# (Call M) has EXACTLY 0.000 s of system speech across ZERO segments.
# What actually distinguishes the failure is that the system track carries no
# speech AT ALL, so that is what we test. A system track with any real speech on
# it — however little relative to the mic — was captured, and we leave it alone.
#
# "No speech at all" is meant literally: ZERO transcribed system segments. An
# earlier revision allowed up to 1.0 s of system speech through, which
# contradicted the contract stated right above — 1.0 s of transcribed speech is
# speech, and a track that produced a segment did capture something. The
# tolerance also bought nothing: measured over the 12 frozen fixtures plus
# Call M, the quietest healthy system track is Call J's 23.4 s across 5
# segments, while the one genuinely broken meeting has 0 segments and 0.000 s.
# Nothing in the corpus lives between those, so the strictest possible gate
# fires on exactly the same single meeting as the loose one — and it can no
# longer be argued into firing on a meeting that recorded real speech.
#
# The 30 s floor on mic speech keeps short clips (a 2 s "hello", a routing test)
# out of the fallback, where there is too little audio to cluster anything
# meaningfully.
MIC_ONLY_MIN_MIC_S = 30.0


def _speech_seconds(segments):
    return sum(float(s["end"]) - float(s["start"]) for s in segments or [])


def _system_track_lost(mic_segs, sys_segs):
    """True when an online meeting looks like it recorded both sides on the mic.

    Deliberately conservative: it fires only when the system track transcribed
    NOTHING — zero segments — while the mic holds at least MIC_ONLY_MIN_MIC_S of
    speech. It never fires on a meeting whose system track captured any speech
    at all, however little.
    """
    if _speech_seconds(mic_segs) < MIC_ONLY_MIN_MIC_S:
        return False
    return not (sys_segs or [])


# The advice here used to be "install BlackHole", which the Core Audio
# process tap replaced: nobody needs a driver any more, and sending someone
# to install one is sending them to fix the wrong thing. On the tap the
# silent-track causes are, in the order they actually happen: macOS never
# granted System Audio Recording (the grant is per app identity, so a fresh
# install or an app update asks again), the call was playing through a
# device the tap was not following, or the far side genuinely never spoke.
MIC_ONLY_WARNING = (
    DIARIZATION_WARNING_PREFIX + " fell back to the microphone: the call-audio "
    "track was silent, so both sides of the conversation were recorded on the "
    "mic. The voices were separated by sound instead, and which one is you "
    "cannot be told apart from the others, so nobody is labelled \"You\". "
    "Rename the speakers above. To capture the call on its own track next "
    "time, open System Settings, Privacy and Security, Screen and System "
    "Audio Recording, and allow MeetingScribe."
)
MIC_ONLY_WARNING_PINNED = (
    DIARIZATION_WARNING_PREFIX + " fell back to the microphone: the call-audio "
    "track was silent, so both sides of the conversation were recorded on the "
    "mic. The voices were separated by sound, and yours was recognised from "
    "earlier calls and labelled \"You\". To capture the call on its own track "
    "next time, open System Settings, Privacy and Security, Screen and System "
    "Audio Recording, and allow MeetingScribe."
)

# --- Is that second cluster on the mic a real second person? --------------------
# COUNTING CLUSTERS IS NOT EVIDENCE. Run the auto path over the MIC track of
# every online meeting in the corpus and 7 of 10 single-voice mic tracks come
# back as 2 or 3 clusters (table below). So "more than one cluster came back"
# cannot be the test that decides whether to throw the "You" label away: on most
# real meetings that valve never closes, and the user loses their own label to a
# split that never had a second person in it.
#
# What separates a real second speaker from a same-voice split is SIZE and
# TEMPORAL COHERENCE, the same two signals diarization._fold_weak_clusters
# already leans on (it folds by duration; here we add coherence, which is what
# duration alone gets wrong). A real participant takes turns: their windows
# arrive in a few long contiguous runs and add up to a comparable share of the
# conversation. A phantom is scattered through the dominant speaker's windows in
# many one-window slivers and carries almost none of the speech.
#
#   duration ratio = runner-up cluster's seconds ÷ biggest cluster's seconds
#   fragmentation  = runner-up's contiguous runs ÷ its windows, time-ordered
#
# MEASURED ON MIC TRACKS — every online meeting in recordings/, auto path,
# threshold 0.6 (the design table in docs/COUNT_ESTIMATION_DESIGN.md measures
# SYSTEM tracks; this branch only ever clusters a mic, so it was re-measured
# there):
#
# Meetings are named by the stable pseudonyms in tools/eval_diarization.py's
# LABELS dict — the same label means the same meeting there, in test_diarization
# and in docs/. Real titles carry participants' names and this repo is public;
# `eval_diarization.py --show-titles` maps them back on this machine.
#
#     meeting                     clusters   ratio   fragmentation
#     Call M (REAL 2 people)          3      0.835       0.200
#     ---------------------------------------------------------- gate
#     Call B                          2      0.029       0.448
#     Call C                          3      0.020       0.750
#     Call H                          3      0.036       0.381
#     Call D                          2      0.004       0.909
#     Call E                          2      0.034       0.567
#     Call F                          2      0.012       0.750
#     Call G                          3      0.020       0.417
#     Call A                          1        -           -
#
# ("Call M" is the one row with no entry in LABELS yet — it has no cached
# fixture, so eval_diarization has never had to name it. The letter is reserved
# here; adding it there is the diarization owner's call.)
#
# Seven of the ten split a solo mic into 2-3 clusters, which is precisely why
# the count is worthless as a test. Both signals separate the corpus completely
# on their own (phantom ratios top out at 0.036 against the real 0.835; phantom
# fragmentation bottoms out at 0.381 against the real 0.200), and BOTH are
# required anyway: duration alone folds a quiet-but-real participant, coherence
# alone risks folding a real speaker who only interjects in short bursts.
#
# The duration gate is set at 0.25 rather than tight against the mic numbers so
# that it also clears the SYSTEM-track phantoms in the design doc (0.02-0.16) —
# it is not tuned to one track type. The cost is deliberate and one-directional:
# a genuine second voice that holds under a quarter of the biggest speaker's
# time is read as the user alone. That verdict is recoverable (set the speaker
# count by hand — see _user_speaker_count); a destroyed "You" label on a meeting
# that was never broken used to be recoverable by nothing at all.
MIC_ONLY_MIN_DURATION_RATIO = 0.25

# ONE NUMBER, ONE DEFINITION — aliased, never re-declared.
#
# This gate and diarization.FOLD_MAX_FRAGMENTATION are not merely similar: they
# are the same statistic (a cluster's contiguous runs ÷ its windows, in time
# order — _runner_up_evidence below and diarization._fragmentation compute it
# identically) about the same question (is this cluster a person, or a split of
# the leading voice?), in opposite polarity: here frag <= gate ACCEPTS a real
# voice, there frag > gate FOLDS a phantom. They are complements of one
# constant. They were nonetheless two literals in two files — 0.30 here against
# 0.25 there — so a cluster at fragmentation 0.27 was a phantom to the clusterer
# and a genuine second speaker to this detector, on the same recording.
#
# diarization owns the number, because that is where it is swept against ground
# truth. Read its comment for the measurement; the short version is that the
# band this file needs, [0.250, 0.3333), and the band the fold rule needs,
# [0.139, 0.3333], intersect at [0.250, 0.3333), and 0.30 is the value inside it
# with margin on both sides. (That 0.139 is Room P's genuine second voice. It
# was re-measured from 0.148 when diarization gained FOLD_KEEP_ABOVE_S: the
# 0.148 case, Room Q, is now held by that absolute duration ceiling instead of
# by this gate, which moved the fold rule's lower edge down. The intersection
# and this value are unchanged either way.)
#
# The lower edge of the intersection is ours: synthetic-two-medium-turns holds a
# KNOWN second speaker at fragmentation exactly 0.250, so any gate below that
# deletes a real participant.
#
# IMPORT, DO NOT COPY. A second literal is precisely how these two drifted
# apart, and an alias cannot drift: whatever diarization sweeps to next, this
# gate is already that number. Nothing here re-derives it, and nothing here may.
#
# MEASURED, before and after: the alias resolves to 0.30, which is the literal
# this line replaced, so today it is a no-op — tools/test_diarization.py's mic
# verdicts, tools/eval_diarization.py's golden regression and its 21 truth-backed
# fixtures are all unchanged. It is a correctness fix for the next time somebody
# moves the threshold, not a behaviour change now.
MIC_ONLY_MAX_FRAGMENTATION = diarization.FOLD_MAX_FRAGMENTATION


def _runner_up_evidence(windows, labels):
    """(duration_ratio, fragmentation) for the second-biggest cluster.

    Both are computed exactly as in the corpus measurement above: clusters are
    ranked by total window duration, the ratio is runner-up ÷ biggest, and
    fragmentation is the runner-up's contiguous runs ÷ its windows over
    time-ordered windows. Returns (None, None) when there is nothing to judge.
    """
    labels = list(labels)
    if len(labels) != len(windows) or len(set(labels)) < 2:
        return None, None
    spans = [(float(w[0]), float(w[1]) - float(w[0])) for w in windows]
    seconds = {}
    for lab, (_, dur) in zip(labels, spans):
        seconds[lab] = seconds.get(lab, 0.0) + dur
    biggest, runner_up = sorted(seconds, key=lambda lab: -seconds[lab])[:2]
    if seconds[biggest] <= 0:
        return None, None
    ratio = seconds[runner_up] / seconds[biggest]

    ordered = [lab for _, lab in sorted(zip([s for s, _ in spans], labels),
                                        key=lambda pair: pair[0])]
    runs = sum(
        1 for i, lab in enumerate(ordered)
        if lab == runner_up and (i == 0 or ordered[i - 1] != runner_up)
    )
    count = sum(1 for lab in ordered if lab == runner_up)
    return ratio, runs / count


def _mic_holds_a_second_voice(state, threshold):
    """Decide whether the mic-only fallback really found somebody else.

    state is the {"windows", "embeddings"} the fallback's own diarization run
    produced. The per-window labels are recomputed from it with the identical
    call diarize_track makes — same inputs, deterministic clustering, therefore
    the same labels; diarize_track returns per-SEGMENT speakers, and the corpus
    thresholds above are defined per WINDOW, so they have to be re-derived here.

    Unknowable means no: without windows to judge we keep the "You" label rather
    than gamble it on a cluster count.
    """
    windows = (state or {}).get("windows")
    embeddings = (state or {}).get("embeddings")
    if windows is None or embeddings is None or len(windows) < 2:
        return False, (None, None)
    durations = [float(w[1]) - float(w[0]) for w in windows]
    labels = diarization.cluster(
        embeddings, n_speakers=None, threshold=threshold, durations=durations
    )
    ratio, fragmentation = _runner_up_evidence(windows, labels)
    if ratio is None:
        return False, (ratio, fragmentation)
    real = (
        ratio >= MIC_ONLY_MIN_DURATION_RATIO
        and fragmentation <= MIC_ONLY_MAX_FRAGMENTATION
    )
    return real, (ratio, fragmentation)


# Written by recluster_meeting() when the count came from the speaker-count
# control, i.e. from a person looking at the result.
SPEAKER_COUNT_USER = "user"

# --- The calendar's guess lives under its OWN key -------------------------------
# meta["speaker_count_hint"] is app.py's record-time guess from the calendar
# invitee count: online, people besides you; in-person, people in the room.
# It is a prediction about the meeting, not an observation of the recording,
# and it is applied as a CAP on the auto path (diarization.cluster's
# max_speakers — surplus small clusters fold down to it, a voice with
# HINT_OVERRIDE_S of speech stays regardless). It is NEVER passed as
# n_speakers: that branch returns exactly N clusters with no cleanup, which is
# how a 3-person invite on a 1:1 call used to ship two phantom speakers, and a
# 1:1 invite on a 3-person call a monologue. meta["expected_speakers"] is now
# only ever a number a person typed — at record time or in the speaker-count
# control — and is still honoured literally.
#
# BACK-COMPAT: meetings recorded before this key existed carry the calendar's
# guess IN expected_speakers with no speaker_count_source, indistinguishable
# from a count typed on the record form. They keep today's forced behaviour;
# an Auto recluster clears it. Nothing is migrated, because nothing on disk
# says which of the two it was.
SPEAKER_COUNT_HINT = "speaker_count_hint"


def _speaker_count_hint(meta):
    """The calendar's invitee-count cap for this meeting, or None."""
    try:
        hint = int(meta.get(SPEAKER_COUNT_HINT) or 0)
    except (TypeError, ValueError):
        return None
    return max(1, min(8, hint)) if hint > 0 else None

# --- What number is meta["expected_speakers"]? ---------------------------------
# It has always meant two different things depending on which control produced
# it, and until now nothing on disk recorded which. meta["speaker_count_basis"]
# is that record:
#
#   "others"  the count EXCLUDES the local user — the historical
#             "Speakers besides you" control. Total voices = count + 1.
#   "total"   the count INCLUDES the local user — the mic-fallback control,
#             "Voices on the mic (you are one of them)". Total voices = count.
#
# Which one a click produces is decided by the mode the user was LOOKING at:
# "total" when meta["diarization_mode"] == "mic_fallback", "others" otherwise.
# recluster_meeting stamps it at the moment of the click, before the run that
# may change diarization_mode underneath it.
#
# BACK-COMPAT — meetings recorded before this key existed. Their
# expected_speakers was set through the only control that shipped, which asked
# "Speakers besides you", so a MISSING basis reads as "others". That is a
# faithful reading of the old data, not a reinterpretation of it: the number
# means today exactly what the user was asked for when they typed it. The rule
# is applied in exactly one place, _user_speaker_count() below, so there is one
# thing to revisit if it is ever wrong.
#
# NOTE the in-person control ("People in the room (you included)") is a total,
# yet it is stamped "others" by the rule above. That is intentional and
# harmless: basis is consumed only by the mic-only fallback, which exists only
# in online mode, so an in-person meeting's basis is never read. Anything that
# starts reading basis outside the fallback must handle in-person first.
SPEAKER_COUNT_OTHERS = "others"
SPEAKER_COUNT_TOTAL = "total"


def _speaker_count_basis(meta):
    """Which question the speaker-count control was asking on this meeting."""
    return (SPEAKER_COUNT_TOTAL
            if meta.get("diarization_mode") == "mic_fallback"
            else SPEAKER_COUNT_OTHERS)


def _user_speaker_count(meta):
    """TOTAL voices on the mic if a PERSON chose the count, else None.

    Only the mic-only fallback needs the distinction. Everywhere else the count
    is applied to the system track, where a calendar guess is a reasonable prior
    and a wrong one is corrected by the same control. In the fallback it decides
    whether the user keeps their "You" label, so a guess is not good enough.

    The return value is always a TOTAL, because that is what the fallback needs:
    under the fallback every voice, the user's included, is on the one mic being
    clustered. A "others"-basis count is therefore converted (count + 1), and a
    "total"-basis count is used as-is. See SPEAKER_COUNT_OTHERS above for the
    back-compat rule when no basis was ever stored.
    """
    if meta.get("speaker_count_source") != SPEAKER_COUNT_USER:
        return None
    try:
        count = int(meta.get("expected_speakers") or 0)
    except (TypeError, ValueError):
        return None
    if count <= 0:
        return None
    basis = meta.get("speaker_count_basis") or SPEAKER_COUNT_OTHERS
    return count if basis == SPEAKER_COUNT_TOTAL else count + 1


def _neural_refine(meeting_dir, meta, cfg, key, segs, classic_segs, n_found,
                   state, precomputed, allow_neural_run, progress_cb,
                   user_forced=False, forced=False):
    """Swap the classic window-vote attribution for neural frame-level turns
    — and, on the AUTO path, let the neural engine RAISE the count.

    The division of labour is measured, and it is not symmetric
    (OFFLINE_RETEST.md + MULTIPARTY.md):

      * Downward, the classic cascade is the authority: on the 1:1-heavy
        real corpus it scores 21/21 while neural auto peaked at 19/21, and
        on Room T it hears a second person the neural engine cannot. When
        neural hears FEWER voices than classic, classic stands.
      * Upward, the classic cascade is the liability: its fold rules were
        tuned on a corpus where 17 of 21 tracks hold ONE voice, and on the
        owner's multi-voice video test it collapsed six people to a single
        speaker — with the old `n_found < 2` gate then never consulting the
        neural engine at all. When neural hears MORE substantial voices
        (each ≥ CONFETTI_MIN_S after fold_confetti) on an UNFORCED track,
        its turns AND its count win. On the 21 truth-backed fixtures this
        changes nothing — neural-after-fold never exceeds the classic count
        there — so the 21/21 is intact by measurement, not by hope.

    A FORCED count is a promise made upstream and is never raised:
      * user_forced (speaker_count_source == "user"): the engine runs
        pinned to the human's number, no self-validation — measured 10x
        better than classic attribution at the same forced count on
        playback audio (27.6% -> 2.7% confusion).
      * calendar/other forced: the old self-validation flow, count kept.

    Cached turns are reused when their count fits the same rules; a fresh
    engine run happens only when `allow_neural_run` — reprocess yes,
    auto-recluster no (sub-second promise), user-forced recluster yes.
    Every failure returns the classic result unchanged.
    """
    engine = str(cfg.get("diarization_engine") or "auto").lower()
    if engine == "classic":
        return classic_segs, n_found, False
    if forced and n_found < 2:
        return classic_segs, n_found, False  # a forced count of 1 needs no turns
    track = (meta.get("tracks") or {}).get(key) or {}

    turns = None
    cached = (precomputed or {}).get(key)
    if cached is not None and len(cached) > 2 and cached[2] is not None:
        cached_turns = [tuple(t) for t in np.asarray(cached[2]).tolist()]
        k_cached = len({int(t[2]) for t in cached_turns})
        if k_cached == n_found or (not forced and k_cached > n_found):
            turns = cached_turns
    if turns is None:
        if not allow_neural_run or not diarization_neural.available():
            return classic_segs, n_found, False
        wav = meeting_dir / (track.get("file") or "")
        if not wav.exists():
            return classic_segs, n_found, False
        progress_cb("Refining speaker turns…")
        offset = float(track.get("start_offset") or 0.0)
        try:
            if user_forced:
                # The human outranks both machines — run pinned to their
                # number, no self-validation detour (see docstring).
                turns = diarization_neural.diarize_turns(
                    wav, num_speakers=n_found, offset=offset)
            else:
                turns = diarization_neural.fold_confetti(
                    diarization_neural.diarize_turns(wav, offset=offset))
                k_neural = len({t[2] for t in turns})
                if k_neural < n_found:
                    # It cannot hear a voice the classic engine can (Room T:
                    # two people in one room read as one). A forced split
                    # from an engine that cannot hear the difference is a
                    # coin toss — keep the classic attribution.
                    log.info("neural engine hears %d voice(s) on %s where "
                             "the classic engine hears %d; keeping classic "
                             "attribution", k_neural, key, n_found)
                    return classic_segs, n_found, False
                if k_neural > n_found and forced:
                    # A machine-promised count (calendar): merge the extras
                    # onto the promised voices rather than break the promise.
                    turns = diarization_neural.diarize_turns(
                        wav, num_speakers=n_found, offset=offset)
                elif k_neural > n_found:
                    log.info("neural engine hears %d substantial voice(s) on "
                             "%s where the classic cascade folded to %d; "
                             "adopting the neural count", k_neural, key, n_found)
                # k_neural == n_found: the turns are used as-is.
        except Exception as exc:
            log.warning("neural turns failed on %s (%s); keeping classic "
                        "attribution", key, exc)
            return classic_segs, n_found, False
    if not turns:
        return classic_segs, n_found, False

    k_turns = len({int(t[2]) for t in turns})
    refined, k = diarization.assign_by_turns(segs, turns)
    if k != k_turns:
        # The turn set didn't put words on every voice it promised (or
        # produced ghosts). The classic attribution keeps the contract.
        log.info("neural turns yielded %d speaker(s) against their own count "
                 "of %d on %s; keeping classic attribution", k, k_turns, key)
        return classic_segs, n_found, False
    state["neural_turns"] = turns
    return refined, k, True


def _embed_windows_subprocess(track_file, windows, progress_cb):
    """diarization.embed_windows in a child process that exits, or None to
    say "embed in-process instead".

    See tools/embed_worker.py for why (the embedder's runtime keeps its memory
    no matter what is deleted; only process exit returns it) and for the argv
    contract. Every failure here is an inconvenience, never an error: the
    caller's in-process fallback computes the same numbers, it just keeps
    the memory. The child's stdout lines are its progress reports, forwarded
    to progress_cb as-is; stderr goes to a temp file read only on failure so
    a chatty model download can't deadlock the pipe.
    """
    worker = BASE_DIR / "tools" / "embed_worker.py"
    if not worker.exists():
        return None
    timeout = max(600.0, _track_duration(track_file) / 2.0)
    win_f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    err_f = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    out_path = Path(win_f.name).with_suffix(".npy")
    try:
        json.dump([[t0, t1] for t0, t1 in windows], win_f)
        win_f.close()
        proc = subprocess.Popen(
            [sys.executable, str(worker), str(track_file), win_f.name,
             "--out", str(out_path)],
            stdout=subprocess.PIPE, stderr=err_f, text=True)
        killer = threading.Timer(timeout, proc.kill)
        killer.start()
        try:
            for line in proc.stdout:
                line = line.strip()
                if line:
                    progress_cb(line)
            proc.wait()
        finally:
            killer.cancel()
        if proc.returncode != 0:
            tail = Path(err_f.name).read_text(errors="replace").strip()
            log.warning("embed worker exited %s: %s", proc.returncode,
                        " | ".join(tail.splitlines()[-3:]) or "(no stderr)")
            return None
        emb = np.load(out_path, allow_pickle=False)
        if emb.shape[0] != len(windows):
            log.warning("embed worker returned %d rows for %d windows",
                        emb.shape[0], len(windows))
            return None
        return emb
    except Exception as exc:
        log.warning("embed worker failed (%s); embedding in-process", exc)
        return None
    finally:
        Path(win_f.name).unlink(missing_ok=True)
        Path(err_f.name).unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)


def _embed_track(track_file, segs, progress_cb):
    """(windows, embeddings) for one track, or None when there is nothing to
    cluster — the `precomputed` argument diarization.diarize_track wants.

    THIS IS A MEMORY FIX, not a behaviour change. Left to itself diarize_track
    reads the WAV through diarization.load_mono_16k, which holds the track
    three times over (stereo float32 + mono copy + resampled copy: 6.99 GB
    measured on a 3-hour recording). Handing it windows and embeddings computed
    here means the embedding read of that file happens in the worker through
    the blocked loader at 0.87 GB — and, since the worker is its own process,
    neither the waveform nor the embedder's runtime memory survives in the
    engine at all. The in-process fallback below keeps the same 0.87 GB profile
    the blocked loader always had.

    The paths are otherwise the same code: diarize_track's own branch is
    build_windows() then embed_windows(), which is exactly what happens here
    and in the worker. Fewer than two windows is left to diarize_track
    (returning None) so its one-speaker early return still fires BEFORE
    `state` is written — a track with nothing to cluster must not seed
    analysis.npz with an empty cache.
    """
    windows = diarization.build_windows(segs)
    if len(windows) < 2:
        return None
    emb = _embed_windows_subprocess(track_file, windows, progress_cb)
    if emb is not None:
        return windows, emb
    audio = load_mono_16k(track_file)
    try:
        return windows, diarization.embed_windows(audio, windows, progress_cb)
    finally:
        del audio


def _label_and_assemble(meeting_dir, meta, transcripts, cfg, expected, progress_cb,
                        precomputed=None, collect=None, allow_neural_run=True,
                        hint=None):
    """Steps 3+4: cluster voices into speakers and build the final transcript.

    Mutates meta (speakers/turns/stats) and the transcript segments. Returns
    the labelling warnings. precomputed maps track -> (windows, embeddings)
    to skip the audio embedding pass; collect (dict) gathers computed
    windows/embeddings per track so the caller can persist them.

    meta["diarization_mode"] is the labelling contract for the UI:

      "mic_fallback"  the online system track was silent, so the mic carried
                      the whole call and was clustered like a room mic. The
                      speaker keys are s1..sN and NONE of them is known to be
                      the local user — we cannot tell which cluster is "you"
                      and deliberately do not guess. The UI must not present
                      any of these speakers as the user.
      absent          every normal case, including in-person meetings and
                      online meetings whose mic collapsed to a single voice.
                      "you" means what it always meant.

    The key is REMOVED (not left stale) whenever the fallback does not engage,
    so reprocessing a meeting out of the fallback clears the marker.

    The fallback is CORRECTABLE. Left to itself it accepts a second voice only
    on the evidence described above, but a count the user chose by hand (see
    _user_speaker_count) overrides that judgement in both directions: a total of
    1 voice on the mic puts the meeting back on the ordinary "You" path and
    clears the marker, a total of N > 1 forces N voices on the mic. _user_speaker
    _count returns that TOTAL whichever way the control phrased the question, so
    this branch never has to know which control the user saw. That is the escape
    hatch for a wrong verdict, and it survives Reprocess because both the count
    and its basis live in meeting.json.
    """
    mode = meta.get("mode", "online")
    threshold = float(cfg["diarization_threshold"])
    warnings = []
    track_state = {}
    neural_used = []

    def diarize(key, n_speakers, prefix, name_fmt, start_index, max_speakers=None):
        """Cluster one track's voices; returns labelled segments + speaker map.

        n_speakers is a person's count and forces the result; max_speakers is
        the calendar's and only caps the auto path (see SPEAKER_COUNT_HINT).
        """
        segs = transcripts.get(key) or []
        if not segs:
            return [], {}
        track_file = meeting_dir / meta["tracks"][key]["file"]
        # Always collected: the mic-only fallback has to weigh the windows and
        # embeddings this run used before it trusts a second voice, whether or
        # not the caller asked for them to be persisted.
        state = {}
        cached = (precomputed or {}).get(key)
        try:
            new_segs, n_found = diarization.diarize_track(
                track_file,
                segs,
                n_speakers=n_speakers,
                threshold=threshold,
                progress_cb=progress_cb,
                precomputed=(cached[:2] if cached is not None
                             else _embed_track(track_file, segs, progress_cb)),
                state=state,
                max_speakers=None if n_speakers else max_speakers,
            )
        except ModelDownloadError as exc:
            # The transcript is already written and is the valuable half, so a
            # model that did not arrive costs speaker labels, not the meeting.
            # It says so in as many words rather than hiding behind the generic
            # "diarization failed": the user can fix a download.
            warnings.append(
                f"{DIARIZATION_WARNING_PREFIX} could not run on the {key} track. "
                f"{exc} Everyone on it shares one label until then."
            )
            new_segs = [dict(s, speaker_idx=0) for s in segs]
            n_found = 1
        except Exception as exc:
            warnings.append(
                f"{DIARIZATION_WARNING_PREFIX} failed on {key} track ({exc}); "
                "using one label."
            )
            new_segs = [dict(s, speaker_idx=0) for s in segs]
            n_found = 1
        # The classic engine proposes HOW MANY voices; the neural engine,
        # when present, re-decides WHO SPEAKS WHEN — and on the auto path
        # may raise the count when it hears more substantial voices than
        # the classic cascade kept. A count the USER typed relaxes the
        # neural gate entirely — see _neural_refine.
        user_forced = bool(n_speakers) and (
            meta.get("speaker_count_source") == SPEAKER_COUNT_USER)
        new_segs, n_found, refined = _neural_refine(
            meeting_dir, meta, cfg, key, segs, new_segs, n_found,
            state, precomputed, allow_neural_run, progress_cb,
            user_forced=user_forced, forced=bool(n_speakers))
        if refined:
            neural_used.append(key)
        if state and "embeddings" in state:
            track_state[key] = state
            if collect is not None:
                collect[key] = state
        speakers = {}
        for seg in new_segs:
            idx = seg.pop("speaker_idx")
            skey = f"{prefix}{idx + 1}"
            speakers[skey] = name_fmt.format(idx + start_index)
            seg["speaker"] = skey
            seg["track"] = key
        # Keep the speaker map ordered by first appearance.
        ordered = {}
        for seg in sorted(new_segs, key=lambda s: s["start"]):
            ordered.setdefault(seg["speaker"], speakers[seg["speaker"]])
        return new_segs, ordered

    def pin_owner(segs, speaker_map):
        """Relabel the mic cluster that matches the owner's voice print as
        "you". Returns (segs, speaker_map, pinned). The mic is clustered here
        because the user is one voice among several on it — in-person, or the
        mic-only fallback — and without the print they are "Speaker N" like
        everyone else. See voice_profiles.recognize_owner for the bars."""
        owner = voice_profiles.recognize_owner(track_state, segs, speaker_map, cfg)
        if not owner:
            return segs, speaker_map, False
        for seg in segs:
            if seg["speaker"] == owner:
                seg["speaker"] = "you"
        renamed = {}
        for k, v in speaker_map.items():
            if k == owner:
                renamed["you"] = "You"
            else:
                renamed[k] = v
        return segs, renamed, True

    labelled = []
    speakers = {}
    mic_only = False
    pinned_you = False
    if mode == "online":
        mic_segs = transcripts.get("mic") or []
        sys_segs = transcripts.get("system") or []
        # BlackHole missing or unrouted: the mic carries the whole call, so
        # cluster it like an in-person room mic instead of calling it all "You".
        mic_only = bool(mic_segs) and _system_track_lost(mic_segs, sys_segs)
        if mic_only:
            progress_cb("Identifying speakers…")
            # TOTAL voices on the mic as the USER counted them, if they counted
            # at all — see _user_speaker_count(), which converts whichever way
            # the control asked the question into a total. A calendar-derived
            # count is NOT usable here: it counts the people invited to the
            # call, which under the fallback says nothing about how many voices
            # reached this mic, and forcing it would manufacture exactly the
            # phantom speakers this branch exists to avoid.
            forced = _user_speaker_count(meta)
            # Without one, ALWAYS the auto path (n_speakers=None). A forced
            # count is a promise cluster() keeps too literally: its
            # AgglomerativeClustering(n_clusters=N) branch applies neither
            # _merge_tiny_clusters nor _fold_weak_clusters and returns exactly N
            # clusters unconditionally — even when the mic holds one voice and
            # two of four hundred windows happen to sit slightly apart. The auto
            # path can legitimately collapse to a single cluster, which is the
            # whole point: the detector only tells us the system track was
            # silent, not that anyone else was actually on the mic.
            # The probe below may be DISCARDED (verdict "just you"), and a
            # discarded labelling must not leave its neural-turns mark on the
            # meeting — snapshot the marker list so it can be rolled back.
            neural_before = list(neural_used)
            fallback_segs, fallback_speakers = diarize(
                "mic", forced, "s", "Speaker {}", 1
            )
            if forced:
                # The user overrode the machine. Honour it literally, including
                # a total of 1, which is how a false positive gets corrected:
                # one voice on the mic means the mic was just you, so we fall
                # through to the normal "You" labelling below.
                second_voice = len(fallback_speakers) > 1
            else:
                # COUNTING CLUSTERS IS NOT EVIDENCE (see
                # _mic_holds_a_second_voice): the auto path splits one voice in
                # two often enough that len(...) > 1 would leave this valve
                # permanently open, and an open valve costs the user their "You"
                # label on a meeting that was never broken.
                second_voice, evidence = _mic_holds_a_second_voice(
                    track_state.get("mic"), threshold
                )
                log.info(
                    "mic-only fallback: %d cluster(s), runner-up duration ratio "
                    "%s / fragmentation %s -> %s",
                    len(fallback_speakers), *(
                        "%.3f" % v if v is not None else "n/a" for v in evidence
                    ),
                    "second voice" if second_voice else "just you",
                )
            if second_voice:
                # The fallback's whole apology is "which one is you cannot be
                # told". With an enrolled print, sometimes it can.
                fallback_segs, fallback_speakers, pinned_you = pin_owner(
                    fallback_segs, fallback_speakers)
                labelled.extend(fallback_segs)
                speakers.update(fallback_speakers)
                warnings.append(MIC_ONLY_WARNING_PINNED if pinned_you
                                else MIC_ONLY_WARNING)
            else:
                # One voice on the mic is the user talking, as usual. The
                # fallback's output is dropped on the floor — diarize_track
                # builds fresh dicts, so transcripts["mic"] is untouched and the
                # "You" pass below labels it exactly as it always would. That
                # includes any neural-turns mark the probe left: a labelling
                # nobody sees must not stamp meta["diarizer"].
                mic_only = False
                neural_used[:] = neural_before
        if not mic_only and mic_segs:
            speakers["you"] = "You"
            for seg in mic_segs:
                seg["speaker"] = "you"
                seg["track"] = "mic"
            labelled.extend(mic_segs)
            # The mic is the user by construction here, which makes it the one
            # clean sample of their voice the product ever records. Embed it
            # (once — the fallback probe above may already have) so
            # process_meeting can enroll the owner print and so the cache
            # carries it for any later run. Nothing is clustered.
            if cfg.get("voice_profiles", True) and "mic" not in track_state:
                cached = (precomputed or {}).get("mic")
                try:
                    pre = (cached[:2] if cached is not None else
                           _embed_track(meeting_dir / meta["tracks"]["mic"]["file"],
                                        mic_segs, progress_cb))
                except Exception as exc:
                    log.warning("mic embedding for the owner print failed (%s)", exc)
                    pre = None
                if pre is not None:
                    state = {
                        "windows": [tuple(w) for w in np.asarray(pre[0]).tolist()],
                        "embeddings": np.asarray(pre[1], dtype=np.float64),
                    }
                    track_state["mic"] = state
                    if collect is not None:
                        collect["mic"] = state
        if sys_segs:
            # mic_only is necessarily False here: _system_track_lost() requires
            # `not sys_segs`, so reaching this block at all means the system
            # track transcribed something and the fallback cannot have engaged.
            # An earlier revision branched on mic_only here to give the system
            # track a "Remote 1..N" namespace, which no input could ever reach;
            # it is gone rather than left to look like a supported path.
            progress_cb("Identifying speakers…")
            new_sys, sys_speakers = diarize("system", expected, "s", "Speaker {}", 1,
                                            max_speakers=hint)
            labelled.extend(new_sys)
            speakers.update(sys_speakers)
    else:  # in-person: everyone shares the mic
        if transcripts.get("mic"):
            progress_cb("Identifying speakers…")
            mic_segs, mic_speakers = diarize("mic", expected, "s", "Speaker {}", 1,
                                             max_speakers=hint)
            mic_segs, mic_speakers, pinned_you = pin_owner(mic_segs, mic_speakers)
            labelled.extend(mic_segs)
            speakers.update(mic_speakers)
        if transcripts.get("system"):
            progress_cb("Identifying remote speakers…")
            sys_segs, sys_speakers = diarize("system", None, "r", "Remote {}", 1)
            labelled.extend(sys_segs)
            speakers.update(sys_speakers)

    progress_cb("Building transcript…")
    for seg in labelled:
        seg.pop("words", None)
    turns = _build_turns(labelled)
    duration = meta.get("duration") or (max((t["end"] for t in turns), default=0.0))
    meeting_stats = stats_mod.compute(turns, speakers, duration)

    # Preserve names the user set on a previous run — but only when the
    # speaker set is unchanged. After a re-cluster with a different count,
    # key s1 can be a different *voice* than before, and carrying "Alex"
    # over to the wrong person is worse than asking for a rename.
    old_names = meta.get("speakers") or {}
    if set(speakers) == set(old_names):
        speakers.update(old_names)
    elif "you" in speakers and "you" in old_names:
        speakers["you"] = old_names["you"]

    # Voice profiles: clusters matching a previously-named voice get that name
    # automatically. AFTER the carry-over above, and defaults-only inside, so
    # recognition can never overwrite a name a human typed — it only fills in
    # labels that would otherwise read "Speaker N".
    voice_profiles.apply_recognition(track_state, labelled, speakers, cfg)

    # Then the conversation itself: "Hi, I'm Marcus", "Thanks, Priya". AFTER
    # voice profiles, so a remembered voice always beats an inference, and
    # defaults-only inside for the same reason recognition is. Every name it
    # applies carries an anchored quote, recorded on meta["speaker_names"];
    # it never raises, and a Mac without the on-device model simply keeps the
    # "Speaker N" labels.
    speaker_names.apply_inferred_names(meta, turns, speakers, cfg, progress_cb)

    meta["speakers"] = speakers
    meta["turns"] = turns
    meta["stats"] = meeting_stats
    # Tell the UI whether "you" means anything on this meeting. See the
    # docstring: under "mic_fallback" no speaker is known to be the local user,
    # so the template must not claim one is. Cleared, never left stale.
    # A pinned owner is the one case the marker's sentence ("which one is you
    # cannot be told") is false, so it is not written — and the speaker-count
    # control then asks "besides you", which _user_speaker_count turns back
    # into the mic total the fallback needs.
    if mic_only and not pinned_you:
        meta["diarization_mode"] = "mic_fallback"
    else:
        meta.pop("diarization_mode", None)
    # Which engine attributed the words — "neural" when frame-level turns
    # replaced window voting on at least one track. Cleared, never stale, so
    # a meeting relabelled classic (engine off, cache mismatch) says so.
    if neural_used:
        meta["diarizer"] = "neural"
    else:
        meta.pop("diarizer", None)
    return warnings


def _carried_state_id(json_path, npz_path, transcripts):
    """The token to stamp analysis.json with when this run writes no npz.

    NOTHING IS EVER DELETED HERE. An existing analysis.npz is the only copy of
    those embeddings: regenerating them costs an ECAPA load and a full pass over
    the WAV, and for a meeting whose audio the user has since removed it cannot
    be done at all. A run that collected no embeddings of its own therefore
    leaves the file alone and only decides whether the pair it now forms with
    the new analysis.json is honest:

      transcripts unchanged  the cached windows still index these exact segments
                             (diarization.build_windows is a pure function of
                             them), so the existing token is carried over and the
                             cache stays usable. This is the case that matters:
                             a Reprocess whose diarization raised, one with too
                             few windows to cluster, and one that never needed to
                             diarize at all (an online meeting with no system
                             track) all land here, and none of them is a reason
                             to cost the user their instant re-cluster.
      transcripts changed    the pair really is mismatched. A fresh token is
                             minted so _load_analysis_cache REJECTS the npz —
                             rejected until some later run has something better
                             to write over it, never destroyed.

    A pair written before the token existed has it in neither file; carrying
    `None` over keeps it that way, which _load_analysis_cache reads as matching.
    """
    if not npz_path.exists():
        return uuid.uuid4().hex
    try:
        old = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return uuid.uuid4().hex
    if not isinstance(old, dict) or old.get("transcripts") != transcripts:
        return uuid.uuid4().hex
    return old.get(STATE_ID_KEY)


def _save_analysis_state(meeting_dir, transcripts, collect):
    """Persist transcripts + voice embeddings so speakers can be re-clustered
    later in under a second. Best effort — recluster just stays unavailable."""
    try:
        arrays = {}
        for key, state in collect.items():
            arrays[f"{key}_windows"] = np.asarray(state["windows"], dtype=np.float64)
            arrays[f"{key}_embeddings"] = np.asarray(state["embeddings"], dtype=np.float32)
            if state.get("neural_turns"):
                # (start, end, label) per neural turn, meeting timeline. Reused
                # by recluster when the count still matches; versioned below.
                arrays[f"{key}_neural_turns"] = np.asarray(
                    state["neural_turns"], dtype=np.float64)
        npz_path = meeting_dir / ANALYSIS_NPZ
        json_path = meeting_dir / ANALYSIS_JSON
        state_id = (uuid.uuid4().hex if arrays
                    else _carried_state_id(json_path, npz_path, transcripts))
        if arrays:
            # Stamp the embedding behaviour that produced these arrays so a
            # later build can tell whether they are still comparable to what it
            # would compute, and the token that ties them to the transcripts
            # written just below. Both are metadata only: separate entries that
            # touch neither the window coordinates nor the embedding values.
            arrays["embed_version"] = np.asarray(diarization.EMBED_VERSION, dtype=np.int32)
            if any(k.endswith("_neural_turns") for k in arrays):
                arrays["neural_version"] = np.asarray(
                    diarization_neural.NEURAL_VERSION, dtype=np.int32)
            arrays[STATE_ID_KEY] = np.asarray(state_id)
            # savez_compressed appends ".npz" to a *name* that lacks it, which
            # would defeat the temp file, so it is handed an open handle.
            def _write_npz(tmp, _arrays=arrays):
                with open(tmp, "wb") as fh:
                    np.savez_compressed(fh, **_arrays)
            _atomic_write(npz_path, _write_npz)
        # ...and when `arrays` is empty there is nothing better to write, so any
        # existing analysis.npz is left exactly where it is; _carried_state_id
        # above has already decided whether it still pairs with these
        # transcripts. Deleting it here (which this used to do) threw away
        # embeddings that only the audio can regenerate every time diarization
        # merely had nothing new to contribute.
        _atomic_write_text(
            json_path,
            json.dumps({"transcripts": transcripts, STATE_ID_KEY: state_id},
                       ensure_ascii=False),
        )
    except Exception as exc:
        log.warning("could not save analysis state for %s: %s", meeting_dir.name, exc)


def _load_analysis_cache(npz_path, keys, json_state_id):
    """{track: (windows, embeddings)} from analysis.npz, or {} if unusable.

    Unusable means any of: the file is missing; it is corrupt (a truncated or
    half-written archive from before the writes were made atomic, or a bad
    disk); it was written by different embedding code; or it does not belong to
    the transcripts alongside it. NEVER raises — every rejection just falls
    through to re-embedding from the WAV, which is slower but always correct.
    """
    if not npz_path.exists():
        return {}
    try:
        with np.load(npz_path) as npz:
            # A cache is only reusable if it was produced by the embedding
            # behaviour this build runs. Mixing them would let the recluster
            # path answer from old embeddings while Reprocess answers from new
            # ones. Caches predate the marker, and were written by EMBED_VERSION
            # 1's behaviour, so a missing marker reads as 1 rather than as
            # "unknown" — otherwise every existing meeting would needlessly
            # re-embed on its next recluster.
            cached_version = (
                int(npz["embed_version"]) if "embed_version" in npz else 1
            )
            if cached_version != diarization.EMBED_VERSION:
                log.info(
                    "%s: analysis.npz is embed_version %s, code is %s — recomputing",
                    npz_path.parent.name, cached_version, diarization.EMBED_VERSION,
                )
                return {}
            # …and only if it is the cache written for THESE transcripts. Absent
            # from both files means an old pair written together before the
            # token existed, which is trusted exactly as it was before. Absent
            # from one but not the other means the two were written by different
            # saves — a crash landed between them — and the pair is broken.
            npz_state_id = (
                npz[STATE_ID_KEY].item() if STATE_ID_KEY in npz else None
            )
            if npz_state_id != json_state_id:
                log.info(
                    "%s: analysis.npz belongs to a different save than "
                    "analysis.json — recomputing", npz_path.parent.name,
                )
                return {}
            # Neural turns ride along only when written by the engine version
            # this build runs; a mismatch drops the turns, never the
            # embeddings — classic attribution remains fully served.
            neural_ok = (
                "neural_version" in npz
                and int(npz["neural_version"]) == diarization_neural.NEURAL_VERSION
            )
            cache = {}
            for key in keys:
                if f"{key}_windows" in npz and f"{key}_embeddings" in npz:
                    turns = (
                        npz[f"{key}_neural_turns"]
                        if neural_ok and f"{key}_neural_turns" in npz else None
                    )
                    cache[key] = (npz[f"{key}_windows"],
                                  npz[f"{key}_embeddings"], turns)
            return cache
    except (zipfile.BadZipFile, ValueError, KeyError, OSError, EOFError) as exc:
        # A corrupt cache must never cost the user their transcript: the arrays
        # are a speed optimisation and can always be recomputed from the WAV.
        log.warning("%s: analysis.npz unreadable (%s) — recomputing",
                    npz_path.parent.name, exc)
        return {}


# --- Publishing a run without reverting the edits made while it ran ------------
# Both entry points below read meeting.json once, then spend anywhere from a
# fraction of a second (recluster off a warm cache) to many minutes (a full
# transcription; or a recluster that hits the mic-only fallback, which runs a
# complete ECAPA embedding pass inside the request) computing a result FROM THAT
# SNAPSHOT. app.py does not block its editing endpoints meanwhile: POST /title
# and POST /speakers accept the change, return 200 and write meeting.json while
# we are still working, and the sync push does the same.
#
# So the snapshot is never written back as a document. Only the keys the run
# actually recomputed are merged into whatever is on disk AT THE MOMENT OF THE
# WRITE — the discipline summarize._store_summary and tidy.tidy_meeting already
# use. Everything else (title, summary, tidied, notes, sync state, calendar
# data) is carried over from disk untouched, whether or not this build even
# knows the key exists.
#
# ABSENCE IS A VALUE. A key the run deliberately REMOVED — "diarization_mode"
# when the fallback does not engage, "error" once processing succeeds — has to
# be removed from the on-disk document too, so the owned-key lists are applied
# as "copy if present, delete if not" rather than dict.update().
_LABELLING_KEYS = ("speakers", "turns", "stats", "diarization_mode", "diarizer")
_RECLUSTER_KEYS = _LABELLING_KEYS + (
    "expected_speakers", "speaker_count_source", "speaker_count_basis",
    "warnings", "status",
)
_PROCESS_KEYS = _LABELLING_KEYS + (
    "warnings", "status", "languages", "processing", "error",
)


def _live_meta(meta_path, meta):
    """meeting.json as it stands on disk RIGHT NOW.

    Falls back to our own snapshot when the file cannot be parsed: ours is a
    complete, valid document that contains the transcript, so publishing it over
    an unreadable one is the repair rather than the damage. A missing or
    unreadable FILE is different — the meeting may have been deleted under us,
    and inventing it again would resurrect a folder the user threw away.
    """
    try:
        latest = json.loads(meta_path.read_text(encoding="utf-8"))
    except ValueError:
        log.warning("%s: meeting.json is not valid JSON — rewriting it",
                    meta_path.parent.name)
        return dict(meta)
    except OSError as exc:
        raise RuntimeError(f"Could not save the result: {exc}") from exc
    if not isinstance(latest, dict):
        log.warning("%s: meeting.json is not a JSON object — rewriting it",
                    meta_path.parent.name)
        return dict(meta)
    return latest


def _keep_live_renames(speakers, snapshot_speakers, live_speakers):
    """Let a rename that landed WHILE we ran survive this run's speaker map.

    _label_and_assemble builds `speakers` from the SNAPSHOT's names, so a rename
    the user made mid-run is not in it and would be overwritten by the older
    name. Restoring it is only safe where the label still points at the same
    voice, and this run says which labels those are: exactly the ones whose name
    it carried over from the snapshot unchanged. Where a re-cluster changed the
    speaker set it reset the names on purpose — key "s1" may now be a different
    person and moving "Alex" onto a stranger is worse than asking for a rename
    (see _label_and_assemble) — so those labels are left as this run made them.
    """
    for label, name in list(speakers.items()):
        prior = snapshot_speakers.get(label)
        live = live_speakers.get(label)
        if prior is None or live is None:
            continue          # label is new this run: nothing was renamed under it
        if name != prior:
            continue          # this run reassigned the label: different voice
        if live != prior:
            speakers[label] = live


def _commit_meta(meta_path, meta, latest, owned, snapshot_speakers):
    """Merge the keys this run owns into `latest` and publish it atomically.

    `latest` must come from _live_meta and should be read as late as possible:
    everything written to meeting.json before that read survives, everything
    after it is still lost. The remaining window is the microseconds between the
    read and the os.replace, instead of the whole duration of the run.
    """
    if "speakers" in owned and isinstance(meta.get("speakers"), dict):
        _keep_live_renames(meta["speakers"], snapshot_speakers or {},
                           latest.get("speakers") or {})
    for key in owned:
        if key in meta:
            latest[key] = meta[key]
        else:
            latest.pop(key, None)
    # meeting.json holds the only copy of the transcript, and this write races
    # app.py's own writer as well as every reader. See _atomic_write.
    try:
        _atomic_write_text(meta_path, json.dumps(latest, ensure_ascii=False, indent=1))
    except OSError as exc:  # e.g. the meeting was deleted while we worked
        raise RuntimeError(f"Could not save the result: {exc}") from exc
    # Hand back what is actually on disk, so a caller that goes on to render the
    # meeting (app.py writes transcript.md from this) uses the live title and
    # names rather than our stale snapshot's.
    return latest


def recluster_meeting(meeting_dir, expected_speakers, progress_cb=lambda msg: None):
    """Re-run speaker clustering from saved analysis state — no transcription.

    expected_speakers: int forces the count (online mode: other speakers on
    the call; in-person: total speakers; under the mic-only fallback: every
    voice on the mic, the user included), None re-runs auto-detection.
    Auto re-detection answers well under a second from the cache; a typed
    count may additionally re-derive neural speaker turns from the audio
    (a few seconds on a long meeting), because a human-chosen count plus
    the neural attributor is the measured rescue for audio the automatic
    engines under-hear.

    Because that number means "besides you" in one of those cases and "you
    included" in the others, which one the caller meant is recorded alongside it
    as meta["speaker_count_basis"] — see SPEAKER_COUNT_OTHERS. It is derived
    from the diarization_mode ON DISK, i.e. the result the user was looking at
    when they picked, not from whatever this run concludes.
    """
    meeting_dir = Path(meeting_dir)
    meta_path = meeting_dir / "meeting.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    # The names as they were when we started. _keep_live_renames compares
    # against these to tell "the user renamed this speaker while we ran" from
    # "this run assigned the label a different name".
    snapshot_speakers = dict(meta.get("speakers") or {})
    analysis_path = meeting_dir / ANALYSIS_JSON
    if not analysis_path.exists():
        raise RuntimeError(
            "No saved voice analysis for this meeting — press Reprocess once, "
            "then the speaker count can be adjusted instantly."
        )
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    transcripts = analysis["transcripts"]

    precomputed = _load_analysis_cache(
        meeting_dir / ANALYSIS_NPZ, list(transcripts), analysis.get(STATE_ID_KEY)
    )

    cfg = load_config()
    # WHICH QUESTION WAS ON SCREEN decides what the number means, so the basis
    # is read off the mode the user was looking at — meta as loaded, before
    # _label_and_assemble below possibly changes diarization_mode under it.
    basis = _speaker_count_basis(meta)
    meta["expected_speakers"] = expected_speakers
    # This path is only ever reached from the speaker-count control, so the
    # number (or its absence) is a human's judgement about the finished
    # recording, not app.py's calendar guess. Record which, so the mic-only
    # fallback knows whether it may act on it — and so the answer survives a
    # later Reprocess, which reads expected_speakers back out of meeting.json.
    if expected_speakers:
        meta["speaker_count_source"] = SPEAKER_COUNT_USER
        meta["speaker_count_basis"] = basis
    else:
        meta.pop("speaker_count_source", None)
        meta.pop("speaker_count_basis", None)
    progress_cb("Re-clustering speakers…")
    # Keep a pristine copy: _label_and_assemble mutates the segments it labels
    # (it strips "words"), and those are the very dicts loaded from analysis.json.
    saved_transcripts = json.loads(json.dumps(transcripts))
    collect = {}
    label_warnings = _label_and_assemble(
        meeting_dir, meta, transcripts, cfg, expected_speakers, progress_cb,
        precomputed=precomputed, collect=collect, hint=_speaker_count_hint(meta),
        # Auto recluster promises sub-second answers, so it only ever reuses
        # cached neural turns. A recluster where the user TYPED a count is an
        # explicit act worth a few seconds: the neural engine re-derives
        # turns at that count, which is the measured rescue for audio the
        # auto counts under-hear (see _neural_refine's user_forced note).
        allow_neural_run=bool(expected_speakers),
    )
    # Any track that had to be embedded here (the mic-only fallback hits a mic
    # track that no earlier run ever cached) is persisted, so the next dropdown
    # change reuses it instead of paying for an ECAPA load and a full WAV pass
    # inside the request again.
    if any(key not in precomputed for key in collect):
        # Carry over cached tracks this run did not re-embed, so persisting the
        # newly embedded one never impoverishes the npz.
        merged = {}
        for key, entry in precomputed.items():
            state = {"windows": entry[0], "embeddings": entry[1]}
            if len(entry) > 2 and entry[2] is not None:
                state["neural_turns"] = np.asarray(entry[2]).tolist()
            merged[key] = state
        for key, state in collect.items():
            # A recluster whose count didn't match the cached turns keeps
            # them ON DISK anyway: they are versioned and count-gated at
            # reuse, and the next Auto recluster may match them again.
            cached_turns = (merged.get(key) or {}).get("neural_turns")
            if cached_turns and not state.get("neural_turns"):
                state = dict(state, neural_turns=cached_turns)
            merged[key] = state
        _save_analysis_state(meeting_dir, saved_transcripts, merged)
    # Read meeting.json again, as late as possible: a rename or a retitle that
    # app.py accepted while this run was working is in THIS document and not in
    # the snapshot above. See _commit_meta.
    latest = _live_meta(meta_path, meta)
    # Only the labelling warnings, because only the labelling step just re-ran:
    # the echo warning in PIPELINE_WARNING_PREFIXES is produced during
    # transcription and would be lost, not refreshed, if it were dropped here.
    # Filtered from the LIVE list so a warning added meanwhile is carried over.
    kept = [
        w for w in (latest.get("warnings") or [])
        if not w.startswith(DIARIZATION_WARNING_PREFIX)
    ]
    meta["warnings"] = kept + label_warnings
    meta["status"] = "done"
    return _commit_meta(meta_path, meta, latest, _RECLUSTER_KEYS, snapshot_speakers)


# "get hub" -> "GitHub" etc. Compiled once; word-boundary, case-insensitive.
_ALIAS_RES = [
    (re.compile(rf"\b{re.escape(wrong)}\b", re.IGNORECASE), right)
    for wrong, right in tech_vocabulary.ALIASES.items()
]


def _fix_known_mishearings(segments):
    """Conservative post-pass: repair unambiguous multi-word mishearings of
    tech terms in the batch transcript (segment text only)."""
    fixed = 0
    for seg in segments:
        text = seg["text"]
        for pattern, right in _ALIAS_RES:
            text, n = pattern.subn(right, text)
            fixed += n
        seg["text"] = text
    if fixed:
        log.info("fixed %d known mishearing(s)", fixed)


def process_meeting(meeting_dir, progress_cb=lambda msg: None):
    """Read meeting.json + WAVs in meeting_dir, write back the transcript."""
    meeting_dir = Path(meeting_dir)
    meta_path = meeting_dir / "meeting.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    cfg = dict(load_config())
    # Per-meeting language override (chosen on the record form) beats config.
    if meta.get("language"):
        cfg["language"] = meta["language"]
    # Bias recognition toward the user's vocabulary, attendee names, and the
    # built-in tech-term list ("GitHub", "Kubernetes", …) — proper names and
    # jargon are the recognizer's biggest error class.
    cfg["_context_strings"] = tech_vocabulary.merge_context(
        cfg.get("vocabulary"),
        (meta.get("calendar_event") or {}).get("names"),
    )
    started = time.time()
    mode = meta.get("mode", "online")
    expected = meta.get("expected_speakers") or None
    # The names as they were when we started — see _keep_live_renames.
    snapshot_speakers = dict(meta.get("speakers") or {})
    # Warnings this pipeline generates itself, rebuilt from scratch on every
    # run. The recorder's warnings are NOT carried in this list: they are merged
    # from the on-disk document at the end (below), because a run that lasts
    # minutes must not publish a minutes-old copy of anything it did not compute.
    echo_warnings = []

    # --- 1. Transcribe each track -------------------------------------------------
    transcripts = {}
    languages = {}
    for key in ("mic", "system"):
        track = meta.get("tracks", {}).get(key)
        if not track:
            continue
        path = meeting_dir / track["file"]
        if not path.exists() or _track_duration(path) < 0.3:
            continue
        progress_cb(f"Transcribing {key} track…")
        segs, lang = transcribe_track(path, key, cfg, progress_cb)
        _apply_offset(segs, float(track.get("start_offset") or 0.0))
        _fix_known_mishearings(segs)
        transcripts[key] = segs
        languages[key] = lang

    # --- 2. Echo cleanup ------------------------------------------------------------
    # Gated on the EVIDENCE (both tracks carry speech), not on the declared mode.
    # A hybrid meeting is recorded in-person mode with remote participants on the
    # call, so it has both a room mic and a live system track; gating on
    # mode == "online" left exactly that case uncleaned, and the remote voice was
    # published twice, once as a clean "Remote N" and once as a degraded mic
    # speaker. A pure in-person meeting has no system speech at all, so it takes
    # the same path it always did.
    if transcripts.get("mic") and transcripts.get("system"):
        kept, dropped, trimmed = drop_echo(transcripts["mic"], transcripts["system"])
        transcripts["mic"] = kept
        if dropped or trimmed:
            parts = []
            if dropped:
                parts.append(f"removed {dropped} echo segment(s)")
            if trimmed:
                parts.append(f"trimmed echoed words from {trimmed} segment(s)")
            echo_warnings.append(
                f"{ECHO_WARNING_PREFIX}: " + " and ".join(parts)
                + " (tip: headphones avoid this entirely)."
            )
        # The other direction: the user's voice coming back on the call audio.
        # On the mic segments drop_echo KEPT — see drop_self_echo.
        kept_sys, self_dropped = drop_self_echo(transcripts["system"], transcripts["mic"])
        transcripts["system"] = kept_sys
        if self_dropped:
            echo_warnings.append(
                f"{SELF_ECHO_WARNING_PREFIX}: removed {self_dropped} echoed "
                "segment(s) from the call audio so it would not be labelled as "
                "another speaker (the other side's echo cancellation let your "
                "voice through)."
            )

    # --- 3+4. Speaker labelling and assembly ---------------------------------------
    # Keep a pristine copy of the transcripts plus the voice embeddings so the
    # speaker count can be changed later without re-transcribing.
    saved_transcripts = json.loads(json.dumps(transcripts))
    collect = {}
    label_warnings = _label_and_assemble(
        meeting_dir, meta, transcripts, cfg, expected, progress_cb, collect=collect,
        hint=_speaker_count_hint(meta),
    )
    _save_analysis_state(meeting_dir, saved_transcripts, collect)

    # --- Owner voice print ---------------------------------------------------------
    # A HEALTHY online call — the mic was "You" by construction and no echo
    # cleanup fired, so nothing of the far end is in those windows — is the
    # sample of the user's own voice that later in-person and mic-only
    # meetings are matched against (voice_profiles.recognize_owner). A call
    # that needed echo removal is not trusted: what escaped the word matcher
    # would be enrolled as the user.
    if (mode == "online" and cfg.get("voice_profiles", True)
            and not echo_warnings and not meta.get("diarization_mode")
            and "you" in (meta.get("speakers") or {}) and collect.get("mic")):
        voice_profiles.enroll_owner_from_state(
            meta.get("id") or meeting_dir.name, collect["mic"], meta.get("turns"))

    # Read meeting.json again, as late as possible. Transcription takes minutes,
    # and the browser can retitle the meeting or rename a speaker throughout;
    # those edits are in THIS document and not in the snapshot taken at the top.
    # See _commit_meta.
    latest = _live_meta(meta_path, meta)
    # Recorder warnings persist — taken from the live document, so one written
    # after we started is kept too. Ours are rebuilt fresh each run, so any
    # earlier copy is dropped first and Reprocess cannot stack duplicates.
    carried = [
        w for w in (latest.get("warnings") or [])
        if not w.startswith(PIPELINE_WARNING_PREFIXES)
    ]
    meta.update(
        {
            "status": "done",
            "warnings": carried + echo_warnings + label_warnings,
            "languages": languages,
            "processing": {
                "model": resolve_model(cfg, pick_backend(cfg)),
                "backend": pick_backend(cfg),
                "seconds": round(time.time() - started, 1),
                "mode": mode,
            },
        }
    )
    meta.pop("error", None)  # absence is published too — see _PROCESS_KEYS
    return _commit_meta(meta_path, meta, latest, _PROCESS_KEYS, snapshot_speakers)
