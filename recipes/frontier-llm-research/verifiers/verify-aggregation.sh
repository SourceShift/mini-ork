#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
RECIPE_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
INPUT_DIR="${MINI_ORK_NODE_INPUT_DIR:?MINI_ORK_NODE_INPUT_DIR required}"

python3 "$RECIPE_DIR/lib/research_pipeline.py" verify \
  --aggregation "$INPUT_DIR/aggregation/aggregation.md"
printf '%s\n' '{"verifier":"frontier-research-completeness","pass":true}'
