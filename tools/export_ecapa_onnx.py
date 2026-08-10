#!/usr/bin/env python3
"""Generate the ONNX speaker embedder the app ships — DEV-ONLY, needs torch.

    python tools/export_ecapa_onnx.py            # -> dist/models/ecapa-onnx-v1/
    python tools/export_ecapa_onnx.py --out /tmp/ecapa.onnx

THIS FILE IS A GENERATOR, NOT PART OF THE ENGINE. It imports torch and
speechbrain, which the shipped app no longer has: it exists to turn the
speechbrain checkpoint in models/ecapa into a single ecapa.onnx that
onnxruntime loads with no torch anywhere. Running it needs those two packages
back, so from a fresh checkout either install them into a throwaway
environment (`python -m venv /tmp/ecapa-export && … pip install torch
speechbrain onnx onnxruntime`) or — far cheaper — copy the artifact straight
out of any built bundle, where it sits at
MeetingScribe.app/Contents/Resources/models/ecapa.onnx.

The output is a build INPUT, not repo content: dist/ is gitignored and the
84 MB file is never committed and never published anywhere. Exactly one
consumer copies it into the bundle (tools/build_mac_app.sh), and app startup
seeds it into <DATA_DIR>/models/ecapa-onnx/ against config.ECAPA_ONNX_SHA256 —
which is the pinned hash this script prints, and must be updated here and there
together whenever the artifact is regenerated.

WHAT IS EXPORTED is exactly speechbrain's
EncoderClassifier.encode_batch(wavs, wav_lens, normalize=False): the Fbank
front end, the sentence-level InputNormalization, then ECAPA_TDNN, taking
(wavs [B,T] float32, wav_lens [B] float32 relative) to (embeddings [B,192]).
Every module is the REAL speechbrain object loaded from the on-disk
checkpoint in <DATA_DIR>/models/ecapa (see build_reference), so the exported
weights are by construction the ones the app has always used. Only torch.stft
is replaced —
see DFTPowerSpectrum. diarization.embed_windows keeps building the batches,
so this changes nothing about windowing, padding or normalisation.

TWO EXPORT TRAPS live in here, both of which fail silently at export time.
They are documented at patch_length_to_mask() and DFTPowerSpectrum(); read
those before touching the export call.

The run finishes with onnx.checker(full_check=True) and a parity self-test
against the live speechbrain pipeline on uniform / ragged / single / short
batches. Anything over 1e-3 raw is a hard failure — the artifact is deleted
rather than left behind for a build to pick up.
"""

import argparse
import hashlib
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import torch
    import torch.nn.functional as F
except ImportError:  # this tool, and only this tool, still needs torch
    sys.exit("tools/export_ecapa_onnx.py needs torch + speechbrain, which the "
             "app no longer installs. See this file's docstring — install them "
             "into a throwaway venv, or copy ecapa.onnx out of a built bundle.")

import numpy as np

# Relative to the repo root. dist/ is gitignored on purpose (see the docstring).
DEFAULT_OUT = Path("dist/models/ecapa-onnx-v1/ecapa.onnx")

# The ungated checkpoint every release has embedded with. No app code names it
# any more — the engine carries the exported graph and never asks a hub for
# anything — so it lives with the dev-only tools that still load the torch
# original.
ECAPA_REPO = "speechbrain/spkrec-ecapa-voxceleb"

# opset 17 is the floor for the ops this graph uses and is comfortably below
# what onnxruntime 1.26 supports; nothing here needs anything newer.
OPSET = 17

# The example inputs the tracer runs. DELIBERATELY A SHAPE NOTHING ELSE USES:
# B=3 is not the app's batch (16) and T=25600 is not 2.0 s (32000) nor the
# 0.4 s floor (6400), so any shape that gets baked into the graph shows up as a
# hard runtime failure in the parity test below instead of quietly matching.
EXAMPLE_BATCH = 3
EXAMPLE_SAMPLES = 25600

# Parity gate. Embeddings have |value| up to ~30 and are L2-normalised before
# anything downstream compares them, so 1e-3 raw is already far looser than
# what a correct export produces (the spike measured ~1e-5); it is a tripwire
# for a BROKEN graph, not a precision budget.
PARITY_RAW_TOL = 1e-3


