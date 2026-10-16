#!/usr/bin/env python3
"""Merge the per-group extractions into the pack both reviewers read."""

import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import RUN_DIR, STATE_FILE, STYLE_LIB, input_file, input_many, run

GROUPS = int(os.environ.get("MO_STYLE_GROUP_COUNT", "8"))

groups = input_many("group_extractions", ".json")
argv = [STYLE_LIB, "pack",
        "--corpus", input_file("source_corpus", "source-corpus.json"),
        "--state", STATE_FILE,
        "--unclustered", os.path.join(RUN_DIR, "unclustered.json"),
        "--out", os.path.join(RUN_DIR, "review-pack.json")]
for path in groups:
    argv += ["--group", path]
run(argv, "pack", expect=(len(groups), GROUPS))
