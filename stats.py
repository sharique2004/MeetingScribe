"""Conversation statistics — the 'how did I do?' part of MeetingScribe.

Computed per speaker from the final transcript turns: talk-time share,
pace, questions asked, filler words, longest monologue.
"""

import re

FILLER_PHRASES = [
    "um", "uh", "umm", "uhh", "hmm", "er", "erm",
    "you know", "i mean", "sort of", "kind of",
    "basically", "actually", "literally", "like",
]

_FILLER_RES = {p: re.compile(r"\b" + re.escape(p) + r"\b") for p in FILLER_PHRASES}
_WORD_RE = re.compile(r"[\w'-]+")


def compute(turns, speakers, duration):
    per = {
        key: {
            "seconds": 0.0,
            "words": 0,
            "turns": 0,
            "questions": 0,
            "longest_turn_seconds": 0.0,
            "fillers": {},
            "filler_total": 0,
        }
        for key in speakers
    }

    for turn in turns:
        st = per.get(turn["speaker"])
        if st is None:
            continue
        length = max(0.0, float(turn["end"]) - float(turn["start"]))
        text = turn["text"]
        lower = text.lower()
        st["seconds"] += length
        st["turns"] += 1
        st["longest_turn_seconds"] = max(st["longest_turn_seconds"], length)
        st["words"] += len(_WORD_RE.findall(text))
        st["questions"] += text.count("?")
        for phrase, rx in _FILLER_RES.items():
            hits = len(rx.findall(lower))
            if hits:
                st["fillers"][phrase] = st["fillers"].get(phrase, 0) + hits
                st["filler_total"] += hits

    total_spoken = sum(s["seconds"] for s in per.values())
    for st in per.values():
        st["seconds"] = round(st["seconds"], 1)
        st["longest_turn_seconds"] = round(st["longest_turn_seconds"], 1)
        st["share"] = round(st["seconds"] / total_spoken, 3) if total_spoken > 0 else 0.0
        minutes = st["seconds"] / 60.0
        st["wpm"] = round(st["words"] / minutes) if minutes > 0.05 else 0
        st["fillers"] = dict(
            sorted(st["fillers"].items(), key=lambda kv: kv[1], reverse=True)
        )

    return {
        "per_speaker": per,
        "total_spoken_seconds": round(total_spoken, 1),
        "duration": round(float(duration or 0.0), 1),
        "total_words": sum(s["words"] for s in per.values()),
    }
