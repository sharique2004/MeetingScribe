"""Lossless FLAC archival of finished recordings.

WHY. Recording writes uncompressed 16-bit WAV — the right choice while the
meeting is live (dumb, crash-tolerant, appendable) and an expensive one
afterwards: ~1.04 GB per meeting-hour, kept forever by policy. FLAC stores the
identical samples at a measured 4.4x smaller on this corpus (mic median 3.4x),
decodes at thousands of times realtime through the same libsndfile the whole
app already reads audio with, and is bit-perfectly reversible. Compressing a
FINISHED meeting is therefore not retention and not cleanup: nothing is
removed until the encoded copy has been decoded back and compared sample for
sample against the original.

WHAT THE CALLER OWNS. This module only turns `<key>.wav` into a verified
`<key>.flac` beside it (and cleans up afterwards). The meeting.json commit —
flipping `tracks[key]["file"]` to the new name — is app.py's job, under its
JOB_LOCK, between compress_track() and remove_wav(). That ordering is the
crash-safety story:

    compress_track()   -> both files exist, meta still names the .wav
    meta commit        -> both files exist, meta names the .flac
    remove_wav()       -> done

Killed before the commit: the sweep re-encodes to a fresh temp and re-verifies
(idempotent, wasteful once, harmless). Killed after the commit but before
remove_wav(): the WAV is "stranded" — remove_stranded_wav() deletes it only
after verifying the pair AGAIN. At no instant does a single unverified copy of
the audio exist, and every temp file lives in the meeting's own directory with
a unique mkstemp name (the same discipline as pipeline._atomic_write, for the
same two-writers reason).

The recorder is untouched: recording always writes WAV. Only meetings whose
processing reached "done" are ever handed to this module.
"""

import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger("meetingscribe.audio_archive")

# One decoded block held at a time per file: 1 << 22 frames is ~16 MB of int16
# stereo, the same bounded-memory discipline as pipeline.load_mono_16k.
_BLOCK_FRAMES = 1 << 22

# libsndfile's FLAC compression knob, 0.0 (fastest) .. 1.0 (smallest). 0.8
# measured ~1,800x realtime on this machine; the last 0.2 buys ~1% of size for
# a large slowdown.
_COMPRESSION_LEVEL = 0.8

# An orphaned temp younger than this may belong to a compressor that is still
# running; older ones are wreckage from a kill and safe to sweep.
_ORPHAN_MIN_AGE_S = 3600.0


def verify_pair(a_path, b_path):
    """True iff both files decode to the identical int16 sample stream.

    A decode comparison, deliberately not a checksum of the encoder's own
    output: it re-reads what a future reader will actually get.
    """
    import numpy as np
    import soundfile as sf

    try:
        ia, ib = sf.info(str(a_path)), sf.info(str(b_path))
        if (ia.frames, ia.samplerate, ia.channels) != (ib.frames, ib.samplerate, ib.channels):
            return False
        with sf.SoundFile(str(a_path)) as fa, sf.SoundFile(str(b_path)) as fb:
            while True:
                ba = fa.read(_BLOCK_FRAMES, dtype="int16", always_2d=True)
                bb = fb.read(_BLOCK_FRAMES, dtype="int16", always_2d=True)
                if len(ba) != len(bb) or not np.array_equal(ba, bb):
                    return False
                if len(ba) < _BLOCK_FRAMES:
                    return True
    except Exception as exc:
        log.warning("verify_pair(%s, %s) failed: %s", a_path, b_path, exc)
        return False


