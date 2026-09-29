#!/usr/bin/env python3
import os
from _stage import REVIEW_LIB, RUN_DIR, input_file, input_many, run

extractions = input_many("shard_extractions", ".json")
argv = [REVIEW_LIB, "catalog", "--corpus", input_file("source_corpus", "source-corpus.json"),
        "--catalog-out", os.path.join(RUN_DIR, "technique-catalog.json"),
        "--papers-out", os.path.join(RUN_DIR, "paper-index.json")]
for path in extractions:
    argv += ["--extraction", path]
run(argv, "catalog", expect=(len(extractions), 10))
