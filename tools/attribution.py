#!/usr/bin/env python3
"""Speaker-attribution scoring: who spoke when, measured against real labels.

WHY THIS FILE EXISTS. Every diarization number this repository can currently
quote is a SPEAKER COUNT. `tools/eval_diarization.py` scores how often the
automatic count is right; a constant predictor that always answers "1" scores
17/21 = 81% on the real corpus, and a correct count is compatible with every
single word being attributed to the wrong person
(native/diarization-ab/CORRECTION.md §c, docs/ATTRIBUTION_AND_ROADMAP_PRD.md §2).
`native/diarization-ab/score_multiparty.py` was the only attribution metric in
the tree, and it works exclusively against exhaustive RTTM truth, which exists
for AMI meetings and for nothing in this product's own corpus.

This module is the shared scoring core. It holds:

  * the FRAME-BASED metric lifted verbatim out of score_multiparty.py
    (load_rttm / frame_labels / score), so that file and the eval harness
    compute the same numbers from the same code;
  * a WORD-REFERENCED mode for the truth the founder can actually produce —
    per-word speaker labels over a saved transcript, which is a few hours of
    listening rather than an exhaustive frame-level relabelling of every
    second of audio;
  * the DEGENERATE FLOORS, computed on every scoring call and never optional,
    because the count metric's whole failure mode was a headline number
    quoted without the floor it had to beat.

TWO MODES, AND THE DIFFERENCE IS NOT COSMETIC.

  WORD-REFERENCED (score_word_reference).  Truth is a label per transcript
  word. The reference therefore claims time only where the recognizer put
  words, so this mode CANNOT measure missed speech or false alarms — silence
  the diarizer invented, or speech it never noticed, is invisible. It scores
  ONLY over reference speech time and reports:

      speaker-attributed word-time confusion   time-weighted, the headline
      word accuracy                            unweighted, one vote per word

  THIS IS NOT DER AND MUST NEVER BE CALLED DER, in code, in output, or in a
  doc quoting the output. DER has a miss and a false-alarm term; this metric
  structurally cannot have either. A DER-shaped name on a confusion-only
  number is exactly the kind of borrowed authority that put "21/21" next to a
  degenerate gate in the first place.

  FULL DER (score_full_der).  Truth is an exhaustive timeline — every second
  of reference speech labelled, as an RTTM. Miss, false alarm and speaker
  confusion are all real, and DER is their sum. Use this only for a fixture
  whose truth is genuinely exhaustive (AMI; eventually Room T, which is 104 s
  and cheap to label end to end). Calling it on word-referenced truth would
  report a huge miss rate that means nothing but "the reference is sparse".

THE FLOORS ARE MANDATORY. Every scoring call returns, and every printed table
shows, three predictors that look at no audio at all:

  all-to-dominant   every word to the truth's single busiest speaker. On a
                    corpus of 1:1 calls this is a very strong predictor, and
                    a diarizer that does not clear it is not diarizing.
  random            each word to a uniformly random truth speaker, seeded
                    (ATTR_RANDOM_SEED) so the floor is reproducible.
  count-oracle      the right NUMBER of speakers with degenerate attribution:
                    everything to one label except one token word per extra
                    label. It exists to make the point that a count metric
                    and an attribution metric measure different things — this
                    predictor scores 100% on the count gate and near-floor
                    here.

Over-count and under-count are reported SEPARATELY and never summed, because
they are not equally repairable: merge-only post-processing can delete a
surplus speaker and can never invent a missing one (CORRECTION.md §b).

THE UNCERTAINTY BAND IS PART OF THE SCORE. truth_words.json carries an
`audit` block recording how many auto-accepted words were re-checked by hand
and how many of those the check disagreed with. That disagreement rate is the
error bar on every number derived from that file, and format_rows() prints it
on the same line as the score. A truth file with no audit block is reported
as UNAUDITED rather than as perfect.

-----------------------------------------------------------------------------
TRUTH FILE SCHEMA — test/fixtures/<slug>/truth_words.json
-----------------------------------------------------------------------------

    {
      "version": 1,
      "track": "mic" | "system",          # which track the labels are for
      "source": "corrected-neural"        # dispute-first: hypotheses proposed,
              | "from-scratch",           # human corrected the disputed ones
      "speakers": {"A": "free-text note", ...},   # label -> what it is
      "words": [{"s": 12.34, "e": 12.61, "spk": "A"}, ...],
      "excluded": [[s, e], ...],          # spans NOT scored (noise, music,
                                          #   unintelligible, tool artefacts)
      "overlap": [[s, e], ...],           # spans with >1 voice active; scored
                                          #   by default, skippable per-run
      "audit": {"sampled": 120, "disagreed": 3},
      "labelled_at": "2026-08-09T14:20:00"
    }

  * `spk` is a short label, not a name. Names are the product's job; truth
    only needs identity. "A" is whoever "A" is.
  * `words` need not cover every word of the transcript: a word the labeller
    could not resolve is simply absent, and absence is not an error. What is
    present is what is scored.
  * `excluded` wins over everything: a word intersecting an excluded span is
    dropped from the denominator, so an exclusion can never be gamed into
    looking like an improvement — it only ever shrinks the evidence.
  * `source` records whether the labeller started from a machine hypothesis
    (fast, but biased toward agreeing with it) or from nothing (slow, and the
    only control on that bias). A corpus that is entirely "corrected-neural"
    cannot prove the neural engine is right; that is why label_turns.py has a
    --from-scratch mode and why this field is not optional.

The file is written by tools/label_turns.py and read by
tools/eval_diarization.py's ATTRIBUTION section. It is gitignored along with
the rest of test/fixtures/ — these are word-level labels of the founder's own
confidential meetings.
"""

