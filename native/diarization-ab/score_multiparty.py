#!/usr/bin/env python3
"""Score the diarizers on MULTI-PARTY audio with real ground truth.

WHY THIS FILE EXISTS. tools/eval_diarization.py's corpus is 1:1 calls — 17 of
its 21 truth-backed fixtures have ONE voice on the clustered track and none
has three or more, so it cannot see the failure the owner reproduced with a
multi-speaker YouTube video played into the room mic: several real voices
merged into one label. This scorer runs the engines over audio with KNOWN
per-second speaker truth (AMI meetings ship reference RTTMs; any WAV + RTTM
pair works, including one cut from a YouTube panel whose turns you label)
and reports the two numbers the count metric cannot: time-weighted speaker
CONFUSION and the count error, per engine.

Engines scored side by side:
  classic  the shipping count path: ASR (whatever pick_backend chooses) ->
           diarization.diarize_track auto. Hypothesis = the labelled words.
  neural   FluidAudio offline community-1, unforced, confetti-folded.
           Hypothesis = its turns, no ASR involved.
  hybrid   what the product actually does since 2026-08-03: classic decides
           the count, the neural gate accepts/forces/rejects (the logic of
           pipeline._neural_refine), words re-attributed by the winner.

METRIC. Frame-based (10 ms) with the reference mapped to each hypothesis by
Hungarian assignment on overlap seconds. Reported per engine:
  count    hypothesis speakers vs truth speakers
  miss/FA/confusion as fractions of reference speech time, both with and
           without a 0.25 s boundary collar (overlap regions always scored).
Word-based hypotheses under-cover the reference by construction (they only
claim time where ASR put words), so MISS is not comparable between `neural`
and the word-based engines — CONFUSION and COUNT are the columns to read.

Usage:
  score_multiparty.py --wav ES2004a.Mix-Headset.wav --rttm ES2004a.rttm
  score_multiparty.py --wav clip.wav --rttm clip.rttm --engines neural,hybrid
  # RTTM lines: SPEAKER <file> 1 <start> <dur> <NA> <NA> <name> <NA> <NA>

Nothing here touches recordings/ or the frozen fixtures; WAVs and RTTMs are
whatever you point it at (AMI downloads under ~/.meetingscribe/multiparty).
"""

import argparse
import collections
import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

import diarization  # noqa: E402
import diarization_neural  # noqa: E402
import pipeline  # noqa: E402

FRAME = 0.01  # seconds


def load_rttm(path):
    """[(start, end, speaker)] from an RTTM file."""
    turns = []
    for line in Path(path).read_text().splitlines():
        f = line.split()
        if len(f) >= 8 and f[0] == "SPEAKER":
            start, dur = float(f[3]), float(f[4])
            turns.append((start, start + dur, f[7]))
    return sorted(turns)


def frame_labels(turns, n_frames, names):
    """(n_frames, n_speakers) activity matrix from [(s, e, name)]."""
    idx = {n: i for i, n in enumerate(names)}
    act = np.zeros((n_frames, len(names)), dtype=bool)
    for s, e, name in turns:
        act[int(round(s / FRAME)):int(round(e / FRAME)), idx[name]] = True
    return act


def score(ref_turns, hyp_turns, total_s, collar=0.0):
    """(miss, fa, confusion) as fractions of reference speech time.

    Frame-based; the collar excludes ±collar around every reference boundary
    from scoring; speaker mapping is Hungarian on overlap seconds.
    """
    from scipy.optimize import linear_sum_assignment

    n = int(np.ceil(total_s / FRAME)) + 1
    ref_names = sorted({t[2] for t in ref_turns})
    hyp_names = sorted({t[2] for t in hyp_turns})
    ref = frame_labels(ref_turns, n, ref_names)
    hyp = frame_labels(hyp_turns, n, hyp_names)

    scored = np.ones(n, dtype=bool)
    if collar > 0:
        c = int(round(collar / FRAME))
        for s, e, _ in ref_turns:
            for b in (s, e):
                i = int(round(b / FRAME))
                scored[max(0, i - c):i + c] = False

    overlap = np.zeros((len(ref_names), len(hyp_names)))
    for i in range(len(ref_names)):
        for j in range(len(hyp_names)):
            overlap[i, j] = np.sum(ref[scored, i] & hyp[scored, j]) * FRAME
    ri, hj = linear_sum_assignment(-overlap)
    mapped = {j: i for i, j in zip(ri, hj)}

    ref_n = ref[scored].sum(axis=1)   # speakers active per frame (truth)
    hyp_n = hyp[scored].sum(axis=1)
    correct = np.zeros(scored.sum())
    for j, i in mapped.items():
        correct += (ref[scored, i] & hyp[scored, j])
    ref_time = ref_n.sum() * FRAME
    miss = np.maximum(ref_n - hyp_n, 0).sum() * FRAME
    fa = np.maximum(hyp_n - ref_n, 0).sum() * FRAME
    conf = (np.minimum(ref_n, hyp_n) - correct).clip(min=0).sum() * FRAME
    return miss / ref_time, fa / ref_time, conf / ref_time


