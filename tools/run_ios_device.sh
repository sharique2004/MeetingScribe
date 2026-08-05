#!/bin/sh
# Install and launch MeetingScribe on a connected iPhone, and print the UDID
# the developer portal asks for.
#
# Run it with no arguments at any point: before anything is set up it tells you
# what it can see and what is missing, which is the question you actually have
# at that stage.
#
# Device state comes from devicectl's JSON, not its table. The table was parsed
# by column position and matched with /available/ — which also matches
# "unavailable", so a phone that was not connected read as ready and the script
# sailed on to a confusing error two steps later.
set -e

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP="${1:-$PROJECT/ios/.build/MeetingScribe.app}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Looking for a connected iPhone…"
if ! xcrun devicectl list devices --json-output "$TMP/devices.json" >/dev/null 2>&1; then
    echo "  devicectl failed. Is Xcode installed?"
    exit 2
fi

python3 - "$TMP/devices.json" "$TMP/pick" <<'PY'
import json, sys
path, out = sys.argv[1], sys.argv[2]
devices = json.load(open(path)).get("result", {}).get("devices", [])
if not devices:
    print("  No devices are paired with this Mac at all.")
ready = None
for d in devices:
    name = d.get("deviceProperties", {}).get("name", "?")
    udid = d.get("hardwareProperties", {}).get("udid", "?")
    conn = d.get("connectionProperties", {})
    tunnel, pairing = conn.get("tunnelState"), conn.get("pairingState")
    print(f"  {name}")
    print(f"    UDID          {udid}")
    print(f"    paired        {pairing}")
    print(f"    connection    {tunnel}")
    if tunnel == "connected" and ready is None:
        ready = d.get("identifier")
open(out, "w").write(ready or "")
PY

DEVICE_ID="$(cat "$TMP/pick" 2>/dev/null || true)"
if [ -z "$DEVICE_ID" ]; then
    cat <<'EOF'

No iPhone is reachable right now. A paired phone still reads as unavailable
when it is locked, unplugged, or only known over the network. Plug it in with
a cable, unlock it, and tap Trust if asked.

The UDID above is what developer.apple.com ▸ Devices asks for — you can
register the phone before it is connected.
EOF
    exit 2
fi

if [ ! -d "$APP" ]; then
    echo
    echo "No build at $APP. Make one with:"
    echo "  MS_IOS_PROFILE=<profile.mobileprovision> tools/build_ios_app.sh --device"
    exit 2
fi
if [ ! -f "$APP/embedded.mobileprovision" ]; then
    echo
    echo "$APP is a SIMULATOR build (no embedded profile) and cannot run on a"
    echo "phone. Rebuild with --device."
    exit 2
fi

echo
echo "Installing…"
xcrun devicectl device install app --device "$DEVICE_ID" "$APP"
echo "Launching…"
xcrun devicectl device process launch --device "$DEVICE_ID" com.meetingscribe.ios
