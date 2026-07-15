#!/bin/bash
# Package MeetingScribe.app into a distributable MeetingScribe.dmg — the
# classic drag-to-Applications disk image. Builds the app fresh into a temp
# staging dir, adds an /Applications symlink, and compresses.
#
# Output: dist/MeetingScribe.dmg
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="MeetingScribe"
STAGE="$(mktemp -d)"
DMG_DIR="$PROJECT/dist"
DMG="$DMG_DIR/$APP_NAME.dmg"
trap 'rm -rf "$STAGE"' EXIT

echo "Building the app into the disk image…"
bash "$PROJECT/tools/build_mac_app.sh" "$STAGE"

# Drag-to-install layout: the app next to an Applications shortcut.
ln -s /Applications "$STAGE/Applications"

mkdir -p "$DMG_DIR"
rm -f "$DMG"
echo "Compressing $DMG…"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO \
    -fs HFS+ "$DMG" >/dev/null

SIZE=$(du -h "$DMG" | cut -f1)
echo "Created: $DMG ($SIZE)"
