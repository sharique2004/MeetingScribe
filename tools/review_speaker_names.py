#!/usr/bin/env python3
"""Run speaker_names over every recorded meeting and write a review artifact.

READ-ONLY. It loads each meeting.json, runs the inference against a COPY of
its speaker map, and writes dist/speaker_names_review.md. No meeting.json is
touched, no voice profile is read or written, nothing is enrolled.

The artifact is for reading, not for scoring: it prints the anchored quote and
its timestamp beside every proposal, plus the turns either side of it, so a
human can decide whether each name is right. Aggregate counts at the top are
mechanical; the error count is not, and this script does not pretend to know
it.

Run:  MEETINGSCRIBE_DATA=~/.meetingscribe \\
        ~/.meetingscribe/venv/bin/python tools/review_speaker_names.py
"""
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import speaker_names as sn  # noqa: E402
import summarize  # noqa: E402
from config import BASE_DIR, RECORDINGS_DIR, load_config  # noqa: E402

OUT = BASE_DIR / "dist" / "speaker_names_review.md"
CONTEXT_TURNS = 1
LINE_CHARS = 300   # long turns are unreadable in full and prove nothing extra


class Capture(logging.Handler):
    """Whatever the module says about the claims it threw away."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def meeting_dirs():
    """Every meeting folder on this Mac, deduped by id (the folder name)."""
    seen, out = set(), []
    for root in (RECORDINGS_DIR, BASE_DIR / "recordings",
                 Path.home() / ".meetingscribe" / "recordings"):
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            if d.name in seen or not (d / "meeting.json").exists():
                continue
            seen.add(d.name)
            out.append(d)
    return out


def context(entries, evidence):
    """The anchored turn plus a turn either side, as rendered lines.

    Two turns can round into the same second, so the cluster the name landed
    on picks between them; taking the first match marked the wrong line as
    the evidence on exactly the meetings worth checking most closely.
    """
    t = int(evidence["t"])
    said_by = evidence.get("said_by")
    same_second = [i for i, e in enumerate(entries) if int(e["start"]) == t]
    if not same_second:
        return []
    hit = next((i for i in same_second if entries[i].get("key") == said_by),
               same_second[0])
    lo = max(0, hit - CONTEXT_TURNS)
    return [(i == hit, entries[i]["line"][:LINE_CHARS])
            for i in range(lo, min(len(entries), hit + CONTEXT_TURNS + 1))]


def main():
    cfg = dict(load_config())
    engine = summarize._pick_engine()
    handler = Capture()
    logging.getLogger("meetingscribe.names").addHandler(handler)
    logging.getLogger("meetingscribe.names").setLevel(logging.INFO)

    rows, totals = [], {"meetings": 0, "clusters": 0, "named": 0, "left": 0,
                        "human": 0, "roster": 0, "held": 0}
    started = time.time()
    for d in meeting_dirs():
        try:
            meta = json.loads((d / "meeting.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"skip {d.name}: {exc}")
            continue
        turns = meta.get("turns") or []
        speakers = dict(meta.get("speakers") or {})
        if not turns or not speakers:
            continue
        totals["meetings"] += 1
        before = dict(speakers)
        handler.lines = []
        t0 = time.time()
        applied = sn.apply_inferred_names(meta, turns, speakers, cfg)
        elapsed = time.time() - t0
        entries = sn._entries(turns, before)
        roster = sn._roster_names(meta)
        totals["roster"] += 1 if roster else 0
        held = [line for line in handler.lines
                if "held back" in line or "contradictory" in line
                or "already on this meeting" in line]
        totals["held"] += len(held)

        clusters = []
        for key, label in before.items():
            if key == "you":
                continue
            totals["clusters"] += 1
            if not label.startswith("Speaker") and not label.startswith("Remote"):
                totals["human"] += 1
                clusters.append({"key": key, "label": label, "state": "already named"})
                continue
            evidence = (meta.get("speaker_names") or {}).get(key)
            if evidence:
                totals["named"] += 1
                clusters.append({"key": key, "label": label, "state": "named",
                                 "evidence": evidence,
                                 "context": context(entries, evidence)})
            else:
                totals["left"] += 1
                clusters.append({"key": key, "label": label, "state": "left alone"})
        rows.append({"dir": d, "title": meta.get("title") or d.name,
                     "turns": len(turns), "roster": roster, "seconds": elapsed,
                     "clusters": clusters, "held": held, "applied": applied})
        print(f"{elapsed:6.1f}s  {len(applied)} named  {d.name[:60]}")

    lines = [
        "# Speaker name inference: review over the real corpus",
        "",
        f"- Engine: **{engine}**  (config `summary_engine`)",
        f"- Meetings with a transcript: **{totals['meetings']}**",
        f"- Clusters seen (excluding `you`): **{totals['clusters']}**",
        f"  - already carried a human name: **{totals['human']}**",
        f"  - got a name from this pass: **{totals['named']}**",
        f"  - left as \"Speaker N\": **{totals['left']}**",
        f"- Meetings with a calendar roster: **{totals['roster']}**",
        f"- Claims gated away (low confidence, contradiction, collision): "
        f"**{totals['held']}**",
        f"- Wall clock: **{time.time() - started:.0f}s**",
        "",
        "Every proposal below is printed with the quote it was anchored to and "
        "the turns either side, because that is what has to be read to say "
        "whether it is right. `>` marks the anchored turn.",
        "",
    ]
    for row in rows:
        lines.append(f"## {row['title']}")
        lines.append("")
        lines.append(f"`{row['dir'].name}` · {row['turns']} turns, "
                     f"{row['seconds']:.1f}s, roster: "
                     f"{', '.join(row['roster']) if row['roster'] else 'none'}")
        lines.append("")
        for cluster in row["clusters"]:
            if cluster["state"] == "named":
                e = cluster["evidence"]
                lines.append(f"- **{cluster['key']}** ({cluster['label']}) -> "
                             f"**{e['name']}**  ·  {e['kind']}  ·  confidence "
                             f"{e['confidence']}  ·  {e['portions']} portion(s)"
                             f"  ·  said by {e.get('said_by')}"
                             f"  ·  [{summarize._fmt_time(e['t'])}]")
                lines.append(f"  - quote: `{e['quote']}`")
                for is_hit, line in cluster["context"]:
                    lines.append(f"    {'>' if is_hit else ' '} {line}")
            else:
                lines.append(f"- **{cluster['key']}** ({cluster['label']}) · "
                             f"{cluster['state']}")
        if row["held"]:
            lines.append("")
            lines.append("  Gated away:")
            for line in row["held"]:
                lines.append(f"  - {line}")
        lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT}")
    print(json.dumps(totals, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
