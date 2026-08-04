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

from config import DATA_DIR

BIN_DIR = DATA_DIR / "bin"

# Set by the packaged app (BackendManager) to Contents/Resources/bin, where
# the pre-built helpers live. Empty when running from a source checkout.
PREBUILT_DIR = Path(os.environ["MEETINGSCRIBE_PREBUILT"]) \
    if os.environ.get("MEETINGSCRIBE_PREBUILT") else None

# Helpers that run IN PLACE from Contents/Resources/bin and are never copied
# to ~/.meetingscribe/bin: a bare copied-out executable never appears in the
# Screen & System Audio Recording settings pane (macOS 26.1 bug, DTS-
# confirmed), and TCC attribution should reach MeetingScribe.app, not a loose
# binary in the home directory. (Which identity the grant ultimately keys on
# under adhoc signing — app bundle vs helper cdhash — is verified empirically
# before Phase 2 ships; build_mac_app.sh keeps the helper byte-stable so the
# cdhash-keyed case also survives app updates.)
BUNDLE_ONLY = {"apple_syscap"}


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
    """Put every bundled helper in place at startup (so every hard-coded
    ~/.meetingscribe/bin path works). No-op from a source checkout."""
    if PREBUILT_DIR is None:
        return
    for prebuilt in PREBUILT_DIR.glob("*"):
        if prebuilt.is_file() and prebuilt.name not in BUNDLE_ONLY:
            _install_prebuilt(prebuilt.name)


def find_binary(name):
    """Where the helper already lives (bundle, then ~/.meetingscribe/bin) —
    NEVER compiles. For cheap availability probes on paths that must not
    block (app startup, UI preflight polls); ensure_binary() is the real
    resolver at point of use."""
    if sys.platform != "darwin":
        return None
    if PREBUILT_DIR is not None:
        in_bundle = PREBUILT_DIR / name
        if in_bundle.exists():
            return str(in_bundle)
    installed = BIN_DIR / name
    return str(installed) if installed.exists() else None


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
    if name in BUNDLE_ONLY and PREBUILT_DIR is not None:
        in_bundle = PREBUILT_DIR / name
        if in_bundle.exists():
            return str(in_bundle)
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
