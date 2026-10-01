#!/bin/sh
# Compatibility entrypoint for macOS, Linux, and WSL.
# The cross-platform installer itself lives in mini_ork.cli.install_command.
# One-liner usage:
#   curl -fsSL https://raw.githubusercontent.com/SourceShift/mini-ork/main/install.sh | sh
# Knobs:
#   MINI_ORK_INSTALL_DIR — install target (default: $HOME/.local/share/mini-ork)
#   MINI_ORK_REPO_URL    — git URL (default: https://github.com/SourceShift/mini-ork.git)
#   MINI_ORK_REF         — git ref to clone/checkout (default: main)
#   INSTALL_SYSTEM_DEPS  — set to 0 to skip scripts/install-system-deps.sh
#   PYTHON               — override the Python interpreter to use
set -eu

DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)

# Checkout mode: script lives next to a real mini-ork checkout.
if [ -e "$DIR/bin/mini-ork" ] && [ -e "$DIR/scripts/full_install.py" ]; then
    exec python3 "$DIR/bin/mini-ork" install "$@"
fi

# Remote mode: piped from curl, $0 is sh, dirname is ".". Resolve Python.
FOUND_PY=""
if [ -n "${PYTHON:-}" ]; then
    if "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
        FOUND_PY="$PYTHON"
    else
        echo "mini-ork needs Python 3.11 or newer (found: $PYTHON)" >&2
        exit 1
    fi
else
    TRIED=""
    for candidate in python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            TRIED="$TRIED $candidate"
            if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
                FOUND_PY="$candidate"
                break
            fi
        fi
    done
    if [ -z "$FOUND_PY" ]; then
        echo "mini-ork needs Python 3.11 or newer (found:${TRIED:- none})" >&2
        exit 1
    fi
fi

if ! command -v git >/dev/null 2>&1; then
    echo "mini-ork install needs git" >&2
    exit 1
fi

TARGET=${MINI_ORK_INSTALL_DIR:-$HOME/.local/share/mini-ork}
URL=${MINI_ORK_REPO_URL:-https://github.com/SourceShift/mini-ork.git}
REF=${MINI_ORK_REF:-main}

if [ -e "$TARGET/scripts/full_install.py" ]; then
    # Existing checkout — try to fast-forward. Warn and continue on failure so an
    # in-place upgrade over a dirty local checkout still gets a usable install.
    FETCH_OK=1
    if ! git -C "$TARGET" fetch --depth 1 origin "$REF"; then
        FETCH_OK=0
    fi
    MERGE_OK=1
    if [ "$FETCH_OK" = "1" ] && ! git -C "$TARGET" merge --ff-only FETCH_HEAD; then
        MERGE_OK=0
    fi
    if [ "$FETCH_OK" = "0" ] || [ "$MERGE_OK" = "0" ]; then
        echo "could not fast-forward $TARGET; local changes? leaving it as is" >&2
    fi
elif [ -e "$TARGET" ] && [ -n "$(ls -A "$TARGET" 2>/dev/null || true)" ]; then
    echo "$TARGET exists and is not a mini-ork checkout; set MINI_ORK_INSTALL_DIR" >&2
    exit 1
else
    mkdir -p "$(dirname "$TARGET")"
    git clone --depth 1 --branch "$REF" "$URL" "$TARGET"
fi

if [ "${INSTALL_SYSTEM_DEPS:-1}" = "1" ] && [ -e "$TARGET/scripts/install-system-deps.sh" ]; then
    sh "$TARGET/scripts/install-system-deps.sh"
fi

cd "$TARGET"
echo "mini-ork: installing into $TARGET (python: $FOUND_PY)" >&2
exec "$FOUND_PY" scripts/full_install.py "$@"
