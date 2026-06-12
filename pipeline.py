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
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from bisect import bisect_left, bisect_right
from pathlib import Path

import numpy as np

import diarization
import stats as stats_mod
from config import BASE_DIR, MODELS_DIR, load_config

log = logging.getLogger("meetingscribe.pipeline")

_WHISPER = None
_WHISPER_KEY = None

TURN_MERGE_GAP_S = 3.0

# Model label shown in the UI when whisper_model is "auto". Apple Speech runs
# on the Neural Engine (fastest, coolest); MLX uses the GPU; faster-whisper
# the CPU.
AUTO_MODEL = {
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

# Apple SpeechAnalyzer helper (macOS 26+). Source ships in tools/; the
# compiled binary is cached outside the synced project folder.
_APPLE_SRC = BASE_DIR / "tools" / "apple_transcribe.swift"
_APPLE_BIN = Path.home() / ".meetingscribe" / "bin" / "apple_transcribe"
_APPLE_TIMEOUT_S = 1800

# Map a bare language code to a default Apple locale; the helper also resolves
# equivalents, so this only needs the common cases.
_APPLE_LOCALES = {
    "en": "en-US", "hi": "hi-IN", "es": "es-ES", "fr": "fr-FR", "de": "de-DE",
    "it": "it-IT", "pt": "pt-BR", "ja": "ja-JP", "ko": "ko-KR", "zh": "zh-CN",
    "ar": "ar-SA", "ru": "ru-RU", "nl": "nl-NL",
}


def _macos_version():
    try:
        return tuple(int(x) for x in platform.mac_ver()[0].split(".")[:2])
    except (ValueError, IndexError):
        return (0, 0)


def _ensure_apple_binary():
    """Return the path to the compiled Apple helper, building it on demand.

    Returns None if the platform can't support it or compilation fails — the
    caller then falls back to a Whisper backend.
    """
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return None
    if _macos_version() < (26, 0):  # SpeechAnalyzer needs macOS 26+
        return None
    if not _APPLE_SRC.exists():
        return None
    if _APPLE_BIN.exists() and _APPLE_BIN.stat().st_mtime >= _APPLE_SRC.stat().st_mtime:
        return str(_APPLE_BIN)
    swiftc = shutil.which("swiftc") or "/usr/bin/swiftc"
    if not (shutil.which("swiftc") or os.path.exists("/usr/bin/swiftc")):
        return None
    try:
        _APPLE_BIN.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [swiftc, "-O", "-parse-as-library", str(_APPLE_SRC), "-o", str(_APPLE_BIN)],
            check=True, capture_output=True, text=True, timeout=300,
        )
        return str(_APPLE_BIN)
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("could not build Apple Speech helper: %s", exc)
        return None


def _mlx_available():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return False
    try:
        import mlx_whisper  # noqa: F401

        return True
    except ImportError:
        return False


def pick_backend(cfg):
    """Pick the transcription backend.

    "apple"  — Apple SpeechAnalyzer on the Neural Engine (macOS 26+): fastest,
               coolest, fully on-device. The default when available.
    "mlx"    — Whisper on the Apple GPU (Apple Silicon + mlx-whisper).
    "faster" — Whisper on the CPU (faster-whisper), the portable fallback.

    Config "whisper_backend" can force any of these.
    """
    backend = cfg.get("whisper_backend", "auto")
    if backend in ("apple", "mlx", "faster"):
        return backend
    if _ensure_apple_binary() is not None:
        return "apple"
    if _mlx_available():
        return "mlx"
    return "faster"


def resolve_model(cfg, backend):
    # Apple Speech has no selectable model variants, so whisper_model does not
    # apply to it. The Whisper backends honour an explicit model, else "auto".
    if backend == "apple":
        return AUTO_MODEL["apple"]
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


def _transcribe_mlx(path, label, cfg, progress_cb):
    """Whisper on the Apple GPU via mlx-whisper — fast and easy on the fans."""
    import mlx_whisper

    model = resolve_model(cfg, "mlx")
    progress_cb(f"Transcribing {label} on the Apple GPU ({model})…")
    # Decode the WAV ourselves (mlx-whisper would otherwise shell out to
    # ffmpeg, which most machines don't have). Whisper wants 16 kHz mono.
    audio = diarization.load_mono_16k(path)
    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=MLX_REPOS.get(model, model),
        language=cfg.get("language") or None,
        word_timestamps=True,
        condition_on_previous_text=False,
        hallucination_silence_threshold=2.0,
        verbose=None,
    )
    out = []
    dropped = 0
    for seg in result.get("segments", []):
        text = seg["text"].strip()
        if not text or not re.search(r"\w", text):
            continue
        if _is_hallucination(seg, audio):
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


