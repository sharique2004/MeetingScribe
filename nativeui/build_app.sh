#!/bin/sh
# Build and install the native MeetingScribe.app for local use.
#
# This used to assemble a bundle of its own, and it shipped a real bug for it:
# it took the on-device helpers by COPYING THEM OUT OF THE ALREADY-INSTALLED
# APP. That is circular, and it is lossy in one direction only, so the first
# install that lacked a helper made every later build lack it too. What went
# missing was apple_syscap, the system-audio tap. With no tap in the bundle the
# engine falls back to compiling one into ~/.meetingscribe/bin, ad-hoc signed,
# with a fresh code identity on every compile. macOS keys the System Audio
# Recording grant to that identity, so each rebuild silently revoked the app's
# permission to hear the far side of a call: the tap ran, wrote a full-size
# WAV, and captured digital zeros. 26 of 44 recordings on the development
# machine are silent that way.
#
# So there is one builder now, tools/build_mac_app.sh, which builds every
# helper from source and signs it with the Developer ID. It builds the SwiftUI
# front end by default (MS_SHELL=legacy selects the retired WKWebView shell),
# which is exactly what this script existed to produce.
set -e

NUI="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$NUI")"
STAGE="$NUI/.build"

mkdir -p "$STAGE"
bash "$PROJECT/tools/build_mac_app.sh" "$STAGE"

APP="$STAGE/MeetingScribe.app"
# The tap is the whole reason this script was rewritten. Fail loudly rather
# than hand back a bundle that will quietly record silence.
if [ ! -x "$APP/Contents/Resources/bin/apple_syscap" ]; then
    echo "ERROR: apple_syscap is missing from the bundle."
    echo "       The far side of calls would not be recorded. Not installing."
    exit 1
fi

echo "Built: $APP"
echo "Install with:  cp -R \"$APP\" /Applications/"
