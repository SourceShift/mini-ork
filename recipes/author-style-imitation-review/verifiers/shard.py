#!/usr/bin/env python3
"""Deal the corpus into equal, audited per-extractor shards."""

import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import RUN_DIR, STYLE_LIB, input_file, run

SHARDS = int(os.environ.get("MO_STYLE_SHARD_COUNT", "10"))

argv = [STYLE_LIB, "shard", "--input", input_file("source_corpus", "source-corpus.json")]
for i in range(1, SHARDS + 1):
    argv += ["--output", os.path.join(RUN_DIR, "shards", f"source-shard-{i:02d}.json")]
run(argv, "shard")
