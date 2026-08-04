"""Shared paths and user configuration for MeetingScribe."""

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("meetingscribe.config")

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
# The next two are read ONCE, at huggingface_hub's own import, into
# huggingface_hub.constants — which is why they are set here, in the module
# every entry point imports first, and not next to the download helpers below.
#
# Per-read socket timeout for model downloads. 20 s is generous for a
# slow-but-alive link; a link that is alive but pointless is caught by the
# stall check in _DownloadSession instead.
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "20")
# Take the plain HTTP transfer path, not Xet. MEASURED on this machine, same
# 83 MB checkpoint, cold cache, two runs each:
#     xet    22.2 s / 19.1 s   2 progress callbacks   worst silence 21.6 s
#     http   19.1 s / 15.9 s   8 progress callbacks   worst silence  4.2 s
# hf_xet downloads into its own chunk cache and reconstructs the file at the
# very end, so it reports the WHOLE file in a single callback and writes
# nothing to the destination until it is done: there is no hook, and no bytes
# on disk, to show progress from. On the 83 MB speaker model that is 20 s of a
# frozen number; on the 2.4 GB speech model it would be the entire download.
# The plain path emits one update per 10 MB chunk. It was not slower here, and
# Xet's advantage (chunk dedup across revisions) is worth nothing to a user who
# downloads each checkpoint exactly once. Set HF_HUB_DISABLE_XET=0 to opt back
# in and lose the progress reporting.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

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
    # Summary + Ask engine. "apple" (default) keeps everything on-device via
    # Apple Intelligence (shallower, but 100% offline). "claude", "codex",
    # "gemini" or "copilot" run on the user's own AI CLI of that name (best
    # quality; transcript TEXT is sent to that provider's cloud, never audio —
    # the registry lives in ai_cli.py). When the chosen CLI is missing or
    # signed out, BOTH summaries and Ask fall back to Apple Intelligence
    # (summarize.summarize_meeting, ask.answer_question) rather than handing
    # back setup instructions.
    #
    # WHY THIS DEFAULT IS "apple" AND NOT "claude", which it was until the
    # fallback above existed on both sides:
    #  * It is the only default that matches what the app promises before it
    #    is used. Onboarding says summaries run "through Apple Intelligence —
    #    nothing goes to the cloud" and calls Claude Code "optional, not
    #    needed"; a default that sends the transcript to a cloud CLI the user
    #    never asked for contradicts both, and this app's whole claim is that
    #    the meeting stays on the Mac unless the user says otherwise.
    #  * On a machine WITHOUT the CLI it is the difference between a working
    #    feature and homework. That is not what fixes it, though — the
    #    fallbacks are. A default cannot help a user whose config names a CLI
    #    they later uninstall, or who picks Claude and then signs out; only the
    #    engine falling back at the point of use covers those, which is why
    #    the fallback landed first and this line second.
    #  * Nothing is taken away: a Mac with a CLI installed says so in Settings
    #    (which recommends Claude when it finds it), and the error a Mac with
    #    Apple Intelligence switched off gets names the installed CLI as the
    #    way out — see app._on_device_unavailable.
    "summary_engine": "apple",
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