def _transcribe_apple(path, label, cfg, progress_cb):
    """Apple SpeechAnalyzer on the Neural Engine — fast, cool, on-device."""
    binary = _ensure_apple_binary()
    if binary is None:
        raise RuntimeError("Apple Speech helper unavailable")
    locale = _apple_locale(cfg)
    progress_cb(f"Transcribing {label} with Apple Speech ({locale})…")
    proc = subprocess.run(
        [binary, str(path), locale],
        capture_output=True, text=True, timeout=_APPLE_TIMEOUT_S,
    )
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
    return out, data.get("language")


def transcribe_track(path, label, cfg, progress_cb):
    """Transcribe one WAV. Returns (segments, language). Tries the configured
    backend, then degrades gracefully (apple -> mlx -> faster-whisper)."""
    backend = pick_backend(cfg)
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


def _echo_containment(mic_tokens, window_tokens):
    """Fraction of the mic segment's words that also appear, in order, in the
    system-track words around it. ~1.0 means the mic segment is pure echo."""
    if not mic_tokens or not window_tokens:
        return 0.0
    sm = difflib.SequenceMatcher(None, mic_tokens, window_tokens, autojunk=False)
    matched = sum(block.size for block in sm.get_matching_blocks())
    return matched / len(mic_tokens)


def drop_echo(mic_segs, sys_segs):
    """Remove mic segments that duplicate overlapping system-track speech.

    Remote voices can leak from the speakers back into the mic when the user
    is not wearing headphones; the system track is the clean copy, so the mic
    duplicate is dropped.

    Matching is done against ALL system-track words inside the mic segment's
    time window (not segment-by-segment): Whisper rarely puts the echo and
    the original on the same segment boundaries, so per-segment comparison
    misses most duplicates that span two system segments.
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

    kept, dropped = [], 0
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
        threshold = 0.7 if len(mic_tokens) >= 4 else 0.999
        if ratio >= threshold:
            dropped += 1
        else:
            kept.append(m)
    return kept, dropped


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


def _label_and_assemble(meeting_dir, meta, transcripts, cfg, expected, progress_cb,
                        precomputed=None, collect=None):
    """Steps 3+4: cluster voices into speakers and build the final transcript.

    Mutates meta (speakers/turns/stats) and the transcript segments. Returns
    the labelling warnings. precomputed maps track -> (windows, embeddings)
    to skip the audio embedding pass; collect (dict) gathers computed
    windows/embeddings per track so the caller can persist them.
    """
    mode = meta.get("mode", "online")
    threshold = float(cfg["diarization_threshold"])
    warnings = []

    def diarize(key, n_speakers, prefix, name_fmt, start_index):
        """Cluster one track's voices; returns labelled segments + speaker map."""
        segs = transcripts.get(key) or []
        if not segs:
            return [], {}
        track_file = meeting_dir / meta["tracks"][key]["file"]
        state = {} if collect is not None else None
        try:
            new_segs, n_found = diarization.diarize_track(
                track_file,
                segs,
                n_speakers=n_speakers,
                threshold=threshold,
                progress_cb=progress_cb,
                precomputed=(precomputed or {}).get(key),
                state=state,
            )
        except Exception as exc:
            warnings.append(f"Diarization failed on {key} track ({exc}); using one label.")
            new_segs = [dict(s, speaker_idx=0) for s in segs]
            n_found = 1
        if collect is not None and state and "embeddings" in state:
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

    labelled = []
    speakers = {}
    if mode == "online":
        mic_segs = transcripts.get("mic") or []
        if mic_segs:
            speakers["you"] = "You"
            for seg in mic_segs:
                seg["speaker"] = "you"
                seg["track"] = "mic"
            labelled.extend(mic_segs)
        if transcripts.get("system"):
            progress_cb("Identifying speakers…")
            sys_segs, sys_speakers = diarize("system", expected, "s", "Speaker {}", 1)
            labelled.extend(sys_segs)
            speakers.update(sys_speakers)
    else:  # in-person: everyone shares the mic
        if transcripts.get("mic"):
            progress_cb("Identifying speakers…")
            mic_segs, mic_speakers = diarize("mic", expected, "s", "Speaker {}", 1)
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
    # key s1 can be a different *voice* than before, and carrying "Jess"
    # over to the wrong person is worse than asking for a rename.
    old_names = meta.get("speakers") or {}
    if set(speakers) == set(old_names):
        speakers.update(old_names)
    elif "you" in speakers and "you" in old_names:
        speakers["you"] = old_names["you"]

    meta["speakers"] = speakers
    meta["turns"] = turns
    meta["stats"] = meeting_stats
    return warnings


