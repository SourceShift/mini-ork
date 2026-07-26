#!/bin/sh
# Compatibility entrypoint for macOS, Linux, and WSL.
# The cross-platform installer itself lives in mini_ork.cli.install_command.
set -eu

MINI_ORK_REPO=$(CDPATH= cd "$(dirname "$0")" && pwd)
exec python3 "$MINI_ORK_REPO/bin/mini-ork" install "$@"
