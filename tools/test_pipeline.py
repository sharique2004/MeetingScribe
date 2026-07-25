"""Smoke test: run the processing pipeline on the synthesized demo meeting.

Two things this test is careful about:

* It never touches the real recording. ``pipeline.process_meeting`` writes back
  into the directory it reads (meeting.json, analysis.json, analysis.npz), so
  running it in place would destroy the before-state and invalidate the
  analysis.npz cache this test is meant to validate against. We copy the
  meeting into a temp directory and run there.
* It asserts the golden result instead of only printing it.

Usage:
    python tools/test_pipeline.py                 # the demo meeting
    python tools/test_pipeline.py <meeting-id>    # any recording (suffix match)
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import process_meeting  # noqa: E402

RECORDINGS = Path(__file__).resolve().parents[1] / "recordings"

# The demo meeting lives in a directory whose name is
# "Demo meeting (synthesized voices) — 20260610-000001"; only the timestamp
# suffix is stable, so resolve by suffix rather than hardcoding the title.
DEMO_ID = "20260610-000001"

# Golden regression values for the demo meeting. If a diarization change moves
# these, that is the change to justify — not the numbers to edit away.
GOLDEN_SPEAKERS = {"you": "You", "s1": "Speaker 1", "s2": "Speaker 2"}
GOLDEN_WARNINGS = []
GOLDEN_EMBEDDING_WINDOWS = {"system": 39}


def resolve_meeting_dir(meeting_id):
    """Find the recording directory for a meeting id.

    Directory names are "<title> — <timestamp>", so an exact-name match is
    tried first, then a suffix match on the timestamp, then a substring match.
    """
    if not RECORDINGS.is_dir():
        raise SystemExit(f"no recordings directory at {RECORDINGS}")
    candidates = sorted(d for d in RECORDINGS.iterdir() if d.is_dir())

    exact = [d for d in candidates if d.name == meeting_id]
    suffix = [d for d in candidates if d.name.endswith(meeting_id)]
    substring = [d for d in candidates if meeting_id in d.name]
    for matches in (exact, suffix, substring):
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = "\n  ".join(d.name for d in matches)
            raise SystemExit(f"{meeting_id!r} is ambiguous:\n  {names}")
    names = "\n  ".join(d.name for d in candidates) or "(none)"
    raise SystemExit(f"no recording matching {meeting_id!r}. Available:\n  {names}")


def embedding_window_counts(meeting_dir):
    """{track: n_windows} from the analysis.npz the run just wrote."""
    npz_path = meeting_dir / "analysis.npz"
    if not npz_path.exists():
        return {}
    import numpy as np

    with np.load(npz_path) as npz:
        return {
            key[: -len("_embeddings")]: int(npz[key].shape[0])
            for key in npz.files
            if key.endswith("_embeddings")
        }


def report(meta, windows):
    print("STATUS:", meta["status"])
    print("LANGUAGES:", meta.get("languages"))
    print("SPEAKERS:", json.dumps(meta["speakers"]))
    print("WARNINGS:", meta["warnings"])
    print("EMBEDDING WINDOWS:", windows)
    print("TURNS:")
    for t in meta["turns"]:
        name = meta["speakers"].get(t["speaker"], t["speaker"])
        print(f"  [{t['start']:6.1f}-{t['end']:6.1f}] {name}: {t['text']}")
    print("STATS:")
    for key, st in meta["stats"]["per_speaker"].items():
        name = meta["speakers"].get(key, key)
        top = ", ".join(f"{w}x{c}" for w, c in list(st["fillers"].items())[:3]) or "-"
        print(
            f"  {name}: {st['seconds']}s ({round(st['share']*100)}%), {st['words']} words, "
            f"{st['wpm']} wpm, {st['questions']} questions, fillers: {top}"
        )


def check_golden(meta, windows):
    """Assert the demo meeting's golden result. Returns nothing; raises on drift."""
    assert meta["status"] == "done", f"status was {meta['status']!r}, expected 'done'"
    assert meta["speakers"] == GOLDEN_SPEAKERS, (
        f"speaker map drifted:\n  got      {meta['speakers']}\n"
        f"  expected {GOLDEN_SPEAKERS}"
    )
    assert meta["warnings"] == GOLDEN_WARNINGS, (
        f"warnings drifted:\n  got      {meta['warnings']}\n"
        f"  expected {GOLDEN_WARNINGS}"
    )
    assert windows == GOLDEN_EMBEDDING_WINDOWS, (
        f"embedding window counts drifted:\n  got      {windows}\n"
        f"  expected {GOLDEN_EMBEDDING_WINDOWS}"
    )
    assert meta["turns"], "no turns were produced"


def main():
    meeting_id = sys.argv[1] if len(sys.argv) > 1 else DEMO_ID
    source = resolve_meeting_dir(meeting_id)
    is_demo = source.name.endswith(DEMO_ID)
    print(f"MEETING: {source.name}")

    # Run against a throwaway copy: process_meeting writes meeting.json,
    # analysis.json and analysis.npz back into the directory it reads.
    with tempfile.TemporaryDirectory(prefix="meetingscribe-test-") as tmp:
        work = Path(tmp) / source.name
        shutil.copytree(source, work)
        print(f"WORKDIR: {work}")

        meta = process_meeting(work, lambda msg: print("  >", msg))
        windows = embedding_window_counts(work)
        report(meta, windows)

        if is_demo:
            check_golden(meta, windows)
            print("\nOK — golden speaker map, warnings and window counts match.")
        else:
            print("\nOK — ran to completion (golden assertions apply to the demo only).")


if __name__ == "__main__":
    main()
