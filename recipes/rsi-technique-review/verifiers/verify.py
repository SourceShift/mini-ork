#!/usr/bin/env python3
import os
from _stage import REVIEW_LIB, RUN_DIR, run

run([REVIEW_LIB, "verify",
     "--final", os.path.join(RUN_DIR, "rsi-technique-review.md"),
     "--merged", os.path.join(RUN_DIR, "merged-review.json"),
     "--corpus", os.path.join(RUN_DIR, "source-corpus.json")], "verify")
