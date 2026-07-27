#!/usr/bin/env python3
# Python port of shard-corpus.sh (bash-removal WS8). Same rc semantics, env
# vars, and output text.

import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RECIPE_DIR = os.path.dirname(SCRIPT_DIR)
RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
INPUT_DIR = os.environ["MINI_ORK_NODE_INPUT_DIR"]

argv = [sys.executable, os.path.join(RECIPE_DIR, "lib", "research_pipeline.py"), "shard",
        "--input", os.path.join(INPUT_DIR, "source_corpus", "source-corpus.json")]
for i in range(1, 11):
    argv += ["--output", os.path.join(RUN_DIR, "shards", f"source-shard-{i:02d}.json")]

rc = subprocess.run(argv, check=False)
if rc.returncode != 0:
    sys.exit(rc.returncode)
print('{"verifier":"frontier-research-sharding","pass":true}')
