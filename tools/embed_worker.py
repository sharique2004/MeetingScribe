"""Embed one track's voice windows in a process that exits.

WHY A SUBPROCESS. torch never gives memory back: with the ECAPA embedder
loaded on MPS the engine sits at ~520 MB over its cold size, and dropping the
model changes nothing — measured in one process, RSS after the embedding pass
668 MB, after `_EMBEDDER = None` + gc.collect() 668 MB, after
torch.mps.empty_cache() 668 MB. The only way to return that memory is for the
process holding it to exit. This worker costs ~2.5 s of startup per track
(0.46 s torch import + 1.95 s model load, measured) against a transcription
phase measured in minutes, and it keeps the resident Flask engine at its
cold size.

CONTRACT (pipeline._embed_track is the only caller; keep them in sync):

    embed_worker.py <wav> <windows.json> --out <embeddings.npy>

  * <windows.json> — JSON list of [start, end] second pairs, the exact output
    of diarization.build_windows. JSON round-trips Python floats exactly, so
    the child slices the same samples the parent would have.
  * --out — the (N, D) float64 L2-normalised array, np.save'd. The path must
    already end in .npy (np.save appends it otherwise and the parent would
    read the wrong name).
  * stdout — progress lines ("Analyzing voices… 32/595"), forwarded verbatim
    to the caller's progress_cb. stderr — log noise. Exit 0 on success.

The embeddings must be BIT-identical to an in-process run: same
diarization.embed_windows, same device pick (MPS with the same CPU fallback),
same pipeline.load_mono_16k blocked reader — this file computes the same
numbers in a different process, nothing more. pipeline._embed_track falls
back to embedding in-process on any failure here, so a broken worker costs
memory, never a meeting.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("windows")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if not args.out.endswith(".npy"):
        ap.error("--out must end in .npy")

    import numpy as np

    import diarization
    import pipeline

    windows = [(float(a), float(b))
               for a, b in json.loads(Path(args.windows).read_text())]
    audio = pipeline.load_mono_16k(args.wav)
    emb = diarization.embed_windows(
        audio, windows, progress_cb=lambda msg: print(msg, flush=True))
    np.save(args.out, emb)


if __name__ == "__main__":
    main()
