"""Speaker diarization: figure out who spoke when by clustering voice-prints.

Uses the ungated speechbrain ECAPA-TDNN speaker-embedding model (downloads
automatically on first use, no account or token needed) plus agglomerative
clustering. No cloud calls — everything runs locally.
"""

import logging
import math
import sys

import numpy as np

from config import (
    MODELS_DIR,
    ModelDownloadError,
    ensure_hf_files,
    hf_download_plan,
)

log = logging.getLogger("meetingscribe.diarization")

EMBED_SR = 16000
WINDOW_S = 2.0
HOP_S = 1.0
MIN_WINDOW_S = 0.4
BATCH_SIZE = 16

# --- How many voices? (defect D2) ---------------------------------------------
# Raw threshold clustering does NOT decide the count. On the 18 real tracks with
# more than 21 windows it returns 22-863 clusters (measured over test/fixtures/),
# always above the ceiling below, so the k=MAX_AUTO_SPEAKERS refit discards it.
# Only the four tiny caches stay under it (Call J 4, Call N 8, Demo 3, E2E 2) and
# skip the refit. Either way the count is decided by what survives
# _fold_weak_clusters(), which is why the rule there is the whole game.
#
# The ceiling is a ceiling, not the answer: it caps the refit, and the fold rule
# then reduces to the real count. MEASURED ALTERNATIVE, rejected: dropping the
# refit entirely and folding straight from the raw threshold clusters scores
# exactly the same (real 21/21 and synthetic 10/11 either way), takes 4.1 s
# instead of milliseconds on the 4594-window in-person fixture, and blows that
# fixture out to 21 clusters — so the refit stays. (Re-measured with
# FOLD_KEEP_ABOVE_S in place; before the ceiling existed the same experiment
# gave 8 clusters there, because the fold used to swallow the large ones.)
MAX_AUTO_SPEAKERS = 8

# Auto mode: a cluster carrying less speech than this is a candidate for folding
# into its nearest voice — short interjections ("yeah", "okay") otherwise become
# phantom extra speakers. It is only a CANDIDATE now; see _fold_weak_clusters.
MIN_CLUSTER_S = 10.0

# --- The absolute ceiling: too much speech to be anything but a person ---------
# MIN_CLUSTER_S is a fold-BELOW floor. This is its inverse, a never-fold-ABOVE
# ceiling, and it is checked before any other signal: a cluster carrying this
# much speech is kept whatever its fragmentation and whatever its ratio to the
# leading voice.
#
# WHY IT HAD TO EXIST. Without it the fold rule was keyed entirely on turn
# LENGTH — both signals below measure how a cluster's speech is SHAPED, neither
# measures how much of it there is — so a genuine participant who speaks in
# short scattered turns was folded no matter how much total speech they had.
# That is the exact "2 detected as 1" failure the project exists to kill.
# Measured: Room W, the only genuinely multi-speaker in-person meeting in the
# corpus, lost a 203.86 s cluster (7 clusters -> 6).
#
# MEASURED, over all 93 fold events across all 33 fixtures (22 real caches + 11
# labelled synthetic). Every cluster the rule removes, largest first:
#     203.86 s  Room W      <- the one the reviewers call a person
#     129.92 s  Call K      <- largest phantom in the corpus (truth: 1 voice)
#     119.74 s  Call G
#      97.92 s  Call C
#      87.76 s  Call H  ... and 88 more, every one below 88 s.
# So the premise "phantoms carry small totals" is FALSE: four fold events carry
# more than 87 s and the largest phantom carries 129.92 s. The only usable
# separation is the gap between 129.92 s and 203.86 s.
#
# SWEPT end to end through this module's own cluster() (the fold is greedy, so a
# protection rule cannot be scored by filtering a recorded log — every value was
# re-run through the whole loop):
#     129.90 s   real 20/21, Room W 7 — Call K's phantom is protected too (1->2)
#     130.00 s   real 21/21, synthetic 10/11, Room W 7
#     203.80 s   real 21/21, synthetic 10/11, Room W 7  — identical throughout
#     203.90 s   real 21/21, synthetic 10/11, Room W 6  — the person is folded
#     no ceiling real 21/21, synthetic 10/11, Room W 6  — the state before this
# So the stable interval is (129.92, 203.86]. 150.0 sits inside it with +20.08 s
# of margin over the largest measured phantom and -53.86 s under the cluster
# being protected. It is placed on the LOW side on purpose: leave the interval
# downwards and the corpus gains a phantom speaker, leave it upwards and the
# corpus loses a real one — and losing a real one is the failure this project
# was started to fix, so the asymmetry is deliberate.
#
# BOTH EDGES ARE SINGLE FIXTURES, and this is the weakest-evidenced number in
# the file. 129.92 s is one cluster in one meeting whose truth is INFERRED, and
# 203.86 s is one cluster in the ONE fixture whose truth the harness records as
# UNKNOWN (Room W is an open hackathon floor with no attendee roster) — so the
# upper edge rests on the judgement that 204 seconds of speech is a person, not
# on a measurement. Re-sweep this whenever a fixture is added.
FOLD_KEEP_ABOVE_S = 150.0

