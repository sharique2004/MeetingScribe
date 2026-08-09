#!/bin/sh
# Render tools/appicon/MeetingScribe.icns from the Icon Composer source
# (MeetingScribe.icon) using actool, which renders the document exactly as
# designed — gradients, shadow, translucency, light/dark appearances.
# Requires Xcode 26+ (Icon Composer .icon support).
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
echo "wrote tools/appicon/MeetingScribe.icns"
