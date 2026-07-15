#!/bin/bash
# MeetingScribe one-time setup for macOS.
# Run from Terminal:  bash setup.sh
# The Python environment is created OUTSIDE this folder (~/.meetingscribe)
# on purpose, so OneDrive doesn't try to sync thousands of package files.
set -e
cd "$(dirname "$0")"

echo "============================================"
echo "  MeetingScribe - one-time setup (macOS)"
echo "  (needs internet, downloads ~2 GB of"
echo "   Python packages; takes a few minutes)"
echo "============================================"
echo

PYBIN=""
for cand in python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,12) else 1)' 2>/dev/null; then
            PYBIN="$cand"
            break
        fi
    fi
done

if [ -z "$PYBIN" ]; then
    echo "Python 3.10-3.12 was not found."
    if command -v brew >/dev/null 2>&1; then
        echo "Installing Python 3.11 with Homebrew..."
        brew install python@3.11
        # python@3.11 is keg-only: its binary lives under the formula prefix.
        PYBIN="$(brew --prefix python@3.11)/bin/python3.11"
        if [ ! -x "$PYBIN" ]; then
            echo "Python did not install where expected ($PYBIN)."
            echo "Check the brew output above, then run this script again."
            exit 1
        fi
    else
        echo "Please install Python 3.12 from https://www.python.org/downloads/macos/"
        echo "then run this script again:  bash setup.sh"
        exit 1
    fi
fi

echo "Using Python: $PYBIN ($($PYBIN --version))"
VENV="$HOME/.meetingscribe/venv"
mkdir -p "$HOME/.meetingscribe"
"$PYBIN" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r requirements.txt

chmod +x run.command setup.sh 2>/dev/null || true

# Compile the Apple SpeechAnalyzer transcriber (macOS 26+). This is the fast,
# on-device (Neural Engine) backend; the pipeline also builds it on demand, so
# a failure here is non-fatal — Whisper is used as the fallback.
MACOS_MAJOR="$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)"
if [ "${MACOS_MAJOR:-0}" -ge 26 ] && command -v xcrun >/dev/null 2>&1; then
    mkdir -p "$HOME/.meetingscribe/bin"
    if xcrun swiftc -O -parse-as-library tools/apple_transcribe.swift \
            -o "$HOME/.meetingscribe/bin/apple_transcribe" 2>/dev/null; then
        echo "Apple Speech transcriber built (fast, on-device)."
    else
        echo "(Apple Speech helper did not build — Whisper will be used instead.)"
    fi
    if xcrun swiftc -O -parse-as-library tools/apple_llm.swift \
            -o "$HOME/.meetingscribe/bin/apple_llm" 2>/dev/null; then
        echo "On-device AI helper built (Apple Intelligence: summaries, tidy, practice)."
    else
        echo "(On-device AI helper did not build — AI features need macOS 26+.)"
    fi
fi

# Build the double-clickable MeetingScribe.app (Dock/Spotlight launcher).
bash tools/make_mac_app.sh || echo "(Could not build MeetingScribe.app — run.command still works.)"

# BlackHole gives MeetingScribe a copy of the system audio (the other
# meeting participants). Routing is automatic once the driver exists.
if [ ! -d "/Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver" ] && command -v brew >/dev/null 2>&1; then
    echo
    read -r -p "Install the BlackHole audio driver now, so the OTHER people in your online meetings are captured too? (asks for your password) [Y/n] " ans
    if [ "$ans" != "n" ] && [ "$ans" != "N" ]; then
        brew install blackhole-2ch \
            && echo "BlackHole installed - meeting audio will be captured automatically." \
            || echo "BlackHole install failed - see README.md for the manual steps."
    fi
fi

echo
echo "============================================"
echo "  Setup complete!"
echo "  Launch MeetingScribe from Spotlight or the"
echo "  Applications folder, like any Mac app."
echo "  (run.command still works from Terminal.)"
echo "============================================"
