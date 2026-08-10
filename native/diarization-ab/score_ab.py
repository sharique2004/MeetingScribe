#!/usr/bin/env python3
"""Score a Swift-native diarization candidate against the GEN3 baseline.

WHY THIS FILE EXISTS. tools/eval_diarization.py scores diarization.cluster()
over FROZEN CACHED ECAPA EMBEDDINGS — its --variant-module hook accepts only a
Python cluster(embeddings, ...) function, so an audio-in/segments-out Swift
engine cannot plug into it. This scorer keeps the harness's scoring unit (the
AUTOMATIC SPEAKER COUNT per fixture, judged against the SAME TRUTH table and
compared to the SAME GEN3 numbers) but feeds each candidate the ORIGINAL track
WAV instead of cached embeddings. Everything truth- and identity-related is
imported from tools/eval_diarization.py, never copied, so the two reports can
never drift apart.

WHAT IS AND IS NOT COMPARABLE:
  * Real corpus: comparable. The candidate diarizes the exact WAV the cached
    embeddings came from (system.wav for online meetings = remote voices only;
    mic.wav for in-person = every voice incl. the local user), so its count is
    judged against the same per-track TRUTH as GEN3.
  * Synthetic corpus: NOT runnable. Those fixtures are generated embedding
    VECTORS with no audio behind them, so an audio-in engine has nothing to
    read. The GEN3 synthetic line (10/11) simply has no candidate-side number.
  * This scorer needs the gitignored real corpus AND the original recordings
    under ~/.meetingscribe/recordings/ — both exist only on the owner's machine.

PRIVACY: same rule as the harness. Everything printed or written is keyed by
the stable pseudonym (Call A.., Room P..). Recording directory names are real
meeting titles and are resolved in memory only.

Usage:
  score_ab.py --engine fluidaudio                  # full real corpus
  score_ab.py --engine speakerkit --only Demo      # one fixture (smoke run)
  score_ab.py --engine fluidaudio --only Demo --only "Call A"
  score_ab.py --engine speakerkit --binary /path/to/speakerkit-diarize

Writes results/<engine>.json (pseudonym-keyed, safe to commit) and prints the
comparison table.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]  # .../MeetingScribe
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO))

import eval_diarization as ev  # noqa: E402  (the single source of TRUTH/GEN3)

RECORDINGS = Path.home() / ".meetingscribe" / "recordings"
TS_RE = re.compile(r"(\d{8}-\d{6})$")

DEFAULT_BINARIES = {
    "fluidaudio": HERE / "FluidDiarize" / ".build" / "release" / "fluid-diarize",
    "speakerkit": HERE / "SpeakerKitDiarize" / ".build" / "release" / "speakerkit-diarize",
}


def resolve_wav(fx):
    """Map a frozen fixture to the original track WAV it was embedded from.

    Fixture slugs and recording directories share their trailing
    YYYYMMDD-HHMMSS timestamp; that suffix is the join key. Returns
    (wav_path or None, reason).
    """
    m = TS_RE.search(fx["slug"])
    if not m:
        return None, "slug has no timestamp suffix"
    ts = m.group(1)
    if not RECORDINGS.is_dir():
        return None, f"no recordings dir at {RECORDINGS}"
    matches = [d for d in RECORDINGS.iterdir() if d.is_dir() and d.name.endswith(ts)]
    if not matches:
        return None, "no recording with this timestamp (deleted?)"
    if len(matches) > 1:
        return None, f"{len(matches)} recordings share timestamp {ts}"
    # Archived meetings hold <track>.flac instead of .wav (audio_archive.py);
    # the engines read either through the same decoders.
    for ext in (".wav", ".flac"):
        wav = matches[0] / f"{fx['track']}{ext}"
        if wav.exists():
            return wav, ""
    return None, f"{fx['track']}.wav/.flac missing from recording"


def run_candidate(binary, wav, out_json, timeout, extra_args=()):
    cmd = [str(binary), str(wav), "--out", str(out_json), *extra_args]
    t0 = time.time()
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout)
    wall = time.time() - t0
    if proc.returncode != 0:
        # stderr may contain the wav path (which embeds no title — recordings
        # paths DO embed titles, so keep it to the last line and mark it local).
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return None, wall, tail[-1] if tail else f"exit {proc.returncode}"
    try:
        return json.loads(Path(out_json).read_text(encoding="utf-8")), wall, ""
    except (OSError, ValueError) as exc:
        return None, wall, f"unreadable output JSON: {exc}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", required=True, choices=sorted(DEFAULT_BINARIES))
    ap.add_argument("--binary", help="candidate executable (default: its .build/release)")
    ap.add_argument("--only", action="append",
                    help="pseudonym(s) to run, e.g. --only Demo (repeatable); "
                         "default: every real fixture with a resolvable WAV")
    ap.add_argument("--timeout", type=float, default=3600.0,
                    help="per-file timeout in seconds (default 3600)")
    ap.add_argument("--num-speakers", type=int,
                    help="force the count instead of auto (NOT the A/B metric; "
                         "auto is what GEN3 pins)")
    ap.add_argument("--mode", choices=("offline", "streaming"),
                    help="fluidaudio only: which pipeline the adapter runs. "
                         "offline = community-1 CoreML (the fair re-test); "
                         "streaming = the path 417ef04 mistakenly benchmarked")
    ap.add_argument("--threshold", type=float,
                    help="clustering threshold passed through to the candidate "
                         "(CORRECTION.md requires sweeping this, not one point)")
    ap.add_argument("--tag",
                    help="suffix for the results file, e.g. --tag offline-t0.7 "
                         "writes results/<engine>-offline-t0.7.json")
    ap.add_argument("--keep-segments", action="store_true",
                    help="also keep per-fixture segment JSON under "
                         "results/segments/<engine>/<pseudonym>.json")
    args = ap.parse_args(argv)

    binary = Path(args.binary) if args.binary else DEFAULT_BINARIES[args.engine]
    if not binary.exists():
        raise SystemExit(
            f"candidate binary not built: {binary}\n"
            f"  build it first:  cd {binary.parents[2]} && swift build -c release")

    label_map, why_not = ev.load_label_map(ev.FIXTURES)
    if not label_map:
        raise SystemExit(
            "the real corpus is unavailable, and it is the only corpus with audio "
            "behind it — the synthetic fixtures are generated vectors, so an "
            "audio-in candidate has nothing to run on.\n  " + why_not)
    fixtures = ev.load_fixtures(ev.FIXTURES, corpus="real")
    if args.only:
        want = set(args.only)
        unknown = want - {fx["key"] for fx in fixtures}
        if unknown:
            raise SystemExit(f"--only names no fixture: {sorted(unknown)}; "
                             f"known: {sorted(fx['key'] for fx in fixtures)}")
        fixtures = [fx for fx in fixtures if fx["key"] in want]
    fixtures.sort(key=lambda fx: ev.label_of(fx["key"]))

    seg_dir = HERE / "results" / "segments" / (
        args.engine + (f"-{args.tag}" if args.tag else ""))
    if args.keep_segments:
        seg_dir.mkdir(parents=True, exist_ok=True)

    rows = {}
    print(f"candidate: {args.engine}  ({binary})")
    print(f"metric   : automatic speaker count vs TRUTH — the same unit "
          f"tools/eval_diarization.py scores")
    if args.num_speakers:
        print(f"NOTE     : --num-speakers {args.num_speakers} forces the count; "
              f"this run is NOT comparable to GEN3's auto column.")
    print()
    hdr = "%-38s %-4s %6s %5s %5s %6s %9s %8s" % (
        "meeting", "trk", "truth", "gen3", "cand", "vs T", "audio_s", "rtf_x")
    print(hdr)
    print("-" * len(hdr))

    for fx in fixtures:
        key = fx["key"]
        name = "%-7s %s" % (ev.label_of(key), ev.kind_of(key))
        truth = ev.scored_truth(key)
        gen3 = ev.GEN3_REAL.get(key)
        wav, reason = resolve_wav(fx)
        if wav is None:
            print("%-38s %-4s %6s %5s %5s %6s   SKIPPED: %s"
                  % (name[:38], fx["track"][:3],
                     "-" if truth is None else truth,
                     "-" if gen3 is None else gen3, "-", "", reason))
            rows[key] = {"skipped": reason, "truth": truth, "gen3": gen3}
            continue
        out_json = seg_dir / f"{key}.json" if args.keep_segments \
            else HERE / "results" / f".tmp-{args.engine}.json"
        extra = ("--num-speakers", str(args.num_speakers)) if args.num_speakers else ()
        if args.mode:
            extra += ("--mode", args.mode)
        if args.threshold is not None:
            extra += ("--threshold", str(args.threshold))
        result, wall, err = run_candidate(binary, wav, out_json, args.timeout, extra)
        if result is None:
            print("%-38s %-4s %6s %5s %5s %6s   FAILED after %.0fs: %s"
                  % (name[:38], fx["track"][:3],
                     "-" if truth is None else truth,
                     "-" if gen3 is None else gen3, "-", "", wall, err))
            rows[key] = {"error": err, "truth": truth, "gen3": gen3,
                         "wall_seconds": round(wall, 1)}
            continue
        count = result["speaker_count"]
        audio_s = result.get("audio_seconds") or 0.0
        rtf = (audio_s / result["elapsed_seconds"]) if result.get("elapsed_seconds") else 0.0
        verdict = ""
        if truth is not None:
            verdict = "HIT" if count == truth else "MISS%+d" % (count - truth)
        print("%-38s %-4s %6s %5s %5d %6s %9.1f %7.1fx"
              % (name[:38], fx["track"][:3],
                 "-" if truth is None else truth,
                 "-" if gen3 is None else gen3, count, verdict, audio_s, rtf))
        rows[key] = {
            "count": count,
            "reported_count": result.get("reported_speaker_count"),
            "truth": truth,
            "gen3": gen3,
            "segments": len(result.get("segments", [])),
            "audio_seconds": round(audio_s, 2),
            "elapsed_seconds": round(result.get("elapsed_seconds", 0.0), 2),
            "track": fx["track"],
        }
        if not args.keep_segments:
            out_json.unlink(missing_ok=True)

    ran = {k: r for k, r in rows.items() if "count" in r}
    scorable = {k: r for k, r in ran.items() if r["truth"] is not None}
    hits = [k for k, r in scorable.items() if r["count"] == r["truth"]]
    gen3_hits = [k for k, r in scorable.items() if r["gen3"] == r["truth"]]
    print()
    print("SUMMARY (%s over the real corpus; auto count vs TRUTH):" % args.engine)
    print("  candidate : %d of %d truth-backed fixtures correct" % (len(hits), len(scorable)))
    print("  GEN3      : %d of %d on the same fixtures (full corpus: 21 of 21; "
          "synthetic 10 of 11 has no audio, so no candidate number exists there)"
          % (len(gen3_hits), len(scorable)))
    if "Demo" in ran:
        print("  golden Demo: candidate %s, GEN3 %s, truth 2 (known)"
              % (ran["Demo"]["count"], ev.GEN3_REAL["Demo"]))
    misses = [k for k, r in scorable.items() if r["count"] != r["truth"]]
    if misses:
        print("  WRONG: " + ", ".join(
            "%s (cand %d, truth %d)" % (k, scorable[k]["count"], scorable[k]["truth"])
            for k in sorted(misses)))
    print()
    print("DECISION RULE (PRD Gate 2): a candidate passes only if, on every "
          "truth-backed real\n  fixture it can run, it matches or beats GEN3 — "
          "i.e. hits = scorable and Demo = 2.\n  Partial runs (--only) are smoke "
          "evidence, not a gate result.")

    name = args.engine + (f"-{args.tag}" if args.tag else "")
    out = HERE / "results" / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "engine": args.engine,
        "binary": str(binary),
        "mode": args.mode,
        "threshold": args.threshold,
        "forced_num_speakers": args.num_speakers,
        "gen3_reference": {"date": ev.BASELINE_DATE, "rule": ev.BASELINE_RULE},
        "rows": rows,
        "summary": {
            "ran": len(ran), "scorable": len(scorable),
            "hits": len(hits), "gen3_hits_same_subset": len(gen3_hits),
        },
    }
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"\nresults written to {out} (pseudonym-keyed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
