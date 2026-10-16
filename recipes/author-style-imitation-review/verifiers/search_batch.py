#!/usr/bin/env python3
"""One searcher agent: a single LibWit batch call over this node's ten queries.

The node's batch index comes from the filename the engine staged under the
`query_batch` input (batch-01.json -> partial-01.json), so the same wrapper
serves every searcher node and the output stays tied to the wired edge.
"""

import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import COLLECTION_PLAN, RUN_DIR, STYLE_LIB, input_single, run

batch = input_single("query_batch", ".json")
stem = os.path.basename(batch).rsplit(".", 1)[0]  # batch-01
index = stem.split("-", 1)[1]
if not index.isdigit():
    _sys.stderr.write(f"searcher: cannot derive a batch index from {batch!r}\n")
    _sys.exit(1)

run(
    [STYLE_LIB, "search", "--batch", batch, "--plan", COLLECTION_PLAN,
     "--out", os.path.join(RUN_DIR, "corpus", f"partial-{index}.json")],
    f"search-{index}",
)
