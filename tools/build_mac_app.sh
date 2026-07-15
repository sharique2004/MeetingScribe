#!/bin/bash
# Build MeetingScribe.app — a self-contained, distributable Mac app.
#
# The Python source ships INSIDE the bundle (Contents/Resources/app), so a
# downloaded copy is complete on its own. It does not ship the ~2 GB of
# Python dependencies; the app builds those on first launch via the bundled
# bootstrap.sh (see BackendManager.runBootstrap). User data (recordings,
# models, config) lives in ~/.meetingscribe, so the bundle stays read-only.
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

echo "Assembling $DEST…"
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
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>MeetingScribe records meetings with your microphone. Audio never leaves this Mac.</string>
  <key>NSCalendarsFullAccessUsageDescription</key>
  <string>MeetingScribe reads today's events to name recordings automatically and remind you to record meetings. Calendar data never leaves this Mac.</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

# Icon: drawn procedurally by tools/make_icon.py (needs numpy — available in
# the build machine's venv). Ships pre-built inside the bundle.
if [ -x "$VENV_PY" ] && "$VENV_PY" "$PROJECT/tools/make_icon.py" \
        "$DEST/Contents/Resources/$APP_NAME.icns" 2>/dev/null; then
    echo "wrote the app icon"
else
    echo "(icon skipped — venv python or numpy unavailable)"
fi

# Ad-hoc signature: enough to run locally and to post notifications. A
# downloaded copy is unsigned by a Developer ID, so Gatekeeper asks the user
# to right-click → Open the first time (the download page explains this).
codesign --force --deep --sign - "$DEST" 2>/dev/null || codesign --force --sign - "$DEST"

touch "$DEST"  # nudge LaunchServices to refresh the icon
echo "Built: $DEST"
