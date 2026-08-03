#!/bin/bash
# Package MeetingScribe.app into a DEV disk image (no bundled Python runtime,
# no notarization — first launch still bootstraps a venv). For anything you
# ship, use tools/build_dmg_bundle.sh, which signs, notarizes and staples.
#
# Output: dist/MeetingScribe-dev.dmg  (deliberately NOT the release filename,
# so this can never overwrite a notarized release artifact)
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="MeetingScribe"
STAGE="$(mktemp -d)"
DMG_DIR="$PROJECT/dist"
DMG="$DMG_DIR/$APP_NAME-dev.dmg"
trap 'rm -rf "$STAGE"' EXIT

echo "Building the app into the disk image…"
# Force ad-hoc signing: this script has no notarization step, and a
# Developer-ID-signed-but-unnotarized DMG looks release-grade locally while
# being hard-blocked by Gatekeeper on every other Mac.
MS_SIGN_IDENTITY="-" bash "$PROJECT/tools/build_mac_app.sh" "$STAGE"
echo "NOTE: dev image — ad-hoc signed, NOT notarized, do not ship."

# Drag-to-install layout: the app next to an Applications shortcut.
ln -s /Applications "$STAGE/Applications"

mkdir -p "$DMG_DIR"
rm -f "$DMG"
echo "Compressing $DMG…"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO \
    -fs HFS+ "$DMG" >/dev/null

SIZE=$(du -h "$DMG" | cut -f1)
echo "Created: $DMG ($SIZE)"
