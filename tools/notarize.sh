#!/bin/bash
# Notarize and staple MeetingScribe release artifacts.
#
#   tools/notarize.sh app <path/to/MeetingScribe.app>
#       Zip the signed app, submit to Apple notary service, wait for the
#       verdict, staple the ticket into the bundle, then assert Gatekeeper
#       accepts it. Run BEFORE packaging so the DMG/tarball carry the ticket.
#
#   tools/notarize.sh dmg <path/to/MeetingScribe.dmg>
#       Submit the (Developer ID signed) DMG, staple it, assert Gatekeeper
#       accepts it.
#
# Credentials come from the keychain profile created once with:
#   xcrun notarytool store-credentials meetingscribe-notary \
#       --apple-id khatrisharique7@gmail.com --team-id 5VJ8KXLF45
#
# bash 3.2 compatible.
set -euo pipefail

PROFILE="${MS_NOTARY_PROFILE:-meetingscribe-notary}"
CMD="${1:-}"
TARGET="${2:-}"
[ -n "$CMD" ] && [ -n "$TARGET" ] && [ -e "$TARGET" ] \
    || { echo "usage: notarize.sh app|dmg <path>"; exit 2; }

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

submit_and_wait() {
    # $1 = file to submit. notarytool exits non-zero on an Invalid verdict,
    # so run it status-blind and decide from the transcript: Accepted wins;
    # anything else dumps the notary log (the whole point of the transcript).
    # caffeinate keeps the Mac from idle-sleeping through the long wait (a
    # sleep kills the poll AND locks the keychain); if the wait still dies
    # after upload, resume it server-side by submission id.
    _sub="$1"
    _out="$WORKDIR/submit.txt"
    echo "Submitting $(basename "$_sub") to Apple notary service (this waits for the verdict)…"
    set +e
    caffeinate -ims xcrun notarytool submit "$_sub" --keychain-profile "$PROFILE" --wait \
        2>&1 | tee "$_out"
    set -e
    _id="$(awk '/^[[:space:]]*id: /{print $2; exit}' "$_out")"
    if [ -n "$_id" ] && ! grep -q "status: Accepted" "$_out" && ! grep -q "status: Invalid" "$_out"; then
        echo "  wait interrupted after upload — resuming server-side wait for $_id…"
        set +e
        caffeinate -ims xcrun notarytool wait "$_id" --keychain-profile "$PROFILE" 2>&1 | tee -a "$_out"
        xcrun notarytool info "$_id" --keychain-profile "$PROFILE" 2>&1 | tee -a "$_out"
        set -e
    fi
    if grep -q "status: Accepted" "$_out"; then
        echo "  notarization: Accepted"
        return 0
    fi
    echo "ERROR: notarization was not accepted."
    if [ -n "$_id" ]; then
        echo "---- notary log ($_id) ----"
        xcrun notarytool log "$_id" --keychain-profile "$PROFILE" || true
    else
        echo "(no submission id in the transcript — likely a network or"
        echo " credentials problem before Apple accepted the upload)"
    fi
    exit 1
}

case "$CMD" in
  app)
    APP="$TARGET"
    ZIP="$WORKDIR/$(basename "$APP").zip"
    echo "Zipping the app for submission…"
    ditto -c -k --keepParent "$APP" "$ZIP"
    submit_and_wait "$ZIP"
    rm -f "$ZIP"
    echo "Stapling the ticket into the app…"
    xcrun stapler staple "$APP"
    xcrun stapler validate "$APP"
    echo "Gatekeeper assessment…"
    spctl --assess --type exec -vv "$APP" 2>&1 | sed 's/^/  /' || true
    spctl --assess --type exec "$APP" \
        || { echo "ERROR: Gatekeeper still rejects the app"; exit 1; }
    echo "  Gatekeeper: accepted"
    ;;
  dmg)
    DMG="$TARGET"
    submit_and_wait "$DMG"
    echo "Stapling the ticket onto the DMG…"
    xcrun stapler staple "$DMG"
    xcrun stapler validate "$DMG"
    echo "Gatekeeper assessment…"
    spctl --assess --type open --context context:primary-signature -vv "$DMG" 2>&1 | sed 's/^/  /' || true
    spctl --assess --type open --context context:primary-signature "$DMG" \
        || { echo "ERROR: Gatekeeper still rejects the DMG"; exit 1; }
    echo "  Gatekeeper: accepted"
    ;;
  *)
    echo "usage: notarize.sh app|dmg <path>"; exit 2 ;;
esac
