#!/usr/bin/env python3
"""Merge the shard extractions into the technique catalog + paper index.

This is the single coverage gate of the run: below the floor (default 95% of the
corpus) the node fails rather than let a partial reading masquerade as complete.
"""

import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import RUN_DIR, STYLE_LIB, input_file, input_many, run

SHARDS = int(os.environ.get("MO_STYLE_SHARD_COUNT", "10"))

extractions = input_many("shard_extractions", ".json")
argv = [STYLE_LIB, "catalog",
        "--corpus", input_file("source_corpus", "source-corpus.json"),
        "--catalog-out", os.path.join(RUN_DIR, "technique-catalog.json"),
        "--papers-out", os.path.join(RUN_DIR, "paper-index.json")]
for path in extractions:
    argv += ["--extraction", path]
run(argv, "catalog", expect=(len(extractions), SHARDS))
