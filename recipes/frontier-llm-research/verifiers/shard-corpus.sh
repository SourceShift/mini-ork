#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
RECIPE_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
INPUT_DIR="${MINI_ORK_NODE_INPUT_DIR:?MINI_ORK_NODE_INPUT_DIR required}"

python3 "$RECIPE_DIR/lib/research_pipeline.py" shard \
  --input "$INPUT_DIR/source_corpus/source-corpus.json" \
  --output "$RUN_DIR/shards/source-shard-01.json" \
  --output "$RUN_DIR/shards/source-shard-02.json" \
  --output "$RUN_DIR/shards/source-shard-03.json" \
  --output "$RUN_DIR/shards/source-shard-04.json" \
  --output "$RUN_DIR/shards/source-shard-05.json" \
  --output "$RUN_DIR/shards/source-shard-06.json" \
  --output "$RUN_DIR/shards/source-shard-07.json" \
  --output "$RUN_DIR/shards/source-shard-08.json" \
  --output "$RUN_DIR/shards/source-shard-09.json" \
  --output "$RUN_DIR/shards/source-shard-10.json"
