#!/usr/bin/env python3
import os
from _stage import FRONTIER_LIB, RUN_DIR, input_file, run

argv = [FRONTIER_LIB, "shard", "--input", input_file("source_corpus", "source-corpus.json")]
for i in range(1, 11):
    argv += ["--output", os.path.join(RUN_DIR, "shards", f"source-shard-{i:02d}.json")]
run(argv, "shard")
