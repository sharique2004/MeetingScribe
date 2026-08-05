#!/bin/sh
# Build MeetingScribe.app for iOS, with no .xcodeproj.
#
# The same approach tools/build_mac_app.sh already takes: compile the sources
# with swiftc and assemble the bundle by hand. An Xcode project buys nothing
# here — there is one target, no storyboards, no asset catalogue and no build
# phases — and it costs a 3000-line pbxproj that no one can review in a diff.
#
#   tools/build_ios_app.sh                 simulator build (default)
#   tools/build_ios_app.sh --device        device build, needs a profile
#
# A DEVICE build needs two things this script will not invent for you, because
# both are decisions on someone's Apple developer account:
#
#   MS_IOS_PROFILE     path to a .mobileprovision downloaded from the portal
#   MS_IOS_IDENTITY    codesigning identity, e.g. "Apple Development: you (XXX)"
#                      defaults to an Apple Development cert on MS_IOS_TEAM
#
# The entitlements are read OUT of the profile rather than written by hand.
# Hand-written entitlements that disagree with the profile are the single most
# common reason a device install fails with a message that names neither.
#
# ONE TEAM, CHECKED. This Mac has certificates from two Apple teams, and the
# wrong one has already cost a day once: notarization was set up against a
# second Apple ID before anyone noticed. So the team is pinned here and both
# the profile and the signing identity are verified against it rather than
# trusted to be the right ones.
#
# Note that "Developer ID Application" — the cert the Mac app ships with —
# CANNOT sign an iOS app. It is macOS-only. iOS needs Apple Development (for a
# device) or Apple Distribution (for TestFlight and the store), issued under
# this same team.
set -e

MS_IOS_TEAM="${MS_IOS_TEAM:-5VJ8KXLF45}"

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="simulator"
[ "$1" = "--device" ] && { MODE="device"; shift; }
OUT="${1:-$PROJECT/ios/.build}"
APP="$OUT/MeetingScribe.app"

if [ "$MODE" = "device" ]; then
    SDK="$(xcrun --sdk iphoneos --show-sdk-path)"
    TARGET="arm64-apple-ios26.0"
else
    SDK="$(xcrun --sdk iphonesimulator --show-sdk-path)"
    TARGET="arm64-apple-ios26.0-simulator"
fi

echo "Building MeetingScribe for iOS ($MODE)…"
rm -rf "$APP"
mkdir -p "$APP"

# -parse-as-library so @main is honoured rather than treated as top-level code.
xcrun swiftc \
    -sdk "$SDK" \
    -target "$TARGET" \
    -swift-version 6 \
    -O \
    -parse-as-library \
    $(find "$PROJECT/ios/Sources" -name '*.swift') \
    -o "$APP/MeetingScribe"

cp "$PROJECT/ios/Resources/Info.plist" "$APP/Info.plist"

if [ "$MODE" = "simulator" ]; then
    # The Simulator does not verify a team identity, but an unsigned binary is
    # refused outright by newer simctl.
    codesign --force --sign - --timestamp=none "$APP" >/dev/null 2>&1 || true
    echo "Built: $APP"
    echo
    echo "Install and run with:"
    echo "  xcrun simctl boot 'iPhone 17 Pro' 2>/dev/null || true"
    echo "  xcrun simctl install booted \"$APP\""
    echo "  xcrun simctl launch booted com.meetingscribe.ios"
    exit 0
fi

# ---------------------------------------------------------------- device ----

if [ -z "$MS_IOS_PROFILE" ] || [ ! -f "$MS_IOS_PROFILE" ]; then
    echo
    echo "ERROR: a device build needs a provisioning profile."
    echo
    echo "  1. developer.apple.com ▸ Identifiers ▸ register App ID"
    echo "       com.meetingscribe.ios"
    echo "  2. developer.apple.com ▸ Devices ▸ register this iPhone's UDID"
    echo "  3. developer.apple.com ▸ Profiles ▸ new iOS App Development profile"
    echo "       for that App ID + that device, then download it"
    echo "  4. re-run:"
    echo "       MS_IOS_PROFILE=~/Downloads/MeetingScribe.mobileprovision \\"
    echo "         tools/build_ios_app.sh --device"
    echo
    echo "Signing identities on this Mac:"
    security find-identity -v -p codesigning 2>/dev/null \
        | grep -E "Apple Development|Apple Distribution" || echo "  (none)"
    exit 2
fi