def patch_length_to_mask():
    """EXPORT TRAP 1: speechbrain bakes the example batch size into the graph.

    speechbrain.dataio.dataio.length_to_mask does

        torch.arange(max_len).expand(len(length), max_len) < length.unsqueeze(1)

    `len(length)` is a Python int, so the TorchScript tracer freezes the BATCH
    SIZE of the example input into an Expand. It is not silent at export — the
    tracer warns "Using len to get tensor shape might cause the trace to be
    incorrect" — but it IS silent afterwards: onnx.checker passes, the example
    batch size runs fine, and every other batch size dies inside onnxruntime
    with "Attempting to broadcast an axis by a dimension other than 1".

    The replacement below is numerically identical (plain broadcasting instead
    of an explicit expand) and leaves the batch axis symbolic. Both module
    globals have to be replaced: ECAPA_TDNN does `from … import length_to_mask`
    at import time, so patching dataio alone leaves the copy the model actually
    calls untouched.
    """
    import speechbrain.dataio.dataio as dataio
    import speechbrain.lobes.models.ECAPA_TDNN as ecapa_mod

    def length_to_mask(length, max_len=None, dtype=None, device=None):
        if max_len is None:
            max_len = length.max().long().item()
        ar = torch.arange(max_len, device=length.device, dtype=length.dtype)
        mask = ar.unsqueeze(0) < length.unsqueeze(1)
        if dtype is None:
            dtype = length.dtype
        if device is None:
            device = length.device
        return mask.to(dtype=dtype, device=device)

    dataio.length_to_mask = length_to_mask
    ecapa_mod.length_to_mask = length_to_mask


