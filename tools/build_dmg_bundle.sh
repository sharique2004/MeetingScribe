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

    # --- prune build-time and test-time weight from the sealed runtime -------
    # Everything removed here is material for COMPILING against a package or
    # for TESTING it, never for running it. Measured on the 3.1 tree: 165 MB
    # uncompressed, 20 MB off the compressed image. Proven by re-running every
    # tools/test_*.py against the pruned runtime (identical output to
    # ~/.meetingscribe/venv) plus a real ECAPA forward pass, a real Parakeet
    # decode, a real numba nopython compile and the cffi audio bindings.
    echo "Pruning build-only weight from the runtime…"

    # 1. C/C++/Cython headers and static libs. Nothing in the bundle compiles
    #    anything at runtime: soundfile and sounddevice bind libsndfile and
    #    portaudio through cffi's ABI mode (no compiler), and numba JITs
    #    through llvmlite rather than a C toolchain.
    find "$SITEPKGS" \( -name '*.h' -o -name '*.hpp' -o -name '*.cuh' \
        -o -name '*.pxd' -o -name '*.pyx' -o -name '*.a' \) -delete
    rm -rf "$SITEPKGS/torch/include" "$SITEPKGS/mlx/include"

    # 2. .pyi stubs are for type checkers, with one runtime exception:
    #    lazy_loader.attach_stub() PARSES a package's .pyi during import, so
    #    deleting librosa's stubs breaks "import parakeet_mlx" outright.
    #    Keep every stub sitting next to a module that calls attach_stub.
    #    (grep exits 1 when nothing matches, which pipefail would read as a
    #    build failure; an empty keep-list just means "delete every stub".)
    KEEP_STUBS="$RUNTIME_ROOT/.keep-stubs"
    { grep -rl "attach_stub" "$SITEPKGS" --include='*.py' 2>/dev/null || true; } \
        | sed 's|/[^/]*$||' | sort -u > "$KEEP_STUBS"
    find "$SITEPKGS" -name '*.pyi' | while read -r f; do
        grep -qxF "${f%/*}" "$KEEP_STUBS" || rm -f "$f"
    done
    rm -f "$KEEP_STUBS"

    # 3. Vendored test suites (scipy, numpy, numba, sklearn, sympy, PyObjC).
    find "$SITEPKGS" -maxdepth 3 -type d \( -name tests -o -name test \) \
        -prune -exec rm -rf {} +
    rm -rf "$SITEPKGS/PyObjCTest"

    # 4. pip and setuptools. The bundle installs nothing on the user's Mac:
    #    tools/bootstrap.sh returns at once when this runtime is present.
    rm -rf "$SITEPKGS"/pip "$SITEPKGS"/pip-*.dist-info \
           "$SITEPKGS"/setuptools "$SITEPKGS"/setuptools-*.dist-info \
           "$SITEPKGS"/pkg_resources "$SITEPKGS"/_distutils_hack \
           "$SITEPKGS"/distutils-precedence.pth

    # 5. torch ships two identical 4 MB copies of protoc. Its neighbour
    #    torch_shm_manager is NOT optional: "import torch" raises
    #    "Unable to find torch_shm_manager" without it, so name protoc alone.
    rm -f "$SITEPKGS"/torch/bin/protoc "$SITEPKGS"/torch/bin/protoc-*

    # 6. speechbrain unpacks its repository docs/build/recipes into site-packages.
    rm -rf "$SITEPKGS/docs" "$SITEPKGS/build" "$SITEPKGS/recipes"

    find "$RUNTIME" -name "__pycache__" -type d -prune -exec rm -rf {} +
    # NOT pruned, and the reason: sympy, mpmath and networkx are torch's own
    # dependencies. torch imports them only for compile/fx/export paths that
    # ECAPA's eager forward never enters, so deleting them passes today's
    # tests, but they are worth 5.8 MB compressed against a real chance of a
    # future ImportError deep inside torch. Not a trade worth making.
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

