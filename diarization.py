"""Speaker diarization: figure out who spoke when by clustering voice-prints.

Uses the ungated speechbrain ECAPA-TDNN speaker-embedding model (downloads
automatically on first use, no account or token needed) plus agglomerative
clustering. No cloud calls — everything runs locally.
"""

import logging
import math
import sys

import numpy as np

from config import MODELS_DIR

log = logging.getLogger("meetingscribe.diarization")

EMBED_SR = 16000
WINDOW_S = 2.0
HOP_S = 1.0
MIN_WINDOW_S = 0.4
MAX_AUTO_SPEAKERS = 8
BATCH_SIZE = 16
# Auto mode: clusters with less speech than this are folded into their
# nearest voice — short interjections ("yeah", "okay") otherwise become
# phantom extra speakers.
MIN_CLUSTER_S = 10.0

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


def _load_embedder(device):
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
        "source": "speechbrain/spkrec-ecapa-voxceleb",
        "savedir": str(MODELS_DIR / "ecapa"),
        "run_opts": {"device": device},
    }
    try:
        # Plain copies instead of symlinks — creating symlinks on Windows
        # requires admin rights and fails with WinError 1314.
        from speechbrain.utils.fetching import LocalStrategy

        return EncoderClassifier.from_hparams(
            local_strategy=LocalStrategy.COPY_SKIP_CACHE, **kwargs
        )
    except (ImportError, TypeError):  # older speechbrain without LocalStrategy
        return EncoderClassifier.from_hparams(**kwargs)


def _get_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        device = _pick_device()
        try:
            _EMBEDDER = _load_embedder(device)
            if device != "cpu":  # prove the GPU path works before trusting it
                import torch

                with torch.no_grad():
                    _EMBEDDER.encode_batch(
                        torch.zeros(1, EMBED_SR), wav_lens=torch.ones(1)
                    )
        except Exception as exc:
            if device == "cpu":
                raise
            log.warning("GPU (%s) embedder failed (%s); using CPU", device, exc)
            _EMBEDDER = _load_embedder("cpu")
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

    model = _get_embedder()
    embeddings = []
    total = len(windows)
    with torch.no_grad():
        for batch_start in range(0, total, BATCH_SIZE):
            batch = windows[batch_start : batch_start + BATCH_SIZE]
            chunks = []
            for (t0, t1) in batch:
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


def _fold_weak_clusters(labels, embeddings, durations, threshold):
    """Auto-mode cleanup: fold clusters with very little total speech into
    their most similar voice, then merge cluster pairs whose centroids are
    clearly the same person. Returns the new labels."""
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

        weak = [lab for lab in unique if seconds[lab] < MIN_CLUSTER_S]
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
    in it so the caller can persist them.
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
    return new_segments, n_found