def _save_analysis_state(meeting_dir, transcripts, collect):
    """Persist transcripts + voice embeddings so speakers can be re-clustered
    later in under a second. Best effort — recluster just stays unavailable."""
    try:
        (meeting_dir / ANALYSIS_JSON).write_text(
            json.dumps({"transcripts": transcripts}, ensure_ascii=False),
            encoding="utf-8",
        )
        arrays = {}
        for key, state in collect.items():
            arrays[f"{key}_windows"] = np.asarray(state["windows"], dtype=np.float64)
            arrays[f"{key}_embeddings"] = np.asarray(state["embeddings"], dtype=np.float32)
        if arrays:
            np.savez_compressed(meeting_dir / ANALYSIS_NPZ, **arrays)
    except Exception as exc:
        log.warning("could not save analysis state for %s: %s", meeting_dir.name, exc)


def recluster_meeting(meeting_dir, expected_speakers, progress_cb=lambda msg: None):
    """Re-run speaker clustering from saved analysis state — no transcription.

    expected_speakers: int forces the count (online mode: other speakers on
    the call; in-person: total speakers), None re-runs auto-detection.
    Takes well under a second for a typical meeting.
    """
    meeting_dir = Path(meeting_dir)
    meta_path = meeting_dir / "meeting.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    analysis_path = meeting_dir / ANALYSIS_JSON
    if not analysis_path.exists():
        raise RuntimeError(
            "No saved voice analysis for this meeting — press Reprocess once, "
            "then the speaker count can be adjusted instantly."
        )
    transcripts = json.loads(analysis_path.read_text(encoding="utf-8"))["transcripts"]

    precomputed = {}
    npz_path = meeting_dir / ANALYSIS_NPZ
    if npz_path.exists():
        with np.load(npz_path) as npz:
            for key in list(transcripts):
                if f"{key}_windows" in npz and f"{key}_embeddings" in npz:
                    precomputed[key] = (npz[f"{key}_windows"], npz[f"{key}_embeddings"])

    cfg = load_config()
    meta["expected_speakers"] = expected_speakers
    progress_cb("Re-clustering speakers…")
    label_warnings = _label_and_assemble(
        meeting_dir, meta, transcripts, cfg, expected_speakers, progress_cb,
        precomputed=precomputed,
    )
    kept = [w for w in meta.get("warnings", []) if not w.startswith("Diarization")]
    meta["warnings"] = kept + label_warnings
    meta["status"] = "done"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def process_meeting(meeting_dir, progress_cb=lambda msg: None):
    """Read meeting.json + WAVs in meeting_dir, write back the transcript."""
    meeting_dir = Path(meeting_dir)
    meta_path = meeting_dir / "meeting.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    cfg = load_config()
    started = time.time()
    mode = meta.get("mode", "online")
    expected = meta.get("expected_speakers") or None
    # Recorder warnings persist; warnings this pipeline generates are rebuilt
    # fresh each run so reprocessing doesn't stack duplicates.
    warnings = [
        w for w in meta.get("warnings", [])
        if not w.startswith(("Diarization", "Removed "))
    ]

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
        transcripts[key] = segs
        languages[key] = lang

    # --- 2. Echo cleanup (online mode) --------------------------------------------
    if mode == "online" and transcripts.get("mic") and transcripts.get("system"):
        kept, dropped = drop_echo(transcripts["mic"], transcripts["system"])
        transcripts["mic"] = kept
        if dropped:
            warnings.append(
                f"Removed {dropped} mic segment(s) that were echo of the meeting audio "
                "(tip: headphones avoid this entirely)."
            )

    # --- 3+4. Speaker labelling and assembly ---------------------------------------
    # Keep a pristine copy of the transcripts plus the voice embeddings so the
    # speaker count can be changed later without re-transcribing.
    saved_transcripts = json.loads(json.dumps(transcripts))
    collect = {}
    label_warnings = _label_and_assemble(
        meeting_dir, meta, transcripts, cfg, expected, progress_cb, collect=collect
    )
    _save_analysis_state(meeting_dir, saved_transcripts, collect)

    meta.update(
        {
            "status": "done",
            "warnings": warnings + label_warnings,
            "languages": languages,
            "processing": {
                "model": resolve_model(cfg, pick_backend(cfg)),
                "backend": pick_backend(cfg),
                "seconds": round(time.time() - started, 1),
                "mode": mode,
            },
        }
    )
    meta.pop("error", None)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta
