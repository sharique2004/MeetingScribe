#!/usr/bin/env python3
"""A3 gate harness: the ONNX speaker embedder vs the torch one, over the whole
real fixture corpus. DEV-ONLY, needs torch + speechbrain + onnxruntime.

    python tools/parity_ecapa_onnx.py                    # all 22 fixtures
    python tools/parity_ecapa_onnx.py --only "Demo,Room W"
    python tools/parity_ecapa_onnx.py --onnx /path/to/ecapa.onnx

WHAT IT ANSWERS. A3 replaces the torch/speechbrain ECAPA embedder with
onnxruntime running dist/models/ecapa-onnx-v1/ecapa.onnx. The question the gate
has to settle is not "are the two embedders close" — the spike already measured
that on one track — but "does the swap change any ANSWER this app has ever
given". So for every frozen fixture this recomputes embeddings BOTH ways on the
EXACT windows the shipped analysis.npz cached, and reports, per fixture:

  (a) ort vs torch      max/median |diff| and the worst per-window cosine
                        similarity, on L2-normalised embeddings
  (b) ort vs CACHE      the same, against the embeddings the app actually
                        shipped — this catches historical drift (a checkpoint
                        that moved, a loader that changed) which (a) cannot see
                        because (a) recomputes both sides today
  (c) the full pairwise cosine-distance matrix, max |delta| over every pair —
                        clustering sees distances, not embeddings, and a drift
                        that cancels in the distance is not a drift that matters
  (d) the decision      sklearn AgglomerativeClustering(cosine, average) at
                        k=2..6 on both sets with exact label agreement after
                        permutation matching, AND the real path:
                        diarization.cluster() on ort / torch / cache, comparing
                        speaker COUNT and the per-window labels themselves.

(d)'s second half is the one that can veto the swap. The k-sweep is context: it
says how the drift behaves at cluster counts the app never picks, so a single
flipped window in the real path can be read against how stable the geometry is
generally.

WHY THIS FILE DUPLICATES diarization.embed_windows — DELIBERATE AND TEMPORARY.
It re-implements window slicing, batching, zero-padding, the relative-length
fractions and the float64 L2 normalisation locally instead of calling
diarization.embed_windows. Two reasons, both expiring:

  1. This harness has to run BOTH embedders side by side. diarization has one.
  2. It was written while diarization's embedder was being rewritten under it,
     and a gate that imports the thing it is gating cannot fail honestly.

The copy is therefore checked, not trusted: CONSTANTS below are cross-read from
diarization at startup and any divergence is a hard FAIL, because the mission
that this harness gates says those constants do not move. The file is KEPT past
A3's completion as the regression harness for any future embedder or artifact
change (a new export, an onnxruntime major bump): the torch reference arm only
runs where torch is installed, and the cross-read constants are what keep this
second copy of embed_windows honest — if they drift, the harness fails loudly
instead of measuring the wrong thing.

WHAT IT DOES NOT TOUCH. Read-only with respect to the repo and to
~/.meetingscribe/recordings. The one thing it writes is
dist/a3-parity/report.json (dist/ is gitignored).

WHAT A FULL RUN COSTS. 22 fixtures, 18,993 windows, about 15 minutes on an M4:
onnxruntime on CPU is the floor at ~27 ms/window (124 s for Room W's 4594 alone)
against ~8.5 ms/window for torch on MPS, and the clustering adds ~25 s on the
big fixture. Peak memory is bounded by the blocked audio reader and the blocked
pairwise pass, not by the corpus — Room W's 111 minutes of 48 kHz audio loads in
3.5 s and never exists as more than its 16 kHz output plus one block.

PSEUDONYMS ONLY, in the report and on stdout. test/fixtures/index.json is the
local-only de-anonymisation key: fixture directory names are meeting titles
with participant names in them. Nothing here prints or serialises a slug, a
meeting title, or a path under recordings/ — the same rule
tools/eval_diarization.py follows, for the same reason.
"""

import argparse
import hashlib
import json
import math
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# --- The embed_windows contract, copied. See the docstring. -------------------
# Cross-checked against diarization at startup; a mismatch fails the run.
EMBED_SR = 16000
MIN_WINDOW_S = 0.4
BATCH_SIZE = 16
EMBED_DIM = 192

ECAPA_REPO = "speechbrain/spkrec-ecapa-voxceleb"

# The artifact under test, in preference order. dist/ first: that is what
# tools/export_ecapa_onnx.py writes and what tools/build_mac_app.sh bundles, so
# it is the file a gate run is asking about. The installed copy and the spike's
# export are fallbacks for a machine that has one but not the other.
ONNX_CANDIDATES = (
    REPO / "dist" / "models" / "ecapa-onnx-v1" / "ecapa.onnx",
    REPO / "docs" / "a3-spike" / "ecapa.onnx",
)
# The artifact this harness was written against. config.ECAPA_ONNX_SHA256 is the
# shipping pin and is cross-checked too; both are reported, neither is fatal —
# a deliberate re-export changes them and the gate should say so, not refuse.
EXPECTED_SHA256 = "ed50545296e861f6cf4b4d96a9444a95000aa4c9298016b34ecbc6c2cb59cb29"