import json
import math
import os
import random
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

FRAME = 0.01  # seconds — the frame-based metric's resolution

TRUTH_FILE = "truth_words.json"
TRUTH_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Tunables. Patchable by eval_diarization's --attribution-set, whitelisted
# there so a typo cannot silently do nothing. Same job as ECHO_TUNABLES: the
# supported way to prove this section would in fact catch a change.
# ---------------------------------------------------------------------------
ATTR_RANDOM_SEED = 20260809      # the random floor's seed; changing it moves a floor
ATTR_COLLAR_S = 0.25             # full-DER mode only; word mode has no boundaries
ATTR_EXCLUDE_OVERLAP = 0         # 1 = drop overlap spans from the word denominator
# GATE PROOF ONLY. Fraction of hypothesis words whose label is deliberately
# corrupted (seeded) before scoring. It exists so that "the attribution gate
# would notice a regression" can be demonstrated by running the harness rather
# than asserted in a comment — the same role --echo-set plays for drop_echo.
# Never non-zero in a run whose numbers are quoted.
ATTR_HYP_PERTURB = 0.0


# ---------------------------------------------------------------------------
# FRAME-BASED CORE — lifted verbatim from score_multiparty.py.
#
# These three functions were the only attribution metric in the tree. They are
# moved here unchanged (not "cleaned up") so that score_multiparty.py's output
# is byte-identical across the extraction and so the eval harness and the AMI
# scorer can never drift into computing two different things under one name.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Truth files
# ---------------------------------------------------------------------------

def truth_path(fixture_dir):
    return Path(fixture_dir) / TRUTH_FILE


