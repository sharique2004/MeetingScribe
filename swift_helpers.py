"""Compile-on-demand Swift helper binaries.

MeetingScribe ships small Swift sources in tools/ (Apple Speech, EventKit,
Apple Intelligence) and compiles them once into ~/.meetingscribe/bin — outside
the (possibly cloud-synced) project folder. A binary is rebuilt whenever its
source is newer.
"""

import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("meetingscribe.swift")

BIN_DIR = Path.home() / ".meetingscribe" / "bin"


def macos_version():
    try:
        return tuple(int(x) for x in platform.mac_ver()[0].split(".")[:2])
    except (ValueError, IndexError):
        return (0, 0)


def ensure_binary(src, name, *, min_macos=None, require_arm64=True,
                  parse_as_library=True, timeout=300):
    """Return the path to the compiled helper, building it on demand.

    Returns None when the platform can't support it or compilation fails —
    callers treat that as "feature unavailable" and degrade gracefully.
    """
    if sys.platform != "darwin":
        return None
    if require_arm64 and platform.machine() != "arm64":
        return None
    if min_macos and macos_version() < tuple(min_macos):
        return None
    src = Path(src)
    if not src.exists():
        return None
    out = BIN_DIR / name
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return str(out)
    swiftc = shutil.which("swiftc") or "/usr/bin/swiftc"
    if not (shutil.which("swiftc") or os.path.exists("/usr/bin/swiftc")):
        return None
    cmd = [swiftc, "-O"]
    if parse_as_library:
        cmd.append("-parse-as-library")
    cmd += [str(src), "-o", str(out)]
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
        return str(out)
    except (subprocess.SubprocessError, OSError) as exc:
        detail = getattr(exc, "stderr", "") or exc
        log.warning("could not build %s: %s", name, detail)
        return None
