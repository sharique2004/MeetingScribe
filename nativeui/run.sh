#!/bin/sh
# Build and launch the recording-capsule prototype (macOS 26+, Xcode 26).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$DIR/.build"
mkdir -p "$BIN"
echo "Compiling…"
xcrun swiftc -O -parse-as-library \
    -target arm64-apple-macos26.0 \
    "$DIR"/Sources/HUDPrototype/*.swift \
    -o "$BIN/hud-prototype"
echo "Launching."
exec "$BIN/hud-prototype"
