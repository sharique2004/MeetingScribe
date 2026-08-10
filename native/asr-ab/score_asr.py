#!/usr/bin/env python3
"""Score a quantized Parakeet candidate against the shipping fp baseline.

WHY THIS FILE EXISTS. native/diarization-ab/score_ab.py answers "does this
engine hear the right NUMBER OF PEOPLE"; nothing in the repo answers "does this
engine hear the right WORDS". tools/eval_diarization.py cannot: it scores
clustering over frozen ECAPA embeddings and has no notion of a transcript. So
this scorer keeps that harness's identity and privacy machinery — the pseudonym
map, the fixture loader, the timestamp join onto the original recordings — and
swaps the scoring unit to TEXT: WER/CER where a reference exists, and
divergence from the shipping transcript where one does not.

READ native/asr-ab/PROTOCOL.md FIRST. It was written before any number existed
and it, not this file, defines the arms, the floors, and the ship rule. This
file only produces the numbers that document argues about.

WHAT IS AND IS NOT COMPARABLE:
  * AMI (ES2004a, IS1009c): reference transcripts exist, built from the manual
    word annotations, so WER and CER here are ACCURACY. They are computed with
    this file's normalization against a mixed reference and are NOT comparable
    to published AMI WER tables.
  * Real corpus: no reference transcript exists and none can be made without a
    human transcribing private meetings. The only available question is
    DIVERGENCE from the shipping arm, which is not accuracy: where the baseline
    is wrong and a candidate is right, that scores as divergence AGAINST the
    candidate. Read it with the non-determinism floor beside it, always.
  * Synthetic fixtures: not runnable. They are generated embedding vectors with
    no audio behind them.

PRIVACY: the same rule as the rest of the harness. Everything printed or
committed is keyed by the stable pseudonym (Call A.., Room P..). Recording
directory names are real meeting titles and are resolved in memory only.
Transcripts are verbatim private speech: they go to results/transcripts/, which
is gitignored, and never into results/*.json.

Usage:
  score_asr.py --engine mlx-fp                      # the baseline pass
  score_asr.py --engine mlx-fp --tag rerun          # the non-determinism floor
  score_asr.py --engine coreml-int8 --only "Room W"
  score_asr.py --list                               # what would run, and from where

Writes results/<engine>[-<tag>].json (pseudonym-keyed, safe to commit), per
fixture transcripts + divergent-span dumps under results/transcripts/ (NOT safe
to commit, gitignored), and prints the comparison table.
"""

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]  # .../MeetingScribe
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

import eval_diarization as ev  # noqa: E402  (the single source of pseudonyms/fixtures)
import rss_probe  # noqa: E402  (the memory sampler, reused read-only)

RECORDINGS = Path.home() / ".meetingscribe" / "recordings"
MULTIPARTY = Path.home() / ".meetingscribe" / "multiparty"
REFS = HERE / "refs"
RESULTS = HERE / "results"
TRANSCRIPTS = RESULTS / "transcripts"
MODELS = HERE / "models"
TS_RE = re.compile(r"(\d{8}-\d{6})$")

# Audio containers the recorder has used. score_ab.py probes only .wav because
# the diarization cache it joins onto predates any other; ASR reads whatever is
# on disk, so both are tried and the one that exists wins.
AUDIO_EXTS = (".wav", ".flac")

FLUID_TRANSCRIBE = REPO / "native" / "fluiddiarizer" / ".build" / "release" / "fluid-transcribe"

# The arms. See PROTOCOL.md §1 — this table is the code half of that table.
ENGINES = ("mlx-fp", "mlx-fp-v3", "mlx-int8", "coreml-int8", "coreml-int4")
BASELINE_ENGINE = "mlx-fp"

# The two AMI meetings whose audio is already on this machine. Public corpus, so
# these keys are real names and need no pseudonym.
AMI_MEETINGS = ("ES2004a", "IS1009c")
AMI_TRACK = "Mix-Headset"  # PROTOCOL.md §5: mixed audio vs the mixed reference


# ---------------------------------------------------------------------------
# Fixtures and audio resolution
# ---------------------------------------------------------------------------

def label(key):
    """The name a row prints under.

    ev.label_of() marks anything it does not recognise as an unlabelled real
    fixture ("?AMI ES2"), which is exactly right for its corpus and exactly
    wrong for AMI: those are public meetings whose real names identify nobody
    and are the whole point of quoting them. Real-corpus keys still go through
    ev.label_of so the pseudonyms can never drift from the diarization report.
    """
    return key if key.startswith("AMI ") else ev.label_of(key)


