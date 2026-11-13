#!/usr/bin/env python3
import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import REVIEW_LIB, RUN_DIR, input_file, input_many, run

reviews = input_many("impact_reviews", ".json")
argv = [REVIEW_LIB, "merge-reviews", "--pack", input_file("review_pack", "review-pack.json"),
        "--out", os.path.join(RUN_DIR, "merged-review.json")]
for path in reviews:
    argv += ["--review", path]
run(argv, "merge-reviews", expect=(len(reviews), 2))