# --- The two signals that separate a real voice from a same-voice split --------
# A cluster is folded away only when it is BOTH temporally scattered AND small.
#
#   fragmentation  = the cluster's contiguous runs / its windows, in time order.
#                    A real participant takes turns, so their windows arrive in a
#                    few long runs (low). A same-voice split is sprinkled through
#                    the dominant speaker's windows in one-window slivers (high).
#   duration ratio = the cluster's seconds / the biggest cluster's seconds.
#
# REQUIRING BOTH IS THE POINT, and the corpus proves each alone is wrong:
#   - Duration ratio alone folds a quiet-but-real participant. In-person "Room P"
#     and "Room Q" hold a genuine second person at duration ratio 0.337 and 0.328
#     — under any ratio gate big enough to catch the phantoms, which reach 0.356.
#     Room P is saved only by its low fragmentation (0.139); Room Q is now saved
#     by FOLD_KEEP_ABOVE_S, its second voice carrying 657.9 s.
#   - Fragmentation alone folds a real speaker who interjects often. The
#     rapid-alternation fixture (two genuine voices swapping every ~3 s) sits at
#     fragmentation 0.333, above the gate, and is saved only by its ratio.
# And BOTH TOGETHER still fold a real participant who is quiet AND scattered,
# however long they talk — which is why FOLD_KEEP_ABOVE_S is checked ahead of
# both of them.
#
# MEASURED (2026-07-25, tools/eval_diarization.py over test/fixtures/ plus the
# labelled test/synthetic/ fixtures; ground truth, not today's behaviour):
#   real cached meetings   6/21 correct -> 21/21
#   synthetic known truth  9/11 correct -> 10/11
# The one loss is test/synthetic/synthetic-two-backchannel: a genuine second
# person whose entire contribution is 12.8 s of scattered one-word "mm-hm"s.
# RE-MEASURED against every signal the fold can see, because "indistinguishable"
# is a claim that has to be shown, not asserted. Its cluster sits INSIDE the real
# corpus's phantom band on all of them:
#   total seconds  12.8 — 26 real phantom folds carry MORE, up to 129.9 s
#   fragmentation  1.000 — the top of the phantom range
#   duration ratio 0.080 — mid-band (real phantoms span 0.001 .. 0.356)
#   centroid distance to the cluster it folds into 0.851 — real phantoms span
#                  0.42 .. 1.010, and Call A's 12.0 s phantom is HIGHER at 1.010
#   window count   8 — identical to that same Call A phantom
#   full-length-window fraction 0.000 — identical to Room Q's 20.1 s phantom
#   raw threshold clusters 2 (no k=8 refit) — identical to the two same-voice
#                  split fixtures, both of which are KNOWN phantoms
# Within-cluster tightness DOES separate it (0.049 against 0.12 .. 0.56 on real
# tracks) but that is a fingerprint of the synthetic generator's fixed noise
# sigma, not a property of voices, so it is not a signal.
#
# The only rule found that returns 2 here AND holds 21/21 on the real corpus is
# "seconds >= 12.5 AND centroid distance >= 0.85" (swept end to end: real 21/21,
# synthetic 11/11). It is rejected deliberately. Its 12.5 exists only inside the
# 0.8 s gap between Call A's 12.0 s phantom and this 12.8 s cluster, and its 0.85
# only inside the 0.013 gap above Room Q's 0.838 phantom; both edges are set by a
# single fixture, so it scores perfectly on the corpus it was fitted to and
# predicts nothing. Separating this case needs a real speaker-verification score,
# i.e. the sherpa-onnx escalation in docs/COUNT_ESTIMATION_DESIGN.md — not
# another threshold.
#
# SWEPT, with the separation margin (the distance either threshold can move
# before ANY fixture's answer changes), not just a pass/fail:
#   FOLD_MAX_FRAGMENTATION  stable over 0.139 .. 0.3333  (chosen 0.30)
#       Bounded below by Room P's genuine second voice at 0.139: it is too quiet
#       (duration ratio 0.337) for the ratio branch to save and too small (139.5
#       s) for the ceiling above, so this gate is the only thing holding it. At
#       0.138 it is folded and the corpus drops to 20/21. Bounded above by
#       phantoms at exactly 0.333: at 0.3334 the folds in Call D, Room T and
#       Room U stop firing and the corpus drops to 18/21. Every value in between
#       scores identically.
#       TWO genuine second voices that used to bound this gate no longer do, and
#       that is FOLD_KEEP_ABOVE_S doing its job: Room Q's (fragmentation 0.148,
#       657.9 s) is now held by the ceiling, and Room T's (0.156 — ABOVE the
#       chosen gate) has always been held by its 0.918 duration ratio. The gate
#       is load-bearing for exactly one fixture now instead of three.
#   FOLD_MAX_DURATION_RATIO stable over 0.356 .. beyond 0.99  (chosen 0.50)
#       bounded below by the big-split phantom at 0.356. The upper edge is
#       unmeasurable on this corpus, so it is NOT set from the data: 0.50 says
#       "a cluster holding half the leading voice's speech is never folded, no
#       matter how scattered", which is what keeps the AND meaningful.
#
# WHY 0.30 AND NOT 0.25 — this is ONE measurement with TWO users. pipeline's
# MIC_ONLY_MAX_FRAGMENTATION asks the same question of the same quantity (the
# runner-up cluster's contiguous runs / its windows, over time-ordered windows)
# for the same purpose (real voice vs same-voice split), in the opposite
# polarity: pipeline accepts frag <= gate as a real voice, this file folds
# frag > gate as a phantom. They are complements of one constant and disagreed —
# 0.30 there, 0.25 here — which made a cluster at frag 0.27 a phantom to the
# clusterer and a real second voice to the fallback detector.
#
# 0.30 is the correct single value, measured on both sides:
#   this file needs the gate in [0.139, 0.3333]  (the sweep above).
#   pipeline needs the gate >= 0.250, or the two-medium-turns fixture's GENUINE
#       second speaker (fragmentation 0.250, KNOWN truth 2) reads as "just you".
#       It has no measured upper bound left: since this file started folding
#       properly, every same-voice and solo fixture arrives at pipeline as a
#       SINGLE cluster, so pipeline scores them "just you" without consulting
#       fragmentation at all. The only value it still sees above 0.250 is
#       two-rapid-alternation's 0.333 — and that is a genuine speaker, not a
#       phantom (see the note in tools/test_diarization.py).
# The intersection is therefore [0.250, 0.3333]. 0.25 sits exactly ON its lower
# edge with zero margin — one genuine speaker measured at the threshold itself.
# 0.30 sits inside it with 0.050 below and 0.033 above. Moving this file
# 0.25 -> 0.30 changes NOTHING on either corpus: no fold candidate anywhere has
# fragmentation in (0.25, 0.3333), the lowest phantom measured being 0.333. So
# the tie is broken on margin and on the direction of danger — too low folds
# real participants, which is the bug this whole area exists to fix.
FOLD_MAX_FRAGMENTATION = 0.30
FOLD_MAX_DURATION_RATIO = 0.50

