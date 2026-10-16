#!/usr/bin/env python3
"""Union the searcher outputs into the one audited source corpus."""

import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import COLLECTION_PLAN, RUN_DIR, STYLE_LIB, input_many, run

BATCHES = int(os.environ.get("MO_STYLE_SEARCH_BATCHES", "5"))

partials = input_many("partial_corpora", ".json")
argv = [STYLE_LIB, "merge", "--plan", COLLECTION_PLAN,
        "--out", os.path.join(RUN_DIR, "source-corpus.json")]
for path in partials:
    argv += ["--partial", path]
run(argv, "merge", expect=(len(partials), BATCHES))