class DFTPowerSpectrum(torch.nn.Module):
    """EXPORT TRAP 2: torch.stft cannot be exported, so the DFT is a conv1d.

    Drop-in replacement for speechbrain's `spectral_magnitude(STFT(x), power=1)`,
    i.e. |stft|^2 with center=True / pad_mode='constant' / onesided=True.
    torch.onnx.export refuses torch.stft outright (its output is complex, which
    has no ONNX representation), so the transform is written as a real-valued
    conv1d against the Hamming-windowed cos and sin bases — mathematically the
    same operation, one that exports.

    The bases are BUILT IN float64 AND CAST TO float32. Building them directly
    in float32 accumulates enough error in `2*pi*k*n/N` at the high bins to move
    the embedding; the cast happens once, at export, so it costs nothing at
    runtime. Row order in the weight matters: the first n_fft//2+1 rows are the
    real part, the next n_fft//2+1 the imaginary, and forward() splits them
    back out at that boundary.
    """

    def __init__(self, n_fft=400, win_length=400, hop_length=160):
        super().__init__()
        assert win_length == n_fft, "only win_length == n_fft is handled"
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.pad = n_fft // 2

        window = torch.hamming_window(win_length, periodic=True, dtype=torch.float64)
        n = torch.arange(n_fft, dtype=torch.float64)
        k = torch.arange(n_fft // 2 + 1, dtype=torch.float64).unsqueeze(1)  # [201,1]
        ang = 2.0 * math.pi * k * n.unsqueeze(0) / n_fft                    # [201,400]
        # torch.stft uses exp(-i 2 pi k n / N): real = cos, imag = -sin.
        cos_b = (torch.cos(ang) * window.unsqueeze(0)).to(torch.float32)
        sin_b = (-torch.sin(ang) * window.unsqueeze(0)).to(torch.float32)
        w = torch.cat([cos_b, sin_b], dim=0).unsqueeze(1).contiguous()      # [402,1,400]
        self.register_buffer("dft_weight", w)

    def forward(self, x):
        # x: [B, T] -> [B, frames, n_fft//2+1]
        x = F.pad(x.unsqueeze(1), (self.pad, self.pad), mode="constant", value=0.0)
        y = F.conv1d(x, self.dft_weight, stride=self.hop_length)  # [B, 402, frames]
        nb = self.n_fft // 2 + 1
        re = y[:, :nb, :]
        im = y[:, nb:, :]
        power = re * re + im * im                                 # [B, 201, frames]
        return power.transpose(1, 2)


class OnnxECAPA(torch.nn.Module):
    """wavs [B,T] float32 + wav_lens [B] float32 relative -> embeddings [B,192].

    encode_batch(normalize=False) rebuilt around the exportable spectrum. The
    fbank / normalisation / embedding modules are the reference's OWN
    instances, not copies, so there is no second set of weights to keep in step.
    """

    def __init__(self, fbank, mean_var_norm, embedding_model):
        super().__init__()
        self.spec = DFTPowerSpectrum(
            n_fft=fbank.compute_STFT.n_fft,
            win_length=fbank.compute_STFT.win_length,
            hop_length=fbank.compute_STFT.hop_length,
        )
        self.compute_fbanks = fbank.compute_fbanks
        self.mean_var_norm = mean_var_norm
        self.embedding_model = embedding_model

    def forward(self, wavs, wav_lens):
        power = self.spec(wavs)
        feats = self.compute_fbanks(power)
        feats = self.mean_var_norm(feats, wav_lens)
        emb = self.embedding_model(feats, wav_lens)   # [B, 1, 192]
        return emb.squeeze(1)


def build_reference():
    """The live speechbrain EncoderClassifier this graph is traced out of.

    THIS LOADER USED TO LIVE IN diarization.py and is here now because that is
    the whole point of the port: the engine no longer has torch or speechbrain,
    so the only file allowed to know how to load the checkpoint is the one that
    converts it. The savedir is unchanged — <DATA_DIR>/models/ecapa, where
    every previous release already downloaded it — so a machine that has run
    the old build re-exports from exactly the bytes it was using.
    """
    try:
        from speechbrain.inference.speaker import EncoderClassifier
    except ImportError:  # speechbrain < 1.0
        from speechbrain.pretrained import EncoderClassifier

    from config import MODELS_DIR

    savedir = MODELS_DIR / "ecapa"
    print(f"checkpoint: {savedir}")
    kwargs = {"source": ECAPA_REPO, "savedir": str(savedir),
              "run_opts": {"device": "cpu"}}
    try:
        # COPY_SKIP_CACHE keeps the files in savedir as plain copies rather
        # than symlinks into the shared HuggingFace cache — which is how the
        # shipped builds filled this directory, so it is how it is read back.
        from speechbrain.utils.fetching import LocalStrategy

        ref = EncoderClassifier.from_hparams(
            local_strategy=LocalStrategy.COPY_SKIP_CACHE, **kwargs)
    except (ImportError, TypeError):  # speechbrain without the strategy
        ref = EncoderClassifier.from_hparams(**kwargs)
    ref.mods.eval()
    return ref


def build_wrapper(ref):
    m = OnnxECAPA(ref.mods.compute_features,
                  ref.mods.mean_var_norm,
                  ref.mods.embedding_model)
    m.eval()
    return m


def export(wrapper, out_path):
    """Trace and write the graph. Both traps above must already be handled."""
    # A ragged example, so the padding/length path is traced too rather than
    # only the all-ones case.
    wavs = torch.zeros(EXAMPLE_BATCH, EXAMPLE_SAMPLES)
    wavs[0] = torch.randn(EXAMPLE_SAMPLES) * 0.1
    wavs[1, :20000] = torch.randn(20000) * 0.1
    wavs[2, :9000] = torch.randn(9000) * 0.1
    lens = torch.tensor([1.0, 20000 / EXAMPLE_SAMPLES, 9000 / EXAMPLE_SAMPLES],
                        dtype=torch.float32)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # dynamo=False keeps the TorchScript exporter, which torch 2.9 demoted from
    # the default. The dynamo path was tried in the spike and is not used: it
    # needs torch.export.Dim bounds on the time axis, which would put a hard
    # min/max on window length into an artifact that outlives this script.
    torch.onnx.export(
        wrapper, (wavs, lens), str(out_path),
        input_names=["wavs", "wav_lens"], output_names=["embeddings"],
        dynamic_axes={"wavs": {0: "batch", 1: "time"},
                      "wav_lens": {0: "batch"},
                      "embeddings": {0: "batch"}},
        opset_version=OPSET, do_constant_folding=True, dynamo=False,
    )

    import onnx

    model = onnx.load(str(out_path))
    onnx.checker.check_model(str(out_path), full_check=True)
    ops = {}
    for node in model.graph.node:
        ops[node.op_type] = ops.get(node.op_type, 0) + 1
    print("onnx.checker(full_check=True): OK")
    print(f"  ir_version {model.ir_version}  opsets "
          f"{[(o.domain or 'ai.onnx', o.version) for o in model.opset_import]}")
    print(f"  {len(model.graph.node)} nodes: "
          f"{dict(sorted(ops.items(), key=lambda kv: -kv[1]))}")


def _l2(x):
    n = np.linalg.norm(x, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return x / n


def _int16_audio(b, t, seed):
    """float32 audio derived from int16, the shape soundfile hands us."""
    rng = np.random.default_rng(seed)
    return (rng.integers(-20000, 20000, size=(b, t))
            .astype(np.int16).astype(np.float32) / 32768.0)


def _parity_cases():
    """The batch shapes embed_windows actually produces, plus the edges."""
    cases = [
        ("uniform B=16 T=32000", _int16_audio(16, 32000, 11), np.ones(16, np.float32)),
        ("single  B=1  T=32000", _int16_audio(1, 32000, 12), np.ones(1, np.float32)),
        ("short   B=8  T=6400 ", _int16_audio(8, 6400, 13), np.ones(8, np.float32)),
    ]
    # Ragged: a last batch whose windows run short is padded to the batch max
    # and described by relative lengths — the case both export traps break.
    rng = np.random.default_rng(14)
    lens = rng.integers(6400, 32000, size=16)
    lens[0] = 32000
    mx = int(lens.max())
    raw = _int16_audio(16, mx, 15)
    wav = np.zeros((16, mx), np.float32)
    for i, n in enumerate(lens):
        wav[i, :n] = raw[i, :n]
    cases.append(("ragged  B=16 mixed  ", wav, (lens / mx).astype(np.float32)))
    return cases


def parity(ref, wrapper, out_path):
    """onnxruntime vs the live speechbrain pipeline. Returns True on pass."""
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    sess = ort.InferenceSession(str(out_path), so,
                                providers=["CPUExecutionProvider"])
    print(f"  inputs  {[(i.name, i.shape) for i in sess.get_inputs()]}")
    print(f"  outputs {[(o.name, o.shape) for o in sess.get_outputs()]}")

    worst = 0.0
    for name, wav, lens in _parity_cases():
        tw = torch.from_numpy(np.ascontiguousarray(wav))
        tl = torch.from_numpy(np.ascontiguousarray(lens))
        with torch.no_grad():
            sb = ref.encode_batch(tw, wav_lens=tl, normalize=False).squeeze(1).numpy()
            wr = wrapper(tw, tl).numpy()
        onx = sess.run(None, {"wavs": np.ascontiguousarray(wav, np.float32),
                              "wav_lens": np.ascontiguousarray(lens, np.float32)})[0]
        d_sb, d_wr = np.abs(onx - sb).max(), np.abs(onx - wr).max()
        n_sb, n_wr = np.abs(_l2(onx) - _l2(sb)).max(), np.abs(_l2(onx) - _l2(wr)).max()
        worst = max(worst, float(d_sb))
        flag = "  <-- OVER TOLERANCE" if d_sb > PARITY_RAW_TOL else ""
        print(f"  {name}  vs speechbrain: raw {d_sb:.3e} / L2 {n_sb:.3e}"
              f"   vs wrapper: raw {d_wr:.3e} / L2 {n_wr:.3e}{flag}")

    # Context for reading those numbers: torch's own answer for one window
    # moves by this much depending on who else is in the batch, so a drift
    # below it is not a difference the clustering can distinguish from noise.
    wav = _int16_audio(16, 32000, 11)
    with torch.no_grad():
        in_batch = ref.encode_batch(torch.from_numpy(wav), wav_lens=torch.ones(16),
                                    normalize=False).squeeze(1).numpy()
        alone = ref.encode_batch(torch.from_numpy(wav[:1]), wav_lens=torch.ones(1),
                                 normalize=False).squeeze(1).numpy()
    print(f"  [context] speechbrain's OWN batch-composition wobble (item 0 in "
          f"B=16 vs B=1): raw {np.abs(in_batch[0] - alone[0]).max():.3e} / "
          f"L2 {np.abs(_l2(in_batch[:1]) - _l2(alone)).max():.3e}")

    if worst > PARITY_RAW_TOL:
        print(f"\nPARITY FAILED: worst raw diff {worst:.3e} > {PARITY_RAW_TOL:.0e}")
        return False
    print(f"\nparity OK: worst raw diff {worst:.3e} (tolerance {PARITY_RAW_TOL:.0e})")
    return True


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(
        description="Export the ECAPA speaker embedder to ONNX (dev-only).")
    ap.add_argument("--out", default=None,
                    help=f"output .onnx path (default: {DEFAULT_OUT})")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parent.parent
    out_path = Path(args.out).expanduser() if args.out else repo / DEFAULT_OUT
    out_path = out_path.resolve()

    # Before anything is built: the tracer reads these module globals.
    patch_length_to_mask()
    ref = build_reference()
    wrapper = build_wrapper(ref)

    print(f"\nexporting -> {out_path}")
    export(wrapper, out_path)
    size = out_path.stat().st_size
    print(f"  {size} bytes ({size / 1e6:.1f} MB)")

    print("\nparity: onnxruntime-CPU vs the live speechbrain pipeline")
    if not parity(ref, wrapper, out_path):
        out_path.unlink(missing_ok=True)
        sys.exit(f"deleted {out_path} — a failing artifact must not reach a build")

    digest = sha256_file(out_path)
    sums = out_path.parent / "SHA256SUMS"
    sums.write_text(f"{digest}  {out_path.name}\n", encoding="utf-8")

    print(f"\nsha256 {digest}")
    print(f"wrote  {out_path}")
    print(f"wrote  {sums}")
    print("\nNext: tools/build_mac_app.sh copies this into the bundle, and app "
          "startup seeds it\nagainst config.ECAPA_ONNX_SHA256 — update that "
          "constant to the sha256 above.")


if __name__ == "__main__":
    main()