def hyp_from_segments(segs):
    """Labelled word-segments -> [(start, end, 's<idx>')]."""
    return [(s["start"], s["end"], f"s{s['speaker_idx']}") for s in segs]


def run_classic(wav, cfg, progress):
    segs, _lang = pipeline.transcribe_track(Path(wav), "track", cfg, progress)
    labelled, n = diarization.diarize_track(Path(wav), segs, n_speakers=None,
                                            threshold=0.6, progress_cb=progress)
    return segs, labelled, n


def run_neural_auto(wav):
    turns = diarization_neural.fold_confetti(
        diarization_neural.diarize_turns(Path(wav)))
    return turns, len({t[2] for t in turns})


def run_hybrid(wav, segs, classic_labelled, n_classic, neural_turns, k_neural):
    """The product's gate, replayed (pipeline._neural_refine's logic)."""
    if n_classic < 2:
        return classic_labelled, n_classic, "classic (count<2)"
    if k_neural == n_classic:
        out, k = diarization.assign_by_turns(segs, neural_turns)
        if k == n_classic:
            return out, k, "neural (agreed)"
        return classic_labelled, n_classic, "classic (assign mismatch)"
    if k_neural > n_classic:
        forced = diarization_neural.diarize_turns(Path(wav), num_speakers=n_classic)
        out, k = diarization.assign_by_turns(segs, forced)
        if k == n_classic:
            return out, k, "neural (forced down)"
        return classic_labelled, n_classic, "classic (forced mismatch)"
    return classic_labelled, n_classic, "classic (neural saw fewer)"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wav", required=True)
    ap.add_argument("--rttm", required=True)
    ap.add_argument("--engines", default="classic,neural,hybrid")
    ap.add_argument("--json-out", help="write the result rows as JSON")
    args = ap.parse_args(argv)

    ref = load_rttm(args.rttm)
    truth_n = len({t[2] for t in ref})
    total = max(e for _s, e, _n in ref)
    import soundfile as sf
    total = max(total, float(sf.info(args.wav).duration))
    print(f"reference: {truth_n} speakers, {len(ref)} turns, "
          f"{sum(e - s for s, e, _ in ref):.0f}s of speech over {total:.0f}s")

    engines = args.engines.split(",")
    cfg = {"language": "en", "whisper_backend": "auto", "compute_type": "int8",
           "_context_strings": [], "diarization_threshold": 0.6}
    quiet = lambda _m: None
    rows = {}

    segs = classic_labelled = n_classic = None
    if {"classic", "hybrid"} & set(engines):
        t0 = time.time()
        segs, classic_labelled, n_classic = run_classic(args.wav, cfg, quiet)
        rows["classic"] = {"count": n_classic, "elapsed": round(time.time() - t0, 1),
                           "hyp": hyp_from_segments(classic_labelled)}
    neural_turns = k_neural = None
    if {"neural", "hybrid"} & set(engines):
        t0 = time.time()
        neural_turns, k_neural = run_neural_auto(args.wav)
        rows["neural"] = {"count": k_neural, "elapsed": round(time.time() - t0, 1),
                          "hyp": [(s, e, f"s{l}") for s, e, l in neural_turns]}
    if "hybrid" in engines:
        t0 = time.time()
        out, k, how = run_hybrid(args.wav, segs, classic_labelled, n_classic,
                                 neural_turns, k_neural)
        rows["hybrid"] = {"count": k, "how": how,
                          "elapsed": round(time.time() - t0, 1),
                          "hyp": hyp_from_segments(out)}

    hdr = "%-8s %5s/%s %-24s %23s %23s" % (
        "engine", "count", "truth", "route",
        "miss/fa/conf (collar .25)", "miss/fa/conf (strict)")
    print("\n" + hdr)
    print("-" * len(hdr))
    for name, row in rows.items():
        lenient = score(ref, row["hyp"], total, collar=0.25)
        strict = score(ref, row["hyp"], total, collar=0.0)
        row["collar25"] = [round(x, 3) for x in lenient]
        row["strict"] = [round(x, 3) for x in strict]
        print("%-8s %5d/%d %-24s %6.1f%%%6.1f%%%6.1f%%    %6.1f%%%6.1f%%%6.1f%%" % (
            name, row["count"], truth_n, row.get("how", ""),
            *(x * 100 for x in lenient), *(x * 100 for x in strict)))
    print("\nCONFUSION and COUNT are the comparable columns; word-based rows "
          "(classic/hybrid)\nonly claim time where ASR put words, so their "
          "MISS is inflated by construction.")

    if args.json_out:
        slim = {k: {kk: vv for kk, vv in v.items() if kk != "hyp"}
                for k, v in rows.items()}
        slim["truth"] = {"speakers": truth_n, "turns": len(ref)}
        Path(args.json_out).write_text(json.dumps(slim, indent=1))
        print(f"\nwritten: {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