# Identifies the embedding behaviour that produced a cached analysis.npz.
# BUMP IT whenever anything that changes window COORDINATES or embedding VALUES
# changes: the ECAPA checkpoint, WINDOW_S / HOP_S / MIN_WINDOW_S, build_windows(),
# the VAD gating, or the audio slicing inside embed_windows().
#
# pipeline._save_analysis_state() writes this number into analysis.npz and the
# recluster path refuses to reuse a cache whose number differs, recomputing
# instead. Without it, a change that alters embedding values while leaving
# window coordinates byte-identical (exactly what the deferred start_offset fix
# in embed_windows() does) makes a stale cache look perfectly loadable, so the
# recluster path would answer from old embeddings while Reprocess answers from
# new ones — the same meeting, two different speaker maps, no way to tell.
#
# Version 1 IS the behaviour shipped up to and including this revision. Caches
# written before the marker existed were produced by exactly this behaviour, so
# a MISSING marker is read as version 1 (see pipeline.recluster_meeting).
EMBED_VERSION = 1

_EMBEDDER = None


def load_mono_16k(path):
    """Load any WAV as float32 mono at 16 kHz."""
    import soundfile as sf
    from scipy.signal import resample_poly

    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if sr != EMBED_SR:
        g = math.gcd(int(sr), EMBED_SR)
        mono = resample_poly(mono, EMBED_SR // g, int(sr) // g)
    return np.ascontiguousarray(mono, dtype=np.float32)


def _pick_device():
    """Apple-GPU (MPS) when available — the voice embeddings are the last
    CPU-heavy step, and moving them to the GPU keeps the laptop cool."""
    try:
        import torch

        if sys.platform == "darwin" and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


ECAPA_REPO = "speechbrain/spkrec-ecapa-voxceleb"
# The files EncoderClassifier.from_hparams pulls for this checkpoint:
# hyperparams.yaml names the other four in its pretrainer block. Listed here so
# the download can be counted and reported BEFORE speechbrain starts fetching
# them one silent file at a time; embedding_model.ckpt is 83 MB of the 89.
# Anything speechbrain wants that is not here still downloads, just without
# progress, and anything here that a future checkpoint drops is skipped on 404.
ECAPA_FILES = ("hyperparams.yaml", "embedding_model.ckpt",
               "mean_var_norm_emb.ckpt", "classifier.ckpt", "label_encoder.txt")
ECAPA_LABEL = "the speaker model"


def _ecapa_savedir():
    return MODELS_DIR / "ecapa"


def _copy_skip_cache():
    """speechbrain's COPY_SKIP_CACHE strategy, or None when it has no such
    concept. Plain copies instead of symlinks — creating symlinks on Windows
    requires admin rights and fails with WinError 1314."""
    try:
        from speechbrain.utils.fetching import LocalStrategy
    except ImportError:  # speechbrain < 1.0
        return None
    return LocalStrategy.COPY_SKIP_CACHE


def _ecapa_where(strategy):
    """Where this speechbrain will put the files, as hf_hub_download kwargs.

    COPY_SKIP_CACHE means it downloads STRAIGHT INTO savedir (it passes
    local_dir=savedir), bypassing the shared HuggingFace cache; without it the
    files land in the cache under HF_HOME and are copied out. The prefetch has
    to aim at whichever of the two the load will read, or the "already
    downloaded" files are in the wrong place and the user pays twice.
    """
    if strategy is not None:
        return {"local_dir": str(_ecapa_savedir())}
    return {}


def embedder_download_size():
    """(bytes still to download, bytes in total) for the speaker model.

    (0, total) once it is on disk, (None, None) with no network. Lets a caller
    show a real total before committing the user to the wait.
    """
    return hf_download_plan(ECAPA_REPO, ECAPA_FILES,
                            **_ecapa_where(_copy_skip_cache()))


def _load_embedder(device, progress_cb=None):
    try:
        from speechbrain.inference.speaker import EncoderClassifier
    except ImportError:  # speechbrain < 1.0
        from speechbrain.pretrained import EncoderClassifier
    # speechbrain 1.1 sets self.device_type only for cpu/cuda and then reads
    # it unconditionally (its autocast context), so "mps" crashes the init.
    # A class-level default fills the gap; modules still land on self.device.
    if not hasattr(EncoderClassifier, "device_type"):
        EncoderClassifier.device_type = "cpu"
    kwargs = {
        "source": ECAPA_REPO,
        "savedir": str(_ecapa_savedir()),
        "run_opts": {"device": device},
    }
    # Fetch first, report the megabytes, THEN hand a fully cached directory to
    # speechbrain: from_hparams reports nothing at all while it downloads, and
    # on a fresh install this is 89 MB of dead air.
    strategy = _copy_skip_cache()
    if strategy is not None:
        _prefetch_ecapa(progress_cb, strategy)
        try:
            return EncoderClassifier.from_hparams(local_strategy=strategy, **kwargs)
        except TypeError:  # from_hparams predates the local_strategy argument
            pass
    _prefetch_ecapa(progress_cb, None)
    return EncoderClassifier.from_hparams(**kwargs)


def _prefetch_ecapa(progress_cb, strategy, bytes_cb=None):
    return ensure_hf_files(ECAPA_REPO, ECAPA_FILES, label=ECAPA_LABEL,
                           progress_cb=progress_cb, bytes_cb=bytes_cb,
                           **_ecapa_where(strategy))


def download_speaker_model(progress_cb=None):
    """Fetch the speaker model NOW, into the folder _load_embedder reads.

    For a caller that wants the download to happen somewhere the user expects a
    wait (onboarding) rather than in the middle of their first meeting. Returns
    the bytes downloaded, raises ModelDownloadError, and is idempotent and cheap
    once the model is on disk.

    progress_cb(done_bytes, total_bytes, label): the numeric form, for a caller
    drawing its own bar. Use _prefetch_ecapa / the pipeline path for the
    one-string form.
    """
    return _prefetch_ecapa(None, _copy_skip_cache(), bytes_cb=progress_cb)


def _get_embedder(progress_cb=None):
    global _EMBEDDER
    if _EMBEDDER is None:
        device = _pick_device()
        try:
            _EMBEDDER = _load_embedder(device, progress_cb)
            if device != "cpu":  # prove the GPU path works before trusting it
                import torch

                with torch.no_grad():
                    _EMBEDDER.encode_batch(
                        torch.zeros(1, EMBED_SR), wav_lens=torch.ones(1)
                    )
        except ModelDownloadError:
            # A download that did not finish is not a GPU problem, and the CPU
            # retry below would spend another three attempts arriving at the
            # same answer. The message already says what to do.
            raise
        except Exception as exc:
            if device == "cpu":
                raise
            # Loud on purpose: this fallback is 78x slower (595 windows:
            # 4.5 s on MPS, 351 s on CPU) and peaks at 3.1 GB where MPS
            # stays under 0.7 — a meeting still completes, but if this line
            # appears in a log, THAT is why diarization crawled.
            log.error("GPU (%s) embedder failed (%s); falling back to CPU — "
                      "expect ~78x slower embedding and ~3 GB peak", device, exc)
            _EMBEDDER = _load_embedder("cpu", progress_cb)
    return _EMBEDDER


def build_windows(segments):
    """Sliding windows (start, end) covering the speech regions of a track."""
    windows = []
    for seg in segments:
        start, end = float(seg["start"]), float(seg["end"])
        if end - start < MIN_WINDOW_S:
            continue
        if end - start <= WINDOW_S:
            windows.append((start, end))
            continue
        t = start
        while t + MIN_WINDOW_S < end:
            windows.append((t, min(t + WINDOW_S, end)))
            if t + WINDOW_S >= end:
                break
            t += HOP_S
    return windows


def embed_windows(audio, windows, progress_cb=None):
    """ECAPA embedding for each (start, end) window. Returns (N, D) L2-normalised."""
    import torch

    # progress_cb reaches the loader, not just the embedding loop: on a fresh
    # install the first thing this call does is download 89 MB.
    model = _get_embedder(progress_cb)
    embeddings = []
    total = len(windows)
    with torch.no_grad():
        for batch_start in range(0, total, BATCH_SIZE):
            batch = windows[batch_start : batch_start + BATCH_SIZE]
            chunks = []
            for (t0, t1) in batch:
                # KNOWN OFF-BY-start_offset, DELIBERATELY NOT FIXED HERE.
                # Window times are on the MEETING timeline: pipeline._apply_offset
                # shifted this track's transcript by meta["tracks"][key]
                # ["start_offset"] so the two tracks line up with each other.
                # `audio` is the raw track WAV, whose samples start at 0. Slicing
                # at t * EMBED_SR therefore reads each window start_offset seconds
                # late; the arithmetically correct index is (t - start_offset).
                #
                # MEASURED MAGNITUDE: max 0.021 s across all 15 recordings
                # (Call D, system track — meetings are named by the corpus
                # pseudonyms in docs/DIARIZATION_AUDIT_ADDENDUM.md §0, the same
                # labels tools/eval_diarization.py prints); mic tracks are 0.000 s
                # on 14 of 15 and every other value is <= 0.016 s. That is about 1%
                # of a 2.0 s window.
                #
                # WHY IT IS DEFERRED: correcting it changes embedding VALUES while
                # leaving window COORDINATES byte-identical, so every already-cached
                # analysis.npz stays perfectly loadable while silently disagreeing
                # with freshly recomputed embeddings — the recluster path reads the
                # cache, Reprocess recomputes, and the same meeting gets two
                # different answers. It was also measured to flip the auto speaker
                # count on 3 of 9 real meetings, including Call H going
                # 1 -> 2 against a ground truth of 1 remote speaker: a regression.
                #
                # It belongs in the clustering wave, where embeddings are recomputed
                # anyway. Doing it there means bumping EMBED_VERSION above, which
                # forces every stale cache to be rebuilt instead of silently mixed.
                i0, i1 = int(t0 * EMBED_SR), int(t1 * EMBED_SR)
                chunk = audio[max(0, i0) : min(len(audio), i1)]
                if len(chunk) < int(MIN_WINDOW_S * EMBED_SR):
                    chunk = np.zeros(int(MIN_WINDOW_S * EMBED_SR), dtype=np.float32)
                chunks.append(chunk)
            max_len = max(len(c) for c in chunks)
            wavs = torch.zeros(len(chunks), max_len)
            lens = torch.zeros(len(chunks))
            for i, c in enumerate(chunks):
                wavs[i, : len(c)] = torch.from_numpy(np.ascontiguousarray(c))
                lens[i] = len(c) / max_len
            out = model.encode_batch(wavs, wav_lens=lens)
            embeddings.append(out.squeeze(1).cpu().numpy())
            if progress_cb and total > BATCH_SIZE:
                done = min(batch_start + BATCH_SIZE, total)
                progress_cb(f"Analyzing voices… {done}/{total}")
    emb = np.concatenate(embeddings, axis=0).astype(np.float64)
    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return emb / norms


def _merge_tiny_clusters(labels, embeddings, min_windows=2):
    """Fold clusters with almost no support into their nearest big cluster."""
    labels = np.asarray(labels).copy()
    unique, counts = np.unique(labels, return_counts=True)
    big = unique[counts >= min_windows]
    small = unique[counts < min_windows]
    if len(big) == 0 or len(small) == 0:
        return labels
    centroids = {lab: embeddings[labels == lab].mean(axis=0) for lab in big}
    for lab in small:
        idx = labels == lab
        vec = embeddings[idx].mean(axis=0)
        best = max(centroids, key=lambda b: float(np.dot(vec, centroids[b])))
        labels[idx] = best
    return labels


def _fragmentation(labels, lab):
    """Contiguous runs of `lab` divided by its window count, in time order.

    Reads the label array as laid out. That IS time order: build_windows() walks
    the transcript segments in time and emits increasing window starts, and the
    cached windows in analysis.npz were written by build_windows(). Verified
    rather than assumed — over all 33 fixtures (22 real caches + 11 labelled
    synthetic) the array order equals the centre-sorted order and this function
    returns identical values either way. Keeping it parameterless is deliberate:
    every caller of cluster() then measures the same thing, including the ones
    that hold durations but not window coordinates.
    """
    mask = np.asarray(labels) == lab
    n = int(mask.sum())
    if n == 0:
        return 0.0
    runs = int(np.sum(mask[1:] & ~mask[:-1])) + int(mask[0])
    return runs / float(n)


def _fold_weak_clusters(labels, embeddings, durations, threshold):
    """Auto-mode cleanup: fold same-voice splits into their most similar voice,
    then merge cluster pairs whose centroids are clearly the same person.
    Returns the new labels.

    A cluster is folded only when it is temporally SCATTERED *and* SMALL — see
    FOLD_MAX_FRAGMENTATION / FOLD_MAX_DURATION_RATIO above for the measurement.
    Small means either small next to the leading voice or under the absolute
    MIN_CLUSTER_S floor; the floor is what keeps a 20-second meeting, where every
    cluster is a large share of a tiny total, from reporting a room full of
    people.

    Both of those signals are keyed on turn LENGTH, so on their own they fold a
    real participant who speaks in short scattered turns however much total
    speech they have. FOLD_KEEP_ABOVE_S is the unconditional ceiling that stops
    that, and it is tested FIRST so it outranks both: past it, a cluster is kept
    no matter how fragmented and no matter how small it is next to the leader.

    The scatter gate is new, and it is also the fix for the "2 detected as 1"
    bug: the previous rule folded ANY cluster under MIN_CLUSTER_S with no
    similarity or coherence floor at all, so a genuinely quiet participant was
    deleted unconditionally. A quiet participant speaks in coherent runs and now
    survives. (No fixture in either corpus exercises that path — every sub-floor
    cluster present is also scattered — so the protection is reasoned, not
    measured. It costs nothing: 0 fixtures change because of it.)
    """
    labels = np.asarray(labels).copy()
    embeddings = np.asarray(embeddings)
    durations = np.asarray(durations, dtype=np.float64)
    # Centroid distance below which two clusters are the same voice. Centroids
    # average out window noise, so same-speaker split clusters sit well below
    # the per-window linkage threshold; 0.9x catches splits like a speaker's
    # first seconds on a call clustering apart from the rest of their speech.
    merge_below = float(threshold) * 0.9

    def centroid(lab):
        vec = embeddings[labels == lab].mean(axis=0)
        norm = np.linalg.norm(vec)
        return vec / norm if norm else vec

    while True:
        unique = np.unique(labels)
        if len(unique) <= 1:
            return labels
        cents = {lab: centroid(lab) for lab in unique}
        seconds = {lab: float(durations[labels == lab].sum()) for lab in unique}
        # The leading voice anchors both signals and is never itself folded.
        leader = max(unique, key=lambda lab: seconds[lab])
        lead_s = seconds[leader]

        weak = []
        for lab in unique:
            if lab == leader:
                continue
            if seconds[lab] >= FOLD_KEEP_ABOVE_S:
                continue  # too much speech to be a split, whatever its shape
            if _fragmentation(labels, lab) <= FOLD_MAX_FRAGMENTATION:
                continue  # coherent runs — a real speaker, however quiet
            small = (seconds[lab] / lead_s) < FOLD_MAX_DURATION_RATIO if lead_s else True
            if small or seconds[lab] < MIN_CLUSTER_S:
                weak.append(lab)
        if weak:
            lab = min(weak, key=seconds.get)
            others = [o for o in unique if o != lab]
            best = max(others, key=lambda o: float(np.dot(cents[lab], cents[o])))
            labels[labels == lab] = best
            continue

        best_pair, best_sim = None, -1.0
        for i, a in enumerate(unique):
            for b in unique[i + 1 :]:
                sim = float(np.dot(cents[a], cents[b]))
                if sim > best_sim:
                    best_pair, best_sim = (a, b), sim
        if best_pair is not None and (1.0 - best_sim) < merge_below:
            labels[labels == best_pair[1]] = best_pair[0]
            continue
        return labels


def cluster(embeddings, n_speakers=None, threshold=0.6, durations=None):
    """Cluster window embeddings into speakers. Returns labels 0..K-1
    renumbered by order of first appearance."""
    from sklearn.cluster import AgglomerativeClustering

    n = len(embeddings)
    if n == 0:
        return []
    if n == 1:
        return [0]

    if n_speakers:
        n_clusters = min(int(n_speakers), n)
        algo = AgglomerativeClustering(n_clusters=n_clusters, metric="cosine", linkage="average")
        labels = algo.fit_predict(embeddings)
    else:
        algo = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=float(threshold),
            metric="cosine",
            linkage="average",
        )
        labels = algo.fit_predict(embeddings)
        if len(np.unique(labels)) > MAX_AUTO_SPEAKERS:
            algo = AgglomerativeClustering(
                n_clusters=MAX_AUTO_SPEAKERS, metric="cosine", linkage="average"
            )
            labels = algo.fit_predict(embeddings)
        labels = _merge_tiny_clusters(labels, np.asarray(embeddings))
        if durations is not None:
            labels = _fold_weak_clusters(labels, embeddings, durations, threshold)

    # Renumber so the first voice heard is speaker 0, the next new voice 1, …
    mapping = {}
    out = []
    for lab in labels:
        if lab not in mapping:
            mapping[lab] = len(mapping)
        out.append(mapping[lab])
    return out


