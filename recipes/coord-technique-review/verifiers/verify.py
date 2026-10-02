#!/usr/bin/env python3
import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import REVIEW_LIB, RUN_DIR, run

run([REVIEW_LIB, "verify",
     "--final", os.path.join(RUN_DIR, "coord-technique-review.md"),
     "--merged", os.path.join(RUN_DIR, "merged-review.json"),
     "--corpus", os.path.join(RUN_DIR, "source-corpus.json")], "verify")