# --- Model downloads ----------------------------------------------------------
# A fresh install fetches ~2.5 GB before its first meeting can be processed: the
# Parakeet speech model (2.4 GB) in pipeline._get_parakeet and the ECAPA speaker
# model (89 MB) in diarization._load_embedder. Both used to block inside a
# library call that reports nothing, so the UI held one static line for however
# long the download took and "working" looked exactly like "hung" — the bug this
# section exists to kill. There was no wall-clock bound either, so a link that
# died mid-file could hold the pipeline forever.
#
# THE MECHANISM: hf_hub_download's public `tqdm_class` argument, with the files
# fetched HERE rather than by the model library. It is the one hook that covers
# both loaders, because every download route in huggingface_hub 1.x builds its
# progress bar through utils.tqdm._get_progress_bar_context(tqdm_class=...).
# `_create_progress_bar` passes a class that does not subclass its own tqdm
# straight through, without injecting `disable`, so a duck-typed object sees
# every byte; an hf-tqdm SUBCLASS would instead be constructed with
# disable=True off a TTY, and tqdm.update() returns early when disabled, which
# is why _ProgressBar below inherits from nothing. The hook only reports
# usefully on the plain HTTP transfer, hence HF_HUB_DISABLE_XET above.
#
# Fetching the files ourselves (into exactly the directory the library will look
# in, so its own loader then finds everything cached) also buys the two things a
# tqdm hook alone cannot: bounded retries around the whole transfer, and a real
# byte total to count against, from the same metadata call the download makes.

# How many times a transfer is attempted before the user is told it failed.
# Partial files survive between attempts (huggingface_hub resumes from the
# .incomplete file), so a retry costs only what is left.
DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_BACKOFF_S = (3.0, 10.0)

# The stall guard. huggingface_hub retries a dropped read internally and RESETS
# its retry budget on every byte that arrives, so a connection trickling one
# packet a minute never dies on its own — the "it just kept processing and never
# stopped" report. Less than 64 KB in 2 minutes is 0.5 KB/s, at which the speech
# model would need 55 days, so treating it as dead cannot misfire on a link that
# is merely slow.
_STALL_WINDOW_S = 120.0
_STALL_MIN_BYTES = 64 * 1024

# Progress messages are throttled to this, so a 2.4 GB download costs the UI a
# couple of thousand updates rather than one per 10 MB chunk.
_PROGRESS_EVERY_S = 0.7

# Errors where retrying is pointless: the file, repo or revision is not there,
# or the repo is gated. Anything else is treated as transient — including
# LocalEntryNotFoundError, which means "the Hub was unreachable and this is not
# cached", i.e. exactly the connection problem a retry is for.
_FATAL_HF_ERRORS = (
    "EntryNotFoundError", "RepositoryNotFoundError", "RevisionNotFoundError",
    "GatedRepoError",
)


class ModelDownloadError(RuntimeError):
    """A model could not be downloaded. str(exc) is user-facing copy."""


class _DownloadStalled(RuntimeError):
    """Raised from inside the progress bar when the bytes stop moving."""


def human_bytes(n):
    """Bytes as download sizes are quoted: "2.4 GB", "340 MB", "89 MB"."""
    n = float(n or 0)
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.0f} MB"
    if n >= 1e3:
        return f"{n / 1e3:.0f} KB"
    return f"{int(n)} B"


def _hf_error_name(exc):
    return type(exc).__name__


def _is_missing_file(exc):
    return _hf_error_name(exc) == "EntryNotFoundError" or "404" in str(exc)


def _is_fatal(exc):
    return _hf_error_name(exc) in _FATAL_HF_ERRORS


