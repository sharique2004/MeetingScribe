#!/bin/bash
# MeetingScribe one-line installer:
#   curl -fsSL https://meetingscribe.shariquekhatri.com/install.sh | sh
#
# Downloads the self-contained app (Python runtime included, nothing else to
# install), puts it in /Applications, and opens it. Apple Silicon + macOS 26+.
set -euo pipefail

RELEASE_URL="https://github.com/sharique2004/MeetingScribe/releases/latest/download/MeetingScribe.app.tar.gz"
APP="MeetingScribe.app"
DEST_DIR="/Applications"

say() { printf '%s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- sanity checks -----------------------------------------------------------
[ "$(uname -s)" = "Darwin" ] || fail "MeetingScribe is a Mac app; this installer only runs on macOS."
[ "$(uname -m)" = "arm64" ] || fail "MeetingScribe needs an Apple Silicon Mac (M1 or newer). This Mac is $(uname -m)."

OS_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
case "$OS_MAJOR" in
    ''|*[!0-9]*) fail "Could not determine your macOS version (sw_vers gave '$OS_MAJOR')." ;;
esac
[ "$OS_MAJOR" -ge 26 ] || fail "MeetingScribe needs macOS 26 or newer (its on-device transcription uses Apple's macOS 26 Speech frameworks). This Mac runs macOS $(sw_vers -productVersion)."

[ -w "$DEST_DIR" ] || fail "$DEST_DIR is not writable by you. Re-run this installer with sudo:
  curl -fsSL <this-script-url> | sudo bash"

# --- download & extract ------------------------------------------------------
TMP="$(mktemp -d /tmp/meetingscribe-install.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

say "downloading MeetingScribe.app (about 310 MB, the whole engine ships in the app)"
curl -fL --retry 3 -o "$TMP/$APP.tar.gz" "$RELEASE_URL" \
    || fail "Download failed. Check your internet connection and try again."

say "unpacking"
tar -xzf "$TMP/$APP.tar.gz" -C "$TMP"
[ -d "$TMP/$APP" ] || fail "The downloaded archive did not contain $APP."

# --- install -----------------------------------------------------------------
if [ -d "$DEST_DIR/$APP" ]; then
    if [ -t 0 ]; then
        read -r -p "$DEST_DIR/$APP already exists. Replace it? [y/N] " answer
        case "$answer" in
            [yY]*) ;;
            *) say "Left the existing copy alone. Nothing was installed."; exit 0 ;;
        esac
    fi
    BACKUP="$DEST_DIR/MeetingScribe.previous-$(date +%Y%m%d%H%M%S).app"
    mv "$DEST_DIR/$APP" "$BACKUP"
    say "Moved the previous copy to $BACKUP (delete it once the new one works)."
fi
mv "$TMP/$APP" "$DEST_DIR/$APP"

say "installed to $DEST_DIR"

# The app is ad-hoc signed (no Apple Developer ID); clear the quarantine flag
# so Gatekeeper doesn't block the first launch.
xattr -dr com.apple.quarantine "$DEST_DIR/$APP" 2>/dev/null || true
say "cleared quarantine, no Gatekeeper prompts"

say "opening MeetingScribe"
open "$DEST_DIR/$APP" || true

say ""
say "Done! Your recordings and settings live in ~/.meetingscribe."
say "Optional: to capture system audio (the other side of a call), install the"
say "free BlackHole audio driver: https://existential.audio/blackhole/"
