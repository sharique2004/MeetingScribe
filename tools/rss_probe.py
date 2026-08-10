"""Sample a process's memory while it works. Dev-only; nothing imports this.

Two numbers are recorded because they disagree in exactly the way that matters
here: `ps` RSS counts Parakeet's 2.4 GB of file-backed safetensors pages even
though the OS can drop them at will, while `footprint`'s phys_footprint is the
figure macOS memory pressure (and Activity Monitor) actually charges the
process for. A fix that moves phys_footprint but not RSS is still a real fix.

Usage:
    python3 tools/rss_probe.py --out report.json -- python3 workload.py args…
    python3 tools/rss_probe.py --pid 12345

In spawn mode the child's stdout is echoed through; any line starting with
"MARK:" is also recorded as a named mark with the nearest samples, so a
workload script narrates its own phases:

    print("MARK:transcribed", flush=True)

The report prints per-mark RSS/footprint plus the peak between marks, and the
JSON holds every sample for anything finer.
"""

import argparse
import json
import re
import signal
import subprocess
import sys
import threading
import time


def _ps_sample(pid):
    """(rss_bytes, vsz_bytes) or None once the process is gone."""
    try:
        out = subprocess.run(["ps", "-o", "rss=,vsz=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return None
    parts = out.stdout.split()
    if len(parts) != 2:
        return None
    return int(parts[0]) * 1024, int(parts[1]) * 1024


def _footprint(pid):
    """phys_footprint in bytes, or None. Noticeably slower than ps, so it is
    taken at marks and every tenth sample rather than every tick."""
    try:
        out = subprocess.run(["/usr/bin/footprint", str(pid)],
                             capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    m = re.search(r"Footprint:\s+([\d.]+)\s+(B|KB|MB|GB)", out.stdout)
    if not m:
        return None
    scale = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}[m.group(2)]
    return int(float(m.group(1)) * scale)


def _mb(n):
    return "      —" if n is None else f"{n / 1048576:7.0f}"


class Probe:
    def __init__(self, pid, interval):
        self.pid = pid
        self.interval = interval
        self.t0 = time.monotonic()
        self.samples = []   # (t, rss, vsz, footprint-or-None)
        self.marks = []     # (t, name)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        n = 0
        while not self._stop.is_set():
            ps = _ps_sample(self.pid)
            if ps is None:
                break
            fp = _footprint(self.pid) if n % 10 == 0 else None
            self.samples.append((time.monotonic() - self.t0, ps[0], ps[1], fp))
            n += 1
            self._stop.wait(self.interval)

    def start(self):
        self._thread.start()

    def mark(self, name):
        t = time.monotonic() - self.t0
        ps = _ps_sample(self.pid)
        fp = _footprint(self.pid)
        if ps is not None:
            self.samples.append((t, ps[0], ps[1], fp))
        self.marks.append((t, name))

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=self.interval + 15)

    def report(self):
        lines = ["", f"{'t':>7}  {'rss MB':>7}  {'foot MB':>7}  mark / peak-rss-since-previous"]
        prev_t = 0.0
        for (t, name) in self.marks:
            at = min(self.samples, key=lambda s: abs(s[0] - t), default=None)
            span = [s for s in self.samples if prev_t <= s[0] <= t]
            peak = max((s[1] for s in span), default=0)
            fp = next((s[3] for s in reversed(span) if s[3] is not None), None)
            if at:
                lines.append(f"{t:7.0f}  {_mb(at[1])}  {_mb(fp)}  {name}"
                             f"   (peak since prev: {peak / 1048576:.0f} MB)")
            prev_t = t
        if self.samples:
            peak = max(s[1] for s in self.samples)
            fps = [s[3] for s in self.samples if s[3] is not None]
            lines.append(f"overall peak rss {peak / 1048576:.0f} MB"
                         + (f", peak footprint {max(fps) / 1048576:.0f} MB" if fps else ""))
        return "\n".join(lines)

    def dump(self, path):
        with open(path, "w") as f:
            json.dump({"pid": self.pid, "interval": self.interval,
                       "samples": self.samples, "marks": self.marks}, f)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--out", help="write raw samples as JSON here")
    ap.add_argument("--pid", type=int, help="attach to a running process")
    ap.add_argument("cmd", nargs="*", help="command to spawn and monitor (after --)")
    args = ap.parse_args()

    if bool(args.pid) == bool(args.cmd):
        ap.error("exactly one of --pid or a command is required")

    if args.pid:
        probe = Probe(args.pid, args.interval)
        probe.start()
        probe.mark("attach")
        try:
            while _ps_sample(args.pid) is not None:
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass
        probe.mark("detach")
        probe.stop()
    else:
        child = subprocess.Popen(args.cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
        probe = Probe(child.pid, args.interval)
        probe.start()
        probe.mark("spawn")
        signal.signal(signal.SIGTERM, lambda *_: child.terminate())
        try:
            for line in child.stdout:
                line = line.rstrip("\n")
                print(line, flush=True)
                if line.startswith("MARK:"):
                    probe.mark(line[5:].strip())
        except KeyboardInterrupt:
            child.terminate()
        child.wait()
        probe.mark("exit")
        probe.stop()

    print(probe.report())
    if args.out:
        probe.dump(args.out)


if __name__ == "__main__":
    main()