class _DownloadSession:
    """Running byte count for one model's transfer, shared by every file's
    progress bar so the user sees one total and not five restarts."""

    def __init__(self, label, progress_cb, total, bytes_cb=None):
        self.label = label
        self.cb = progress_cb
        self.bytes_cb = bytes_cb
        self.total = float(total or 0)
        self.done = 0.0
        self._moved = 0.0
        self._checked_at = time.monotonic()
        self._emitted_at = 0.0

    def message(self):
        if self.total:
            pct = min(100, int(self.done / self.total * 100))
            return (f"Downloading {self.label}, {human_bytes(self.done)} of "
                    f"{human_bytes(self.total)} ({pct}%)…")
        return f"Downloading {self.label}, {human_bytes(self.done)} so far…"

    def reset_clock(self):
        self._moved = 0.0
        self._checked_at = time.monotonic()

    def restart(self):
        """Begin an attempt. The byte count goes back to zero because a resumed
        transfer re-reports what is already on disk: huggingface_hub builds the
        new bar with initial=resume_size, so carrying the previous attempt's
        total forward counts those bytes twice (measured: a 2.5 GB file
        finishing at "2.8 GB")."""
        self.done = 0.0
        self.reset_clock()

    def add(self, n):
        """Count n bytes. Raises _DownloadStalled when the link has gone quiet.

        The check lives here because this is the only code that runs while a
        transfer is in flight: it is called from huggingface_hub's own chunk
        loop, so raising aborts the read (leaving the .incomplete file for the
        next attempt to resume) instead of waiting for a timeout that a
        trickling connection never triggers.
        """
        now = time.monotonic()
        self.done = max(0.0, self.done + float(n or 0))
        self._moved += max(0.0, float(n or 0))
        if self._moved >= _STALL_MIN_BYTES:
            self._moved = 0.0
            self._checked_at = now
        elif now - self._checked_at > _STALL_WINDOW_S:
            waited = int(now - self._checked_at)
            self.reset_clock()
            raise _DownloadStalled(
                f"under {human_bytes(_STALL_MIN_BYTES)} moved in {waited} s")
        if now - self._emitted_at >= _PROGRESS_EVERY_S:
            self._emitted_at = now
            self.cb(self.message())
            if self.bytes_cb is not None:
                self.bytes_cb(int(self.done), int(self.total), self.label)


def _progress_bar_class(session):
    """A `tqdm_class` for hf_hub_download that reports into `session`.

    Deliberately NOT a tqdm subclass: huggingface_hub only injects `disable`
    (and `name`) when the class it is handed derives from its own tqdm, and a
    disabled tqdm returns from update() before doing anything, which is exactly
    the silence this whole section is fixing. The hub documents that a custom
    class need only "mimic the behaviour", so this implements the four calls it
    actually makes: construction with total/initial, update (which can be
    NEGATIVE when a resumed range request is rejected and the file restarts),
    close, and use as a context manager.
    """

    class _ProgressBar:
        def __init__(self, *args, **kwargs):
            initial = kwargs.get("initial") or 0
            if initial:
                session.add(initial)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def update(self, n=1):
            session.add(n)

        def close(self):
            pass

        def set_description(self, *args, **kwargs):
            pass

        def refresh(self, *args, **kwargs):
            pass

    return _ProgressBar


def _hf_plan(repo_id, filenames, **where):
    """[(filename, size, will_download)] for the files that EXIST in `repo_id`,
    or None when the Hub cannot be reached / this huggingface_hub has no dry
    run. One HEAD per file, the same metadata request the real download makes.

    Whether a file is in the repo at all is settled HERE, not by catching a 404
    during the transfer: a filename this module lists but a checkpoint no longer
    ships is a skip, while a 404 on a file the plan said was there is a genuine
    failure. Deciding it from the exception conflated the two, and a repository
    missing its weights reported a cheerful "Downloaded (0 B)".
    """
    from huggingface_hub import hf_hub_download

    rows = []
    for name in filenames:
        try:
            info = hf_hub_download(repo_id, name, dry_run=True, **where)
        except Exception as exc:  # noqa: BLE001 - absent file, or no network
            if _is_missing_file(exc):
                log.info("%s: %s is not in the repository, skipping", repo_id, name)
                continue
            log.debug("download plan for %s unavailable: %s", repo_id, exc)
            return None
        rows.append((name, int(getattr(info, "file_size", 0) or 0),
                     bool(getattr(info, "will_download", True))))
    return rows


def hf_download_plan(repo_id, filenames, **where):
    """(bytes still to fetch, bytes in total) for these files of `repo_id`.

    `where` is the destination as hf_hub_download takes it (`cache_dir=` or
    `local_dir=`) and must match what the model library will use, or the answer
    describes a different copy than the one that gets loaded.

    Returns (None, None) when the Hub cannot be reached, so a caller that only
    wants to show a number can degrade instead of failing.
    """
    rows = _hf_plan(repo_id, filenames, **where)
    if rows is None:
        return None, None
    return (sum(size for _n, size, wanted in rows if wanted),
            sum(size for _n, size, _w in rows))


