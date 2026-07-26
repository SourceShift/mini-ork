#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
RECIPE_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
INPUT_DIR="${MINI_ORK_NODE_INPUT_DIR:?MINI_ORK_NODE_INPUT_DIR required}"

inputs=()
while IFS= read -r -d '' path; do
  inputs+=(--input "$path")
done < <(find "$INPUT_DIR/source_summaries" -maxdepth 1 -type f -name '*.json' -print0 | sort -z)

if [ "${#inputs[@]}" -ne 20 ]; then
  echo "expected ten summary JSON artifacts" >&2
  exit 1
fi

python3 "$RECIPE_DIR/lib/research_pipeline.py" rollup "${inputs[@]}" \
  --output "$RUN_DIR/technique-rollup.json"
