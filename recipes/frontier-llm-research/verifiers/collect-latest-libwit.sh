#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
RECIPE_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"

python3 "$RECIPE_DIR/lib/research_pipeline.py" collect \
  --plan "$RECIPE_DIR/collection-plan.json" \
  --output "$RUN_DIR/source-corpus.json"
