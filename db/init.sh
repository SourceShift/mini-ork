#!/usr/bin/env bash
# Compatibility entrypoint; the migration engine is mini_ork.stores.migrate.
set -euo pipefail

ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m mini_ork.stores.migrate "$@"
