#!/usr/bin/env python3
"""Merge the two independent impact reviews into one ranked list."""

import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import RUN_DIR, STYLE_LIB, input_file, input_many, run

REVIEWERS = 2

reviews = input_many("impact_reviews", ".json")
argv = [STYLE_LIB, "merge-reviews",
        "--pack", input_file("review_pack", "review-pack.json"),
        "--out", os.path.join(RUN_DIR, "merged-review.json")]
for path in reviews:
    argv += ["--review", path]
run(argv, "merge-reviews", expect=(len(reviews), REVIEWERS))
