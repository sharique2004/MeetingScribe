#!/usr/bin/env python3
"""Hand-label word-level speaker truth for one meeting. Founder tool, dev only.

WHAT THIS IS FOR. There is no turn-level attribution truth anywhere in this
repository, so nothing measures the product's actual deliverable — a
speaker-labelled transcript. tools/attribution.py can score one; this is how
one gets made. Output: test/fixtures/<slug>/truth_words.json, schema in that
module's docstring.

DISPUTE-FIRST, because labelling every word of a 27-minute meeting by hand is
6+ hours and labelling the words the machines argue about is 20 minutes.

  1. Read the SAVED transcript words out of analysis.json. No ASR runs, no
     model loads, no engine is invoked.
  2. Read whichever CACHED hypotheses the meeting already carries: `shipped`
     (meeting.json turns — what the product published), `classic`
     (diarization.cluster over the frozen ECAPA windows in analysis.npz), and
     `neural` (<track>_neural_turns in the same npz). A missing hypothesis is
     not an error; with none, every word is disputed.
  3. WORKLIST = words where the hypotheses disagree (after aligning their
     label namespaces), words in runs shorter than SHORT_RUN_S, and words
     within BOUNDARY_S of any run edge. The rest is auto-accepted from the
     lead hypothesis.
  4. AUDIT SECTION: a seeded 10% sample of the auto-accepted words, appended
     to the queue. Every one the labeller changes is a word the fast path got
     wrong and never asked about, so that rate is the error bar on the whole
     file (attribution.audit_band) and is what makes step 3 defensible.

THE BIAS THIS TOOL HAS, stated rather than hidden. Correcting a hypothesis is
not labelling from nothing: the labeller sees the machine's answer first and
agrees with it more than they would have unprompted. Recorded in the file as
source="corrected-neural"; --from-scratch is the control (it shows no
hypothesis at all), and Room T is short enough to label both ways.

  python tools/label_turns.py "Demo meeting"            # dispute-first
  python tools/label_turns.py 20260610-000001 --track system
  python tools/label_turns.py "Room T" --from-scratch   # bias control
  python tools/label_turns.py "Demo" --export-rttm out.rttm   # exhaustive only

KEYS (the whole interface):
  1..9   assign speaker A..J to the current word
  space  replay the current word's span ±REPLAY_PAD_S
  s      toggle scope: this word only  <->  this word to the end of its run
  o      mark the current word as overlapping speech
  x      exclude the current word from scoring
  n / p  next / previous item     (also -> and <-)
  u      undo the last decision
  w      write now (the file is written after every decision anyway)

Progress lives inside the truth file itself, so closing the tab and coming
back resumes where you were. The write is atomic (attribution.save_truth_words).

ROBUSTNESS OVER POLISH: this is a single-user localhost tool with no auth, no
CSRF, and no concurrency story. It binds 127.0.0.1 and it is not a server.
"""

import argparse
import json
import os
import random
import re
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(REPO), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import attribution  # noqa: E402

RECORDINGS = Path.home() / ".meetingscribe" / "recordings"
FIXTURES = REPO / "test" / "fixtures"
LETTERS = "ABCDEFGHJ"          # 9 keys; I reads as 1
SHORT_RUN_S = 1.0              # runs below this are disputed by construction
BOUNDARY_S = 0.3               # words this close to a run edge are disputed
AUDIT_RATE = 0.10
REPLAY_PAD_S = 1.0
DEFAULT_SEED = 20260809


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def resolve_meeting(query, root=RECORDINGS):
    """Meeting directory whose folder name or meeting.json id matches `query`."""
    root = Path(root)
    if not root.is_dir():
        raise SystemExit("no recordings directory at %s" % root)
    q = query.lower()
    hits = []
    for d in sorted(root.iterdir()):
        if not (d / "meeting.json").exists():
            continue
        mid = ""
        try:
            mid = str(json.loads((d / "meeting.json").read_text(encoding="utf-8"))
                      .get("id") or "")
        except (OSError, ValueError):
            pass
        if q in d.name.lower() or q in mid.lower():
            hits.append(d)
    if not hits:
        raise SystemExit("no meeting under %s matches %r" % (root, query))
    if len(hits) > 1:
        raise SystemExit("%r matches %d meetings:\n  %s"
                         % (query, len(hits), "\n  ".join(h.name for h in hits)))
    return hits[0]


