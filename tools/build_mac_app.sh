#!/bin/bash
# Build and install MeetingScribe.app — the native menu-bar Mac app.
#
# Compiles macapp/Sources with bare swiftc (Command Line Tools are enough,
# same as the other Swift helpers), assembles the .app bundle, draws the
# icon, ad-hoc code-signs it (required for notifications), and installs to
# /Applications (falling back to ~/Applications).
#
# The project path is baked into Info.plist (MSProjectDir) so the app can
# find app.py and the venv wherever the user keeps this folder.
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="MeetingScribe"
BUNDLE_ID="com.meetingscribe.app"
VENV_PY="$HOME/.meetingscribe/venv/bin/python"

DEST_DIR="/Applications"
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
mkdir -p "$DEST/Contents/MacOS" "$DEST/Contents/Resources"
cp "$BUILD_DIR/$APP_NAME" "$DEST/Contents/MacOS/$APP_NAME"

cat > "$DEST/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleVersion</key><string>2.0</string>
  <key>CFBundleShortVersionString</key><string>2.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>$APP_NAME</string>
  <key>CFBundleIconFile</key><string>$APP_NAME</string>
  <key>LSMinimumSystemVersion</key><string>26.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>MSProjectDir</key><string>$PROJECT</string>
  <key>NSMicrophoneUsageDescription</key>
  <string>MeetingScribe records meetings with your microphone. Audio never leaves this Mac.</string>
  <key>NSCalendarsFullAccessUsageDescription</key>
  <string>MeetingScribe reads today's events to name recordings automatically and remind you to record meetings. Calendar data never leaves this Mac.</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

# Icon: drawn procedurally by tools/make_icon.py (needs the venv's numpy).
if [ -x "$VENV_PY" ] && "$VENV_PY" "$PROJECT/tools/make_icon.py" \
        "$DEST/Contents/Resources/$APP_NAME.icns" 2>/dev/null; then
    echo "wrote $DEST/Contents/Resources/$APP_NAME.icns"
else
    echo "(icon skipped — venv python or numpy unavailable)"
fi

# Ad-hoc signature: enough for local use, and required for the app to post
# native notifications. Distribution needs Developer ID + notarization.
codesign --force --sign - "$DEST"

touch "$DEST"  # nudge LaunchServices to refresh the icon
echo "Installed: $DEST"
echo "Launch it from Spotlight (⌘-space, type MeetingScribe), or drag it to the Dock."
