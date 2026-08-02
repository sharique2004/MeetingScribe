#!/bin/bash
# Build MeetingScribe.app — a self-contained, distributable Mac app.
#
# The Python source ships INSIDE the bundle (Contents/Resources/app), so a
# downloaded copy is complete on its own. Two flavours:
#
#   MS_PYTHON_RUNTIME=<dir>  — a prepared relocatable CPython runtime (see
#       tools/build_dmg_bundle.sh) is copied to Contents/Resources/python and
#       the app runs with NO install step: no system Python, no pip, no
#       bootstrap. This is what the DMG / curl installer ships.
#   unset — the app builds a ~/.meetingscribe venv on first launch via the
#       bundled bootstrap.sh (see BackendManager.runBootstrap), as before.
#
# User data (recordings, models, config) lives in ~/.meetingscribe, so the
# bundle stays read-only.
#
# Compiles macapp/Sources with bare swiftc (Command Line Tools are enough),
# assembles the bundle, draws the icon, ad-hoc signs it. Pass a destination
# dir as arg 1 (default: install into /Applications).
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="MeetingScribe"
BUNDLE_ID="com.meetingscribe.app"
VERSION="2.0"
VENV_PY="$HOME/.meetingscribe/venv/bin/python"

DEST_DIR="${1:-/Applications}"
[ -w "$DEST_DIR" ] || DEST_DIR="$HOME/Applications"
mkdir -p "$DEST_DIR"
DEST="$DEST_DIR/$APP_NAME.app"