# --- 3b. notarize the app (when release-signed and credentials exist) --------
# Order matters: staple the app FIRST, then package, so the DMG and the
# tarball both carry the ticket and installs verify fully offline.
# NOTE: Authority= lines only appear at codesign verbosity 2 (-dvv); -dv
# prints none and would silently skip notarization on every release. The
# check is deliberately pipeline-free: under pipefail, `codesign | grep -q`
# can read as false when grep's early exit SIGPIPEs codesign.
NOTARIZE=0
RELEASE_SIGNED=0
SIG_INFO="$(codesign -dvv "$APP" 2>&1)" || true
case "$SIG_INFO" in
*"Authority=Developer ID Application"*) IS_RELEASE_SIG=1 ;;
*) IS_RELEASE_SIG=0 ;;
esac
if [ "$IS_RELEASE_SIG" = 1 ]; then
    RELEASE_SIGNED=1
    PROFILE="${MS_NOTARY_PROFILE:-meetingscribe-notary}"
    HIST_ERR="$(xcrun notarytool history --keychain-profile "$PROFILE" 2>&1 >/dev/null)" \
        && NOTARIZE=1 || true
    if [ "$NOTARIZE" = 0 ]; then
        if printf '%s' "$HIST_ERR" | grep -q "No Keychain password item found"; then
            echo "WARNING: app is Developer ID signed but the notary keychain profile"
            echo "         '$PROFILE' is missing. Run:"
            echo "           xcrun notarytool store-credentials meetingscribe-notary \\"
            echo "             --apple-id khatrisharique7@gmail.com --team-id 5VJ8KXLF45"
            [ "${MS_ALLOW_UNNOTARIZED:-0}" = 1 ] \
                || { echo "Refusing to build unnotarized release artifacts at the release"; \
                     echo "paths (set MS_ALLOW_UNNOTARIZED=1 to override)."; exit 1; }
        else
            echo "ERROR: cannot reach the Apple notary service to verify credentials:"
            printf '%s\n' "$HIST_ERR" | sed 's/^/  /'
            echo "Notarization needs the network anyway — fix this and re-run"
            echo "(or set MS_ALLOW_UNNOTARIZED=1 to build unnotarized artifacts)."
            [ "${MS_ALLOW_UNNOTARIZED:-0}" = 1 ] || exit 1
        fi
    fi
else
    echo "NOTE: ad-hoc build — skipping notarization (downloads must be approved"
    echo "      in System Settings > Privacy & Security > Open Anyway)."
fi
if [ "$NOTARIZE" = 1 ]; then
    bash "$PROJECT/tools/notarize.sh" app "$APP"
fi

# --- 4. package: DMG + tarball -----------------------------------------------
DMG="$PROJECT/dist/$APP_NAME.dmg"
TARBALL="$PROJECT/dist/$APP_NAME.app.tar.gz"
mkdir -p "$PROJECT/dist"

# UDIF compression. Measured on this bundle (1231 MB app, same content, same
# machine): UDZO/zlib 377 MB, UDZO with zlib-level=9 331 MB, ULFO/lzfse 303 MB,
# UDBZ/bzip2 282 MB, ULMO/lzma 221 MB. lzma wins by 156 MB, which is the whole
# point: the first complaint about the 3.1 release was the size of the
# download. It costs about two extra minutes to build here and about 17 extra
# seconds for the user's drag-to-Applications copy (13 s -> 30 s, measured),
# and it needs macOS 10.15+ to mount, which this app already requires.
# Set MS_DMG_FORMAT=ULFO for the middle ground, or UDZO to go back.
DMG_FORMAT="${MS_DMG_FORMAT:-ULMO}"

echo "Compressing $DMG ($DMG_FORMAT)…"
ln -sfn /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format "$DMG_FORMAT" \
    -fs HFS+ "$DMG" >/dev/null
rm -f "$STAGE/Applications"

echo "Compressing $TARBALL…"
rm -f "$TARBALL"
tar -czf "$TARBALL" -C "$STAGE" "$APP_NAME.app"

# --- 5. sign, notarize + staple the DMG itself --------------------------------
# The image itself must be Developer ID signed: spctl assesses the DMG's
# primary signature, and an unsigned image is "no usable signature" even
# after a successful notarization.
if [ "$RELEASE_SIGNED" = 1 ]; then
    DMG_IDENTITY="${MS_SIGN_IDENTITY:-}"
    if [ -z "$DMG_IDENTITY" ] || [ "$DMG_IDENTITY" = "-" ]; then
        DMG_IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
            | awk -F'"' '/Developer ID Application/{if (!found) {id=$2; found=1}} END{if (found) print id}')"
    fi
    if [ -n "$DMG_IDENTITY" ]; then
        echo "Signing the disk image…"
        codesign --force --sign "$DMG_IDENTITY" --timestamp "$DMG"
    fi
fi
if [ "$NOTARIZE" = 1 ]; then
    bash "$PROJECT/tools/notarize.sh" dmg "$DMG"
fi

echo
echo "Built:"
du -sh "$APP" "$DMG" "$TARBALL"
if [ "$NOTARIZE" = 1 ]; then
    echo "Release-ready: Developer ID signed, notarized, stapled (app + DMG)."
else
    echo "Local build only: NOT notarized — do not ship this as a release."
fi
