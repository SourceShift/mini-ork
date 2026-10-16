#!/usr/bin/env python3
"""Split the collection plan into one bounded file per searcher agent."""

import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import COLLECTION_PLAN, RUN_DIR, STYLE_LIB, run

run(
    [STYLE_LIB, "plan-batches", "--plan", COLLECTION_PLAN,
     "--out-dir", os.path.join(RUN_DIR, "queries")],
    "plan-batches",
)