def load_truth_words(path):
    """Read and validate a truth_words.json. Returns the dict or raises ValueError.

    Validation is deliberately strict about the things a scoring run cannot
    recover from (version, word shape, label presence) and deliberately silent
    about everything else: a truth file is hand-produced and will accumulate
    fields this module has never heard of.
    """
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("%s: not a JSON object" % path)
    ver = data.get("version")
    if ver != TRUTH_SCHEMA_VERSION:
        raise ValueError("%s: version %r, this module reads %d"
                         % (path, ver, TRUTH_SCHEMA_VERSION))
    words = data.get("words")
    if not isinstance(words, list) or not words:
        raise ValueError("%s: no words" % path)
    clean = []
    for i, w in enumerate(words):
        try:
            s, e, spk = float(w["s"]), float(w["e"]), str(w["spk"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("%s: word %d is not {s,e,spk}: %r" % (path, i, w))
        if e < s:
            raise ValueError("%s: word %d ends before it starts" % (path, i))
        clean.append({"s": s, "e": e, "spk": spk})
    clean.sort(key=lambda w: (w["s"], w["e"]))
    data["words"] = clean
    data["excluded"] = [[float(a), float(b)] for a, b in data.get("excluded") or []]
    data["overlap"] = [[float(a), float(b)] for a, b in data.get("overlap") or []]
    return data


def save_truth_words(path, data):
    """Write a truth file atomically: temp file in the same directory, fsync,
    os.replace. A half-written truth file is worse than none — it would score,
    and it would score wrong."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".truth-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def audit_band(truth):
    """The uncertainty band a truth file carries, from its audit block.

    Returns {"sampled", "disagreed", "rate", "hi95", "text"}. `rate` is the
    point estimate of how often the fast dispute-first pass got a word wrong
    where it never asked; `hi95` is the upper end of a 95% Wilson interval on
    that rate, which is the honest number to quote when the sample is small.
    A file with no audit block returns rate None and text "UNAUDITED" — the
    absence of a measurement is not a measurement of zero.
    """
    a = (truth or {}).get("audit") or {}
    n = int(a.get("sampled") or 0)
    k = int(a.get("disagreed") or 0)
    if n <= 0:
        return {"sampled": 0, "disagreed": 0, "rate": None, "hi95": None,
                "text": "UNAUDITED (no sample re-checked; the band on every "
                        "number from this truth is unknown)"}
    p = k / n
    z = 1.96
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    hi = min(1.0, centre + half)
    return {"sampled": n, "disagreed": k, "rate": p, "hi95": hi,
            "text": "audit %d/%d disagreed = %.1f%% (95%% upper %.1f%%)"
                    % (k, n, p * 100, hi * 100)}


def truth_to_rttm(truth, uri="fixture"):
    """Truth words -> RTTM lines, merging consecutive same-speaker words.

    Only meaningful for EXHAUSTIVELY labelled truth: an RTTM asserts that the
    spans it lists are all the speech there is, and word-referenced truth does
    not assert that. label_turns.py's --export-rttm refuses on truth that was
    not produced by --from-scratch for exactly this reason.
    """
    runs = []
    for w in truth["words"]:
        if runs and runs[-1][2] == w["spk"] and w["s"] - runs[-1][1] < 0.5:
            runs[-1][1] = max(runs[-1][1], w["e"])
        else:
            runs.append([w["s"], w["e"], w["spk"]])
    return ["SPEAKER %s 1 %.3f %.3f <NA> <NA> %s <NA> <NA>"
            % (uri, s, e - s, spk) for s, e, spk in runs if e > s]


# ---------------------------------------------------------------------------
# Hypotheses
#
# A hypothesis is always expressed as SPANS: [(start, end, label)]. Turns from
# the neural engine are already spans; labelled transcript segments become
# spans by taking each word (or, word-less, the whole segment). Scoring then
# asks one question per truth word: which label was active at its centre.
#
# The "active at its centre, else nearest edge" rule is diarization.
# assign_by_turns' rule, copied deliberately: scoring a hypothesis by a
# different attribution rule than the product uses would measure the rule, not
# the engine.
# ---------------------------------------------------------------------------

def spans_from_turns(turns):
    """[(s, e, idx)] neural turns -> hypothesis spans with string labels."""
    return [(float(s), float(e), "n%d" % int(lab)) for s, e, lab in turns]


def spans_from_segments(segs):
    """Labelled transcript segments (diarize_track / assign_by_turns output)
    -> one span per word, falling back to the whole segment when a segment
    carries no word timings."""
    out = []
    for seg in segs:
        lab = "s%d" % int(seg.get("speaker_idx", 0))
        words = [w for w in (seg.get("words") or [])
                 if w.get("s") is not None and w.get("e") is not None]
        if words:
            out += [(float(w["s"]), float(w["e"]), lab) for w in words]
        else:
            out.append((float(seg["start"]), float(seg["end"]), lab))
    return out


def spans_from_windows(windows, labels):
    """Cached ECAPA windows + cluster labels -> hypothesis spans.

    This is the CLASSIC arm as the eval harness can replay it: cluster() over
    the frozen embeddings, then the window's own span carries its label. It is
    NOT identical to diarize_track's output, which snaps labels to word
    centres via the nearest window CENTRE; the difference shows up only where
    windows do not tile the speech, and scoring against word-referenced truth
    resolves it the same way for every arm.
    """
    return [(float(w[0]), float(w[1]), "c%d" % int(lab))
            for w, lab in zip(windows, labels)]


def label_at(spans):
    """Callable t -> label. Covering span wins; otherwise the nearest edge.

    Mirrors diarization.assign_by_turns.label_at. Returns None only when there
    are no spans at all, which the caller reports as unattributed rather than
    silently scoring as wrong-in-a-particular-way.
    """
    if not spans:
        return lambda t: None
    ordered = sorted(spans, key=lambda x: (x[0], x[1]))
    starts = [s[0] for s in ordered]
    from bisect import bisect_right

    def fn(t):
        i = bisect_right(starts, t) - 1
        best, best_d = ordered[0][2], float("inf")
        for j in (i, i + 1):
            if 0 <= j < len(ordered):
                s, e, lab = ordered[j]
                if s <= t <= e:
                    return lab
                d = min(abs(t - s), abs(t - e))
                if d < best_d:
                    best, best_d = lab, d
        return best

    return fn


# ---------------------------------------------------------------------------
# Word-referenced scoring
# ---------------------------------------------------------------------------

def _intersects(s, e, spans):
    return any(not (e <= a or s >= b) for a, b in spans)


def _scored_words(truth, exclude_overlap):
    """The words that count, and why the others do not."""
    excluded = truth.get("excluded") or []
    overlap = truth.get("overlap") or []
    kept, dropped_excl, dropped_ovl, in_overlap = [], 0, 0, 0
    for w in truth["words"]:
        if _intersects(w["s"], w["e"], excluded):
            dropped_excl += 1
            continue
        ovl = _intersects(w["s"], w["e"], overlap)
        if ovl:
            in_overlap += 1
            if exclude_overlap:
                dropped_ovl += 1
                continue
        kept.append(w)
    return kept, dropped_excl, dropped_ovl, in_overlap


def _perturb_labels(hyp_labels, rate, seed):
    """Corrupt a seeded fraction of hypothesis labels. Gate proof only.

    An EXACT count is corrupted — ceil(rate * n), at least one — rather than
    each label being flipped with probability `rate`. A knob whose whole job is
    to prove the gate bites must bite every time it is turned: with per-label
    coin flips a small fixture can draw zero flips at a perfectly ordinary seed
    (10 words at 0.3 does exactly that at ATTR_RANDOM_SEED), and a gate proof
    that silently no-ops is worse than no gate proof.
    """
    if rate <= 0 or not hyp_labels:
        return hyp_labels
    pool = sorted({l for l in hyp_labels if l is not None})
    if len(pool) < 2:
        pool = pool + ["__perturbed__"]
    n = len(hyp_labels)
    k = min(n, max(1, int(math.ceil(rate * n))))
    rng = random.Random(seed ^ 0x5EED)
    victims = set(rng.sample(range(n), k))
    out = list(hyp_labels)
    for i in sorted(victims):
        alt = [p for p in pool if p != out[i]] or pool
        out[i] = rng.choice(alt)
    return out


def _confusion(words, hyp_labels):
    """Core arithmetic shared by the real arms and by every floor.

    Hungarian assignment on SHARED WORD TIME between truth speakers and
    hypothesis labels, then correct/incorrect per word. Returns the metric
    block; the caller adds the floors so the floors themselves do not recurse.
    """
    from scipy.optimize import linear_sum_assignment

    truth_names = sorted({w["spk"] for w in words})
    hyp_names = sorted({l for l in hyp_labels if l is not None})
    ti = {n: i for i, n in enumerate(truth_names)}
    hj = {n: j for j, n in enumerate(hyp_names)}
    shared = np.zeros((len(truth_names), max(1, len(hyp_names))))
    for w, lab in zip(words, hyp_labels):
        if lab is None:
            continue
        shared[ti[w["spk"]], hj[lab]] += (w["e"] - w["s"])
    mapping = {}
    if hyp_names:
        ri, hjj = linear_sum_assignment(-shared)
        for i, j in zip(ri, hjj):
            if j < len(hyp_names):
                mapping[hyp_names[j]] = truth_names[i]

    total_t = sum(w["e"] - w["s"] for w in words)
    correct_t = 0.0
    correct_n = 0
    unattributed = 0
    for w, lab in zip(words, hyp_labels):
        if lab is None:
            unattributed += 1
            continue
        if mapping.get(lab) == w["spk"]:
            correct_t += (w["e"] - w["s"])
            correct_n += 1
    n = len(words)
    return {
        "scored_words": n,
        "scored_word_time": round(total_t, 3),
        "word_time_confusion": (1.0 - correct_t / total_t) if total_t else 0.0,
        "word_accuracy": (correct_n / n) if n else 0.0,
        "unattributed_words": unattributed,
        "truth_speakers": len(truth_names),
        "hyp_speakers": len(hyp_names),
        "over_count": max(0, len(hyp_names) - len(truth_names)),
        "under_count": max(0, len(truth_names) - len(hyp_names)),
        "mapping": {k: v for k, v in sorted(mapping.items())},
    }


def _floor_dominant(words):
    counts = {}
    for w in words:
        counts[w["spk"]] = counts.get(w["spk"], 0.0) + (w["e"] - w["s"])
    top = max(counts, key=lambda k: (counts[k], k)) if counts else "?"
    return ["D:%s" % top] * len(words)


def _floor_random(words, seed):
    names = sorted({w["spk"] for w in words})
    rng = random.Random(seed)
    return ["R:%s" % rng.choice(names) for _ in words]


def _floor_count_oracle(words):
    """The right NUMBER of speakers, degenerate attribution.

    Everything goes to one label; the last (k-1) words in time each get a
    label of their own so the emitted count equals the truth count exactly.
    This predictor is PERFECT on the count gate eval_diarization headlines and
    approximately as bad as all-to-dominant here, which is the whole point of
    carrying it: it separates "knows how many" from "knows who".
    """
    k = len({w["spk"] for w in words})
    labs = ["O:0"] * len(words)
    for i in range(1, k):
        idx = len(words) - i
        if idx >= 0:
            labs[idx] = "O:%d" % i
    return labs


def score_word_reference(truth, hyp_spans, seed=None, exclude_overlap=None,
                         perturb=None):
    """Score a hypothesis against word-level truth. Returns a result dict.

    hyp_spans is [(start, end, label)] — see spans_from_turns /
    spans_from_segments / spans_from_windows.

    The reference claims time only where the transcript has words, so this
    reports CONFUSION and never miss or false alarm. See the module docstring
    for why the result of that must not be called DER.
    """
    seed = ATTR_RANDOM_SEED if seed is None else seed
    exclude_overlap = (bool(ATTR_EXCLUDE_OVERLAP) if exclude_overlap is None
                       else bool(exclude_overlap))
    perturb = ATTR_HYP_PERTURB if perturb is None else perturb

    words, n_excl, n_ovl_dropped, n_ovl = _scored_words(truth, exclude_overlap)
    if not words:
        raise ValueError("no scorable words left after exclusions")

    at = label_at(hyp_spans)
    hyp_labels = [at((w["s"] + w["e"]) / 2.0) for w in words]
    hyp_labels = _perturb_labels(hyp_labels, perturb, seed)

    out = _confusion(words, hyp_labels)
    out["mode"] = "word-reference"
    out["excluded_words"] = n_excl
    out["overlap_words"] = n_ovl
    out["overlap_words_dropped"] = n_ovl_dropped
    out["perturb"] = perturb
    out["audit"] = audit_band(truth)
    out["floors"] = {
        "dominant": _confusion(words, _floor_dominant(words)),
        "random": _confusion(words, _floor_random(words, seed)),
        "count_oracle": _confusion(words, _floor_count_oracle(words)),
    }
    out["beats_floors"] = all(
        out["word_time_confusion"] <= f["word_time_confusion"]
        for f in out["floors"].values())
    return out


# ---------------------------------------------------------------------------
# Full-DER mode
# ---------------------------------------------------------------------------

def _turn_floors(ref_turns, seed):
    """Degenerate hypotheses on an exhaustive reference timeline."""
    names = sorted({t[2] for t in ref_turns})
    dur = {}
    for s, e, n in ref_turns:
        dur[n] = dur.get(n, 0.0) + (e - s)
    top = max(dur, key=lambda k: (dur[k], k)) if dur else "?"
    rng = random.Random(seed)
    ordered = sorted(ref_turns)
    dominant = [(s, e, "D") for s, e, _ in ordered]
    rand = [(s, e, "R:%s" % rng.choice(names)) for s, e, _ in ordered]
    oracle = [[s, e, "O:0"] for s, e, _ in ordered]
    for i in range(1, len(names)):
        idx = len(oracle) - i
        if idx >= 0:
            oracle[idx][2] = "O:%d" % i
    del top
    return {"dominant": dominant, "random": rand,
            "count_oracle": [tuple(x) for x in oracle]}


def score_full_der(ref_turns, hyp_turns, total_s, collar=None, seed=None):
    """Frame-based miss / false alarm / confusion / DER against EXHAUSTIVE truth.

    Only valid when every second of reference speech is labelled. On sparse
    (word-referenced) truth the miss term measures the reference's sparseness
    and nothing else — use score_word_reference there.
    """
    collar = ATTR_COLLAR_S if collar is None else collar
    seed = ATTR_RANDOM_SEED if seed is None else seed

    def one(hyp):
        miss, fa, conf = score(ref_turns, hyp, total_s, collar=collar)
        return {"miss": miss, "false_alarm": fa, "confusion": conf,
                "der": miss + fa + conf,
                "truth_speakers": len({t[2] for t in ref_turns}),
                "hyp_speakers": len({t[2] for t in hyp})}

    out = one(hyp_turns)
    out["mode"] = "full-der"
    out["collar"] = collar
    out["over_count"] = max(0, out["hyp_speakers"] - out["truth_speakers"])
    out["under_count"] = max(0, out["truth_speakers"] - out["hyp_speakers"])
    out["floors"] = {k: one(v) for k, v in _turn_floors(ref_turns, seed).items()}
    out["beats_floors"] = all(out["der"] <= f["der"] for f in out["floors"].values())
    return out


# ---------------------------------------------------------------------------
# HYBRID ARBITRATION — extracted from score_multiparty.run_hybrid.
#
# The product's gate is a real rule with real history and it must be scored as
# written, not paraphrased. This function holds the arbitration; the callers
# supply the engine outputs. score_multiparty.py hands it a live neural run;
# the eval harness hands it FROZEN turns off disk and a forced_turns_fn that
# looks up a frozen forced run, because a --check that shells out to a CoreML
# binary is not a gate, it is a build step.
#
# HONEST LIMIT, stated because the alternative is a silent one: this replays
# pipeline._neural_refine AS OF 2026-08-03, which is what score_multiparty.py
# has always replayed. _neural_refine has since gained an upward rule (when
# the neural engine hears MORE substantial voices than the classic cascade on
# an unforced track, its turns and its count win — MULTIPARTY.md addendum).
# That rule is not replayed here. Any number this produces is therefore a
# measurement of the 2026-08-03 gate, and the report says so rather than
# calling it "the product".
# ---------------------------------------------------------------------------

def hybrid_arbitrate(segs, classic_labelled, n_classic, neural_turns, k_neural,
                     forced_turns_fn, assign_by_turns=None):
    """(segments, count, route). The 2026-08-03 gate, engine-agnostic.

    forced_turns_fn(n) returns a turn list forced to n speakers, or None when
    no such turn set is available (the frozen-turns case). A None answer routes
    to classic with a route string that says why, instead of pretending.
    """
    if assign_by_turns is None:
        import diarization
        assign_by_turns = diarization.assign_by_turns
    if n_classic < 2:
        return classic_labelled, n_classic, "classic (count<2)"
    if k_neural == n_classic:
        out, k = assign_by_turns(segs, neural_turns)
        if k == n_classic:
            return out, k, "neural (agreed)"
        return classic_labelled, n_classic, "classic (assign mismatch)"
    if k_neural > n_classic:
        forced = forced_turns_fn(n_classic)
        if forced is None:
            return classic_labelled, n_classic, "classic (no forced turns)"
        out, k = assign_by_turns(segs, forced)
        if k == n_classic:
            return out, k, "neural (forced down)"
        return classic_labelled, n_classic, "classic (forced mismatch)"
    return classic_labelled, n_classic, "classic (neural saw fewer)"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_ARM_W = 22


def format_rows(rows, title=None, mode="word-reference"):
    """Render scored arms as a table. rows is [(arm_name, result_dict)].

    The floors are rendered as ordinary rows, below a rule, so a result that
    fails to clear them is visible in the same glance as the result itself.
    Every table carries the audit band of the truth it came from, because a
    score quoted without its error bar is a score quoting more precision than
    it has.
    """
    lines = []
    if title:
        lines.append(title)
    if mode == "word-reference":
        hdr = ("%-*s %7s %7s %6s %6s %6s %6s"
               % (_ARM_W, "arm", "conf%", "wordacc", "spk", "over", "under", "unatt"))
    else:
        hdr = ("%-*s %7s %7s %7s %7s %6s %6s"
               % (_ARM_W, "arm", "DER%", "miss%", "fa%", "conf%", "over", "under"))
    lines.append(hdr)
    lines.append("-" * len(hdr))

    def row(name, r):
        if mode == "word-reference":
            return ("%-*s %6.1f%% %6.1f%% %6d %6d %6d %6d"
                    % (_ARM_W, name, r["word_time_confusion"] * 100,
                       r["word_accuracy"] * 100, r["hyp_speakers"],
                       r["over_count"], r["under_count"], r["unattributed_words"]))
        return ("%-*s %6.1f%% %6.1f%% %6.1f%% %6.1f%% %6d %6d"
                % (_ARM_W, name, r["der"] * 100, r["miss"] * 100,
                   r["false_alarm"] * 100, r["confusion"] * 100,
                   r["over_count"], r["under_count"]))

    floors = None
    for name, r in rows:
        lines.append(row(name, r))
        floors = floors or r.get("floors")
    if floors:
        lines.append("-" * len(hdr))
        for fname, key in (("floor: all-to-dominant", "dominant"),
                           ("floor: random (seeded)", "random"),
                           ("floor: count-oracle", "count_oracle")):
            if key in floors:
                lines.append(row(fname, floors[key]))
    first = rows[0][1] if rows else {}
    if mode == "word-reference":
        band = first.get("audit") or {}
        lines.append("  truth: %d words scored, %d excluded, %d in overlap; %s"
                     % (first.get("scored_words", 0), first.get("excluded_words", 0),
                        first.get("overlap_words", 0), band.get("text", "UNAUDITED")))
        lines.append("  conf% is speaker-attributed word-time confusion over "
                     "reference speech only.")
        lines.append("  It is NOT DER: word-referenced truth has no miss and no "
                     "false-alarm term.")
    return lines


def print_report(rows, title=None, mode="word-reference"):
    for line in format_rows(rows, title, mode):
        print(line)


# ---------------------------------------------------------------------------
# Regression gate
# ---------------------------------------------------------------------------

# The metrics a baseline pins, and the direction that counts as WORSE. Counts
# are pinned exactly and in BOTH directions separately: an over-count and an
# under-count are not interchangeable (CORRECTION.md §b — merge-only
# post-processing repairs the first and can never repair the second), so they
# are never summed into one "count error" that could net out.
GATED_METRICS = (
    ("word_time_confusion", "up", 4),
    ("word_accuracy", "down", 4),
    ("over_count", "up", 0),
    ("under_count", "up", 0),
)


def baseline_row(result):
    """The tuple a baseline generation stores for one (fixture, arm)."""
    return (round(result["word_time_confusion"], 4),
            round(result["word_accuracy"], 4),
            int(result["over_count"]),
            int(result["under_count"]))


def regressions(baseline, live):
    """Compare {(key, arm): tuple} baseline against live. Returns failure strings.

    A metric that moved in the BETTER direction is not a failure and is not
    silently accepted either — the caller reports movement separately, the
    same way BEHAVIOUR_BASELINE reports a count that changed for the better.
    Missing baseline entries are the caller's problem (they are coverage, not
    regression) and are not reported here.
    """
    fails = []
    for pair, want in sorted(baseline.items()):
        got = live.get(pair)
        if got is None:
            continue
        for i, (name, worse, places) in enumerate(GATED_METRICS):
            w, g = want[i], got[i]
            if places:
                w, g = round(float(w), places), round(float(g), places)
            bad = (g > w) if worse == "up" else (g < w)
            if bad:
                fails.append("%s / %s: %s %s (baseline %s, now %s)"
                             % (pair[0], pair[1], name,
                                "rose" if worse == "up" else "fell", w, g))
    return fails


def regression_pairs(baseline, live):
    """(key, arm) pairs with at least one metric moving the wrong way."""
    out = set()
    for pair, want in baseline.items():
        got = live.get(pair)
        if got is None:
            continue
        for i, (_name, worse, places) in enumerate(GATED_METRICS):
            w, g = want[i], got[i]
            if places:
                w, g = round(float(w), places), round(float(g), places)
            if (g > w) if worse == "up" else (g < w):
                out.add(pair)
    return out


def moved(baseline, live):
    """(key, arm) pairs that differ from the baseline WITHOUT regressing.

    A pair that regressed is reported by regressions(); listing it here too,
    under a heading that says "in the better direction", would be a report
    contradicting itself on the same screen.
    """
    bad = regression_pairs(baseline, live)
    return sorted(p for p, want in baseline.items()
                  if p in live and p not in bad
                  and tuple(live[p]) != tuple(want))
