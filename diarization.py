"""Speaker diarization: figure out who spoke when by clustering voice-prints.

Uses the ungated speechbrain ECAPA-TDNN speaker-embedding model — the same
checkpoint as ever, exported to a single ONNX graph and run by onnxruntime on
the CPU (see _get_embedder) — plus agglomerative clustering. The model ships
INSIDE the app instead of downloading; config.seed_ecapa_onnx() puts it where
this file reads it. No cloud calls — everything runs locally.
"""

import hashlib
import logging
import math
import os
import subprocess
import threading

import numpy as np

from config import (
    ECAPA_ONNX_PATH,
    ECAPA_ONNX_SHA256,
    ModelDownloadError,
    seed_ecapa_onnx,
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
#
# THE TORCH -> ONNXRUNTIME SWAP DELIBERATELY DID NOT BUMP THIS, and the reason
# is measurement, not optimism: it is the same checkpoint through the same
# arithmetic, so the values are the same values. Re-embedding the 595 windows
# behind a shipped analysis.npz and comparing against that cache gives max
# element drift 1.5e-6 on unit-norm vectors, nearest-neighbour ordering
# unchanged for 595 of 595 windows, and identical agglomerative labels at
# k=2..6 — i.e. nothing cluster() can see moved. Bumping would have thrown away
# every cached embedding in the corpus to recompute the same numbers.
EMBED_VERSION = 1


# --- The embedder ------------------------------------------------------------
# ONE onnxruntime session on the CPU, built once per process. It replaced
# speechbrain-on-torch, which was 2.5 GB of install (torch + torchaudio +
# speechbrain) for one 84 MB model, and this is the only thing that used them.
#
# THE WEIGHTS ARE UNCHANGED. tools/export_ecapa_onnx.py traces the real
# speechbrain modules out of the same checkpoint into one graph; see
# EMBED_VERSION above for the parity measurement that says so.
#
# WHY THE DEVICE PICK AND THE GPU FALLBACK ARE GONE. The old code ran on MPS
# and fell back to torch-CPU, which was 78x slower — 595 windows in 4.5 s on
# MPS against 351 s on CPU, a cliff worth a log line screaming about. ort-CPU
# does those same 595 windows in 15.4-16.7 s at 4 threads. There is no GPU path
# to lose any more and nothing to fall back FROM, so a failure here is just a
# failure: pipeline's caller turns it into "everyone shares one label" and the
# meeting completes (see diarize_track's callers in pipeline._label_and_assemble).
_EMBEDDER = None
# Built under a lock because two meetings can process concurrently, each on its
# own request thread. Only the BUILD is serialised: InferenceSession.run is
# thread-safe and is what the concurrency is actually for.
_EMBEDDER_LOCK = threading.Lock()


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


ECAPA_LABEL = "the speaker model"


def _intra_op_threads():
    """How many threads the session gets.

    The PERFORMANCE cores, not every core. An M-series reports its efficiency
    cores in hw.logicalcpu too (this M4: 10 logical = 4 performance + 6
    efficiency), and handing ort all ten makes every batch wait on the slow
    six. 4 threads is also what the benchmark behind this port measured:
    595 windows in 15.4-16.7 s.
    """
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.perflevel0.logicalcpu"],
            capture_output=True, text=True, timeout=5, check=True).stdout
        n = int(out.strip())
        if n > 0:
            return n
    except Exception:  # noqa: BLE001 — not macOS, no sysctl, odd output
        pass
    # Everywhere else: half the logical cores, which is the same "skip the
    # hyperthreads/small cores" guess without a way to ask.
    return max(1, (os.cpu_count() or 2) // 2)


def _sha256_file(path):
    """The file's digest. config._sha256 is the same loop — what must not be
    duplicated is the CONSTANT, and that is imported from config, not retyped."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _verified_model_path():
    """The ecapa.onnx this build is allowed to load, or ModelDownloadError.

    NOTHING IS DOWNLOADED HERE. The model rides inside the app bundle and
    config.seed_ecapa_onnx() installs it at ECAPA_ONNX_PATH on startup; the
    call below is that same seeding, retried at the point of use because the
    two entry points that matter most never run app.py's startup — the
    tools/embed_worker.py subprocess and the test/eval harnesses.

    THE HASH IS CHECKED HERE, ONCE PER PROCESS, not at seed time. Seeding
    verifies before it copies, and then deliberately never re-hashes an
    installed model on later launches; this is the other half of that bargain,
    so a file that was swapped, truncated or left behind by an older build
    cannot reach onnxruntime. It costs 0.05 s for 84 MB — measured on this M4,
    against ~16 s of embedding it gates.

    IT RAISES ModelDownloadError, whose name is now half wrong since nothing
    downloads — but whose CHANNEL is exactly right, which is why it is still
    the one used. pipeline.diarize() has a dedicated arm for that type which
    shows str(exc) to the user as the reason and labels the track with a single
    speaker, and app.py's prefetch route treats it as user-facing copy. A plain
    RuntimeError lands in the generic arm and is presented as an internal
    failure, which a missing file with a one-command fix is not.
    """
    path = ECAPA_ONNX_PATH
    if not path.exists():
        seed_ecapa_onnx()
    if not path.is_file():
        raise ModelDownloadError(
            f"The speaker model is missing. It normally ships inside the app, "
            f"so a packaged install should never see this; a source checkout "
            f"makes one with `python tools/export_ecapa_onnx.py` (dev-only, "
            f"needs torch) or by copying ecapa.onnx out of a built "
            f"MeetingScribe.app at Contents/Resources/models/ecapa.onnx. It "
            f"belongs at {path}.")
    digest = _sha256_file(path)
    if digest != ECAPA_ONNX_SHA256:
        # Self-heal before refusing: the mismatch is almost always an OLD
        # model left behind by a previous build (the install path is not
        # versioned; the pin moves with the app), and the correct copy is
        # sitting in the bundle right now. Without this, the day the artifact
        # is ever regenerated every existing install would fail this check on
        # every meeting, forever, with the models page still showing green —
        # a landmine this function would otherwise plant and never defuse.
        path.unlink(missing_ok=True)
        seed_ecapa_onnx()
        if path.is_file() and _sha256_file(path) == ECAPA_ONNX_SHA256:
            log.warning("speaker model at %s did not match this build "
                        "(sha256 %s…); reinstalled the bundled copy", path,
                        digest[:12])
            return path
        raise ModelDownloadError(
            f"The speaker model at {path} is not the one this build expects "
            f"(sha256 {digest[:12]}…, expected {ECAPA_ONNX_SHA256[:12]}…), "
            f"and no good bundled copy was available to reinstall. Delete "
            f"that file and reinstall MeetingScribe.")
    return path


def embedder_download_size():
    """(bytes still to download, bytes in total) for the speaker model.

    (0, 0) — ALWAYS, and honestly: the model is bundled, so there is no
    download to size and none to wait for. Kept as the uniform answer for a
    caller that sizes every model the same way (pipeline.model_download_sizes
    still asks; app.py's survey no longer does — _speaker_spec sets
    "plan": None), and "nothing to fetch" is what such a caller needs to hear.
    """
    return 0, 0


def download_speaker_model(progress_cb=None):
    """Put the speaker model in place NOW. Returns 0 bytes; nothing downloads.

    Onboarding's fetch hook. All it can do is the bundle copy — the same
    idempotent config.seed_ecapa_onnx() the loader and app startup call — so
    the byte count it returns is 0 and progress_cb is never called: there is no
    transfer to report. Raises ModelDownloadError when the model cannot be put
    in place at all, because a fetch that quietly does nothing would leave the
    UI saying "still missing" without ever saying why.
    """
    _verified_model_path()  # seeds from the bundle if needed, then verifies
    return 0


def _get_embedder(progress_cb=None):
    """The onnxruntime session, built once and reused.

    progress_cb is accepted and unused. It existed so a first meeting could
    show an 89 MB download; the load is now a hash and a session init, 0.15 s
    together on this M4, with nothing in it worth a line of UI.
    """
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER
    with _EMBEDDER_LOCK:
        if _EMBEDDER is None:
            import onnxruntime as ort

            path = _verified_model_path()
            threads = _intra_op_threads()
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = threads
            # CPU explicitly, not "whatever is installed": onnxruntime on macOS
            # also offers CoreML, and this graph has never been measured there.
            _EMBEDDER = ort.InferenceSession(
                str(path), opts, providers=["CPUExecutionProvider"])
            # Once per process, and it is the replacement for the old "GPU
            # failed, falling back" line: thread count is now the only thing
            # that explains a run that took far longer than it should have.
            log.info("speaker embedder ready: onnxruntime CPU, %d thread(s)",
                     threads)
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
    """ECAPA embedding for each (start, end) window. Returns (N, D) L2-normalised.

    THE BATCHES ARE BUILT EXACTLY AS THEY ALWAYS WERE. The session takes the
    same two arrays speechbrain's encode_batch took — wavs [B, T] float32 zero
    padded to the batch's own longest member, wav_lens [B] float32 as fractions
    of that length — because the ONNX graph IS speechbrain's encode_batch,
    traced. Slicing, the MIN_WINDOW_S zero-fill, BATCH_SIZE, the float64 cast
    and the L2 normalisation are all unchanged; anything altered here changes
    embedding values and needs EMBED_VERSION bumped with it.
    """
    model = _get_embedder(progress_cb)
    embeddings = []
    total = len(windows)
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
        wavs = np.zeros((len(chunks), max_len), dtype=np.float32)
        lens = np.zeros(len(chunks), dtype=np.float32)
        for i, c in enumerate(chunks):
            wavs[i, : len(c)] = c
            lens[i] = len(c) / max_len
        # [B, 192] already: the exported graph does encode_batch's squeeze(1).
        embeddings.append(model.run(None, {"wavs": wavs, "wav_lens": lens})[0])
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


# A cluster carrying this much speech is kept even when it exceeds a
# max_speakers HINT (the calendar's invitee count). The hint is a prediction
# about the meeting; this much speech is an observation of the recording, and
# the observation wins. Deliberately the same ceiling as the fold cascade's
# FOLD_KEEP_ABOVE_S: one definition of "too much speech to be a mistake".
HINT_OVERRIDE_S = FOLD_KEEP_ABOVE_S


def _cap_speakers(labels, embeddings, durations, max_speakers):
    """Fold the smallest clusters into their most similar voice until at most
    max_speakers remain — but never a cluster with HINT_OVERRIDE_S of speech.

    This is how a calendar count is applied: as a CAP on the auto path, after
    the fold cascade has already removed the same-voice splits. It is not
    cluster(n_speakers=N), which returns exactly N clusters with no cleanup at
    all and so turns a 1:1 invite with a third voice into a monologue, or a
    3-person invite on a 1:1 call into two phantom speakers. Under a cap the
    auto path keeps deciding the count; the hint can only pull surplus SMALL
    clusters down to the invitee count, and a voice that plainly spoke at
    length stays whatever the invite said.
    """
    labels = np.asarray(labels).copy()
    embeddings = np.asarray(embeddings)
    durations = np.asarray(durations, dtype=np.float64)
    max_speakers = int(max_speakers)

    def centroid(lab):
        vec = embeddings[labels == lab].mean(axis=0)
        norm = np.linalg.norm(vec)
        return vec / norm if norm else vec

    while True:
        unique = np.unique(labels)
        if len(unique) <= max_speakers:
            return labels
        seconds = {lab: float(durations[labels == lab].sum()) for lab in unique}
        surplus = [lab for lab in unique if seconds[lab] < HINT_OVERRIDE_S]
        if not surplus:
            return labels  # every voice spoke at length: the evidence outranks the hint
        lab = min(surplus, key=seconds.get)
        cents = {o: centroid(o) for o in unique}
        others = [o for o in unique if o != lab]
        best = max(others, key=lambda o: float(np.dot(cents[lab], cents[o])))
        labels[labels == lab] = best


def cluster(embeddings, n_speakers=None, threshold=0.6, durations=None,
            max_speakers=None):
    """Cluster window embeddings into speakers. Returns labels 0..K-1
    renumbered by order of first appearance.

    n_speakers forces the count exactly (a human's answer); max_speakers caps
    the auto path (a calendar's guess) and is ignored when n_speakers is given
    — see _cap_speakers for why the two are not the same thing.
    """
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
            if max_speakers:
                labels = _cap_speakers(labels, embeddings, durations, max_speakers)

    # Renumber so the first voice heard is speaker 0, the next new voice 1, …
    mapping = {}
    out = []
    for lab in labels:
        if lab not in mapping:
            mapping[lab] = len(mapping)
        out.append(mapping[lab])
    return out


def diarize_track(wav_path, segments, n_speakers=None, threshold=0.6, progress_cb=None,
                  precomputed=None, state=None, max_speakers=None):
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
    labels = cluster(embeddings, n_speakers=n_speakers, threshold=threshold,
                     durations=durations, max_speakers=max_speakers)
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
