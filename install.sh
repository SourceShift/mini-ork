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
ORIG_DIR=$(pwd)

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
    for candidate in python3.13 python3.12 python3.11 python3; do
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

# ui/ is the IDE crate (SourceShift/mini-ork-ui). Nothing in the Python runtime
# reads it, so this is best-effort: a clone without it still installs, it just
# leaves ui/ empty until someone runs the same command by hand.
if ! git -C "$TARGET" submodule update --init --recursive --depth 1; then
    echo "mini-ork: could not fetch the ui/ submodule; the IDE crate stays empty" >&2
    echo "  (the Python runtime does not need it) — retry with:" >&2
    echo "  git -C $TARGET submodule update --init --recursive" >&2
fi

if [ "${INSTALL_SYSTEM_DEPS:-1}" = "1" ] && [ -e "$TARGET/scripts/install-system-deps.sh" ]; then
    if command -v bash >/dev/null 2>&1; then
        bash "$TARGET/scripts/install-system-deps.sh"
    else
        echo "mini-ork: scripts/install-system-deps.sh needs bash; install bash or re-run with INSTALL_SYSTEM_DEPS=0" >&2
        exit 1
    fi
fi

cd "$TARGET"
echo "mini-ork: installing into $TARGET (python: $FOUND_PY)" >&2
"$FOUND_PY" scripts/full_install.py "$@" || exit $?

# Offer the first-run wizard on a real terminal, the way openclaw's installer
# does. In a piped install (`curl … | sh`) stdin is the pipe, so prompt through
# /dev/tty; with no controlling terminal (CI, Docker build) skip silently. Set
# MINI_ORK_NO_ONBOARD=1 to skip it even interactively.
TTY=""
if [ -t 0 ] && [ -t 1 ]; then
    TTY=stdio
elif ( : </dev/tty ) 2>/dev/null && ( : >/dev/tty ) 2>/dev/null; then
    # Permission bits are not enough: with no controlling terminal the node is
    # readable/writable but every open fails with ENXIO ("Device not
    # configured"), which killed the install under a test runner or a
    # container. Open it once both ways before committing to it.
    #
    # Each probe runs in a subshell on purpose. `:` is a POSIX special
    # builtin, and a redirection error on one makes a non-interactive shell
    # exit outright — dash does exactly this, so the brace-group form exited
    # the whole installer with status 2 before it could reach the "no tty,
    # skip" branch. In a subshell the exit is contained and the condition is
    # merely false.
    TTY=/dev/tty
fi
if [ -n "$TTY" ] && [ "${MINI_ORK_NO_ONBOARD:-0}" != "1" ]; then
    PY_BIN="$FOUND_PY"
    if [ -x "$TARGET/.venv/bin/python" ]; then
        PY_BIN="$TARGET/.venv/bin/python"
    fi
    PROMPT='mini-ork: run the first-run wizard now? It initializes a workspace and offers the optional ContextNest and Zed components. [Y/n] '
    if [ "$TTY" = "stdio" ]; then
        printf '\n%s' "$PROMPT" >&2
        read -r ANSWER || ANSWER=n
    else
        printf '\n%s' "$PROMPT" >"$TTY"
        read -r ANSWER <"$TTY" || ANSWER=n
    fi
    case "$ANSWER" in
        ""|y|Y|yes|YES)
            # The wizard initializes the directory the user ran the installer
            # from, not the engine checkout this script cd'ed into.
            cd "$ORIG_DIR"
            "$PY_BIN" -m mini_ork.cli.onboard
            ;;
    esac
fi
