#!/bin/sh
# Assemble the NATIVE MeetingScribe.app — the SwiftUI front end with the
# Python engine bundled, ready to replace the old shell in /Applications.
# Mirrors tools/build_mac_app.sh's engine bundling without touching it or
# dist/ (the release pipeline owns those).
set -e

NUI="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$NUI")"
OUT="$NUI/.build/MeetingScribe.app"
IDENTITY="Developer ID Application: Sharique Khatri (5VJ8KXLF45)"

echo "Compiling the native app…"
xcrun swiftc -O -parse-as-library -target arm64-apple-macos26.0 \
    "$NUI"/Sources/MainApp/*.swift -o "$NUI/.build/MeetingScribe-native"

echo "Assembling $OUT…"
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources/app"
cp "$NUI/.build/MeetingScribe-native" "$OUT/Contents/MacOS/MeetingScribe"

echo "Bundling the engine…"
( cd "$PROJECT" && rsync -a \
    --exclude ".git" --exclude "__pycache__" --exclude "*.pyc" \
    --exclude "recordings" --exclude "models" --exclude "practice" \
    --exclude "macapp" --exclude "mobile" --exclude ".insforge" \
    --exclude "config.json" --exclude "*.log" --exclude ".DS_Store" \
    --exclude "venv" --exclude "nativeui" --exclude "ui-demos" \
    --exclude "native" --exclude "dist" --exclude "docs" \
    ./*.py templates tools requirements.txt static "$OUT/Contents/Resources/app/" 2>/dev/null || true )

# Prebuilt on-device helpers: reuse the signed ones from the installed app.
if [ -d "/Applications/MeetingScribe.app/Contents/Resources/bin" ]; then
    cp -R "/Applications/MeetingScribe.app/Contents/Resources/bin" "$OUT/Contents/Resources/bin"
fi
mkdir -p "$OUT/Contents/Resources/bin"

# fluid-diarizer (neural speaker turns) is an SPM package, not a single-file
# helper; build it fresh and ship it. Its ~22 MB of CoreML models are NOT
# bundled — they download to <DATA_DIR>/models/fluid-diarization on first
# use, and diarization falls back to the classic engine until they exist.
if (cd "$PROJECT/native/fluiddiarizer" && swift build -c release >/dev/null 2>&1); then
    cp "$PROJECT/native/fluiddiarizer/.build/release/fluid-diarizer" \
        "$OUT/Contents/Resources/bin/fluid-diarizer"
    echo "  staged fluid-diarizer"
else
    echo "  WARNING: fluid-diarizer failed to build (neural turns will fall back to classic)"
fi

cp "$PROJECT/tools/appicon/MeetingScribe.icns" "$OUT/Contents/Resources/AppIcon.icns"

cat > "$OUT/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>MeetingScribe</string>
    <key>CFBundleIdentifier</key><string>com.meetingscribe.app</string>
    <key>CFBundleName</key><string>MeetingScribe</string>
    <key>CFBundleDisplayName</key><string>MeetingScribe</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundleShortVersionString</key><string>3.1</string>
    <key>CFBundleVersion</key><string>3.1</string>
    <key>LSMinimumSystemVersion</key><string>26.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSMicrophoneUsageDescription</key><string>MeetingScribe records meetings with your microphone. Audio never leaves this Mac.</string>
    <key>NSCalendarsFullAccessUsageDescription</key><string>MeetingScribe reads today's events to name recordings automatically and remind you to record meetings. Calendar data never leaves this Mac.</string>
</dict>
</plist>
EOF

echo "Signing…"
codesign --force --options runtime --timestamp \
    --entitlements "$PROJECT/tools/entitlements/app.entitlements" \
    -s "$IDENTITY" "$OUT" 2>&1 | tail -1
codesign --verify --strict "$OUT" && echo "Built and signed: $OUT"
