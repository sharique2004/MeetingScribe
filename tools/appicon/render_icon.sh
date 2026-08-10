#!/bin/sh
# Render the committed icon artifacts from the Icon Composer source
# (MeetingScribe.icon) using actool, which renders the document exactly as
# designed — gradients, shadow, translucency, light/dark appearances.
# Requires Xcode 26+ (Icon Composer .icon support); the build script only
# needs the committed artifacts, so a Mac with bare Command Line Tools can
# still build the app.
#
# Two artifacts, two jobs:
#   MeetingScribe.icns  — one static image (the LIGHT appearance), used by
#                         anything that reads CFBundleIconFile.
#   Assets.car          — the compiled Icon Composer document. Shipping this
#                         plus CFBundleIconName is what lets macOS 26 switch
#                         the icon's appearance live with light/dark/tinted
#                         mode; a flat icns can never do that.
#
# Usage:  sh tools/appicon/render_icon.sh
set -e
cd "$(dirname "$0")"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT
xcrun actool --target-device mac --platform macosx --minimum-deployment-target 26.0 \
    --app-icon MeetingScribe --include-all-app-icons \
    --output-partial-info-plist "$OUT/partial.plist" \
    --compile "$OUT" "$PWD/MeetingScribe.icon" > /dev/null
cp "$OUT/MeetingScribe.icns" MeetingScribe.icns
cp "$OUT/Assets.car" Assets.car
echo "wrote tools/appicon/MeetingScribe.icns and tools/appicon/Assets.car"
