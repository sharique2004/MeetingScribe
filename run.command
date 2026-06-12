#!/bin/bash
# Starts MeetingScribe on macOS. Double-click this file (after running
# setup.sh once) or run:  bash run.command
cd "$(dirname "$0")"

PY="$HOME/.meetingscribe/venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "MeetingScribe is not set up yet on this Mac."
    echo "Open Terminal in this folder and run:  bash setup.sh"
    read -r -p "Press Enter to close..."
    exit 1
fi

exec "$PY" app.py