def ensure_hf_files(repo_id, filenames, *, label, progress_cb=None,
                    bytes_cb=None, **where):
    """Put `filenames` on disk, reporting the megabytes as they land.

    `label` is user-facing ("the speech model"). `progress_cb` is the pipeline's
    existing one-string callback; None means report nothing. `bytes_cb` is the
    numeric form, bytes_cb(done_bytes, total_bytes, label), for a caller that
    draws its own bar rather than printing a sentence. `where` is the
    destination, exactly as the model library will ask for it.

    Returns the number of bytes downloaded (0 when everything was already
    cached, in which case nothing is reported at all — a cached load must stay
    as quiet as it is fast). Raises ModelDownloadError, whose message names the
    model, the size, the underlying failure and the fact that a retry resumes.
    """
    from huggingface_hub import hf_hub_download

    cb = progress_cb or (lambda msg: None)
    rows = _hf_plan(repo_id, filenames, **where)
    if rows is None:
        # No usable plan: offline, or a huggingface_hub without dry runs. Ask
        # for everything and let the transfer produce the real error. A 404 is
        # tolerated here only because nothing else can tell an absent optional
        # file from a broken repository without the plan.
        wanted, pending, strict = list(filenames), None, False
    else:
        if not rows:
            raise ModelDownloadError(
                f"Could not download {label}: none of the files it needs "
                f"({', '.join(filenames)}) are in {repo_id}. This build is "
                "asking for a checkpoint that no longer exists."
            )
        pending = sum(size for _n, size, w in rows if w)
        if pending == 0:
            return 0
        wanted = [name for name, _size, w in rows if w]
        strict = True

    session = _DownloadSession(label, cb, pending, bytes_cb)
    cb(session.message())
    bar = _progress_bar_class(session)
    last = None
    attempt = 0
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        session.restart()
        try:
            for name in wanted:
                try:
                    hf_hub_download(repo_id, name, tqdm_class=bar, **where)
                except Exception as exc:  # noqa: BLE001 - see `strict`
                    if not strict and _is_missing_file(exc):
                        continue
                    raise
            log.info("%s: downloaded %s", label, human_bytes(session.done))
            # Never leave the UI parked on the last throttled percentage.
            cb(f"Downloaded {label} ({human_bytes(session.done)}).")
            return int(session.done)
        except Exception as exc:  # noqa: BLE001 - re-raised as ModelDownloadError
            last = exc
            log.warning("%s: download attempt %d of %d failed: %s: %s",
                        label, attempt, DOWNLOAD_ATTEMPTS, _hf_error_name(exc), exc)
            if _is_fatal(exc) or attempt >= DOWNLOAD_ATTEMPTS:
                break
            wait = _DOWNLOAD_BACKOFF_S[min(attempt - 1, len(_DOWNLOAD_BACKOFF_S) - 1)]
            cb(f"Download of {label} was interrupted, retrying in {int(wait)} s "
               f"(attempt {attempt + 1} of {DOWNLOAD_ATTEMPTS})…")
            session.reset_clock()
            time.sleep(wait)

    size = f" ({human_bytes(pending)})" if pending else ""
    if _is_fatal(last):
        advice = ("Retrying will not help: the model host refused the request "
                  "for that file.")
    else:
        advice = ("Check your internet connection and try again. The part that "
                  "already arrived is kept, so a retry resumes where this "
                  "stopped.")
    detail = str(last).strip().rstrip(".") or _hf_error_name(last)
    raise ModelDownloadError(
        f"Could not download {label}{size} after {attempt} of "
        f"{DOWNLOAD_ATTEMPTS} attempts. Last error: {_hf_error_name(last)}: "
        f"{detail}. {advice}"
    ) from last