FIXTURES_DIR = REPO / "test" / "fixtures"
FIXTURE_INDEX = FIXTURES_DIR / "index.json"
# Both roots real meetings live under, same pair tools/eval_diarization.py uses:
# recordings/ in the repo is a symlink here and a real directory elsewhere.
RECORDING_ROOTS = (Path.home() / ".meetingscribe" / "recordings", REPO / "recordings")

DEFAULT_OUT = REPO / "dist" / "a3-parity" / "report.json"

# diarization.cluster()'s own defaults, i.e. what pipeline passes when the user
# has not pinned a speaker count. The gate runs the auto path because that is
# the path that decides how many people were in the room.
CLUSTER_THRESHOLD = 0.6
KS = (2, 3, 4, 5, 6)


# ---------------------------------------------------------------------------
# Audio. Verbatim copy of pipeline.load_mono_16k (blocked reader).
# ---------------------------------------------------------------------------
# COPIED, NOT IMPORTED, for the reason in the docstring — and it has to be THIS
# loader rather than the three-array diarization.load_mono_16k, because
# tools/embed_worker.py used this one to produce the caches comparison (b) is
# against, and because Room W is 111 minutes of 48 kHz audio that the whole-file
# path holds three times over. The two are documented to be bitwise identical;
# the memory is the only difference.
_LOAD_BLOCK_FRAMES = 1 << 22   # ~4.2M input frames, ~87 s at 48 kHz
_LOAD_PAD_FRAMES = 1 << 14     # overlap carried into each block and thrown away


