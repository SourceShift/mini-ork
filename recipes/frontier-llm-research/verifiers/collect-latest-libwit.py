#!/usr/bin/env python3
# Python port of collect-latest-libwit.sh (bash-removal WS8). Same rc semantics,
# env vars, and output text.

import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RECIPE_DIR = os.path.dirname(SCRIPT_DIR)
RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]

rc = subprocess.run(
    [sys.executable, os.path.join(RECIPE_DIR, "lib", "research_pipeline.py"), "collect",
     "--plan", os.path.join(RECIPE_DIR, "collection-plan.json"),
     "--output", os.path.join(RUN_DIR, "source-corpus.json")],
    check=False,
)
if rc.returncode != 0:
    sys.exit(rc.returncode)
print('{"verifier":"frontier-research-collection","pass":true}')
