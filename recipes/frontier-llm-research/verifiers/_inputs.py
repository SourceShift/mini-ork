"""The staged-input boundary for this recipe's deterministic verifier nodes.

A verifier reads the artifacts its edges delivered under
``$MINI_ORK_NODE_INPUT_DIR/<input-name>/`` and writes its declared outputs
under ``$MINI_ORK_RUN_DIR``. The engine publishes both at the node boundary:
``mini-ork execute`` when it materializes the ledger, and ``mini-ork verify``
by recovering the boundary the run already wrote.

Running one of these scripts by hand has no run, so there is no boundary and
nothing to judge. Fail with that sentence instead of a KeyError traceback —
a missing boundary is a fact about the invocation, and the caller needs to
hear it.
"""

import os
import sys


def input_dir() -> str:
    """``$MINI_ORK_NODE_INPUT_DIR``, or exit 2 naming how to get one."""
    value = os.environ.get("MINI_ORK_NODE_INPUT_DIR", "")
    if value:
        return value
    name = os.path.basename(sys.argv[0]) or "verifier"
    sys.stderr.write(
        f"{name}: MINI_ORK_NODE_INPUT_DIR is not set — this is a recipe verifier "
        "node; run it through `mini-ork execute` or `mini-ork verify`, which "
        "publish the staged inputs\n")
    sys.exit(2)