def diarize_track(wav_path, segments, n_speakers=None, threshold=0.6, progress_cb=None,
                  precomputed=None, state=None):
    """Split transcript segments by speaker.

    Returns (new_segments, n_found) where each new segment has a
    "speaker_idx" key (0-based). Splits inside a Whisper segment when the
    voice changes mid-segment, using word timestamps. Segments without word
    timestamps are assigned whole to the speaker active at their midpoint.

    precomputed: optional (windows, embeddings) from an earlier run — skips
    the audio loading/embedding so re-clustering is near-instant.
    state: optional dict; when given, the windows/embeddings used are stored
    in it so the caller can persist them. Anything persisted from it belongs to
    EMBED_VERSION, so the cache can be invalidated when the embedding changes.
    """
    if not segments:
        return [], 0

    if precomputed is not None:
        windows = [tuple(w) for w in np.asarray(precomputed[0]).tolist()]
        embeddings = np.asarray(precomputed[1], dtype=np.float64)
    else:
        windows = build_windows(segments)
        if len(windows) < 2:
            out = [dict(seg, speaker_idx=0) for seg in segments]
            return out, 1
        audio = load_mono_16k(wav_path)
        embeddings = embed_windows(audio, windows, progress_cb)

    if state is not None:
        state["windows"] = windows
        state["embeddings"] = embeddings

    if len(windows) < 2:
        out = [dict(seg, speaker_idx=0) for seg in segments]
        return out, 1

    durations = [w[1] - w[0] for w in windows]
    labels = cluster(embeddings, n_speakers=n_speakers, threshold=threshold, durations=durations)
    n_found = len(set(labels))
    if n_found == 1:
        out = [dict(seg, speaker_idx=0) for seg in segments]
        return out, 1

    centers = np.array([(w[0] + w[1]) / 2.0 for w in windows])
    order = np.argsort(centers)
    centers_sorted = centers[order]
    labels_sorted = np.array(labels)[order]

    def label_at(t):
        i = int(np.searchsorted(centers_sorted, t))
        if i <= 0:
            return int(labels_sorted[0])
        if i >= len(centers_sorted):
            return int(labels_sorted[-1])
        before, after = centers_sorted[i - 1], centers_sorted[i]
        return int(labels_sorted[i - 1] if t - before <= after - t else labels_sorted[i])

    return _split_segments(segments, label_at), n_found


