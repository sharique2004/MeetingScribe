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

# --- Swift helpers (macOS 26+: Speech + Apple Intelligence) -----------------
MACOS_MAJOR="$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)"
if [ "${MACOS_MAJOR:-0}" -ge 26 ] && command -v xcrun >/dev/null 2>&1; then
    echo "Building the on-device Speech and AI helpers…"
    for src in apple_transcribe apple_llm; do
        xcrun swiftc -O -parse-as-library "$APP_SRC/tools/$src.swift" -o "$BIN/$src" 2>/dev/null \
            && echo "  built $src" || echo "  ($src skipped — Whisper/Apple-Intelligence fallback still works)"
    done
fi
if command -v xcrun >/dev/null 2>&1; then
    xcrun swiftc -O "$APP_SRC/tools/calendar_events.swift" -o "$BIN/calendar_events" 2>/dev/null \
        && echo "  built calendar helper" || true
fi

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
