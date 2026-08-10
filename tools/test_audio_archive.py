"""Checks for audio_archive.py — the lossless FLAC archival of recordings.

The offline checks (test_* below) synthesize their own audio in a temp dir:
no recording, no model, no network, safe for pytest to collect. main() runs
the one check that needs the corpus: compress the demo meeting's tracks and
prove a reprocess produces the identical transcript.

Usage:
    python tools/test_audio_archive.py            # offline checks + demo run
    pytest tools/test_audio_archive.py            # offline checks only
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio_archive  # noqa: E402


def _write_wav(path, seconds=2.0, rate=48000, channels=1, seed=7):
    """A deterministic int16 test signal — tones + noise so FLAC has work."""
    import soundfile as sf

    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * rate)) / rate
    sig = 0.4 * np.sin(2 * np.pi * 220 * t) + 0.05 * rng.standard_normal(len(t))
    data = (np.clip(sig, -1, 1) * 32767).astype(np.int16)
    if channels == 2:
        data = np.stack([data, data[::-1]], axis=1)
    sf.write(str(path), data, rate, subtype="PCM_16")
    return data


def test_flac_roundtrip_is_bit_identical(tmp_path=None):
    import soundfile as sf

    with tempfile.TemporaryDirectory() as td:
        for channels, rate in ((1, 48000), (2, 48000), (1, 16000)):
            wav = Path(td) / f"mic_{channels}_{rate}.wav"
            data = _write_wav(wav, channels=channels, rate=rate)
            flac = Path(td) / wav.with_suffix(".flac").name
            sf.write(str(flac), data, rate, format="FLAC", subtype="PCM_16")
            back, sr = sf.read(str(flac), dtype="int16")
            assert sr == rate
            assert np.array_equal(np.atleast_2d(data.T).T, np.atleast_2d(back.T).T)
            assert audio_archive.verify_pair(wav, flac)


def test_compress_returns_patch_and_is_idempotent():
    with tempfile.TemporaryDirectory() as td:
        meeting = Path(td)
        _write_wav(meeting / "mic.wav", channels=2)
        patch = audio_archive.compress_track(meeting, "mic", "mic.wav")
        assert patch is not None
        assert patch["file"] == "mic.flac" and patch["codec"] == "flac"
        assert (meeting / "mic.flac").exists()
        assert (meeting / "mic.wav").exists(), "WAV must survive until the meta commit"
        assert patch["flac_bytes"] < patch["wav_bytes"]
        # Second run with meta already flipped: nothing to do.
        assert audio_archive.compress_track(meeting, "mic", "mic.flac") is None
        # Missing file: nothing to do.
        assert audio_archive.compress_track(meeting, "system", "system.wav") is None


def test_zero_frame_wav_is_left_alone():
    # 28 of the corpus's 102 tracks were dead captures; two were bare 44-byte
    # headers whose zero-frame FLAC cannot be reopened. Nothing to save, so
    # the compressor must decline quietly instead of failing loudly forever.
    import soundfile as sf

    with tempfile.TemporaryDirectory() as td:
        meeting = Path(td)
        sf.write(str(meeting / "system.wav"),
                 np.zeros((0, 2), dtype=np.int16), 48000, subtype="PCM_16")
        assert audio_archive.compress_track(meeting, "system", "system.wav") is None
        assert (meeting / "system.wav").exists()
        assert not (meeting / "system.flac").exists()
        assert not list(meeting.glob("*.tmp"))


def test_verification_failure_keeps_the_wav(monkeypatch=None):
    with tempfile.TemporaryDirectory() as td:
        meeting = Path(td)
        _write_wav(meeting / "mic.wav")
        original = audio_archive.verify_pair
        audio_archive.verify_pair = lambda a, b: False
        try:
            assert audio_archive.compress_track(meeting, "mic", "mic.wav") is None
        finally:
            audio_archive.verify_pair = original
        assert (meeting / "mic.wav").exists()
        assert not (meeting / "mic.flac").exists()
        assert not list(meeting.glob("*.tmp")), "failed run must not leave temps"


def test_interrupted_before_meta_commit_recovers():
    # Crash window: .flac exists, meta still says .wav. The sweep simply
    # compresses again — fresh temp, fresh verify, replace — and succeeds.
    with tempfile.TemporaryDirectory() as td:
        meeting = Path(td)
        _write_wav(meeting / "mic.wav")
        first = audio_archive.compress_track(meeting, "mic", "mic.wav")
        assert first is not None  # both files now exist, "meta" never flipped
        second = audio_archive.compress_track(meeting, "mic", "mic.wav")
        assert second is not None and second["file"] == "mic.flac"
        assert audio_archive.verify_pair(meeting / "mic.wav", meeting / "mic.flac")


def test_stranded_wav_is_only_removed_after_reverify():
    import soundfile as sf

    with tempfile.TemporaryDirectory() as td:
        meeting = Path(td)
        _write_wav(meeting / "mic.wav")
        assert audio_archive.compress_track(meeting, "mic", "mic.wav") is not None
        # Corrupt the FLAC: the stranded WAV must NOT be removed.
        good = (meeting / "mic.flac").read_bytes()
        data, rate = sf.read(str(meeting / "mic.flac"), dtype="int16")
        sf.write(str(meeting / "mic.flac"), data[:-100], rate,
                 format="FLAC", subtype="PCM_16")
        assert audio_archive.remove_stranded_wav(meeting, "mic") is False
        assert (meeting / "mic.wav").exists()
        # Restore the good FLAC: now it may go.
        (meeting / "mic.flac").write_bytes(good)
        assert audio_archive.remove_stranded_wav(meeting, "mic") is True
        assert not (meeting / "mic.wav").exists()
        # And it is idempotent once gone.
        assert audio_archive.remove_stranded_wav(meeting, "mic") is False


def test_orphan_sweep_respects_age():
    import os
    import time

    with tempfile.TemporaryDirectory() as td:
        meeting = Path(td) / "m1"
        meeting.mkdir()
        young = meeting / "mic.flac.abc123.tmp"
        old = meeting / "system.flac.def456.tmp"
        young.write_bytes(b"x")
        old.write_bytes(b"x")
        stale = time.time() - 2 * audio_archive._ORPHAN_MIN_AGE_S
        os.utime(old, (stale, stale))
        assert audio_archive.sweep_orphan_temps(td) == 1
        assert young.exists() and not old.exists()


def test_load_mono_16k_is_identical_across_containers():
    # The load-bearing check: everything downstream (ASR, embeddings) reads
    # audio through pipeline.load_mono_16k, whose own docstring stakes its
    # correctness on bitwise identity. FLAC in, same floats out — exactly.
    import soundfile as sf

    import pipeline

    with tempfile.TemporaryDirectory() as td:
        for channels, rate in ((1, 48000), (2, 48000), (1, 16000)):
            wav = Path(td) / f"t_{channels}_{rate}.wav"
            data = _write_wav(wav, seconds=3.1, channels=channels, rate=rate)
            flac = wav.with_suffix(".flac")
            sf.write(str(flac), data, rate, format="FLAC", subtype="PCM_16")
            a = pipeline.load_mono_16k(wav)
            b = pipeline.load_mono_16k(flac)
            assert np.array_equal(a, b), f"load_mono_16k differs for {wav.name}"


OFFLINE_CHECKS = [
    test_flac_roundtrip_is_bit_identical,
    test_compress_returns_patch_and_is_idempotent,
    test_zero_frame_wav_is_left_alone,
    test_verification_failure_keeps_the_wav,
    test_interrupted_before_meta_commit_recovers,
    test_stranded_wav_is_only_removed_after_reverify,
    test_orphan_sweep_respects_age,
    test_load_mono_16k_is_identical_across_containers,
]


def main():
    for check in OFFLINE_CHECKS:
        check()
        print(f"   PASS {check.__name__}")

    # Compress-then-reprocess equality on the demo meeting: the transcript a
    # user gets from the FLAC must be the one they got from the WAV.
    from pipeline import process_meeting

    recordings = Path(__file__).resolve().parents[1] / "recordings"
    demo = next((d for d in recordings.iterdir()
                 if d.is_dir() and d.name.endswith("20260610-000001")), None)
    if demo is None:
        print("SKIP demo compress-then-reprocess (demo meeting not found)")
        return
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / demo.name
        shutil.copytree(demo, work)
        process_meeting(work)
        before = json.loads((work / "meeting.json").read_text())
        meta_tracks = before.get("tracks") or {}
        compressed = 0
        for key, track in meta_tracks.items():
            patch = audio_archive.compress_track(work, key, track.get("file", ""))
            if patch is None:
                continue
            track.update(patch)
            audio_archive.remove_wav(work, key)
            compressed += 1
        assert compressed, "demo meeting had nothing to compress"
        (work / "meeting.json").write_text(json.dumps(before))
        process_meeting(work)
        after = json.loads((work / "meeting.json").read_text())
        for field in ("turns", "speakers", "stats", "segments"):
            if before.get(field) != after.get(field):
                raise AssertionError(
                    f"reprocess after FLAC changed {field!r} — compression must "
                    "be invisible to the pipeline")
        print(f"   PASS demo compress({compressed} tracks)-then-reprocess is identical")
    print("\nOK — audio archive checks passed.")


if __name__ == "__main__":
    main()
