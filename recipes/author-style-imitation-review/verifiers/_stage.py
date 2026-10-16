"""Shared plumbing for the recipe's deterministic verifier nodes.

Every verifier node is a thin wrapper: it resolves the artifacts the engine
staged under ``MINI_ORK_NODE_INPUT_DIR``, shells out to one subcommand of
``lib/style_pipeline.py``, and lets a non-zero exit fail the node. Keeping the
logic in one library means a stage can be replayed by hand with the same argv
the node used.
"""

import os
import subprocess
import sys

VERIFIER_DIR = os.path.dirname(os.path.abspath(__file__))
RECIPE_DIR = os.path.dirname(VERIFIER_DIR)
STYLE_LIB = os.path.join(RECIPE_DIR, "lib", "style_pipeline.py")
COLLECTION_PLAN = os.environ.get("MINI_ORK_COLLECTION_PLAN") or os.path.join(
    RECIPE_DIR, "collection-plan.json"
)
STATE_FILE = os.environ.get("MO_STYLE_STATE_FILE") or os.path.join(
    RECIPE_DIR, "context", "style-imitation-state.md"
)
RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
INPUT_DIR = os.environ.get("MINI_ORK_NODE_INPUT_DIR", "")


def input_file(name, filename):
    return os.path.join(INPUT_DIR, name, filename)


def input_many(name, suffix):
    folder = os.path.join(INPUT_DIR, name)
    if not os.path.isdir(folder):
        return []
    return [os.path.join(folder, f) for f in sorted(os.listdir(folder)) if f.endswith(suffix)]


def input_single(name, suffix):
    """The single staged artifact under this input name, or a hard failure.

    A node whose edge supplies exactly one upstream artifact cannot guess which
    one it got: taking the filename from the staged directory keeps the wrapper
    identical for all five searchers while staying bound to what the engine
    actually wired.
    """
    files = input_many(name, suffix)
    if len(files) != 1:
        sys.stderr.write(f"expected exactly one {suffix} under input {name!r}, found {len(files)}\n")
        sys.exit(1)
    return files[0]


def run(argv, stage, expect=None):
    if expect is not None and expect[0] != expect[1]:
        sys.stderr.write(f"{stage}: expected {expect[1]} input artifacts, got {expect[0]}\n")
        sys.exit(1)
    rc = subprocess.run([sys.executable] + argv, check=False).returncode
    if rc != 0:
        sys.exit(rc)
    print('{"verifier":"style-review-%s","pass":true}' % stage)
