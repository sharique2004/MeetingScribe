#!/bin/sh
# Run the MeetingScribe engine WITHOUT the old app shell — no old timer HUD,
# no old notes panel. The new capsule (nativeui/run.sh) is the only HUD, and
# the web UI stays available at http://127.0.0.1:5005 for review.
#
# Replicates exactly how the installed app launches its backend (same python
# venv, same app code, same env), minus the AppKit shell around it.
set -e

APP="/Applications/MeetingScribe.app"
PY="$HOME/.meetingscribe/venv/bin/python"
LOG="$HOME/.meetingscribe/headless.log"

# Refuse to yank the engine out from under a live meeting.
if curl -s -m 2 http://127.0.0.1:5005/api/record/status | grep -q '"recording":true'; then
    echo "A recording is in progress — stop it first." >&2
    exit 1
fi

# Quit the old shell (takes its backend and the old HUD panels with it).
osascript -e 'tell application "MeetingScribe" to quit' 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -x MeetingScribe >/dev/null || break
    sleep 0.5
done

MEETINGSCRIBE_DATA="$HOME/.meetingscribe" \
MEETINGSCRIBE_NO_BROWSER=1 \
MEETINGSCRIBE_PREBUILT="$APP/Contents/Resources/bin" \
nohup "$PY" "$APP/Contents/Resources/app/app.py" >>"$LOG" 2>&1 &

echo "Engine starting headless (log: $LOG)…"
for _ in $(seq 1 30); do
    if curl -s -m 1 http://127.0.0.1:5005/api/record/status >/dev/null 2>&1; then
        echo "Engine is up at http://127.0.0.1:5005 — capsule is the only HUD now."
        exit 0
    fi
    sleep 0.5
done
echo "Engine did not come up — check $LOG" >&2
exit 1
