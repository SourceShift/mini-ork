"""Shared plumbing for the recipe's deterministic verifier nodes."""

import os
import subprocess
import sys

VERIFIER_DIR = os.path.dirname(os.path.abspath(__file__))
RECIPE_DIR = os.path.dirname(VERIFIER_DIR)
FRONTIER_LIB = os.path.join(os.path.dirname(RECIPE_DIR), "frontier-llm-research", "lib", "research_pipeline.py")
REVIEW_LIB = os.path.join(RECIPE_DIR, "lib", "rsi_review.py")
RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
INPUT_DIR = os.environ.get("MINI_ORK_NODE_INPUT_DIR", "")


def input_file(name, filename):
    return os.path.join(INPUT_DIR, name, filename)


def input_many(name, suffix):
    folder = os.path.join(INPUT_DIR, name)
    if not os.path.isdir(folder):
        return []
    return [os.path.join(folder, f) for f in sorted(os.listdir(folder)) if f.endswith(suffix)]


def run(argv, stage, expect=None):
    if expect is not None and expect[0] != expect[1]:
        sys.stderr.write(f"{stage}: expected {expect[1]} input artifacts, got {expect[0]}\n")
        sys.exit(1)
    rc = subprocess.run([sys.executable] + argv, check=False).returncode
    if rc != 0:
        sys.exit(rc)
    print('{"verifier":"rsi-review-%s","pass":true}' % stage)
