#!/bin/sh
# Build MeetingScribe.app for the iOS Simulator, with no .xcodeproj.
#
# The same approach tools/build_mac_app.sh already takes: compile the sources
# with swiftc and assemble the bundle by hand. An Xcode project buys nothing
# here — there is one target, no storyboards, no asset catalogue and no build
# phases — and it costs a 3000-line pbxproj that no one can review in a diff.
#
# Simulator only, on purpose. A device build needs a provisioning profile and
# a signing identity per device, which is a decision for the person who owns
# the developer account, not something a build script should invent. See
# docs/IOS.md for the device/TestFlight path.
#
# Usage:  tools/build_ios_app.sh [outdir]
set -e

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$PROJECT/ios/.build}"
APP="$OUT/MeetingScribe.app"

SDK="$(xcrun --sdk iphonesimulator --show-sdk-path)"
# Match the deployment target in Info.plist. FoundationModels and
# SpeechAnalyzer are both iOS 26 APIs, so there is no lower floor available.
TARGET="arm64-apple-ios26.0-simulator"

echo "Building MeetingScribe for the iOS Simulator…"
rm -rf "$APP"
mkdir -p "$APP"

# -parse-as-library so @main is honoured rather than treated as top-level code.
xcrun swiftc \
    -sdk "$SDK" \
    -target "$TARGET" \
    -swift-version 6 \
    -O \
    -parse-as-library \
    $(find "$PROJECT/ios/Sources" -name '*.swift') \
    -o "$APP/MeetingScribe"

cp "$PROJECT/ios/Resources/Info.plist" "$APP/Info.plist"

# Ad-hoc signature. The Simulator does not verify a team identity, but an
# unsigned binary is refused outright by newer simctl.
codesign --force --sign - --timestamp=none "$APP" >/dev/null 2>&1 || true

echo "Built: $APP"
echo
echo "Install and run with:"
echo "  xcrun simctl boot 'iPhone 17 Pro' 2>/dev/null || true"
echo "  xcrun simctl install booted \"$APP\""
echo "  xcrun simctl launch booted com.meetingscribe.ios"
