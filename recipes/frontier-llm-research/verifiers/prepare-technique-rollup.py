#!/usr/bin/env python3
# Python port of prepare-technique-rollup.sh (bash-removal WS8). Same rc
# semantics, env vars, and output text.

import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RECIPE_DIR = os.path.dirname(SCRIPT_DIR)
RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
INPUT_DIR = os.environ["MINI_ORK_NODE_INPUT_DIR"]

inputs = []
summaries_dir = os.path.join(INPUT_DIR, "source_summaries")
if os.path.isdir(summaries_dir):
    for name in sorted(os.listdir(summaries_dir)):
        path = os.path.join(summaries_dir, name)
        if os.path.isfile(path) and name.endswith(".json"):
            inputs += ["--input", path]

if len(inputs) // 2 != 20:
    sys.stderr.write("expected ten summary JSON artifacts\n")
    sys.exit(1)

rc = subprocess.run(
    [sys.executable, os.path.join(RECIPE_DIR, "lib", "research_pipeline.py"), "rollup"]
    + inputs
    + ["--output", os.path.join(RUN_DIR, "technique-rollup.json")],
    check=False,
)
if rc.returncode != 0:
    sys.exit(rc.returncode)
print('{"verifier":"frontier-research-rollup","pass":true}')
