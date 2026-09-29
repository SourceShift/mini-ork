#!/usr/bin/env python3
import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import RECIPE_DIR, REVIEW_LIB, RUN_DIR, input_file, input_many, run

groups = input_many("group_extractions", ".json")
argv = [REVIEW_LIB, "pack", "--corpus", input_file("source_corpus", "source-corpus.json"),
        "--state", os.environ.get("MO_RSI_STATE_FILE") or os.path.join(RECIPE_DIR, "context", "miniork-rsi-state.md"),
        "--unclustered", os.path.join(RUN_DIR, "unclustered.json"),
        "--out", os.path.join(RUN_DIR, "review-pack.json")]
for path in groups:
    argv += ["--group", path]
run(argv, "pack", expect=(len(groups), 10))
