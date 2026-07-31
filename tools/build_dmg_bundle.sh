#!/bin/bash
# Build the fully self-contained MeetingScribe distribution: a .app that
# bundles a relocatable CPython runtime (python-build-standalone) plus every
# dependency from tools/requirements.bundle.lock, so a downloader needs no
# Python, no pip and no Homebrew. Only the HuggingFace model downloads remain
# a first-run network action.
#
# Outputs:
#   dist/stage/MeetingScribe.app     — the staged bundle (never /Applications)
#   dist/MeetingScribe.dmg           — drag-to-Applications disk image
#   dist/MeetingScribe.app.tar.gz    — for the curl installer (tools/install.sh)
#
# Apple Silicon + macOS 26 only (the pre-built Speech helpers are minos 26.0).
# bash 3.2 compatible.
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="MeetingScribe"

# Pinned runtime: python-build-standalone release + CPython version. Matches
# the known-good ~/.meetingscribe/venv (Python 3.11.15) the lock was frozen
# from. Bump both together, then refresh tools/requirements.bundle.lock.
PBS_TAG="20260728"
PY_VERSION="3.11.15"
PBS_ASSET="cpython-${PY_VERSION}+${PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_ASSET}"

LOCK="$PROJECT/tools/requirements.bundle.lock"
[ -f "$LOCK" ] || { echo "ERROR: $LOCK is missing"; exit 1; }

# Warn (don't fail) if the reference venv drifted from the pinned version.
VENV_PY="$HOME/.meetingscribe/venv/bin/python"
if [ -x "$VENV_PY" ]; then
    VENV_VER="$("$VENV_PY" --version 2>&1 | awk '{print $2}')"
    [ "$VENV_VER" = "$PY_VERSION" ] || echo "WARNING: venv is Python $VENV_VER but bundling $PY_VERSION — refresh the pin/lock if intentional."
fi

CACHE="$PROJECT/dist/pbs-cache"
RUNTIME_ROOT="$PROJECT/dist/runtime-build"
RUNTIME="$RUNTIME_ROOT/python"
STAGE="${1:-$PROJECT/dist/stage}"
case "$STAGE" in
    /Applications|/Applications/*) echo "ERROR: refusing to stage into /Applications"; exit 1 ;;
esac

# --- 1. the relocatable CPython runtime --------------------------------------
mkdir -p "$CACHE"
if [ ! -s "$CACHE/$PBS_ASSET" ]; then
    echo "Downloading $PBS_ASSET…"
    curl -fL --retry 3 -o "$CACHE/$PBS_ASSET.part" "$PBS_URL"
    mv "$CACHE/$PBS_ASSET.part" "$CACHE/$PBS_ASSET"
fi

# --- 2. install the exact locked package set into it -------------------------
# Rebuilt only when the pin or the lock changes (stamp file).
STAMP="$RUNTIME_ROOT/.stamp"
WANT="$PBS_ASSET $(shasum -a 256 "$LOCK" | awk '{print $1}')"
if [ ! -x "$RUNTIME/bin/python3" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$WANT" ]; then
    echo "Preparing the bundled runtime (extract + pip install, takes a while)…"
    rm -rf "$RUNTIME_ROOT"
    mkdir -p "$RUNTIME_ROOT"
    tar -xzf "$CACHE/$PBS_ASSET" -C "$RUNTIME_ROOT"   # extracts to python/
    PYTHONDONTWRITEBYTECODE=1 "$RUNTIME/bin/python3" -m pip install \
        --no-warn-script-location --no-compile -r "$LOCK"
    # Belt and suspenders: never write .pyc inside the sealed bundle, even if
    # someone runs the bundled interpreter without the env var.
    SITEPKGS="$("$RUNTIME/bin/python3" -c 'import site; print(site.getsitepackages()[0])')"
    cat > "$SITEPKGS/sitecustomize.py" <<'EOF'
# The runtime ships inside a code-signed, read-only .app bundle: writing .pyc
# files would break the codesign resource seal. (Also set by the launcher via
# PYTHONDONTWRITEBYTECODE=1.)
import sys
sys.dont_write_bytecode = True
EOF
    # Obvious dead weight only: bytecode caches and the stdlib test suite.
    find "$RUNTIME" -name "__pycache__" -type d -prune -exec rm -rf {} +
    find "$RUNTIME" -name "*.pyc" -delete
    rm -rf "$RUNTIME/lib/python${PY_VERSION%.*}/test"
    echo "$WANT" > "$STAMP"
else
    echo "Bundled runtime is up to date (dist/runtime-build)."
fi

# --- 3. build the .app into the staging dir ----------------------------------
rm -rf "$STAGE"
mkdir -p "$STAGE"
MS_PYTHON_RUNTIME="$RUNTIME" bash "$PROJECT/tools/build_mac_app.sh" "$STAGE"
APP="$STAGE/$APP_NAME.app"

echo "Verifying the signature…"
codesign --verify --deep --strict "$APP"
echo "  codesign: OK"

# --- 4. package: DMG + tarball -----------------------------------------------
DMG="$PROJECT/dist/$APP_NAME.dmg"
TARBALL="$PROJECT/dist/$APP_NAME.app.tar.gz"
mkdir -p "$PROJECT/dist"

echo "Compressing $DMG…"
ln -sfn /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO \
    -fs HFS+ "$DMG" >/dev/null
rm -f "$STAGE/Applications"

echo "Compressing $TARBALL…"
rm -f "$TARBALL"
tar -czf "$TARBALL" -C "$STAGE" "$APP_NAME.app"

echo
echo "Built:"
du -sh "$APP" "$DMG" "$TARBALL"