def load_mono_16k(path):
    """Any WAV/FLAC as float32 mono at 16 kHz, without ever holding it three times."""
    import soundfile as sf
    from scipy.signal import resample_poly

    with sf.SoundFile(str(path)) as snd:
        sr, frames, channels = int(snd.samplerate), int(snd.frames), int(snd.channels)
        if frames <= 0:
            return np.zeros(0, dtype=np.float32)

        def mono(block):
            return block.mean(axis=1) if channels > 1 else block[:, 0]

        if sr == EMBED_SR:
            out = np.empty(frames, dtype=np.float32)
            done = 0
            while done < frames:
                block = snd.read(min(_LOAD_BLOCK_FRAMES, frames - done),
                                 dtype="float32", always_2d=True)
                if not len(block):
                    break
                out[done:done + len(block)] = mono(block)
                done += len(block)
            return out[:done]

        g = math.gcd(sr, EMBED_SR)
        up, down = EMBED_SR // g, sr // g
        total_out = -(-frames * up // down)
        out = np.empty(total_out, dtype=np.float32)
        step = max(down, (_LOAD_BLOCK_FRAMES // down) * down)
        pad = max(down, (_LOAD_PAD_FRAMES // down) * down)
        pos = written = 0
        while pos < frames:
            take = min(step, frames - pos)
            left = min(pad, pos)
            right = min(pad, frames - pos - take)
            snd.seek(pos - left)
            block = snd.read(left + take + right, dtype="float32", always_2d=True)
            short = len(block) < left + take + right
            piece = resample_poly(mono(block), up, down)
            del block
            lo = left * up // down
            want = (total_out - written if pos + take >= frames
                    else take * up // down)
            want = min(want, max(0, len(piece) - lo))
            out[written:written + want] = piece[lo:lo + want]
            written += want
            pos += take
            if short:
                break
        return out[:written]


# ---------------------------------------------------------------------------
# The embed_windows contract, as data. Both back ends consume the SAME batches.
# ---------------------------------------------------------------------------

def build_batches(audio, windows):
    """The exact (wavs, wav_lens) batches diarization.embed_windows builds.

    Line for line: slice at int(t * EMBED_SR) (the KNOWN off-by-start_offset is
    reproduced, not fixed — see the essay in diarization.embed_windows), clamp
    to the array, replace anything shorter than the MIN_WINDOW_S floor with
    silence of exactly that length, take BATCH_SIZE at a time, zero-pad each
    batch to its own longest member and describe the rest as a relative length.

    Yielding numpy arrays that BOTH back ends then consume is the point: the ort
    and torch runs cannot disagree about their input, only about what they
    compute from it.
    """
    total = len(windows)
    floor = int(MIN_WINDOW_S * EMBED_SR)
    for batch_start in range(0, total, BATCH_SIZE):
        batch = windows[batch_start:batch_start + BATCH_SIZE]
        chunks = []
        for (t0, t1) in batch:
            i0, i1 = int(t0 * EMBED_SR), int(t1 * EMBED_SR)
            chunk = audio[max(0, i0):min(len(audio), i1)]
            if len(chunk) < floor:
                chunk = np.zeros(floor, dtype=np.float32)
            chunks.append(chunk)
        max_len = max(len(c) for c in chunks)
        wavs = np.zeros((len(chunks), max_len), dtype=np.float32)
        lens = np.zeros(len(chunks), dtype=np.float32)
        for i, c in enumerate(chunks):
            wavs[i, :len(c)] = np.ascontiguousarray(c)
            lens[i] = len(c) / max_len   # float64 divide, stored float32: as torch does
        yield batch_start, wavs, lens


def l2_normalise(emb):
    """embed_windows' tail: float64, unit rows, zero rows left alone."""
    emb = np.asarray(emb, dtype=np.float64)
    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return emb / norms


def embed_ort(sess, audio, windows, progress=None):
    """The candidate: onnxruntime on the exported graph."""
    out = []
    for start, wavs, lens in build_batches(audio, windows):
        emb = sess.run(None, {"wavs": wavs, "wav_lens": lens})[0]
        out.append(np.asarray(emb))
        if progress:
            progress(min(start + BATCH_SIZE, len(windows)))
    return l2_normalise(np.concatenate(out, axis=0))


def embed_torch(model, audio, windows, progress=None):
    """The reference: speechbrain EncoderClassifier.encode_batch, as shipped.

    Batched at BATCH_SIZE on whatever device the model was loaded onto (MPS by
    default, which is what makes Room W's 4594 windows a 30-second job rather
    than a 20-minute one) — the same chunking embed_windows does, so the batch
    composition, and therefore the batch-dependent wobble in the answer, is the
    same too.
    """
    import warnings

    import torch

    out = []
    # speechbrain's STFT reuses an `out=` tensor, which torch 2.12 warns about
    # once per process. It is noise here and it buries the table.
    warnings.filterwarnings("ignore", message=".*was resized since it had shape.*")
    with torch.no_grad():
        for start, wavs, lens in build_batches(audio, windows):
            emb = model.encode_batch(torch.from_numpy(wavs),
                                     wav_lens=torch.from_numpy(lens))
            out.append(emb.squeeze(1).cpu().numpy())
            if progress:
                progress(min(start + BATCH_SIZE, len(windows)))
    return l2_normalise(np.concatenate(out, axis=0))


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compare_embeddings(a, b):
    """(a) and (b): elementwise drift plus the worst per-window cosine."""
    d = np.abs(a - b)
    cos = (a * b).sum(axis=1)
    return {
        "max_abs": float(d.max()),
        "median_abs": float(np.median(d)),
        "mean_abs": float(d.mean()),
        "min_cos_sim": float(cos.min()),
        "max_cos_dist": float(1.0 - cos.min()),
    }


def pairwise_delta(a, b, block=512):
    """(c): max |delta| over every pairwise cosine distance, in row blocks.

    Blocked because the full matrix for the largest fixture is 4594^2 float64 =
    169 MB per side, and this is the number the clustering actually consumes, so
    it is worth computing exactly rather than sampling. Upper triangle only: the
    diagonal is 0 by construction on both sides and would flatter the mean.
    """
    n = len(a)
    if n < 2:
        return {"pairs": 0}
    mx = 0.0
    total = 0.0
    count = 0
    ref_lo, ref_hi = math.inf, -math.inf
    cols = np.arange(n)
    for i0 in range(0, n, block):
        i1 = min(i0 + block, n)
        da = 1.0 - a[i0:i1] @ a.T
        db = 1.0 - b[i0:i1] @ b.T
        mask = cols[None, :] > np.arange(i0, i1)[:, None]
        if not mask.any():
            continue
        ref = da[mask]
        delta = np.abs(da - db)[mask]
        mx = max(mx, float(delta.max()))
        total += float(delta.sum())
        count += int(delta.size)
        ref_lo = min(ref_lo, float(ref.min()))
        ref_hi = max(ref_hi, float(ref.max()))
    spread = ref_hi - ref_lo
    return {
        "pairs": count,
        "ref_min": ref_lo,
        "ref_max": ref_hi,
        "max_abs_delta": mx,
        "mean_abs_delta": total / count if count else 0.0,
        "frac_of_spread": (mx / spread) if spread > 0 else None,
    }


def best_label_agreement(la, lb):
    """Exact agreement between two labellings after the best permutation match.

    Hungarian on the negated contingency table, padded square so a run that
    produced fewer clusters than asked for is still matchable. Returns
    (agreement in [0,1], number of windows that disagree).
    """
    from scipy.optimize import linear_sum_assignment

    ua, ub = np.unique(la), np.unique(lb)
    ia = {v: i for i, v in enumerate(ua)}
    ib = {v: i for i, v in enumerate(ub)}
    k = max(len(ua), len(ub))
    m = np.zeros((k, k), dtype=np.int64)
    for x, y in zip(la, lb):
        m[ia[x], ib[y]] += 1
    r, c = linear_sum_assignment(-m)
    hit = int(m[r, c].sum())
    return hit / len(la), len(la) - hit


def agglomerative_sweep(a, b, ks):
    """(d) first half: the same clustering the app uses, at counts it does not pick.

    metric="cosine", linkage="average", n_clusters=k — the exact call
    diarization.cluster() makes on its forced-count path, refit per k on each
    embedding set rather than cut from a cached tree, so nothing about the
    comparison depends on this file's idea of how sklearn builds its dendrogram.
    """
    from sklearn.cluster import AgglomerativeClustering

    out = {}
    for k in ks:
        if len(a) <= k:
            out[str(k)] = {"skipped": "fewer windows than clusters"}
            continue
        fit = lambda x: AgglomerativeClustering(
            n_clusters=k, metric="cosine", linkage="average").fit_predict(x)
        la, lb = fit(a), fit(b)
        agree, differ = best_label_agreement(la, lb)
        out[str(k)] = {"agreement": agree, "mismatched_windows": differ}
    return out


def labels_digest(labels):
    return hashlib.sha256(np.asarray(labels, dtype=np.int64).tobytes()).hexdigest()[:16]


def cluster_sizes(labels):
    u, c = np.unique(np.asarray(labels), return_counts=True)
    return {int(k): int(v) for k, v in zip(u, c)}


def decision_path(diarization, sets, windows, threshold):
    """(d) second half: THE decision, through diarization.cluster() itself.

    Auto mode (n_speakers=None) with the durations diarize_track passes, because
    that is the path that decides how many people were in the room: the raw
    threshold pass, the MAX_AUTO_SPEAKERS refit, _merge_tiny_clusters and the
    fold cascade, all of it. Labels come back renumbered by order of first
    appearance, so two runs can be compared with ==; when they differ, the
    permutation-matched agreement says whether it is a relabelling or a genuinely
    different answer.
    """
    durations = [float(w[1]) - float(w[0]) for w in windows]
    out = {"threshold": threshold, "runs": {}}
    labels = {}
    for name, emb in sets.items():
        t0 = time.time()
        lab = np.asarray(diarization.cluster(emb, n_speakers=None,
                                             threshold=threshold,
                                             durations=durations), dtype=np.int64)
        labels[name] = lab
        out["runs"][name] = {
            "n_speakers": int(len(np.unique(lab))),
            "labels_sha256_16": labels_digest(lab),
            "cluster_sizes": cluster_sizes(lab),
            "seconds": round(time.time() - t0, 2),
        }
    for other in ("torch", "cache"):
        if "ort" not in labels or other not in labels:
            continue
        a, b = labels["ort"], labels[other]
        same = bool(np.array_equal(a, b))
        entry = {
            "identical_labels": same,
            "same_speaker_count": bool(out["runs"]["ort"]["n_speakers"]
                                       == out["runs"][other]["n_speakers"]),
        }
        if not same:
            agree, differ = best_label_agreement(a, b)
            entry["best_permutation_agreement"] = agree
            entry["mismatched_windows"] = differ
            entry["first_mismatches"] = [int(i) for i in np.flatnonzero(a != b)[:50]]
        out[f"ort_vs_{other}"] = entry
    return out


# ---------------------------------------------------------------------------
# Corpus plumbing
# ---------------------------------------------------------------------------

def read_index():
    """{pseudonym: slug} out of the local-only corpus key."""
    if not FIXTURE_INDEX.exists():
        raise SystemExit(
            f"{FIXTURE_INDEX} is missing. It is the de-anonymisation key for the "
            "real corpus and is deliberately kept out of git, so a clean clone "
            "cannot run this harness at all — there is nothing to be parity "
            "against without it.")
    data = json.loads(FIXTURE_INDEX.read_text(encoding="utf-8"))
    labels = data.get("labels", data)
    if not isinstance(labels, dict) or not labels:
        raise SystemExit(f"{FIXTURE_INDEX} carries no 'labels' map")
    return {str(k): str(v) for k, v in labels.items()}


def find_recording_dir(meeting_id):
    """The live recording directory for a fixture, by meeting id.

    Directory names are matched by their timestamp suffix and then CONFIRMED
    against the meeting.json id, because app._sync_folder_name renames the
    directory whenever the user retitles a meeting and only the id is stable.
    """
    for root in RECORDING_ROOTS:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or not d.name.endswith(meeting_id):
                continue
            meta = d / "meeting.json"
            if not meta.exists():
                continue
            try:
                if str(json.loads(meta.read_text(encoding="utf-8")).get("id")) == meeting_id:
                    return d
            except (OSError, ValueError):
                continue
    return None


def find_track_audio(rec_dir, track, fixture_meta):
    """The audio file for one track. Archived meetings hold <track>.flac and
    meeting.json names it; older ones still hold <track>.wav. soundfile decodes
    both to the identical sample stream, so either is the same input."""
    names = []
    for meta_path in (rec_dir / "meeting.json",):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        name = ((meta.get("tracks") or {}).get(track) or {}).get("file")
        if name:
            names.append(name)
    name = ((fixture_meta.get("tracks") or {}).get(track) or {}).get("file")
    if name:
        names.append(name)
    names += [f"{track}.flac", f"{track}.wav"]
    for name in names:
        stem = Path(name).stem
        for ext in (Path(name).suffix, ".flac", ".wav"):
            if not ext:
                continue
            p = rec_dir / f"{stem}{ext}"
            if p.exists():
                return p
    return None


def load_fixture(key, slug):
    """One fixture record, or (None, reason). Pseudonym in, no slug out."""
    d = FIXTURES_DIR / slug
    npz_path, meta_path = d / "analysis.npz", d / "meeting.json"
    if not npz_path.exists():
        return None, "no analysis.npz"
    if not meta_path.exists():
        return None, "no meeting.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    with np.load(npz_path, allow_pickle=False) as npz:
        tracks = {}
        for track in ("mic", "system"):
            wk, ek = f"{track}_windows", f"{track}_embeddings"
            if wk in npz.files and ek in npz.files:
                tracks[track] = (np.asarray(npz[wk], dtype=np.float64),
                                 np.asarray(npz[ek], dtype=np.float64))
    if not tracks:
        return None, "analysis.npz holds no <track>_windows/_embeddings pair"
    # The track diarization actually clusters: system for online meetings, mic
    # for in-person; whatever the cache holds when the preferred one is absent.
    mode = meta.get("mode", "online")
    preferred = "system" if mode == "online" else "mic"
    track = preferred if preferred in tracks else next(iter(tracks))
    meeting_id = str(meta.get("id") or slug[-15:])
    rec = find_recording_dir(meeting_id)
    if rec is None:
        return None, "no live recording directory for this meeting id"
    audio_path = find_track_audio(rec, track, meta)
    if audio_path is None:
        return None, f"no {track} audio in the recording directory"
    return {
        "key": key,
        "dir": d,
        "mode": mode,
        "track": track,
        "windows": tracks[track][0],
        "cache": tracks[track][1],
        "audio_path": audio_path,
        "expected_speakers": meta.get("expected_speakers") or None,
        "shipped_speakers": len(meta.get("speakers") or {}) or None,
    }, None


# ---------------------------------------------------------------------------
# Back-end construction
# ---------------------------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def resolve_onnx(explicit):
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_file():
            raise SystemExit(f"no such ONNX artifact: {p}")
        return p
    candidates = list(ONNX_CANDIDATES)
    try:
        import config
        candidates.insert(1, Path(config.ECAPA_ONNX_PATH))
    except Exception:
        pass
    for p in candidates:
        if p.is_file():
            return p.resolve()
    raise SystemExit("no ecapa.onnx found — run tools/export_ecapa_onnx.py first, "
                     "or pass --onnx")


def open_session(path, intra_op, providers):
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    if intra_op:
        so.intra_op_num_threads = intra_op
    sess = ort.InferenceSession(str(path), so, providers=list(providers))
    names = {i.name for i in sess.get_inputs()}
    if names != {"wavs", "wav_lens"}:
        raise SystemExit(f"unexpected ONNX inputs {sorted(names)} — this harness "
                         "feeds the wavs/wav_lens contract only")
    return sess


def load_torch_reference(device):
    """The shipped reference, straight from speechbrain — NOT via diarization.

    diarization._load_embedder is the app's loader and is being replaced by the
    very change this harness gates, so the reference is built here from the same
    on-disk checkpoint (MODELS_DIR/ecapa) with the same run_opts. The
    device_type shim is speechbrain 1.1's: it sets that attribute only for
    cpu/cuda and then reads it unconditionally, so "mps" crashes the init
    without it.
    """
    try:
        from speechbrain.inference.speaker import EncoderClassifier
    except ImportError:  # speechbrain < 1.0
        from speechbrain.pretrained import EncoderClassifier
    if not hasattr(EncoderClassifier, "device_type"):
        EncoderClassifier.device_type = "cpu"
    import config

    savedir = Path(config.MODELS_DIR) / "ecapa"
    model = EncoderClassifier.from_hparams(source=ECAPA_REPO, savedir=str(savedir),
                                           run_opts={"device": device})
    model.mods.eval()
    return model, savedir


def pick_torch_device(requested):
    import torch

    if requested and requested != "auto":
        return requested
    if sys.platform == "darwin" and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def constant_drift(diarization):
    """The copied contract vs diarization's own. Non-empty means this harness is
    measuring something the app no longer does, which is a FAIL either way."""
    out = []
    for name, mine in (("EMBED_SR", EMBED_SR), ("MIN_WINDOW_S", MIN_WINDOW_S),
                       ("BATCH_SIZE", BATCH_SIZE)):
        live = getattr(diarization, name, "MISSING")
        if live != mine:
            out.append({"constant": name, "harness": mine, "diarization": live})
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def fmt(x, spec=".2e"):
    if x is None:
        return "n/a"
    return format(x, spec)


def print_embedding_table(rows):
    print()
    print("EMBEDDING PARITY — L2-normalised (N,192), recomputed on the cached windows")
    # Cosine similarity is reported as its complement: every value here is
    # 0.99999999+, and "1.00000" in a column is not a measurement.
    head = (f"{'fixture':8s} {'trk':6s} {'N':>5s} | {'ort-vs-torch':>27s} | "
            f"{'ort-vs-cache':>27s} | {'pairwise cos-dist':>21s}")
    sub = (f"{'':8s} {'':6s} {'':>5s} | {'max|d|':>9s} {'med|d|':>7s} {'1-mincos':>9s} | "
           f"{'max|d|':>9s} {'med|d|':>7s} {'1-mincos':>9s} | {'max|Δ|':>9s} {'/spread':>10s}")
    print("-" * len(head))
    print(head)
    print(sub)
    print("-" * len(head))
    for r in rows:
        if r.get("error"):
            print(f"{r['key']:8s} {'-':6s} {'-':>5s} | SKIPPED: {r['error']}")
            continue
        ot, oc, pw = r["ort_vs_torch"], r["ort_vs_cache"], r["pairwise_cosine"]
        print(f"{r['key']:8s} {r['track']:6s} {r['windows']:5d} | "
              f"{fmt(ot['max_abs']):>9s} {fmt(ot['median_abs'],'.1e'):>7s} "
              f"{fmt(ot['max_cos_dist']):>9s} | "
              f"{fmt(oc['max_abs']):>9s} {fmt(oc['median_abs'],'.1e'):>7s} "
              f"{fmt(oc['max_cos_dist']):>9s} | "
              f"{fmt(pw.get('max_abs_delta')):>9s} "
              f"{fmt(pw.get('frac_of_spread'),'.1e'):>10s}")
    print("-" * len(head))


def print_decision_table(rows, ks):
    print()
    print("DECISION PARITY — sklearn k-sweep agreement, then diarization.cluster() itself")
    kcols = " ".join(f"k={k:<5d}" for k in ks)
    head = (f"{'fixture':8s} {'N':>5s} | {kcols} | {'speakers (ort/torch/cache)':>26s} | "
            f"{'labels ort=torch':>16s} {'ort=cache':>10s}")
    print("-" * len(head))
    print(head)
    print("-" * len(head))
    for r in rows:
        if r.get("error"):
            print(f"{r['key']:8s} {'-':>5s} | SKIPPED: {r['error']}")
            continue
        sw = r.get("agglomerative") or {}
        cells = []
        for k in ks:
            e = sw.get(str(k)) or {}
            if "agreement" in e:
                cells.append(f"{e['agreement'] * 100:6.2f}%")
            else:
                cells.append(f"{'--':>7s}")
        dec = r.get("decision") or {}
        runs = dec.get("runs") or {}
        counts = "/".join(str((runs.get(n) or {}).get("n_speakers", "?"))
                          for n in ("ort", "torch", "cache"))
        ot = (dec.get("ort_vs_torch") or {}).get("identical_labels")
        oc = (dec.get("ort_vs_cache") or {}).get("identical_labels")
        mark = lambda v: "YES" if v else ("NO" if v is not None else "n/a")
        print(f"{r['key']:8s} {r['windows']:5d} | {' '.join(cells)} | {counts:>26s} | "
              f"{mark(ot):>16s} {mark(oc):>10s}")
    print("-" * len(head))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_fixture(fx, sess, torch_model, diarization, args, ks):
    key = fx["key"]
    windows = [(float(a), float(b)) for a, b in fx["windows"].tolist()]
    truncated = False
    if args.max_windows and len(windows) > args.max_windows:
        windows = windows[:args.max_windows]
        truncated = True
    n = len(windows)
    cache_raw = fx["cache"][:n]

    row = {
        "key": key,
        "track": fx["track"],
        "mode": fx["mode"],
        "windows": n,
        "cached_windows": int(len(fx["windows"])),
        "truncated": truncated,
        "expected_speakers": fx["expected_speakers"],
        "shipped_speakers": fx["shipped_speakers"],
        "audio": {"codec": fx["audio_path"].suffix.lstrip(".")},
    }

    t0 = time.time()
    audio = load_mono_16k(fx["audio_path"])
    row["audio"].update({"samples_16k": int(len(audio)),
                         "seconds": round(len(audio) / EMBED_SR, 2),
                         "load_seconds": round(time.time() - t0, 2)})

    def tick(label):
        def _p(done):
            if done % (BATCH_SIZE * 32) == 0 or done >= n:
                print(f"\r  {key}: {label} {done}/{n}", end="", flush=True)
        return _p

    t0 = time.time()
    ort_emb = embed_ort(sess, audio, windows, tick("ort"))
    row["ort_seconds"] = round(time.time() - t0, 2)
    t0 = time.time()
    torch_emb = embed_torch(torch_model, audio, windows, tick("torch"))
    row["torch_seconds"] = round(time.time() - t0, 2)
    print(f"\r  {key}: embedded {n} windows "
          f"(ort {row['ort_seconds']}s, torch {row['torch_seconds']}s)      ")
    del audio

    if ort_emb.shape != (n, EMBED_DIM):
        raise SystemExit(f"{key}: ort returned {ort_emb.shape}, expected {(n, EMBED_DIM)}")

    # The cache is stored float32, so its rows are only unit-norm to ~1e-8.
    # Drift metrics compare like with like and renormalise; the DECISION path
    # below gets the array exactly as the app reads it, unnormalised.
    cache_norm = l2_normalise(cache_raw)

    row["ort_vs_torch"] = compare_embeddings(ort_emb, torch_emb)
    row["ort_vs_cache"] = compare_embeddings(ort_emb, cache_norm)
    row["torch_vs_cache"] = compare_embeddings(torch_emb, cache_norm)
    row["pairwise_cosine"] = pairwise_delta(ort_emb, torch_emb)
    row["pairwise_cosine_vs_cache"] = pairwise_delta(ort_emb, cache_norm)

    t0 = time.time()
    row["agglomerative"] = agglomerative_sweep(ort_emb, torch_emb, ks)
    row["agglomerative_seconds"] = round(time.time() - t0, 2)

    if diarization is not None:
        t0 = time.time()
        row["decision"] = decision_path(
            diarization,
            {"ort": ort_emb, "torch": torch_emb, "cache": cache_raw},
            [tuple(w) for w in windows], args.threshold)
        row["decision_seconds"] = round(time.time() - t0, 2)
    return row


def verdict_of(row):
    """PASS iff the swap changes no answer on this fixture."""
    dec = row.get("decision")
    if dec is None:
        return "NO-DECISION"
    ot = (dec.get("ort_vs_torch") or {}).get("identical_labels")
    oc = (dec.get("ort_vs_cache") or {}).get("identical_labels")
    if ot and oc:
        return "PASS"
    if ot is False or oc is False:
        return "DIFFERS"
    return "NO-DECISION"


def main():
    ap = argparse.ArgumentParser(
        description="ONNX vs torch ECAPA parity over the frozen fixture corpus.")
    ap.add_argument("--onnx", default=None, help="artifact under test")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help=f"report path (default {DEFAULT_OUT})")
    ap.add_argument("--only", default=None, help="comma-separated pseudonyms, e.g. 'Demo,Room W'")
    ap.add_argument("--skip", default=None, help="comma-separated pseudonyms to leave out")
    ap.add_argument("--torch-device", default="auto", choices=("auto", "mps", "cpu"))
    ap.add_argument("--intra-op", type=int, default=4,
                    help="onnxruntime intra_op_num_threads (spike measured 4 as the knee)")
    ap.add_argument("--providers", default="CPUExecutionProvider",
                    help="comma-separated onnxruntime providers")
    ap.add_argument("--threshold", type=float, default=CLUSTER_THRESHOLD)
    ap.add_argument("--ks", default=",".join(str(k) for k in KS))
    ap.add_argument("--max-windows", type=int, default=0,
                    help="SMOKE TEST ONLY: truncate every fixture to N windows. "
                         "Stamps the report non-authoritative (verdict SMOKE) — a "
                         "truncated run's LAST batch is a different size from the "
                         "one that produced the cache, so ort-vs-cache picks up "
                         "batch-composition wobble that a full run does not have.")
    args = ap.parse_args()

    ks = tuple(int(x) for x in args.ks.split(",") if x.strip())
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]

    index = read_index()
    wanted = None
    if args.only:
        wanted = {s.strip().lower() for s in args.only.split(",") if s.strip()}
    skipped_names = set()
    if args.skip:
        skipped_names = {s.strip().lower() for s in args.skip.split(",") if s.strip()}

    onnx_path = resolve_onnx(args.onnx)
    digest = sha256_file(onnx_path)
    sess = open_session(onnx_path, args.intra_op, providers)

    device = pick_torch_device(args.torch_device)
    torch_model, savedir = load_torch_reference(device)

    # diarization is imported for cluster() ONLY — the real decision path cannot
    # be replicated honestly, it has to be called. The embedder inside it is
    # never touched. A broken import costs the decision half of the report and
    # nothing else, so it is caught rather than fatal.
    diarization = None
    import_error = None
    try:
        import diarization as _diarization
        diarization = _diarization
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        import_error = f"{type(exc).__name__}: {exc}"
        print(f"WARNING: import diarization failed ({import_error}) — the sklearn "
              "k-sweep still runs, the real decision path cannot.")

    drift = constant_drift(diarization) if diarization is not None else []

    import onnxruntime as ort
    import sklearn
    import speechbrain
    import torch

    config_pin = None
    try:
        import config
        config_pin = config.ECAPA_ONNX_SHA256
    except Exception:
        pass

    report = {
        "tool": "tools/parity_ecapa_onnx.py",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "authoritative": not args.max_windows,
        "artifact": {
            "path": str(onnx_path),
            "bytes": onnx_path.stat().st_size,
            "sha256": digest,
            "matches_harness_pin": digest == EXPECTED_SHA256,
            "matches_config_pin": (digest == config_pin) if config_pin else None,
        },
        "settings": {
            "embed_sr": EMBED_SR, "min_window_s": MIN_WINDOW_S,
            "batch_size": BATCH_SIZE, "embed_dim": EMBED_DIM,
            "ort_providers": sess.get_providers(), "ort_intra_op": args.intra_op,
            "torch_device": device, "checkpoint": str(savedir),
            "cluster_threshold": args.threshold, "ks": list(ks),
            "max_windows": args.max_windows or None,
        },
        "env": {
            "python": sys.version.split()[0], "platform": platform.platform(),
            "numpy": np.__version__, "onnxruntime": ort.__version__,
            "torch": torch.__version__, "speechbrain": speechbrain.__version__,
            "sklearn": sklearn.__version__,
        },
        "constant_drift": drift,
        "diarization_import_error": import_error,
        "fixtures": {},
    }

    print(f"artifact  {onnx_path}")
    print(f"          sha256 {digest} "
          f"(harness pin {'OK' if digest == EXPECTED_SHA256 else 'DIFFERS'}"
          f"{'' if config_pin is None else ', config pin ' + ('OK' if digest == config_pin else 'DIFFERS')})")
    print(f"ort       {sess.get_providers()} intra_op={args.intra_op}")
    print(f"torch     {device}, checkpoint {savedir}")
    if drift:
        print(f"CONSTANT DRIFT: {drift} — this harness is no longer measuring what "
              "the app does")

    rows = []
    started = time.time()
    for key in sorted(index):
        if wanted is not None and key.lower() not in wanted:
            continue
        if key.lower() in skipped_names:
            continue
        fx, why = load_fixture(key, index[key])
        if fx is None:
            print(f"  {key}: SKIPPED — {why}")
            rows.append({"key": key, "error": why})
            continue
        rows.append(run_fixture(fx, sess, torch_model, diarization, args, ks))

    for row in rows:
        if not row.get("error"):
            row["verdict"] = verdict_of(row)
        report["fixtures"][row["key"]] = row

    done = [r for r in rows if not r.get("error")]
    print_embedding_table(rows)
    print_decision_table(rows, ks)

    summary = {
        "fixtures_in_index": len(index),
        "compared": len(done),
        "skipped": [r["key"] for r in rows if r.get("error")],
        "windows": int(sum(r["windows"] for r in done)),
        "elapsed_seconds": round(time.time() - started, 1),
    }
    if done:
        summary.update({
            "worst_ort_vs_torch_max_abs": max(r["ort_vs_torch"]["max_abs"] for r in done),
            "worst_ort_vs_torch_min_cos": min(r["ort_vs_torch"]["min_cos_sim"] for r in done),
            "worst_ort_vs_cache_max_abs": max(r["ort_vs_cache"]["max_abs"] for r in done),
            "worst_ort_vs_cache_min_cos": min(r["ort_vs_cache"]["min_cos_sim"] for r in done),
            # Isolates the two halves of (b): whatever this number is, the
            # ONNX swap did not cause it — it is today's torch against the
            # shipped cache, i.e. pure historical drift.
            "worst_torch_vs_cache_max_abs": max(r["torch_vs_cache"]["max_abs"] for r in done),
            "worst_pairwise_max_abs_delta": max(
                r["pairwise_cosine"].get("max_abs_delta", 0.0) for r in done),
            "worst_agglomerative_agreement": min(
                [e["agreement"] for r in done for e in r["agglomerative"].values()
                 if "agreement" in e] or [1.0]),
            "decision_differs": [r["key"] for r in done if r.get("verdict") == "DIFFERS"],
            "decision_missing": [r["key"] for r in done if r.get("verdict") == "NO-DECISION"],
        })
        failures = (summary["decision_differs"] or summary["decision_missing"]
                    or drift or summary["skipped"])
        summary["verdict"] = ("SMOKE" if args.max_windows
                              else ("PASS" if not failures else "FAIL"))
    else:
        summary["verdict"] = "NOTHING COMPARED"
    report["summary"] = summary

    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=1, sort_keys=False) + "\n",
                        encoding="utf-8")

    print()
    print(f"compared {summary['compared']} fixture(s), "
          f"{summary['windows']} windows, {summary['elapsed_seconds']}s")
    if done:
        print(f"  worst ort-vs-torch  max|d| {summary['worst_ort_vs_torch_max_abs']:.2e}  "
              f"1-mincos {1.0 - summary['worst_ort_vs_torch_min_cos']:.2e}")
        print(f"  worst ort-vs-cache  max|d| {summary['worst_ort_vs_cache_max_abs']:.2e}  "
              f"1-mincos {1.0 - summary['worst_ort_vs_cache_min_cos']:.2e}")
        print(f"  worst torch-vs-cache (historical drift, not the swap) max|d| "
              f"{summary['worst_torch_vs_cache_max_abs']:.2e}")
        print(f"  worst pairwise cosine-distance delta {summary['worst_pairwise_max_abs_delta']:.2e}")
        print(f"  worst k-sweep label agreement {summary['worst_agglomerative_agreement'] * 100:.2f}%")
        if summary["decision_differs"]:
            print(f"  DECISION CHANGED on: {', '.join(summary['decision_differs'])}")
    if summary["skipped"]:
        print(f"  skipped: {', '.join(summary['skipped'])}")
    print(f"VERDICT {summary['verdict']}")
    print(f"wrote {out_path}")
    return 0 if summary["verdict"] in ("PASS", "SMOKE") else 1


if __name__ == "__main__":
    sys.exit(main())
