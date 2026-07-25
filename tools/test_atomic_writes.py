#!/usr/bin/env python3
"""Crash-safety of the files a meeting cannot be recovered without.

WHAT IS AT STAKE
----------------
meeting.json is the ONLY copy of a meeting's transcript, speakers and summary —
the WAVs can be re-transcribed, that file cannot be rebuilt. analysis.npz holds
the ECAPA embeddings that make re-clustering instant; truncated mid-archive it
is an unreadable zip.

Path.write_text and np.savez_compressed both TRUNCATE THE TARGET IN PLACE. For
the whole duration of the write the file on disk is a prefix of the new
contents, and anything that stops the process there freezes it that way — a
crash, a power cut, or the os._exit() that /api/shutdown performs while a
background processing thread is still running. The window is small and it is
real: the app writes meeting.json at the end of every process/recluster, and the
user can hit Quit at any moment.

WHAT IS CHECKED
---------------
  * an exception mid-write leaves the previous file byte-for-byte intact;
  * a HARD process death mid-write (os._exit in a real subprocess, no unwinding,
    no finally blocks) leaves it intact too — this is the case an exception test
    cannot reach;
  * a failure at the publish step of a real recluster_meeting() run leaves a
    parseable meeting.json holding the old content;
  * a crash landing BETWEEN analysis.npz and analysis.json is detected rather
    than silently reusing a cache that belongs to a different save (which is how
    a new transcript ends up paired with an old run's embeddings);
  * no orphaned .tmp files are left behind when the process lives long enough to
    clean up. A hard kill obviously cannot clean up after itself, so that case
    only reports the leftover count — one stray temp file is the acceptable
    price, an unreadable meeting.json is not.

Everything runs in temp directories seeded from the frozen fixture corpora. The
committed test/synthetic/ corpus is the baseline and always runs; test/fixtures/
is an optional deeper LOCAL corpus, skipped with a message when absent (it is
gitignored: real participants' names and voice embeddings). recordings/ and
~/.meetingscribe/ are never read or written, no audio is touched, no model is
loaded, no network.

    ~/.meetingscribe/venv/bin/python tools/test_atomic_writes.py

Check functions are named check_* and all work sits behind __main__, so a stray
`pytest tools/` collects and runs nothing.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pipeline  # noqa: E402

sys.path.insert(0, str(REPO / "tools"))
from test_diarization import (  # noqa: E402
    fixture_dir,
    recluster_workdir,
    report,
    run,
)

PYTHON = sys.executable

OLD = json.dumps({"id": "old", "turns": [{"start": 0.0, "end": 1.0,
                                          "text": "the only copy of this"}]})
NEW_BIG = json.dumps({"id": "new", "pad": "x" * 400000})


def temp_files(d):
    return sorted(p.name for p in Path(d).iterdir() if p.name.endswith(".tmp"))


# -------------------------------------------------------------------- checks --

def check_exception_mid_write_keeps_the_old_file(fail):
    """write_body raises halfway: the target must be untouched."""
    with tempfile.TemporaryDirectory(prefix="ms-atomic-") as tmp:
        target = Path(tmp) / "meeting.json"
        target.write_text(OLD, encoding="utf-8")

        def half_then_die(path):
            path.write_text(NEW_BIG[: len(NEW_BIG) // 2], encoding="utf-8")
            raise RuntimeError("simulated crash mid-write")

        try:
            pipeline._atomic_write(target, half_then_die)
        except RuntimeError:
            pass
        else:
            fail("_atomic_write swallowed the write_body exception")

        got = target.read_text(encoding="utf-8")
        print(f"    after a raising write: {len(got)} bytes, parses="
              f"{_parses(got)}, leftovers={temp_files(tmp)}")
        if got != OLD:
            fail("a failed write changed meeting.json — it must be all-or-nothing")
        if temp_files(tmp):
            fail(f"a failed write left temp files behind: {temp_files(tmp)}")


def check_publish_failure_keeps_the_old_file(fail):
    """The rename itself fails: still all-or-nothing, still no litter."""
    with tempfile.TemporaryDirectory(prefix="ms-atomic-") as tmp:
        target = Path(tmp) / "meeting.json"
        target.write_text(OLD, encoding="utf-8")

        real_replace = pipeline.os.replace

        def boom(src, dst):
            raise OSError("simulated crash at the rename")

        pipeline.os.replace = boom
        try:
            pipeline._atomic_write_text(target, NEW_BIG)
        except OSError:
            pass
        else:
            fail("_atomic_write_text reported success though os.replace failed")
        finally:
            pipeline.os.replace = real_replace

        print(f"    after a failed publish: parses={_parses(target.read_text())}, "
              f"leftovers={temp_files(tmp)}")
        if target.read_text(encoding="utf-8") != OLD:
            fail("a failed publish changed meeting.json")
        if temp_files(tmp):
            fail(f"a failed publish left temp files behind: {temp_files(tmp)}")


def check_hard_process_death_keeps_the_old_file(fail):
    """os._exit() mid-write, in a real subprocess — no unwinding, no cleanup.

    This is the /api/shutdown case, and the only one an in-process exception
    test cannot simulate: nothing runs after it, so the file on disk is exactly
    what the kernel had at that instant.
    """
    for label, body in (("meeting.json", _KILL_JSON), ("analysis.npz", _KILL_NPZ)):
        with tempfile.TemporaryDirectory(prefix="ms-atomic-kill-") as tmp:
            proc = subprocess.run(
                [PYTHON, "-c", body, tmp, str(REPO)],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 66:
                fail(f"{label}: the child did not reach the kill point "
                     f"(rc={proc.returncode}, stderr={proc.stderr[-400:]!r})")
                continue
            d = Path(tmp)
            leftovers = temp_files(tmp)
            if label == "meeting.json":
                text = (d / "meeting.json").read_text(encoding="utf-8")
                ok = text == OLD
                detail = f"{len(text)} bytes, parses={_parses(text)}"
            else:
                ok, detail = _npz_intact(d / "analysis.npz")
            print(f"    killed mid-write of {label:13s} -> survivor {detail}, "
                  f"{len(leftovers)} orphaned temp file(s) — expected, nothing "
                  "ran after the kill")
            if not ok:
                fail(f"{label} did not survive a hard kill mid-write: {detail}")


# Child programs for the hard-kill check. Each writes a large payload through
# pipeline's atomic helper and calls os._exit(66) from inside write_body, i.e.
# with the new bytes on disk under the temp name and the target not yet
# replaced. Kept as source strings so the death is a real process death.
_KILL_JSON = """
import json, os, sys
tmp, repo = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)
import pipeline
from pathlib import Path
target = Path(tmp) / "meeting.json"
old = json.dumps({"id": "old", "turns": [{"start": 0.0, "end": 1.0,
                                          "text": "the only copy of this"}]})