def load_words(meeting_dir, track):
    """Flat word list off the saved transcript. [{i, s, e, w}]"""
    path = meeting_dir / "analysis.json"
    if not path.exists():
        raise SystemExit("%s has no analysis.json — nothing to label" % meeting_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    segs = (data.get("transcripts") or {}).get(track) or []
    out = []
    for seg in segs:
        for w in seg.get("words") or []:
            if w.get("s") is None or w.get("e") is None:
                continue
            out.append({"i": len(out), "s": float(w["s"]), "e": float(w["e"]),
                        "w": str(w.get("w") or "")})
    if not out:
        raise SystemExit("no word timings on the %s track of %s"
                         % (track, meeting_dir.name))
    out.sort(key=lambda x: (x["s"], x["e"]))
    for i, w in enumerate(out):
        w["i"] = i
    return out


def load_hypotheses(meeting_dir, meta, track):
    """{name: [(s, e, label)]} from what is CACHED. Engines are never invoked."""
    import numpy as np

    hyps = {}
    turns = [t for t in (meta.get("turns") or []) if t.get("track") == track]
    if turns:
        hyps["shipped"] = [(float(t["start"]), float(t["end"]), str(t["speaker"]))
                           for t in turns]
    npz_path = meeting_dir / "analysis.npz"
    if not npz_path.exists():
        return hyps
    try:
        with np.load(npz_path, allow_pickle=False) as npz:
            wk, ek, nk = ("%s_windows" % track, "%s_embeddings" % track,
                          "%s_neural_turns" % track)
            windows = np.asarray(npz[wk]) if wk in npz.files else None
            embeds = np.asarray(npz[ek], dtype=np.float64) if ek in npz.files else None
            neural = np.asarray(npz[nk]) if nk in npz.files else None
    except (OSError, ValueError) as exc:
        print("note: %s unreadable (%s); continuing without cached hypotheses"
              % (npz_path.name, exc))
        return hyps
    if neural is not None and len(neural):
        hyps["neural"] = [(float(s), float(e), "n%d" % int(k)) for s, e, k in neural]
    if windows is not None and embeds is not None and len(windows) > 1:
        try:
            import diarization
            labels = diarization.cluster(
                embeds, n_speakers=None, threshold=0.6,
                durations=[float(b) - float(a) for a, b in windows])
            hyps["classic"] = attribution.spans_from_windows(windows, labels)
        except Exception as exc:      # sklearn missing, cluster refusing, …
            print("note: classic replay unavailable (%s: %s)"
                  % (type(exc).__name__, exc))
    return hyps


# ---------------------------------------------------------------------------
# Worklist
# ---------------------------------------------------------------------------

def _label_words(words, spans):
    at = attribution.label_at(spans)
    return [at((w["s"] + w["e"]) / 2.0) for w in words]


def _align(lead, other, words):
    """Map `other`'s labels onto `lead`'s namespace by shared word time."""
    from scipy.optimize import linear_sum_assignment
    import numpy as np

    a = sorted({x for x in lead if x is not None})
    b = sorted({x for x in other if x is not None})
    if not a or not b:
        return {}
    ai = {n: i for i, n in enumerate(a)}
    bi = {n: i for i, n in enumerate(b)}
    m = np.zeros((len(a), len(b)))
    for w, x, y in zip(words, lead, other):
        if x is not None and y is not None:
            m[ai[x], bi[y]] += (w["e"] - w["s"])
    ri, cj = linear_sum_assignment(-m)
    return {b[j]: a[i] for i, j in zip(ri, cj)}


def _runs(labels):
    """[(start_index, end_index_exclusive, label)] over consecutive equal labels."""
    out = []
    for i, lab in enumerate(labels):
        if out and out[-1][2] == lab:
            out[-1][1] = i + 1
        else:
            out.append([i, i + 1, lab])
    return [tuple(r) for r in out]


def build_worklist(words, hyps, from_scratch=False, seed=DEFAULT_SEED,
                   audit_rate=AUDIT_RATE):
    """(items, lead_labels, lead_name, stats).

    items is the ordered queue the UI walks: every disputed word first, then
    the seeded audit sample of the auto-accepted ones.
    """
    from bisect import bisect_left, bisect_right

    found = [n for n in ("neural", "classic", "shipped") if n in hyps]
    # --from-scratch deliberately drops the lead: pre-filling the ribbon with a
    # machine answer is exactly the bias this mode exists to control for. The
    # hypotheses are still recorded in stats so the file says what was on disk.
    order = [] if from_scratch else found
    lead_name = order[0] if order else None
    per_hyp = {n: _label_words(words, hyps[n]) for n in order}
    lead = per_hyp.get(lead_name) or [None] * len(words)
    centres = [(w["s"] + w["e"]) / 2.0 for w in words]

    disputed = set()
    reasons = {}

    def flag(i, why):
        disputed.add(i)
        reasons.setdefault(i, []).append(why)

    if from_scratch or not order:
        for i in range(len(words)):
            flag(i, "from-scratch")
    else:
        # (a) the hypotheses disagree, after aligning their label namespaces
        for name in order[1:]:
            mapped = _align(lead, per_hyp[name], words)
            for i, (x, y) in enumerate(zip(lead, per_hyp[name])):
                if mapped.get(y, y) != x:
                    flag(i, "disagree:%s" % name)
        # (b) and (c): short runs, and words near a run edge, in ANY hypothesis
        for name in order:
            for a, b, _lab in _runs(per_hyp[name]):
                span = words[b - 1]["e"] - words[a]["s"]
                if span < SHORT_RUN_S:
                    for i in range(a, b):
                        flag(i, "short-run:%s" % name)
                for edge in (words[a]["s"], words[b - 1]["e"]):
                    lo = bisect_left(centres, edge - BOUNDARY_S)
                    hi = bisect_right(centres, edge + BOUNDARY_S)
                    for i in range(lo, hi):
                        flag(i, "boundary:%s" % name)

    auto = [i for i in range(len(words)) if i not in disputed]
    rng = random.Random(seed)
    n_audit = int(round(len(auto) * audit_rate))
    audit = sorted(rng.sample(auto, n_audit)) if n_audit else []

    items = [{"i": i, "kind": "disputed", "why": sorted(set(reasons.get(i, [])))}
             for i in sorted(disputed)]
    items += [{"i": i, "kind": "audit", "why": ["seeded %d%% sample of auto-accepted"
                                                % round(audit_rate * 100)]}
              for i in audit]
    stats = {"words": len(words), "disputed": len(disputed), "auto": len(auto),
             "audit": len(audit), "hypotheses": found, "lead": lead_name}
    return items, lead, lead_name, stats


def letters_for(labels):
    """Stable hypothesis-label -> truth-letter map, by first appearance.

    Past the nine keyboard letters the names keep going as Z10, Z11 … rather
    than wrapping: wrapping would silently MERGE two hypothesis speakers into
    one truth label, which is a labelling error the file would carry forever
    with nothing to show it happened. Those speakers have no key and must be
    reached by re-labelling from a lower letter, which is the correct amount of
    friction for a corpus whose largest fixture holds seven voices.
    """
    out = {}
    for lab in labels:
        if lab is not None and lab not in out:
            i = len(out)
            out[lab] = LETTERS[i] if i < len(LETTERS) else "Z%d" % i
    return out


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

class Session:
    def __init__(self, meeting_dir, meta, track, out_path, words, items, lead,
                 lead_name, stats, from_scratch):
        self.meeting_dir = meeting_dir
        self.meta = meta
        self.track = track
        self.out_path = Path(out_path)
        self.words = words
        self.items = items
        self.stats = stats
        self.from_scratch = from_scratch
        self.lead_name = lead_name
        self.letters = letters_for(lead)
        self.auto = [self.letters.get(l) or LETTERS[0] for l in lead]
        self.labels = list(self.auto)
        self.excluded = []
        self.overlap = []
        self.decided = {}        # word index -> True once a human touched it
        self.cursor = 0
        self.scope_run = False
        self.history = []
        self.lock = threading.Lock()
        self._resume()

    # -- persistence --------------------------------------------------------
    def _resume(self):
        if not self.out_path.exists():
            return
        try:
            data = json.loads(self.out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        prog = data.get("_progress") or {}
        if prog.get("n_words") != len(self.words) or data.get("track") != self.track:
            print("note: existing %s does not match this track/word count; "
                  "starting fresh" % self.out_path.name)
            return
        by_index = {int(k): v for k, v in (prog.get("labels") or {}).items()}
        for i, lab in by_index.items():
            if 0 <= i < len(self.labels):
                self.labels[i] = lab
                self.decided[i] = True
        self.excluded = [list(map(float, x)) for x in data.get("excluded") or []]
        self.overlap = [list(map(float, x)) for x in data.get("overlap") or []]
        self.cursor = int(prog.get("cursor") or 0)
        print("resumed %s: %d decisions, cursor %d"
              % (self.out_path.name, len(self.decided), self.cursor))

    def document(self):
        audit_items = [it["i"] for it in self.items if it["kind"] == "audit"]
        disagreed = sum(1 for i in audit_items
                        if self.decided.get(i) and self.labels[i] != self.auto[i])
        sampled = sum(1 for i in audit_items if self.decided.get(i))
        words = [{"s": round(w["s"], 3), "e": round(w["e"], 3),
                  "spk": self.labels[w["i"]]}
                 for w in self.words
                 if not _covered(w, self.excluded)]
        speakers = {}
        for lab in sorted({x["spk"] for x in words}):
            speakers[lab] = "speaker %s (%s)" % (lab, self.meta.get("title", "")[:40])
        return {
            "version": attribution.TRUTH_SCHEMA_VERSION,
            "track": self.track,
            "source": "from-scratch" if self.from_scratch else "corrected-neural",
            "speakers": speakers,
            "words": words,
            "excluded": self.excluded,
            "overlap": self.overlap,
            "audit": {"sampled": sampled, "disagreed": disagreed},
            "labelled_at": datetime.now().isoformat(timespec="seconds"),
            "_progress": {
                "n_words": len(self.words),
                "cursor": self.cursor,
                "lead": self.lead_name,
                "labels": {str(i): self.labels[i] for i in sorted(self.decided)},
                "worklist": len(self.items),
                "stats": self.stats,
            },
        }

    def save(self):
        attribution.save_truth_words(self.out_path, self.document())

    # -- edits --------------------------------------------------------------
    def _targets(self, idx):
        if not self.scope_run:
            return [idx]
        lab = self.labels[idx]
        j = idx
        while j + 1 < len(self.labels) and self.labels[j + 1] == lab:
            j += 1
        return list(range(idx, j + 1))

    def apply(self, action, value=None):
        with self.lock:
            if not self.items:
                return
            self.cursor = max(0, min(self.cursor, len(self.items) - 1))
            idx = self.items[self.cursor]["i"]
            snap = (list(self.labels), list(self.excluded), list(self.overlap),
                    dict(self.decided), self.cursor, self.scope_run)
            if action == "label":
                for t in self._targets(idx):
                    self.labels[t] = value
                    self.decided[t] = True
                self.scope_run = False
                self.cursor += 1
            elif action == "overlap":
                self.overlap.append([self.words[idx]["s"], self.words[idx]["e"]])
                self.decided[idx] = True
                self.cursor += 1
            elif action == "exclude":
                self.excluded.append([self.words[idx]["s"], self.words[idx]["e"]])
                self.decided[idx] = True
                self.cursor += 1
            elif action == "scope":
                self.scope_run = not self.scope_run
                return
            elif action == "next":
                self.cursor += 1
            elif action == "prev":
                self.cursor -= 1
            elif action == "undo":
                if self.history:
                    (self.labels, self.excluded, self.overlap, self.decided,
                     self.cursor, self.scope_run) = self.history.pop()
                    self.save()
                return
            self.history.append(snap)
            self.cursor = max(0, min(self.cursor, len(self.items) - 1))
            self.save()

    # -- view ---------------------------------------------------------------
    def view(self, context=14):
        if not self.items:
            return {"done": True, "stats": self.stats}
        cur = self.items[max(0, min(self.cursor, len(self.items) - 1))]
        idx = cur["i"]
        lo, hi = max(0, idx - context), min(len(self.words), idx + context + 1)
        offset = float(((self.meta.get("tracks") or {}).get(self.track) or {})
                       .get("start_offset") or 0.0)
        w0 = self.words[idx]
        return {
            "done": False, "title": self.meta.get("title", ""),
            "track": self.track, "lead": self.lead_name, "cursor": self.cursor,
            "total": len(self.items), "kind": cur["kind"], "why": cur["why"],
            "scope_run": self.scope_run, "letters": LETTERS,
            "stats": self.stats, "decisions": len(self.decided),
            "out": str(self.out_path),
            "word": {"i": idx, "w": w0["w"], "s": w0["s"], "e": w0["e"]},
            # times are on the MEETING timeline; the WAV is not, so the track's
            # start_offset comes back off before the <audio> element seeks
            "play": {"s": max(0.0, w0["s"] - offset - REPLAY_PAD_S),
                     "e": w0["e"] - offset + REPLAY_PAD_S},
            "ribbon": [{"i": w["i"], "w": w["w"], "s": w["s"], "e": w["e"],
                        "spk": self.labels[w["i"]], "auto": self.auto[w["i"]],
                        "done": bool(self.decided.get(w["i"])),
                        "cur": w["i"] == idx}
                       for w in self.words[lo:hi]],
        }


def _covered(w, spans):
    return any(not (w["e"] <= a or w["s"] >= b) for a, b in spans)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

PAGE = """<!doctype html><meta charset=utf-8><title>label turns</title>
<style>
body{font:14px ui-monospace,Menlo,monospace;margin:18px;background:#111;color:#ddd}
#rib{line-height:2.1;margin:14px 0;max-width:900px}
.w{padding:2px 4px;border-radius:3px;background:#222}
.w.cur{outline:2px solid #ffd166}
.w.done{background:#1d3b2a}
.sp{font-size:10px;color:#88a;vertical-align:super}
#hd{color:#8ab}#why{color:#e58}#big{font-size:22px;margin:8px 0}
kbd{background:#333;padding:1px 5px;border-radius:3px}
#keys{color:#889;margin-top:16px;line-height:1.8}
</style>
<div id=hd></div><div id=big></div><div id=why></div>
<div id=rib></div>
<audio id=au preload=auto></audio>
<div id=keys><kbd>1</kbd>..<kbd>9</kbd> speaker &nbsp; <kbd>space</kbd> replay ±1s
&nbsp; <kbd>s</kbd> scope word/run &nbsp; <kbd>o</kbd> overlap &nbsp; <kbd>x</kbd>
exclude &nbsp; <kbd>n</kbd>/<kbd>p</kbd> next/prev &nbsp; <kbd>u</kbd> undo
&nbsp; <kbd>w</kbd> write</div>
<script>
let S=null, stopAt=0;
const au=document.getElementById('au');
au.src='/audio';
au.addEventListener('timeupdate',()=>{if(stopAt&&au.currentTime>=stopAt){au.pause();stopAt=0;}});
async function load(){S=await (await fetch('/api/state')).json();draw();}
function draw(){
 if(S.done){document.getElementById('big').textContent='worklist complete — '+
   (S.stats.disputed||0)+' disputed, '+(S.stats.audit||0)+' audit';return;}
 document.getElementById('hd').textContent=S.title+'  ['+S.track+' track]  lead='+
   S.lead+'   item '+(S.cursor+1)+'/'+S.total+'  decisions '+S.decisions+
   '  scope='+(S.scope_run?'RUN':'word')+'  -> '+S.out;
 document.getElementById('big').textContent='['+S.kind+'] "'+S.word.w.trim()+'"  '+
   S.word.s.toFixed(2)+'–'+S.word.e.toFixed(2)+'s';
 document.getElementById('why').textContent=S.why.join('  ·  ');
 const r=document.getElementById('rib');r.innerHTML='';
 for(const w of S.ribbon){const e=document.createElement('span');
  e.className='w'+(w.cur?' cur':'')+(w.done?' done':'');
  e.innerHTML=(w.w||'').replace(/</g,'&lt;')+'<span class=sp>'+(w.spk||'?')+'</span>';
  r.appendChild(e);r.appendChild(document.createTextNode(' '));}
}
async function act(a,v){await fetch('/api/action',{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({action:a,value:v})});await load();}
function play(){if(!S||S.done)return;au.currentTime=S.play.s;stopAt=S.play.e;au.play();}
addEventListener('keydown',ev=>{
 const k=ev.key;
 if(k===' '){ev.preventDefault();play();return;}
 if(k>='1'&&k<='9'){ev.preventDefault();act('label',S.letters[+k-1]);return;}
 if(k==='s'){act('scope');return;}
 if(k==='o'){act('overlap');return;}
 if(k==='x'){act('exclude');return;}
 if(k==='n'||k==='ArrowRight'){act('next');return;}
 if(k==='p'||k==='ArrowLeft'){act('prev');return;}
 if(k==='u'){act('undo');return;}
 if(k==='w'){fetch('/api/save',{method:'POST'});return;}
});
load();
</script>"""


def make_handler(session):
    wav = session.meeting_dir / (
        ((session.meta.get("tracks") or {}).get(session.track) or {}).get("file")
        or ("%s.wav" % session.track))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if path == "/api/state":
                return self._send(200, json.dumps(session.view()))
            if path == "/audio":
                return self._audio()
            return self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            path = urlparse(self.path).path
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            if path == "/api/save":
                session.save()
                return self._send(200, json.dumps({"ok": True}))
            if path == "/api/action":
                try:
                    payload = json.loads(raw or b"{}")
                except ValueError:
                    return self._send(400, json.dumps({"error": "bad json"}))
                session.apply(str(payload.get("action") or ""),
                              payload.get("value"))
                return self._send(200, json.dumps(session.view()))
            if path == "/api/quit":
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return self._send(200, json.dumps({"ok": True}))
            return self._send(404, json.dumps({"error": "not found"}))

        def _audio(self):
            """The WAV, with Range support — <audio> cannot seek without it."""
            if not wav.exists():
                return self._send(404, json.dumps({"error": "no wav at %s" % wav}))
            size = wav.stat().st_size
            rng = self.headers.get("Range") or ""
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            start, end = 0, size - 1
            partial = False
            if m and (m.group(1) or m.group(2)):
                partial = True
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:                      # suffix range: last N bytes
                    start = max(0, size - int(m.group(2)))
            if start >= size:
                return self._send(416, b"", "application/json",
                                  {"Content-Range": "bytes */%d" % size})
            length = end - start + 1
            self.send_response(206 if partial else 200)
            # audio/flac (not mimetypes' audio/x-flac) — the type AVFoundation
            # and browsers accept for archived tracks; see audio_archive.py.
            self.send_header("Content-Type",
                             "audio/flac" if wav.suffix == ".flac" else "audio/wav")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if partial:
                self.send_header("Content-Range",
                                 "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            with open(wav, "rb") as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

    return Handler


# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("meeting", help="meeting id or a substring of its folder name")
    ap.add_argument("--track", choices=("mic", "system"),
                    help="default: system for online meetings, mic for in-person")
    ap.add_argument("--recordings", default=str(RECORDINGS))
    ap.add_argument("--out", help="truth file path (default "
                                  "test/fixtures/<slug>/truth_words.json)")
    ap.add_argument("--from-scratch", action="store_true",
                    help="label EVERY word, no hypothesis shown as accepted. The "
                         "bias control for the dispute-first files.")
    ap.add_argument("--export-rttm", metavar="PATH",
                    help="write the truth as an RTTM (exhaustive truth only)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--audit-rate", type=float, default=AUDIT_RATE)
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--headless", action="store_true",
                    help="serve without opening a browser (used by the tests)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build the worklist, print it, write nothing, serve nothing")
    args = ap.parse_args(argv)

    meeting_dir = resolve_meeting(args.meeting, args.recordings)
    meta = json.loads((meeting_dir / "meeting.json").read_text(encoding="utf-8"))
    track = args.track or ("system" if meta.get("mode") == "online" else "mic")
    words = load_words(meeting_dir, track)
    hyps = load_hypotheses(meeting_dir, meta, track)
    items, lead, lead_name, stats = build_worklist(
        words, hyps, from_scratch=args.from_scratch, seed=args.seed,
        audit_rate=args.audit_rate)

    slug = re.sub(r"[^a-z0-9]+", "-", meeting_dir.name.lower()).strip("-")
    out = Path(args.out) if args.out else FIXTURES / slug / attribution.TRUTH_FILE

    print("%s  [%s track]" % (meta.get("title", meeting_dir.name), track))
    print("  words %d | hypotheses %s | lead %s"
          % (stats["words"], ",".join(stats["hypotheses"]) or "none", lead_name))
    print("  worklist %d = %d disputed + %d audit (of %d auto-accepted, rate %.0f%%)"
          % (len(items), stats["disputed"], stats["audit"], stats["auto"],
             args.audit_rate * 100))
    print("  truth file: %s" % out)

    if args.export_rttm:
        truth = attribution.load_truth_words(out)
        if truth.get("source") != "from-scratch":
            raise SystemExit(
                "refusing to export RTTM from source=%r: an RTTM asserts that the "
                "spans it lists are ALL the speech there is, and dispute-first truth "
                "does not assert that. Re-label with --from-scratch."
                % truth.get("source"))
        Path(args.export_rttm).write_text(
            "\n".join(attribution.truth_to_rttm(truth, meta.get("id", "fixture")))
            + "\n", encoding="utf-8")
        print("  wrote %s" % args.export_rttm)
        return 0

    if args.dry_run:
        for it in items[:20]:
            w = words[it["i"]]
            print("    %-8s %7.2f  %-18r %s"
                  % (it["kind"], w["s"], w["w"][:18], ",".join(it["why"])))
        if len(items) > 20:
            print("    … %d more" % (len(items) - 20))
        return 0

    session = Session(meeting_dir, meta, track, out, words, items, lead,
                      lead_name, stats, args.from_scratch)
    session.save()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(session))
    url = "http://127.0.0.1:%d/" % args.port
    print("  serving %s   (ctrl-c to stop; every decision is written immediately)"
          % url)
    if not args.headless:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        session.save()
        server.server_close()
        print("\n  written: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
