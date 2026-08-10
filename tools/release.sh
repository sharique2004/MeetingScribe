#!/bin/bash
# Publish a MeetingScribe release: one command from working tree to
# "everyone who downloads gets this version".
#
#   tools/release.sh 3.2.0 [--notes "text" | --notes-file path]
#
# What one run does, and why each step exists:
#   1. Builds the distributable DMG + tarball via tools/build_dmg_bundle.sh
#      with CFBundleShortVersionString stamped to the given version. That
#      script already refuses to produce unnotarized release artifacts, so a
#      release that reaches step 2 is installable on a stranger's Mac.
#   2. Creates GitHub release v<version> with BOTH assets under their
#      CANONICAL names (MeetingScribe.dmg / MeetingScribe.app.tar.gz) and
#      marks it latest. The names are load-bearing three times over:
#      the site redirect (mobile/vercel.json) and the curl installer point at
#      releases/latest/download/<canonical name>, and the in-app update
#      checker sends people to the same place — publish under these names
#      and every download channel serves the new version, automatically.
#
# The version gates below exist because the update checker compares
# CFBundleShortVersionString against the release tag: a tag that does not
# parse as numbers, or that is not newer than the latest published tag,
# would make installed apps either mis-compare or nag about a "new" version
# they already have.

set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="sharique2004/MeetingScribe"

VERSION="${1:-}"
case "$VERSION" in
    [0-9]*.[0-9]*) ;;  # looks like a version
    *) echo "usage: tools/release.sh <version e.g. 3.2.0> [--notes ... | --notes-file ...]"; exit 2 ;;
esac
shift

NOTES=""
NOTES_FILE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --notes)      NOTES="$2"; shift 2 ;;
        --notes-file) NOTES_FILE="$2"; shift 2 ;;
        *) echo "unknown argument: $1"; exit 2 ;;
    esac
done

command -v gh >/dev/null || { echo "ERROR: gh CLI is required"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "ERROR: gh is not authenticated (gh auth login)"; exit 1; }

if gh release view "v$VERSION" -R "$REPO" >/dev/null 2>&1; then
    echo "ERROR: release v$VERSION already exists. Bump the version."
    exit 1
fi

# Newer-than-latest gate, numeric component-wise (3.10 > 3.9, 3.1 == 3.1.0).
LATEST_TAG="$(gh release list -R "$REPO" --limit 1 --json tagName -q '.[0].tagName' 2>/dev/null || true)"
if [ -n "$LATEST_TAG" ]; then
    NEWER="$(python3 - "$VERSION" "${LATEST_TAG#v}" <<'EOF'
import sys
def parts(v): return [int(x) for x in v.split(".")]
a, b = parts(sys.argv[1]), parts(sys.argv[2])
n = max(len(a), len(b))
a += [0] * (n - len(a)); b += [0] * (n - len(b))
print("yes" if a > b else "no")
EOF
)"
    [ "$NEWER" = "yes" ] || { echo "ERROR: $VERSION is not newer than published $LATEST_TAG"; exit 1; }
fi

if [ -n "$(git -C "$PROJECT" status --porcelain)" ]; then
    echo "WARNING: the working tree is dirty — this release will not match any commit."
    echo "         (Ctrl-C now to commit first; continuing in 5s.)"
    sleep 5
fi

echo "== Building $VERSION (bundle + notarize; this takes a while) =="
MS_VERSION="$VERSION" bash "$PROJECT/tools/build_dmg_bundle.sh"

DMG="$PROJECT/dist/MeetingScribe.dmg"
TAR="$PROJECT/dist/MeetingScribe.app.tar.gz"
[ -f "$DMG" ] && [ -f "$TAR" ] || { echo "ERROR: expected artifacts missing in dist/"; exit 1; }

# The artifact must carry the version it claims: a stale dist/ from an old
# run publishing as v$VERSION would hand every updater the wrong build.
STAMPED="$(defaults read "$PROJECT/dist/stage/MeetingScribe.app/Contents/Info" CFBundleShortVersionString 2>/dev/null || true)"
if [ -n "$STAMPED" ] && [ "$STAMPED" != "$VERSION" ]; then
    echo "ERROR: staged app says $STAMPED, releasing as $VERSION — refusing."
    exit 1
fi

echo "== Publishing GitHub release v$VERSION =="
ARGS=(release create "v$VERSION" "$DMG" "$TAR"
      -R "$REPO" --title "MeetingScribe $VERSION" --latest)
if [ -n "$NOTES_FILE" ]; then ARGS+=(--notes-file "$NOTES_FILE")
elif [ -n "$NOTES" ]; then ARGS+=(--notes "$NOTES")
else ARGS+=(--generate-notes); fi
gh "${ARGS[@]}"

echo
echo "Released. Every channel now serves $VERSION:"
echo "  site:      https://meetingscribe.shariquekhatri.com/MeetingScribe.dmg"
echo "  installer: curl -fsSL https://meetingscribe.shariquekhatri.com/install.sh | sh"
echo "  in-app:    installed apps see it on their next update check (<=24h)"
