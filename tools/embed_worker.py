"""Embed one track's voice windows in a process that exits.

WHY A SUBPROCESS. THE EMBEDDER'S RUNTIME NEVER GIVES MEMORY BACK. That was
true of torch and it stayed true when onnxruntime replaced it, which is why
this file survived the port — measured in one process, RSS 33 MB with the
modules imported, 199 MB with the session built, 1.5 GB after embedding a
1.9-hour track's 595 windows, and 1.5 GB still after deleting both the
waveform and the session and calling gc.collect(). Freed blocks go back to the
allocator — ort's CPU arena holds its peak — never to the OS, so the only way
to return that memory is for the process holding it to exit. Starting it also
got much cheaper: 0.15 s for the onnxruntime import, the model's sha256 check
and the session, where torch charged 2.5 s (0.46 s import + 1.95 s model
load). It still keeps the resident Flask engine at its cold size, which is the
whole point.

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
diarization.embed_windows, same onnxruntime CPU session built the same way,
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
