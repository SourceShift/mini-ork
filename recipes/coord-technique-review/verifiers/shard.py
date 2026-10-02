#!/usr/bin/env python3
import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import FRONTIER_LIB, RUN_DIR, input_file, run

argv = [FRONTIER_LIB, "shard", "--input", input_file("source_corpus", "source-corpus.json")]
for i in range(1, 11):
    argv += ["--output", os.path.join(RUN_DIR, "shards", f"source-shard-{i:02d}.json")]
run(argv, "shard")