def resolve_audio(fx):
    """Map a frozen fixture to the original track audio it was embedded from.

    Fixture slugs and recording directories share their trailing
    YYYYMMDD-HHMMSS timestamp; that suffix is the join key, exactly as in
    native/diarization-ab/score_ab.py. Returns (path or None, reason).

    BOTH containers are probed, and the answer is DELIBERATELY NOT CACHED —
    see the note on re-resolution in run_targets(). audio_archive.py converts
    finished meetings from `<key>.wav` to a verified `<key>.flac` in the
    background, so which of the two exists is a function of WHEN you ask.
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
    for ext in AUDIO_EXTS:
        cand = matches[0] / f"{fx['track']}{ext}"
        if cand.exists():
            return cand, ""
    return None, f"{fx['track']}{{{','.join(AUDIO_EXTS)}}} missing from recording"


def ami_targets():
    """The AMI rows: (key, audio path, reference words) for what is on disk."""
    out = []
    for mtg in AMI_MEETINGS:
        audio = MULTIPARTY / f"{mtg}.{AMI_TRACK}.wav"
        if not audio.exists():
            continue
        out.append({"key": f"AMI {mtg}", "meeting": mtg, "audio": audio,
                    "track": AMI_TRACK, "corpus": "ami"})
    return out


def real_targets(label_map):
    """The real-corpus rows, keyed by pseudonym, newest-format agnostic.

    The `fx` record is kept on the row so the path can be resolved AGAIN at
    spawn time; see run_targets().
    """
    out = []
    if not label_map:
        return out
    for fx in ev.load_fixtures(ev.FIXTURES, corpus="real"):
        audio, why = resolve_audio(fx)
        out.append({"key": fx["key"], "audio": audio, "skip": why, "fx": fx,
                    "track": fx["track"], "corpus": "real", "slug": fx["slug"]})
    out.sort(key=lambda r: label(r["key"]))
    return out


def refresh_audio(target):
    """Re-resolve one row's audio immediately before it is run.

    THE CORPUS IS NOT FROZEN WHILE THIS RUNS, and assuming it was cost a full
    arm's pass: a planning sweep resolved every fixture to `<track>.wav`, and
    by the time the eleventh fixture was reached audio_archive.py had converted
    the rest to `<track>.flac` and deleted the WAVs, so eleven rows failed with
    "System error" from libsndfile on paths that no longer existed. A path
    resolved minutes ago is a CACHED FACT ABOUT A MUTABLE DIRECTORY, and the
    only fix is not to cache it. Returns (path or None, reason).
    """
    if target["corpus"] != "real":
        audio = target.get("audio")
        return (audio, "") if audio and audio.exists() else (None, "audio missing")
    audio, why = resolve_audio(target["fx"])
    target["audio"], target["skip"] = audio, why
    return audio, why


# ---------------------------------------------------------------------------
# AMI reference transcripts
#
# Built from ami_public_manual_1.6.2/words/<meeting>.<speaker>.words.xml (CC-BY;
# see PROTOCOL.md §5 for provenance). A single-channel transcriber hears every
# speaker on one track, so the reference is every speaker's words merged in time
# order — the MIXED reference the Mix-Headset audio corresponds to.
#
# Excluded on purpose, because an ASR is not supposed to emit them and counting
# them as deletions would charge every arm for the same non-error:
#   * <w punc="true"> — punctuation is a separate token in AMI; this file's
#     normalization strips punctuation from both sides anyway.
#   * <vocalsound>, <disfmarker>, <gap> — laughter, disfluency markers, and
#     un-transcribable audio. Not words.
# ---------------------------------------------------------------------------

AMI_WORDS_DIR = REFS / "ami_public_manual_1.6.2" / "words"


def ami_reference(meeting, audio_seconds=None):
    """(words, meta) for one AMI meeting, or (None, reason)."""
    if not AMI_WORDS_DIR.is_dir():
        return None, (f"AMI manual annotations not unpacked at {AMI_WORDS_DIR} — "
                      "see PROTOCOL.md §5 for the download")
    files = sorted(AMI_WORDS_DIR.glob(f"{meeting}.*.words.xml"))
    if not files:
        return None, f"no {meeting}.*.words.xml in {AMI_WORDS_DIR}"
    rows = []
    untimed = 0
    for path in files:
        speaker = path.name.split(".")[1]
        last_t = 0.0
        for idx, el in enumerate(ET.parse(path).getroot()):
            tag = el.tag.split("}")[-1]
            if tag != "w" or el.get("punc") == "true":
                continue
            text = (el.text or "").strip()
            if not text:
                continue
            start = el.get("starttime")
            if start is None:
                untimed += 1
                t = last_t  # keep it in sequence rather than dropping a real word
            else:
                t = float(start)
                last_t = t
            rows.append((t, speaker, idx, text))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    clipped = 0
    if audio_seconds:
        keep = [r for r in rows if r[0] <= audio_seconds + 0.5]
        clipped = len(rows) - len(keep)
        rows = keep
    meta = {
        "source": "ami_public_manual_1.6.2 words/*.xml (CC-BY)",
        "speakers": len(files),
        "words": len(rows),
        "untimed_words": untimed,
        "clipped_past_audio_end": clipped,
        "span_seconds": round(rows[-1][0], 2) if rows else 0.0,
    }
    return [r[3] for r in rows], meta


# ---------------------------------------------------------------------------
# Text normalization and scoring
#
# TWO normalizations, both reported, never one instead of the other:
#   raw  — lowercase, strip punctuation (PROTOCOL.md §3's primary number)
#   norm — additionally digit- and formatting-insensitive: numbers spelled out,
#          apostrophes and hyphens dissolved. "1,500" vs "fifteen hundred" is a
#          formatting difference; a gate that cannot tell it from a content
#          difference fails candidates for the wrong reason. It is reported
#          BESIDE the raw number so nobody has to trust this list of rules.
# ---------------------------------------------------------------------------

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve "
         "thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety")
_SCALES = ((1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand"))


def _spell_int(n):
    if n < 0:
        return ["minus"] + _spell_int(-n)
    if n < 20:
        return [_ONES[n]]
    if n < 100:
        out = [_TENS[n // 10]]
        return out + ([_ONES[n % 10]] if n % 10 else [])
    if n < 1000:
        out = [_ONES[n // 100], "hundred"]
        return out + (_spell_int(n % 100) if n % 100 else [])
    for value, name in _SCALES:
        if n >= value:
            out = _spell_int(n // value) + [name]
            return out + (_spell_int(n % value) if n % value else [])
    return [str(n)]  # unreachable for ints below a billion-billion


_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_NUM_RE = re.compile(r"^\d+$")


def strip_punct(text):
    """Lowercase, drop punctuation, collapse whitespace. Unicode dashes and
    curly quotes are folded first so a smart apostrophe is not a word boundary
    on one side of the comparison and a letter on the other."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'")
    text = re.sub(r"[‐-―]", "-", text)
    text = _PUNCT_RE.sub(" ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def norm_words(text):
    """The raw normalization, as a word list."""
    return strip_punct(text).split()


def deep_norm_words(text):
    """The digit- and formatting-insensitive variant. Digits become words;
    hyphenated and apostrophised forms collapse to their letters."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'")
    # 1,500 -> 1500 so it spells as one number, not "one" "five hundred"
    text = re.sub(r"(?<=\d),(?=\d\d\d\b)", "", text)
    text = re.sub(r"[^\w\s.]", " ", text.lower())
    out = []
    for tok in text.split():
        tok = tok.strip(".")
        if not tok:
            continue
        if _NUM_RE.match(tok):
            out.extend(_spell_int(int(tok)))
        elif re.match(r"^\d+\.\d+$", tok):
            whole, frac = tok.split(".")
            out.extend(_spell_int(int(whole)) + ["point"] + [_ONES[int(d)] for d in frac])
        else:
            out.extend(w for w in re.sub(r"[^\w]", "", tok).split() if w)
            if not re.sub(r"[^\w]", "", tok):
                continue
    return [w for w in out if w]


# --- alignment -------------------------------------------------------------
#
# Exact Levenshtein alignment is O(N*M); a 111-minute meeting is ~15 000 words
# per side, so a full backtrace matrix is ~200 M cells and simply will not fit.
# The transcripts being compared are near-identical by construction, which is
# exactly the case difflib is good at, so: take difflib's LONG common blocks as
# anchors (a run of ANCHOR identical words is not a coincidental alignment) and
# run exact DP only in the small windows between them. Inside every window the
# alignment is optimal; across windows it is optimal given the anchors. Windows
# too large even for that (a wholesale disagreement) fall back to difflib's own
# opcodes and are counted in "approx_regions" so the report can say so.

ANCHOR = 4
DP_CELL_LIMIT = 4_000_000


def _dp_ops(a, b):
    """Exact Levenshtein opcodes for two small sequences."""
    n, m = len(a), len(b)
    if n == 0 and m == 0:
        return []
    if n == 0:
        return [("insert", 0, 0, 0, m)]
    if m == 0:
        return [("delete", 0, n, 0, 0)]
    dist = np.zeros((n + 1, m + 1), dtype=np.int32)
    dist[:, 0] = np.arange(n + 1)
    dist[0, :] = np.arange(m + 1)
    back = np.zeros((n + 1, m + 1), dtype=np.uint8)  # 0=diag 1=up(del) 2=left(ins)
    back[1:, 0] = 1
    back[0, 1:] = 2
    for i in range(1, n + 1):
        ai = a[i - 1]
        row_prev, row = dist[i - 1], dist[i]
        eq = np.fromiter((ai == x for x in b), dtype=bool, count=m)
        diag = row_prev[:-1] + (~eq)
        up = row_prev[1:] + 1
        for j in range(1, m + 1):
            best = diag[j - 1]
            code = 0
            if up[j - 1] < best:
                best, code = up[j - 1], 1
            if row[j - 1] + 1 < best:
                best, code = row[j - 1] + 1, 2
            row[j] = best
            back[i, j] = code
    ops = []
    i, j = n, m
    while i > 0 or j > 0:
        code = back[i, j]
        if code == 0:
            tag = "equal" if a[i - 1] == b[j - 1] else "replace"
            ops.append((tag, i - 1, i, j - 1, j))
            i, j = i - 1, j - 1
        elif code == 1:
            ops.append(("delete", i - 1, i, j, j))
            i -= 1
        else:
            ops.append(("insert", i, i, j - 1, j))
            j -= 1
    ops.reverse()
    return _coalesce(ops)


def _coalesce(ops):
    out = []
    for op in ops:
        if out and out[-1][0] == op[0] and out[-1][2] == op[1] and out[-1][4] == op[3]:
            tag, i1, _, j1, _ = out[-1]
            out[-1] = (tag, i1, op[2], j1, op[4])
        else:
            out.append(op)
    return out


def align_ops(a, b):
    """Opcodes aligning sequence a (reference/baseline) onto b (candidate).

    Returns (ops, approx_regions). Each op is (tag, i1, i2, j1, j2) with tag in
    equal/replace/delete/insert, i indexing a and j indexing b.
    """
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    anchors = [bl for bl in sm.get_matching_blocks() if bl.size >= ANCHOR]
    ops = []
    approx = 0
    ai = bi = 0
    for bl in anchors + [difflib.Match(len(a), len(b), 0)]:
        sub_a, sub_b = a[ai:bl.a], b[bi:bl.b]
        if sub_a or sub_b:
            if len(sub_a) * len(sub_b) <= DP_CELL_LIMIT:
                window = _dp_ops(sub_a, sub_b)
            else:
                approx += 1
                window = [(t, i1, i2, j1, j2) for t, i1, i2, j1, j2
                          in difflib.SequenceMatcher(None, sub_a, sub_b,
                                                     autojunk=False).get_opcodes()]
            ops.extend((t, i1 + ai, i2 + ai, j1 + bi, j2 + bi)
                       for t, i1, i2, j1, j2 in window)
        if bl.size:
            ops.append(("equal", bl.a, bl.a + bl.size, bl.b, bl.b + bl.size))
        ai, bi = bl.a + bl.size, bl.b + bl.size
    return _coalesce(ops), approx


def error_counts(ops):
    sub = dele = ins = 0
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        n, m = i2 - i1, j2 - j1
        if tag == "replace":
            sub += min(n, m)
            dele += max(0, n - m)
            ins += max(0, m - n)
        elif tag == "delete":
            dele += n
        elif tag == "insert":
            ins += m
    return sub, dele, ins


def rate(a, b):
    """The error rate of hypothesis b against reference a, and its ops."""
    ops, approx = align_ops(a, b)
    sub, dele, ins = error_counts(ops)
    denom = len(a)
    value = (sub + dele + ins) / denom if denom else (1.0 if b else 0.0)
    return {"rate": value, "sub": sub, "del": dele, "ins": ins,
            "ref_len": denom, "hyp_len": len(b), "approx_regions": approx}, ops


# ---------------------------------------------------------------------------
# Transcript shapes
# ---------------------------------------------------------------------------

def flatten_words(segments):
    """Every word in a transcript, in time order, as (normalized, dict)."""
    out = []
    for seg in segments:
        for w in seg.get("words") or []:
            token = strip_punct(w.get("w", ""))
            for piece in token.split():
                out.append((piece, w))
    return out


def transcript_text(segments):
    return " ".join((seg.get("text") or "").strip() for seg in segments).strip()


def word_time_agreement(base_words, cand_words):
    """(median, p95, n) of |Δstart| in ms over textually agreeing words."""
    a = [w[0] for w in base_words]
    b = [w[0] for w in cand_words]
    ops, _ = align_ops(a, b)
    deltas = []
    for tag, i1, i2, j1, j2 in ops:
        if tag != "equal":
            continue
        for k in range(i2 - i1):
            wa, wb = base_words[i1 + k][1], cand_words[j1 + k][1]
            try:
                deltas.append(abs(float(wa["s"]) - float(wb["s"])) * 1000.0)
            except (KeyError, TypeError, ValueError):
                continue
    if not deltas:
        return None, None, 0
    arr = np.array(deltas)
    return float(np.median(arr)), float(np.percentile(arr, 95)), len(deltas)


def divergent_spans(base_words, cand_words, ops, top=20, context=6):
    """The most divergent stretches, largest first, with surrounding context.

    Written for a human to READ (PROTOCOL.md §4 clause 3), so each span carries
    the words either side and the baseline timestamp — enough to find the moment
    in the meeting and decide whether the meaning changed.
    """
    spans = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        size = max(i2 - i1, j2 - j1)
        base_txt = " ".join(w[0] for w in base_words[i1:i2])
        cand_txt = " ".join(w[0] for w in cand_words[j1:j2])
        before = " ".join(w[0] for w in base_words[max(0, i1 - context):i1])
        after = " ".join(w[0] for w in base_words[i2:i2 + context])
        at = None
        anchor = base_words[i1] if i1 < len(base_words) else None
        if anchor is None and i1 > 0:
            anchor = base_words[i1 - 1]
        if anchor is not None:
            try:
                at = round(float(anchor[1]["s"]), 2)
            except (KeyError, TypeError, ValueError):
                at = None
        spans.append({"size": size, "op": tag, "at_seconds": at,
                      "baseline": base_txt, "candidate": cand_txt,
                      "before": before, "after": after})
    spans.sort(key=lambda s: (-s["size"], s["at_seconds"] if s["at_seconds"] is not None else 0))
    return spans[:top]


# ---------------------------------------------------------------------------
# WORKER — one engine, one file, one fresh process
#
# Every arm runs here, in a subprocess the driver spawns and measures. In-process
# arm switching would measure the wrong memory (the second arm inherits the
# first arm's warmed allocator) and the wrong time, which is why PROTOCOL.md §3
# requires the fresh process.
# ---------------------------------------------------------------------------

INT8_REPO = "sonic-speech/parakeet-tdt-0.6b-v3-int8"
V3_REPO = "mlx-community/parakeet-tdt-0.6b-v3"


def _mark(name):
    print(f"MARK:{name}", flush=True)


def _quantized_predicate(config_path):
    """Rebuild the class_predicate the checkpoint was quantized with.

    From the repo's quantization_config.json: bits=8, group_size=64,
    strategy=encoder_only. "encoder_only" means exactly what it says — the
    Conformer encoder is quantized, the decoder and joint stay bfloat16 — so the
    predicate is "this module lives under .encoder and can be quantized at this
    group size". Modules whose last dimension is not a multiple of group_size
    cannot be, and mlx would raise rather than skip them, so they are excluded
    here the same way the quantizer must have excluded them.
    """
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    bits = int(cfg.get("bits", 8))
    group_size = int(cfg.get("group_size", 64))
    strategy = cfg.get("strategy", "encoder_only")
    if strategy != "encoder_only":
        raise RuntimeError(f"unhandled quantization strategy {strategy!r}")

    def predicate(path, module):
        if not path.startswith("encoder"):
            return False
        if not hasattr(module, "to_quantized"):
            return False
        weight = getattr(module, "weight", None)
        if weight is None or weight.ndim != 2:
            return False
        return weight.shape[-1] % group_size == 0

    return predicate, bits, group_size


def load_int8_model():
    """The mlx-int8 arm's model, or a RuntimeError explaining the drop.

    parakeet_mlx.from_pretrained cannot load this checkpoint: its last step
    casts every parameter to bfloat16, and a quantized layer's weight is packed
    uint32 — the cast would reinterpret the packing as numbers. So the load is
    reimplemented minimally here: build the architecture from config, quantize
    it into the same shape the checkpoint has, then load the weights and STOP.
    """
    import mlx.core as mx  # noqa: F401  (imported for the side effect of init)
    import mlx.nn as nn
    from huggingface_hub import hf_hub_download
    from parakeet_mlx.utils import from_config

    cache = str(REPO / "models" / "parakeet")
    config_path = hf_hub_download(INT8_REPO, "config.json", cache_dir=cache)
    quant_path = hf_hub_download(INT8_REPO, "quantization_config.json", cache_dir=cache)
    weight_path = hf_hub_download(INT8_REPO, "model.safetensors", cache_dir=cache)

    model = from_config(json.loads(Path(config_path).read_text(encoding="utf-8")))
    predicate, bits, group_size = _quantized_predicate(quant_path)
    nn.quantize(model, group_size=group_size, bits=bits, class_predicate=predicate)

    # strict=True on purpose: a predicate that quantized the wrong set of
    # modules produces a parameter tree that does not match the checkpoint, and
    # that must be a loud failure (and a dropped arm) rather than a model that
    # silently runs on half-random weights.
    model.load_weights(weight_path, strict=True)
    model.eval()
    return model


def run_mlx_arm(engine, audio_path, language):
    """The three MLX arms, all through pipeline._transcribe_parakeet.

    The point of routing every MLX arm through the SHIPPING function is that the
    chunking, the overlap token-merge, the silence-hallucination filter and the
    word assembly are then provably identical across arms: the only thing that
    differs between mlx-fp, mlx-fp-v3 and mlx-int8 is the model object. An arm
    that reimplemented the decode loop would be measuring its own reimplementation.
    """
    import pipeline

    cfg = {"language": language}
    if engine == "mlx-fp-v3":
        # Same code path, different checkpoint — this is the arm that separates
        # "v2 -> v3" from "fp -> int8" (PROTOCOL.md §1).
        pipeline.PARAKEET_REPO_EN = V3_REPO
    repo = pipeline.PARAKEET_REPO_EN

    if engine == "mlx-int8":
        model = load_int8_model()
        pipeline._PARAKEET = model
        pipeline._PARAKEET_REPO = INT8_REPO
        pipeline._get_parakeet = lambda repo, progress_cb=None: model
        repo = INT8_REPO
    else:
        pipeline._get_parakeet(repo)
    _mark("model-loaded")

    t0 = time.time()
    segments, lang = pipeline._transcribe_parakeet(
        audio_path, "eval", cfg, lambda *_a, **_k: None)
    elapsed = time.time() - t0
    _mark("transcribed")
    return {"engine": engine, "repo": repo, "language": lang,
            "elapsed_seconds": elapsed, "segments": segments}


def run_coreml_arm(engine, audio_path, language):
    """The CoreML arms, via the fluid-transcribe helper at the pinned revision.

    The helper emits raw SentencePiece pieces with times; they are rebuilt into
    parakeet_mlx AlignedTokens and pushed through the SAME
    tokens_to_sentences / sentences_to_result the MLX arms use, so the segment
    shape, the sentence splitting, and the word assembly are identical and only
    the acoustic model differs. "▁" becomes a leading space, which is exactly
    the convention pipeline._parakeet_words reads to find word starts.
    """
    import pipeline
    from parakeet_mlx.alignment import (AlignedToken, sentences_to_result,
                                        tokens_to_sentences)

    if not FLUID_TRANSCRIBE.exists():
        raise RuntimeError(
            f"fluid-transcribe not built: {FLUID_TRANSCRIBE}\n"
            f"  build it:  cd {FLUID_TRANSCRIBE.parents[2]} && swift build -c release")
    precision = engine.split("-", 1)[1]
    out_json = RESULTS / f".tmp-worker-{os.getpid()}.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(FLUID_TRANSCRIBE), str(audio_path), "--out", str(out_json),
           "--models-dir", str(MODELS), "--precision", precision]
    if language:
        cmd += ["--language", language]
    _mark("helper-start")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(tail[-1] if tail else f"fluid-transcribe exit {proc.returncode}")
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    out_json.unlink(missing_ok=True)
    _mark("transcribed")

    tokens = []
    for tok in payload["tokens"]:
        text = tok["t"].replace("▁", " ")
        if not text:
            continue
        start = float(tok["s"])
        tokens.append(AlignedToken(id=int(tok["id"]), text=text, start=start,
                                   duration=max(0.0, float(tok["e"]) - start),
                                   confidence=float(tok.get("c", 1.0))))
    segments = []
    if tokens:
        result = sentences_to_result(tokens_to_sentences(tokens))
        for sent in result.sentences:
            text = (sent.text or "").strip()
            if not text or not re.search(r"\w", text):
                continue
            segments.append({"start": float(sent.start), "end": float(sent.end),
                             "text": text, "words": pipeline._parakeet_words(sent)})
    return {"engine": engine, "repo": "FluidInference/parakeet-tdt-0.6b-v3-coreml",
            "language": language, "precision": payload.get("precision"),
            "engine_revision": payload.get("engine_revision"),
            "elapsed_seconds": float(payload["elapsed_seconds"]),
            "audio_seconds": float(payload["audio_seconds"]),
            "segments": segments}


def worker_main(args):
    audio_path = Path(args.audio)
    payload = (run_coreml_arm if args.engine.startswith("coreml") else run_mlx_arm)(
        args.engine, audio_path, args.language)
    if "audio_seconds" not in payload:
        import soundfile as sf
        with sf.SoundFile(str(audio_path)) as snd:
            payload["audio_seconds"] = snd.frames / float(snd.samplerate)
    Path(args.worker_out).write_text(json.dumps(payload), encoding="utf-8")
    _mark("written")
    return 0


# ---------------------------------------------------------------------------
# DRIVER
# ---------------------------------------------------------------------------

def spawn_arm(engine, audio_path, out_json, language, timeout, interval):
    """Run one arm in a fresh process, sampling its RSS the way
    tools/rss_probe.py does (imported, not copied). Returns (payload, wall,
    peak_rss_bytes, peak_footprint_bytes, error)."""
    cmd = [sys.executable, str(Path(__file__).resolve()), "--worker",
           "--engine", engine, "--audio", str(audio_path),
           "--worker-out", str(out_json)]
    if language:
        cmd += ["--language", language]
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    t0 = time.time()
    child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, env=env)
    probe = rss_probe.Probe(child.pid, interval)
    probe.start()
    probe.mark("spawn")
    tail = []
    try:
        for line in child.stdout:
            line = line.rstrip("\n")
            if line.startswith("MARK:"):
                probe.mark(line[5:].strip())
            elif line.strip():
                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        probe.stop()
        return None, time.time() - t0, 0, 0, f"timed out after {timeout:.0f}s"
    probe.mark("exit")
    probe.stop()
    wall = time.time() - t0
    peak_rss = max((s[1] for s in probe.samples), default=0)
    foots = [s[3] for s in probe.samples if s[3] is not None]
    peak_foot = max(foots) if foots else 0
    if child.returncode != 0:
        return None, wall, peak_rss, peak_foot, (tail[-1] if tail else
                                                 f"exit {child.returncode}")
    try:
        return (json.loads(Path(out_json).read_text(encoding="utf-8")), wall,
                peak_rss, peak_foot, "")
    except (OSError, ValueError) as exc:
        return None, wall, peak_rss, peak_foot, f"unreadable worker output: {exc}"


def arm_dir(engine, tag):
    return TRANSCRIPTS / (engine + (f"-{tag}" if tag else ""))


def load_baseline(key):
    path = arm_dir(BASELINE_ENGINE, None) / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def score_one(row, payload, baseline, out_dir, key):
    """Every metric for one arm on one fixture. Returns the committed row."""
    segments = payload["segments"]
    text = transcript_text(segments)
    audio_s = float(payload.get("audio_seconds") or 0.0)
    elapsed = float(payload.get("elapsed_seconds") or 0.0)
    out = {
        "track": row["track"],
        "corpus": row["corpus"],
        "audio_seconds": round(audio_s, 2),
        "elapsed_seconds": round(elapsed, 2),
        "rtf": round(audio_s / elapsed, 2) if elapsed else None,
        "segments": len(segments),
        "words": len(norm_words(text)),
        "chars": len(strip_punct(text)),
    }

    # Accuracy, where a reference exists.
    if row["corpus"] == "ami":
        ref, meta = ami_reference(row["meeting"], audio_s)
        if ref is None:
            out["reference"] = {"unavailable": meta}
        else:
            ref_text = " ".join(ref)
            wer, _ = rate(norm_words(ref_text), norm_words(text))
            cer, _ = rate(list(strip_punct(ref_text).replace(" ", "")),
                          list(strip_punct(text).replace(" ", "")))
            dwer, _ = rate(deep_norm_words(ref_text), deep_norm_words(text))
            out["reference"] = meta
            out["wer"] = round(wer["rate"] * 100, 2)
            out["wer_detail"] = wer
            out["cer"] = round(cer["rate"] * 100, 2)
            out["wer_normalized"] = round(dwer["rate"] * 100, 2)
            # PROTOCOL.md §2(a): the floor is on every table, not in a footnote.
            out["wer_empty_floor"] = 100.0

    # Divergence from the shipping arm, where there is a baseline to diverge from.
    if baseline is not None:
        base_text = transcript_text(baseline["segments"])
        base_words = flatten_words(baseline["segments"])
        cand_words = flatten_words(segments)
        div, ops = rate([w[0] for w in base_words], [w[0] for w in cand_words])
        ndiv, _ = rate(deep_norm_words(base_text), deep_norm_words(text))
        med, p95, n = word_time_agreement(base_words, cand_words)
        out["divergence_pct"] = round(div["rate"] * 100, 3)
        out["divergence_detail"] = div
        out["divergence_normalized_pct"] = round(ndiv["rate"] * 100, 3)
        out["word_time"] = {
            "median_ms": None if med is None else round(med, 1),
            "p95_ms": None if p95 is None else round(p95, 1),
            "agreeing_words": n,
        }
        spans = divergent_spans(base_words, cand_words, ops)
        (out_dir / f"{key}.spans.json").write_text(
            json.dumps({"key": key, "spans": spans}, indent=1), encoding="utf-8")
        out["spans_dumped"] = len(spans)
    return out


def print_table(engine, tag, rows):
    hdr = "%-14s %-4s %8s %7s %7s %7s %7s %8s %8s" % (
        "fixture", "trk", "audio_s", "rtf", "WER%", "div%", "ndiv%", "p95ms", "rssMB")
    print()
    print(hdr)
    print("-" * len(hdr))
    for key, r in rows.items():
        if "error" in r or "skipped" in r:
            print("%-14s %-4s   %s" % (label(key)[:14], (r.get("track") or "-")[:3],
                                       r.get("error") or "SKIPPED: " + r["skipped"]))
            continue
        wt = r.get("word_time") or {}
        print("%-14s %-4s %8.1f %6.1fx %7s %7s %7s %8s %8s" % (
            label(key)[:14], (r.get("track") or "-")[:3], r["audio_seconds"],
            r.get("rtf") or 0.0,
            "-" if r.get("wer") is None else "%.2f" % r["wer"],
            "-" if r.get("divergence_pct") is None else "%.3f" % r["divergence_pct"],
            "-" if r.get("divergence_normalized_pct") is None
            else "%.3f" % r["divergence_normalized_pct"],
            "-" if wt.get("p95_ms") is None else "%.0f" % wt["p95_ms"],
            "-" if not r.get("peak_rss_mb") else "%.0f" % r["peak_rss_mb"]))
    print()
    print("FLOORS (PROTOCOL.md §2, printed on every table):")
    print("  empty-transcript WER = 100.00%  — what 'no transcript at all' scores")
    print("  non-determinism floor = results/mlx-fp-rerun.json (mlx-fp vs itself);")
    print("    a divergence near it is the baseline disagreeing with the baseline.")
    print()
    print("PROTOCOL.md §4: partial runs (--only) are SMOKE evidence, never a gate "
          "result.\n  No clause of the ship rule may be marked satisfied from a "
          "subset table.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", choices=ENGINES, help="which arm to run")
    ap.add_argument("--only", action="append",
                    help="pseudonym(s) to run, e.g. --only 'Room W' (repeatable); "
                         "default: AMI + every real fixture with resolvable audio")
    ap.add_argument("--tag", help="suffix for the results file; --tag rerun on "
                                  "mlx-fp is how the non-determinism floor is taken")
    ap.add_argument("--language", default="en")
    ap.add_argument("--timeout", type=float, default=14400.0)
    ap.add_argument("--rss-interval", type=float, default=1.0)
    ap.add_argument("--list", action="store_true",
                    help="print what would run, and why anything is skipped")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--audio", help=argparse.SUPPRESS)
    ap.add_argument("--worker-out", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.worker:
        return worker_main(args)

    label_map, why_not = ev.load_label_map(ev.FIXTURES)
    targets = ami_targets() + real_targets(label_map)
    if not targets:
        raise SystemExit("nothing to run: no AMI audio and no real corpus.\n  " + why_not)
    if args.only:
        want = set(args.only)
        known = {t["key"] for t in targets}
        unknown = want - known
        if unknown:
            raise SystemExit(f"--only names no fixture: {sorted(unknown)}; "
                             f"known: {sorted(known)}")
        targets = [t for t in targets if t["key"] in want]

    if args.list:
        print("%-14s %-8s %-4s %s" % ("fixture", "corpus", "trk", "status"))
        for t in targets:
            print("%-14s %-8s %-4s %s" % (
                label(t["key"])[:14], t["corpus"], (t["track"] or "-")[:3],
                "ok" if t.get("audio") else "SKIP: " + (t.get("skip") or "no audio")))
        if not label_map:
            print("\nreal corpus unavailable: " + why_not)
        return 0

    if not args.engine:
        raise SystemExit("--engine is required (or --list)")

    out_dir = arm_dir(args.engine, args.tag)
    out_dir.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    print(f"arm      : {args.engine}" + (f" (tag {args.tag})" if args.tag else ""))
    print(f"baseline : {BASELINE_ENGINE} transcripts in {arm_dir(BASELINE_ENGINE, None)}")
    print("metric   : WER/CER vs reference where one exists; divergence vs the "
          "shipping arm where\n           none does. PROTOCOL.md is the contract.")

    rows = {}
    for t in targets:
        key = t["key"]
        # Re-resolved here, not trusted from the planning sweep above — the
        # recordings directory changes under a long run (see refresh_audio).
        audio, why = refresh_audio(t)
        if not audio:
            rows[key] = {"skipped": why or "no audio", "track": t["track"]}
            print("%-14s SKIPPED: %s" % (label(key)[:14], rows[key]["skipped"]))
            continue
        t["audio"] = audio
        tmp = RESULTS / f".tmp-{args.engine}-{os.getpid()}.json"
        print("%-14s running %s…" % (label(key)[:14], args.engine), flush=True)
        payload, wall, peak_rss, peak_foot, err = spawn_arm(
            args.engine, t["audio"], tmp, args.language, args.timeout,
            args.rss_interval)
        if payload is None:
            rows[key] = {"error": err, "track": t["track"],
                         "wall_seconds": round(wall, 1)}
            print("  FAILED after %.0fs: %s" % (wall, err))
            tmp.unlink(missing_ok=True)
            continue
        # The transcript itself is private speech: it lands in the gitignored
        # transcripts dir, keyed by pseudonym, and never in results/*.json.
        shutil.move(str(tmp), str(out_dir / f"{key}.json"))
        baseline = None if (args.engine == BASELINE_ENGINE and not args.tag) \
            else load_baseline(key)
        row = score_one(t, payload, baseline, out_dir, key)
        # Which container this arm actually read. Recorded because it CHANGED
        # mid-experiment (WAV -> verified-lossless FLAC) and a reader comparing
        # two arms deserves to see that from the data, not from a footnote.
        row["audio_container"] = t["audio"].suffix
        row["wall_seconds"] = round(wall, 1)
        row["peak_rss_mb"] = round(peak_rss / 1048576, 1) if peak_rss else None
        row["peak_footprint_mb"] = round(peak_foot / 1048576, 1) if peak_foot else None
        row["repo"] = payload.get("repo")
        rows[key] = row
        print("  ok: rtf %.1fx, %d segments, peak rss %s MB"
              % (row.get("rtf") or 0.0, row["segments"], row["peak_rss_mb"]))

    print_table(args.engine, args.tag, rows)

    name = args.engine + (f"-{args.tag}" if args.tag else "")
    out = RESULTS / f"{name}.json"
    out.write_text(json.dumps({
        "arm": args.engine,
        "tag": args.tag,
        "baseline_arm": BASELINE_ENGINE,
        "protocol": "native/asr-ab/PROTOCOL.md",
        "complete_run": not args.only,
        "evidence_class": "GATE" if not args.only else "SMOKE",
        "normalization": {"raw": "lowercase, strip punctuation",
                          "normalized": "raw + digits spelled out, apostrophes "
                                        "and hyphens dissolved"},
        "degenerate_floors": {"empty_transcript_wer_pct": 100.0,
                              "non_determinism": "results/mlx-fp-rerun.json"},
        "rows": rows,
    }, indent=1), encoding="utf-8")
    print(f"\nresults written to {out} (pseudonym-keyed)")
    print(f"transcripts + divergent spans in {out_dir} (GITIGNORED — verbatim speech)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