echo "Compiling the native app…"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT
xcrun swiftc -O -parse-as-library \
    "$PROJECT"/macapp/Sources/MeetingScribe/*.swift \
    -o "$BUILD_DIR/$APP_NAME"

echo "Assembling ${DEST}…"
rm -rf "$DEST"
mkdir -p "$DEST/Contents/MacOS" "$DEST/Contents/Resources/app"
cp "$BUILD_DIR/$APP_NAME" "$DEST/Contents/MacOS/$APP_NAME"

# Bundle the Python source (everything the backend needs, none of the data).
echo "Bundling the Python engine…"
APP_SRC="$DEST/Contents/Resources/app"
( cd "$PROJECT" && rsync -a \
    --exclude ".git" --exclude "__pycache__" --exclude "*.pyc" \
    --exclude "recordings" --exclude "models" --exclude "practice" \
    --exclude "macapp" --exclude "mobile" --exclude ".insforge" \
    --exclude "config.json" --exclude "*.log" --exclude ".DS_Store" \
    --exclude "venv" \
    ./*.py templates tools requirements.txt static "$APP_SRC/" 2>/dev/null || true )
# The static/ folder is optional.
cp "$PROJECT/tools/bootstrap.sh" "$DEST/Contents/Resources/bootstrap.sh"
chmod +x "$DEST/Contents/Resources/bootstrap.sh"

# Optional: bundle a relocatable CPython runtime (site-packages included) so
# the downloaded app needs no Python, no pip and no first-launch install.
# tools/build_dmg_bundle.sh prepares the runtime and points us at it.
if [ -n "${MS_PYTHON_RUNTIME:-}" ]; then
    [ -x "$MS_PYTHON_RUNTIME/bin/python3" ] \
        || { echo "ERROR: MS_PYTHON_RUNTIME=$MS_PYTHON_RUNTIME has no bin/python3"; exit 1; }
    echo "Bundling the Python runtime…"
    rsync -a "$MS_PYTHON_RUNTIME/" "$DEST/Contents/Resources/python/"
fi

# Pre-compile the Swift helpers on THIS machine's up-to-date SDK and ship the
# binaries. Compiling on the user's Mac is unreliable — the Speech and
# FoundationModels frameworks need a recent Command Line Tools SDK that a
# random Mac may lack (this is what broke transcription on a fresh install).
# These are OS-linked binaries: built on macOS 26 here, they run on any
# macOS 26 Mac. The backend copies them into ~/.meetingscribe/bin at startup.
echo "Pre-building the on-device helpers…"
PREBUILT="$DEST/Contents/Resources/bin"
mkdir -p "$PREBUILT"
if command -v xcrun >/dev/null 2>&1; then
    xcrun swiftc -O -parse-as-library "$PROJECT/tools/apple_transcribe.swift" -o "$PREBUILT/apple_transcribe" \
        && echo "  built apple_transcribe" || echo "  WARNING: apple_transcribe failed to build here"
    xcrun swiftc -O -parse-as-library "$PROJECT/tools/apple_live.swift" -o "$PREBUILT/apple_live" \
        && echo "  built apple_live" || echo "  WARNING: apple_live failed to build here"
    xcrun swiftc -O -parse-as-library "$PROJECT/tools/apple_llm.swift" -o "$PREBUILT/apple_llm" \
        && echo "  built apple_llm" || echo "  WARNING: apple_llm failed to build here"
    xcrun swiftc -O "$PROJECT/tools/calendar_events.swift" -o "$PREBUILT/calendar_events" \
        && echo "  built calendar_events" || echo "  WARNING: calendar_events failed to build here"

    # apple_syscap stays BYTE-STABLE across releases as a defensive measure:
    # under adhoc signing the System Audio Recording grant may key on the
    # helper's cdhash (research memo; to be verified against TCC.db before
    # Phase 2 ships), and an unchanged helper preserves that identity even
    # when the app changes. Reuse the cached build unless the source or the
    # toolchain changed (the later adhoc codesign pass is deterministic for
    # identical input bytes).
    SYSCAP_CACHE="$HOME/.meetingscribe/build-cache"
    SYSCAP_KEY=$( (shasum -a 256 "$PROJECT/tools/apple_syscap.swift"; xcrun swiftc --version 2>/dev/null) | shasum -a 256 | cut -d' ' -f1 )
    mkdir -p "$SYSCAP_CACHE"
    if [ -f "$SYSCAP_CACHE/apple_syscap-$SYSCAP_KEY" ]; then
        cp "$SYSCAP_CACHE/apple_syscap-$SYSCAP_KEY" "$PREBUILT/apple_syscap"
        echo "  reused cached apple_syscap (unchanged — keeps the user's TCC grant)"
    elif xcrun swiftc -O -parse-as-library "$PROJECT/tools/apple_syscap.swift" -o "$PREBUILT/apple_syscap"; then
        cp "$PREBUILT/apple_syscap" "$SYSCAP_CACHE/apple_syscap-$SYSCAP_KEY"
        echo "  built apple_syscap (new cdhash — first recording after update re-prompts)"
    else
        echo "  WARNING: apple_syscap failed to build here"
    fi
fi

cat > "$DEST/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>$APP_NAME</string>
  <key>CFBundleIconFile</key><string>$APP_NAME</string>
  <!-- The pre-built Speech/AI helpers link macOS 26 frameworks (minos 26.0),
       so the app is Apple Silicon + macOS 26 only. -->
  <key>LSMinimumSystemVersion</key><string>26.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>MeetingScribe records meetings with your microphone. Audio never leaves this Mac.</string>
  <key>NSAudioCaptureUsageDescription</key>
  <string>MeetingScribe records the audio your Mac plays during a meeting so the other participants are transcribed. Nothing leaves this Mac.</string>
  <key>NSCalendarsFullAccessUsageDescription</key>
  <string>MeetingScribe reads today's events to name recordings automatically and remind you to record meetings. Calendar data never leaves this Mac.</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

# Icon: prefer the committed app icon (tools/appicon/MeetingScribe.icns,
# rendered from the Icon Composer source tools/appicon/MeetingScribe.icon by
# tools/appicon/render_icon.py). Fall back to the procedural tools/make_icon.py
# only if that committed icns is missing.
if [ -f "$PROJECT/tools/appicon/MeetingScribe.icns" ]; then
    cp "$PROJECT/tools/appicon/MeetingScribe.icns" "$DEST/Contents/Resources/$APP_NAME.icns"
    echo "installed the app icon (tools/appicon/MeetingScribe.icns)"
elif [ -x "$VENV_PY" ] && "$VENV_PY" "$PROJECT/tools/make_icon.py" \
        "$DEST/Contents/Resources/$APP_NAME.icns" 2>/dev/null; then
    echo "wrote the app icon (procedural fallback)"
else
    echo "(icon skipped — no committed icns and venv python/numpy unavailable)"
fi

# Ad-hoc signature: enough to run locally and to post notifications. A
# downloaded copy is unsigned by a Developer ID, so Gatekeeper asks the user
# to right-click → Open the first time (the download page explains this).
#
# Sign every nested Mach-O first — the bundled runtime carries thousands of
# .so extension modules and dylibs — then the bundle itself, so the resource
# seal covers the freshly signed files. (--deep is deprecated and would not
# reach code inside Resources anyway.)
echo "Signing…"
SIGNLOG="$BUILD_DIR/codesign.log"
find "$DEST/Contents/Resources" -type f \( -name "*.so" -o -name "*.dylib" \) -print0 \
    | xargs -0 -n 64 codesign --force --sign - 2>>"$SIGNLOG" \
    || { echo "codesign failed on a nested library:"; cat "$SIGNLOG"; exit 1; }
# Extension-less executables (helper binaries, python3.11 itself): sign
# anything whose magic says Mach-O.
for dir in "$DEST/Contents/Resources/bin" "$DEST/Contents/Resources/python/bin"; do
    [ -d "$dir" ] || continue
    for f in "$dir"/*; do
        [ -f "$f" ] && [ ! -L "$f" ] || continue
        case "$(head -c 4 "$f" | xxd -p)" in
            cffaedfe|cafebabe|feedfacf)
                codesign --force --sign - "$f" 2>>"$SIGNLOG" \
                    || { echo "codesign failed on $f:"; cat "$SIGNLOG"; exit 1; } ;;
        esac
    done
done
codesign --force --sign - "$DEST"

touch "$DEST"  # nudge LaunchServices to refresh the icon
echo "Built: $DEST"
