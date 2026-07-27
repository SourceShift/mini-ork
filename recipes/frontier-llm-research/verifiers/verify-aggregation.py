#!/usr/bin/env python3
# Python port of verify-aggregation.sh (bash-removal WS8). Same rc semantics,
# env vars, and output text.

import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RECIPE_DIR = os.path.dirname(SCRIPT_DIR)
INPUT_DIR = os.environ["MINI_ORK_NODE_INPUT_DIR"]

rc = subprocess.run(
    [sys.executable, os.path.join(RECIPE_DIR, "lib", "research_pipeline.py"), "verify",
     "--aggregation", os.path.join(INPUT_DIR, "aggregation", "aggregation.md")],
    check=False,
)
if rc.returncode != 0:
    sys.exit(rc.returncode)
print('{"verifier":"frontier-research-completeness","pass":true}')