target.write_text(old, encoding="utf-8")
def body(p):
    p.write_text("x" * 400000, encoding="utf-8")
    os._exit(66)
pipeline._atomic_write(target, body)
"""

_KILL_NPZ = """
import os, sys
import numpy as np
tmp, repo = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)
import pipeline
from pathlib import Path
target = Path(tmp) / "analysis.npz"
with open(target, "wb") as fh:
    np.savez_compressed(fh, mic_windows=np.zeros((8, 2)),
                        mic_embeddings=np.zeros((8, 192), dtype=np.float32),
                        embed_version=np.asarray(1, dtype=np.int32))
def body(p):
    with open(p, "wb") as fh:
        np.savez_compressed(fh, mic_windows=np.zeros((6000, 2)),
                            mic_embeddings=np.zeros((6000, 192), dtype=np.float32))
        fh.flush()
        os._exit(66)
pipeline._atomic_write(target, body)
"""


def check_recluster_survives_a_crash_at_the_publish(fail, corpus):
    """End to end: a real recluster_meeting() whose final write cannot publish.

    The user pressed the speaker-count control and the machine died as the
    result was being saved. They must still have the meeting they had before.
    """
    with recluster_workdir(corpus, corpus.solo_mic) as work:
        meta_path = work / "meeting.json"
        before = meta_path.read_text(encoding="utf-8")

        real_replace = pipeline.os.replace

        def boom(src, dst):
            if str(dst).endswith("meeting.json"):
                raise OSError("simulated crash saving meeting.json")
            return real_replace(src, dst)

        pipeline.os.replace = boom
        try:
            pipeline.recluster_meeting(work, 2)
        except Exception as exc:
            # The CLASS is not the contract — what matters is that the failure
            # is reported rather than swallowed, and that the OSError underneath
            # is still reachable so a caller can tell a disk failure from a bug.
            # recluster_meeting wraps it (RuntimeError("Could not save the
            # result: ...")), so accept either shape but insist on the cause.
            causes = []
            cur = exc
            while cur is not None and cur not in causes:
                causes.append(cur)
                cur = cur.__cause__
            if not any(isinstance(c, OSError) for c in causes):
                fail(f"the failed save raised {type(exc).__name__}: {exc}, which "
                     "neither is nor wraps the OSError that caused it — a caller "
                     "cannot tell a full disk from a programming error")
            else:
                print(f"    reported as {type(exc).__name__} wrapping "
                      f"{type(causes[-1]).__name__}")
        else:
            fail("recluster_meeting reported success though the save failed")
        finally:
            pipeline.os.replace = real_replace

        after = meta_path.read_text(encoding="utf-8")
        print(f"    meeting.json after the crash: {len(after)} bytes, "
              f"parses={_parses(after)}, unchanged={after == before}, "
              f"leftovers={len(temp_files(work))}")
        if not _parses(after):
            fail("meeting.json is no longer valid JSON after a crash mid-save — "
                 "the transcript is unrecoverable")
        if after != before:
            fail("meeting.json was partially updated by a save that failed")
        if temp_files(work):
            fail(f"a crashed save left temp files behind: {temp_files(work)}")


def check_a_crash_between_the_two_files_is_detected(fail):
    """analysis.npz and analysis.json are written one after the other.

    A crash in between leaves a cache belonging to a different save than the
    transcripts beside it. Reusing it silently pairs a NEW transcript with an
    OLD run's embeddings — speakers assigned from voices that are no longer
    there. The pairing token must catch that, and rejection must fall back to
    re-embedding rather than raise.
    """
    with tempfile.TemporaryDirectory(prefix="ms-atomic-pair-") as tmp:
        d = Path(tmp)
        collect = {"mic": {"windows": np.zeros((8, 2)),
                           "embeddings": np.zeros((8, 192), dtype=np.float32)}}
        pipeline._save_analysis_state(d, {"mic": [{"start": 0, "end": 1}]}, collect)
        first = json.loads((d / pipeline.ANALYSIS_JSON).read_text(encoding="utf-8"))
        cache = pipeline._load_analysis_cache(
            d / pipeline.ANALYSIS_NPZ, ["mic"], first.get(pipeline.STATE_ID_KEY))
        print(f"    a matched pair          -> cache tracks {sorted(cache)}")
        if "mic" not in cache:
            fail("a freshly written analysis pair was rejected by its own loader")

        # The crash: analysis.npz gets rewritten by a later save, analysis.json
        # never does.
        pipeline._save_analysis_state(d, {"mic": [{"start": 0, "end": 2}]}, collect)
        (d / pipeline.ANALYSIS_JSON).write_text(json.dumps(first), encoding="utf-8")
        cache = pipeline._load_analysis_cache(
            d / pipeline.ANALYSIS_NPZ, ["mic"], first.get(pipeline.STATE_ID_KEY))
        print(f"    a pair split by a crash -> cache tracks {sorted(cache)}")
        if cache:
            fail("an analysis.npz from a different save than analysis.json was "
                 "accepted — a new transcript would be labelled from an old "
                 "run's embeddings")

        # A truncated archive must be rejected, never raised on.
        (d / pipeline.ANALYSIS_NPZ).write_bytes(
            (d / pipeline.ANALYSIS_NPZ).read_bytes()[:200])
        try:
            cache = pipeline._load_analysis_cache(
                d / pipeline.ANALYSIS_NPZ, ["mic"], None)
        except Exception as exc:
            fail(f"a truncated analysis.npz raised {type(exc).__name__}: {exc} — "
                 "a corrupt cache must cost a re-embed, never the request")
            return
        print(f"    a truncated archive     -> cache tracks {sorted(cache)}")
        if cache:
            fail("a truncated analysis.npz was accepted as a usable cache")


def check_a_clean_write_leaves_nothing_behind(fail, corpus):
    """The happy path: contents complete, directory clean, on a real fixture."""
    with tempfile.TemporaryDirectory(prefix="ms-atomic-clean-") as tmp:
        work = Path(tmp) / corpus.solo_mic
        shutil.copytree(fixture_dir(corpus, corpus.solo_mic), work)
        pipeline._atomic_write_text(work / "meeting.json", NEW_BIG)
        got = (work / "meeting.json").read_text(encoding="utf-8")
        print(f"    wrote {len(NEW_BIG)} bytes, read back {len(got)}, "
              f"leftovers={temp_files(work)}")
        if got != NEW_BIG:
            fail("a clean atomic write did not round-trip its contents")
        if temp_files(work):
            fail(f"a clean atomic write left temp files behind: {temp_files(work)}")


# ------------------------------------------------------------------- helpers --

def _parses(text):
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def _npz_intact(path):
    try:
        with np.load(path) as npz:
            shape = npz["mic_embeddings"].shape
        return shape == (8, 192), f"readable, mic_embeddings{shape}"
    except Exception as exc:
        return False, f"UNREADABLE ({type(exc).__name__}: {exc})"


# Crash safety is a property of the write helpers, not of any particular
# meeting, so most of these build their own payloads and run once. The two that
# drive a real recluster or copy a real fixture run against every corpus present.
PURE_CHECKS = [
    ("an exception mid-write keeps the old file", check_exception_mid_write_keeps_the_old_file),
    ("a failed publish keeps the old file", check_publish_failure_keeps_the_old_file),
    ("a hard process death keeps the old file", check_hard_process_death_keeps_the_old_file),
    ("a crash between the two analysis files is detected",
     check_a_crash_between_the_two_files_is_detected),
]

CORPUS_CHECKS = [
    ("recluster survives a crash at the save", check_recluster_survives_a_crash_at_the_publish),
    ("a clean write leaves nothing behind", check_a_clean_write_leaves_nothing_behind),
]


def main():
    failures = []
    ran = 0
    for name, fn in PURE_CHECKS:
        print(f"\n== {name}")
        before = len(failures)
        try:
            fn(failures.append)
        except Exception as exc:
            failures.append(f"{name}: raised {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
        ran += 1
        print(f"   {'FAIL' if len(failures) > before else 'PASS'}")

    ran += run(CORPUS_CHECKS, failures)
    return report(failures, ran)


if __name__ == "__main__":
    sys.exit(main())
