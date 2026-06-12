#!/bin/bash
# Build the distributable zip from the committed git tree — guarantees the
# download contains exactly what the repo does (no recordings, models, or
# local config ever leak in).
# Usage:  bash tools/make_zip.sh [output.zip]
set -e
cd "$(dirname "$0")/.."
OUT="${1:-meetingscribe.zip}"
git archive --format=zip --prefix=MeetingScribe/ HEAD -o "$OUT"
echo "wrote $OUT ($(du -h "$OUT" | cut -f1 | tr -d ' '))"