# Pick an identity that BELONGS TO MS_IOS_TEAM. The team is the OU of the
# certificate subject, so read it rather than guessing from the common name.
IDENTITY="${MS_IOS_IDENTITY:-}"
if [ -z "$IDENTITY" ]; then
    for candidate in $(security find-identity -v -p codesigning 2>/dev/null \
        | awk -F'"' '/Apple Development|Apple Distribution/{print $2}' | tr ' ' '\001'); do
        name="$(printf '%s' "$candidate" | tr '\001' ' ')"
        ou="$(security find-certificate -c "$name" -p 2>/dev/null \
            | openssl x509 -noout -subject 2>/dev/null \
            | sed -n 's/.*OU=\([A-Z0-9]*\).*/\1/p')"
        if [ "$ou" = "$MS_IOS_TEAM" ]; then IDENTITY="$name"; break; fi
    done
fi
if [ -z "$IDENTITY" ]; then
    echo
    echo "ERROR: no iOS signing certificate on team $MS_IOS_TEAM in this keychain."
    echo
    echo "What IS here:"
    security find-identity -v -p codesigning 2>/dev/null \
        | awk -F'"' '/Apple|Developer ID/{print "  " $2}' | while read -r line; do
            n="${line#  }"
            o="$(security find-certificate -c "$n" -p 2>/dev/null \
                | openssl x509 -noout -subject 2>/dev/null \
                | sed -n 's/.*OU=\([A-Z0-9]*\).*/\1/p')"
            echo "  $n   [team ${o:-?}]"
        done
    echo
    echo "Developer ID Application is a macOS certificate and cannot sign for"
    echo "iOS. Create an Apple Development certificate under team"
    echo "$MS_IOS_TEAM (Xcode ▸ Settings ▸ Accounts ▸ Manage Certificates ▸ +,"
    echo "with that team selected), then re-run."
    exit 2
fi

# The profile goes INSIDE the bundle under this exact name. iOS looks for it
# there and nowhere else.
cp "$MS_IOS_PROFILE" "$APP/embedded.mobileprovision"

# Entitlements come out of the profile. A .mobileprovision is a CMS-signed
# plist, so decode it and lift the Entitlements dict verbatim: anything we
# wrote by hand that the profile does not grant is a launch failure on device
# whose error message mentions neither file.
ENTITLEMENTS="$OUT/entitlements.plist"
security cms -D -i "$MS_IOS_PROFILE" > "$OUT/profile.plist" 2>/dev/null
/usr/libexec/PlistBuddy -x -c "Print :Entitlements" "$OUT/profile.plist" > "$ENTITLEMENTS"

PROFILE_APP_ID="$(/usr/libexec/PlistBuddy -c "Print :application-identifier" "$ENTITLEMENTS" 2>/dev/null || true)"
PROFILE_TEAM="$(/usr/libexec/PlistBuddy -c "Print :com.apple.developer.team-identifier" "$ENTITLEMENTS" 2>/dev/null || true)"
PROFILE_EXPIRY="$(/usr/libexec/PlistBuddy -c "Print :ExpirationDate" "$OUT/profile.plist" 2>/dev/null || true)"
echo "Profile grants: $PROFILE_APP_ID  [team $PROFILE_TEAM]"
[ -n "$PROFILE_EXPIRY" ] && echo "Profile expires: $PROFILE_EXPIRY"

if [ -n "$PROFILE_TEAM" ] && [ "$PROFILE_TEAM" != "$MS_IOS_TEAM" ]; then
    echo "ERROR: this profile belongs to team $PROFILE_TEAM, not $MS_IOS_TEAM."
    echo "       Download the profile from the $MS_IOS_TEAM account"
    echo "       (khatrisharique7@gmail.com), or set MS_IOS_TEAM deliberately."
    exit 2
fi

case "$PROFILE_APP_ID" in
    *".com.meetingscribe.ios"|*".\*")
        ;;
    *)
        echo "ERROR: this profile is for '$PROFILE_APP_ID', which does not cover"
        echo "       com.meetingscribe.ios. Installing it would fail on device."
        exit 2
        ;;
esac

echo "Signing with: $IDENTITY"
codesign --force --sign "$IDENTITY" \
    --entitlements "$ENTITLEMENTS" \
    --generate-entitlement-der \
    --timestamp=none \
    "$APP"

codesign --verify --verbose=2 "$APP" 2>&1 | sed 's/^/  /'

echo "Built: $APP"
echo
echo "Install on the connected iPhone with:"
echo "  xcrun devicectl device install app --device <device-id> \"$APP\""
echo "  tools/run_ios_device.sh          # finds the device and does both"
