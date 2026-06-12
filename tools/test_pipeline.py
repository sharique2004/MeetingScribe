"""Run the processing pipeline on the synthesized demo meeting and print the result."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import process_meeting  # noqa: E402

meeting = sys.argv[1] if len(sys.argv) > 1 else "20260610-000001"
base = Path(__file__).resolve().parents[1] / "recordings" / meeting

meta = process_meeting(base, lambda msg: print("  >", msg))

print("STATUS:", meta["status"])
print("LANGUAGES:", meta.get("languages"))
print("SPEAKERS:", json.dumps(meta["speakers"]))
print("WARNINGS:", meta["warnings"])
print("TURNS:")
for t in meta["turns"]:
    name = meta["speakers"].get(t["speaker"], t["speaker"])
    print(f"  [{t['start']:6.1f}-{t['end']:6.1f}] {name}: {t['text']}")
print("STATS:")
for key, st in meta["stats"]["per_speaker"].items():
    name = meta["speakers"].get(key, key)
    top = ", ".join(f"{w}x{c}" for w, c in list(st["fillers"].items())[:3]) or "-"
    print(
        f"  {name}: {st['seconds']}s ({round(st['share']*100)}%), {st['words']} words, "
        f"{st['wpm']} wpm, {st['questions']} questions, fillers: {top}"
    )