def compress_track(meeting_dir, key, file_name):
    """Encode `<meeting_dir>/<file_name>` to a verified `<key>.flac` beside it.

    Returns the meta patch to commit — {"file", "codec", "wav_bytes",
    "flac_bytes"} — or None when there is nothing to do or anything at all
    went wrong. None is always safe: the WAV is untouched on every failure
    path, and the caller simply doesn't commit.
    """
    import soundfile as sf

    meeting_dir = Path(meeting_dir)
    if not str(file_name).endswith(".wav"):
        return None  # already archived (or something exotic): not ours to touch
    src = meeting_dir / file_name
    if not src.exists():
        return None
    dst = meeting_dir / f"{key}.flac"

    try:
        info = sf.info(str(src))
        if info.frames == 0:
            # A bare 44-byte header from a capture that produced nothing (the
            # broken-tap era). There are no bytes to save, and libsndfile's
            # zero-frame FLAC does not reopen — so this would fail verification
            # on every sweep forever. Say so once at INFO and leave it.
            log.info("%s/%s: zero frames (dead capture); leaving alone",
                     meeting_dir.name, file_name)
            return None
        if info.subtype != "PCM_16":
            # The recorder only ever writes int16; anything else predates us or
            # was hand-edited, and a lossy-for-them conversion must not happen.
            log.warning("%s/%s: subtype %s is not PCM_16; leaving alone",
                        meeting_dir.name, file_name, info.subtype)
            return None
    except Exception as exc:
        log.warning("%s/%s: unreadable (%s); leaving alone",
                    meeting_dir.name, file_name, exc)
        return None

    fd, tmp_name = tempfile.mkstemp(dir=str(meeting_dir),
                                    prefix=f"{key}.flac.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with sf.SoundFile(str(src)) as fin, \
             sf.SoundFile(str(tmp), "w", samplerate=fin.samplerate,
                          channels=fin.channels, format="FLAC",
                          subtype="PCM_16",
                          compression_level=_COMPRESSION_LEVEL) as fout:
            while True:
                block = fin.read(_BLOCK_FRAMES, dtype="int16", always_2d=True)
                if len(block) == 0:
                    break
                fout.write(block)

        if not verify_pair(src, tmp):
            log.error("%s/%s: FLAC verification FAILED; keeping the WAV",
                      meeting_dir.name, file_name)
            return None

        # Same flush-before-publish rationale as pipeline._atomic_write.
        try:
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
        except OSError:
            pass
        os.replace(tmp, dst)
    except Exception as exc:
        log.warning("%s/%s: compression failed (%s); keeping the WAV",
                    meeting_dir.name, file_name, exc)
        return None
    finally:
        tmp.unlink(missing_ok=True)

    wav_bytes, flac_bytes = src.stat().st_size, dst.stat().st_size
    ratio = wav_bytes / max(1, flac_bytes)
    # A three-digit ratio means the track was digital silence — real speech
    # lands around 3-6x. Worth a log line: it is evidence of a dead capture,
    # and the compressor is simply the first thing to notice.
    log.log(logging.WARNING if ratio > 100 else logging.INFO,
            "%s/%s: %.0f MB -> %.0f MB (%.1fx)%s", meeting_dir.name, file_name,
            wav_bytes / 2**20, flac_bytes / 2**20, ratio,
            " — this track appears to be silent" if ratio > 100 else "")
    return {"file": f"{key}.flac", "codec": "flac",
            "wav_bytes": wav_bytes, "flac_bytes": flac_bytes}


def remove_wav(meeting_dir, key):
    """Delete the WAV after its meta commit. Call ONLY after meeting.json
    names the .flac."""
    (Path(meeting_dir) / f"{key}.wav").unlink(missing_ok=True)


def remove_stranded_wav(meeting_dir, key):
    """A WAV left behind by a crash between meta commit and remove_wav().

    Deletes it only after re-verifying the pair — the meta says .flac, but
    this function does not trust that a byte of it was ever checked.
    Returns True if the WAV was removed.
    """
    meeting_dir = Path(meeting_dir)
    wav, flac = meeting_dir / f"{key}.wav", meeting_dir / f"{key}.flac"
    if not (wav.exists() and flac.exists()):
        return False
    if not verify_pair(wav, flac):
        log.error("%s/%s.wav: stranded WAV does NOT match its FLAC; keeping "
                  "both — this needs a human", meeting_dir.name, key)
        return False
    wav.unlink()
    log.info("%s/%s.wav: stranded WAV removed after re-verification",
             meeting_dir.name, key)
    return True


def sweep_orphan_temps(recordings_dir):
    """Remove `<key>.flac.*.tmp` wreckage older than an hour. Returns count."""
    removed = 0
    now = time.time()
    for tmp in Path(recordings_dir).glob("*/*.flac.*.tmp"):
        try:
            if now - tmp.stat().st_mtime > _ORPHAN_MIN_AGE_S:
                tmp.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        log.info("swept %d orphaned compression temp file(s)", removed)
    return removed
