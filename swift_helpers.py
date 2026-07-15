"""Swift helper binaries (Apple Speech, EventKit, Apple Intelligence).

In the packaged app these ship PRE-COMPILED inside the bundle (built on an
up-to-date SDK at release time) and are simply copied into ~/.meetingscribe/bin
on the user's Mac — no compiler or matching SDK required. Compiling on the
user's machine is fragile: the newer Speech/FoundationModels frameworks need a
Command Line Tools SDK that a random Mac may not have, which is exactly what
broke transcription on a fresh install.

Running from a source checkout (no bundled binaries) falls back to
compile-on-demand for developer convenience.
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

# Set by the packaged app (BackendManager) to Contents/Resources/bin, where
# the pre-built helpers live. Empty when running from a source checkout.
PREBUILT_DIR = Path(os.environ["MEETINGSCRIBE_PREBUILT"]) \
    if os.environ.get("MEETINGSCRIBE_PREBUILT") else None


def macos_version():
    try:
        return tuple(int(x) for x in platform.mac_ver()[0].split(".")[:2])
    except (ValueError, IndexError):
        return (0, 0)


def _install_prebuilt(name):
    """Copy a pre-built helper from the bundle into ~/.meetingscribe/bin,
    refreshing it when the bundled copy is newer. Returns the path or None."""
    if PREBUILT_DIR is None:
        return None
    prebuilt = PREBUILT_DIR / name
    if not prebuilt.exists():
        return None
    out = BIN_DIR / name
    try:
        if not out.exists() or out.stat().st_mtime < prebuilt.stat().st_mtime:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(prebuilt, out)
            os.chmod(out, 0o755)
            # Downloaded bundles are quarantined; clear it so exec isn't blocked.
            subprocess.run(["xattr", "-d", "com.apple.quarantine", str(out)],
                           capture_output=True)
        return str(out)
    except OSError as exc:
        log.warning("could not install pre-built %s: %s", name, exc)
        return None


def install_all_prebuilt():
    """Put every bundled helper in place at startup (so hard-coded paths like
    screener's apple_transcribe work). No-op from a source checkout."""
    if PREBUILT_DIR is None:
        return
    for prebuilt in PREBUILT_DIR.glob("*"):
        if prebuilt.is_file():
            _install_prebuilt(prebuilt.name)


def ensure_binary(src, name, *, min_macos=None, require_arm64=True,
                  parse_as_library=True, timeout=300):
    """Path to the helper: prefer the pre-built bundled copy; otherwise
    compile on demand (developer/source-checkout mode). None if unsupported."""
    if sys.platform != "darwin":
        return None
    if require_arm64 and platform.machine() != "arm64":
        return None
    if min_macos and macos_version() < tuple(min_macos):
        return None

    # Packaged app: use the pre-built binary shipped in the bundle.
    installed = _install_prebuilt(name)
    if installed:
        return installed

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