def _split_segments(segments, label_at):
    """Segments re-cut by speaker: each word joins the speaker `label_at`
    says is active at its centre; consecutive same-speaker words re-form
    segments. Word-less segments are labelled whole at their midpoint."""
    new_segments = []
    for seg in segments:
        words = seg.get("words") or []
        usable = [w for w in words if w.get("s") is not None and w.get("e") is not None]
        if not usable:
            mid = (seg["start"] + seg["end"]) / 2.0
            new_segments.append(dict(seg, speaker_idx=label_at(mid)))
            continue
        # Group consecutive words by the speaker active at the word's centre.
        runs = []
        for w in usable:
            lab = label_at((w["s"] + w["e"]) / 2.0)
            if runs and runs[-1]["lab"] == lab:
                runs[-1]["words"].append(w)
            else:
                runs.append({"lab": lab, "words": [w]})
        for run in runs:
            text = "".join(w["w"] for w in run["words"]).strip()
            if not text:
                continue
            new_segments.append(
                {
                    "start": run["words"][0]["s"],
                    "end": run["words"][-1]["e"],
                    "text": text,
                    "words": run["words"],
                    "speaker_idx": run["lab"],
                }
            )
    return new_segments


def assign_by_turns(segments, turns):
    """Label transcript segments from diarizer TURNS instead of window
    clusters. Same contract as diarize_track's return: (new_segments,
    n_found), speaker_idx renumbered by first appearance in the output.

    `turns` is [(start, end, label)] on the same timeline as `segments`
    (the neural engine's frame-level who-spoke-when). A word inside a turn
    takes that turn's speaker; a word in a gap takes the nearest turn edge —
    the diarizer heard silence there, but the recognizer transcribed
    something, and the nearest voice is the only honest guess.
    """
    turns = sorted((float(s), float(e), int(lab)) for s, e, lab in turns)
    if not segments or not turns:
        return [dict(seg, speaker_idx=0) for seg in segments], (1 if segments else 0)
    starts = [t[0] for t in turns]

    from bisect import bisect_right

    def label_at(t):
        i = bisect_right(starts, t) - 1
        best_lab, best_d = turns[0][2], float("inf")
        for j in (i, i + 1):
            if 0 <= j < len(turns):
                s, e, lab = turns[j]
                if s <= t <= e:
                    return lab
                d = min(abs(t - s), abs(t - e))
                if d < best_d:
                    best_lab, best_d = lab, d
        return best_lab

    new_segments = _split_segments(segments, label_at)
    # First voice heard is speaker 0, next new voice 1, … — the same
    # invariant cluster() keeps, re-derived on the OUTPUT because a turn that
    # caught no words must not reserve a number.
    mapping = {}
    for seg in sorted(new_segments, key=lambda s: s["start"]):
        lab = seg["speaker_idx"]
        if lab not in mapping:
            mapping[lab] = len(mapping)
    for seg in new_segments:
        seg["speaker_idx"] = mapping[seg["speaker_idx"]]
    return new_segments, len(mapping)
