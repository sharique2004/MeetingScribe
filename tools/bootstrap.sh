#!/bin/bash
# First-launch setup for the downloaded MeetingScribe.app.
#
# The .app ships the Python source but not its ~2 GB of dependencies (that
# would make the download enormous). On first launch the app runs this once
# to build the local environment. Idempotent: safe to re-run; exits fast
# when everything is already in place.
#
# Arg 1: the bundled app source dir (…/MeetingScribe.app/Contents/Resources/app)
# Writes the environment to ~/.meetingscribe (venv, bin, and — migrated once —
# any recordings from an older source checkout at ~/MeetingScribe).
set -uo pipefail

APP_SRC="${1:?usage: bootstrap.sh <app-source-dir>}"
DATA="$HOME/.meetingscribe"
VENV="$DATA/venv"
BIN="$DATA/bin"
mkdir -p "$DATA" "$BIN"

# Bundles built by tools/build_dmg_bundle.sh ship their own CPython runtime
# (with all dependencies) next to this script — nothing to install at all.
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -x "$SELF_DIR/python/bin/python3" ]; then
    echo "This copy of MeetingScribe ships its own Python runtime — no setup needed."
    echo "SETUP-COMPLETE"
    exit 0
fi

echo "MeetingScribe first-time setup"
echo "This runs once and takes a few minutes. Everything installs on this Mac."
echo

# --- Python -----------------------------------------------------------------
PYBIN=""
for cand in python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        v=$("$cand" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
        case "$v" in
            3.1[0-2]) PYBIN="$(command -v "$cand")"; break ;;
        esac
    fi
done
if [ -z "$PYBIN" ]; then
    echo "ERROR: Python 3.10–3.12 is required and was not found."
    echo "Install it with Homebrew (brew install python@3.12) or from python.org,"
    echo "then reopen MeetingScribe."
    exit 3
fi
echo "Using Python: $PYBIN ($("$PYBIN" --version 2>&1))"

# --- venv + dependencies ----------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating the Python environment…"
    "$PYBIN" -m venv "$VENV" || { echo "ERROR: could not create the virtual environment."; exit 4; }
fi
echo "Installing dependencies (~2 GB the first time; needs internet)…"
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null 2>&1
if ! "$VENV/bin/python" -m pip install -r "$APP_SRC/requirements.txt"; then
    echo "ERROR: dependency installation failed (check your internet connection)."
    exit 5
fi
# Same removal setup.sh does, for the same reason: mlx-whisper declares a
# `torch` dependency that nothing in its own import graph — or in this app —
# ever uses, so pip drags 533 MB of PyTorch (plus sympy, networkx, mpmath)
# onto every Apple Silicon Mac. The bundled-runtime builds avoid it with
# --no-deps against tools/requirements.bundle.lock, whose header carries the
# verification; this path has no lock to be authoritative, so it uninstalls
# after the fact. Best effort: the app works either way.
if "$VENV/bin/python" -c "import torch" >/dev/null 2>&1; then
    echo "Removing PyTorch, which this app does not use…"
    "$VENV/bin/python" -m pip uninstall -y torch torchaudio sympy networkx mpmath \
        >/dev/null 2>&1 || true
fi

# The on-device Speech/AI helpers ship pre-built inside the app bundle and are
# installed by the backend at startup (swift_helpers.install_all_prebuilt) —
# no compiling on the user's Mac. Nothing to do here.

# --- one-time migration from an older source checkout -----------------------
OLD="$HOME/MeetingScribe/recordings"
if [ -d "$OLD" ] && [ ! -d "$DATA/recordings" ]; then
    echo "Bringing over your existing recordings…"
    mkdir -p "$DATA/recordings"
    cp -R "$OLD"/* "$DATA/recordings/" 2>/dev/null || true
    [ -f "$HOME/MeetingScribe/config.json" ] && cp "$HOME/MeetingScribe/config.json" "$DATA/config.json" 2>/dev/null || true
fi

echo
echo "SETUP-COMPLETE"
