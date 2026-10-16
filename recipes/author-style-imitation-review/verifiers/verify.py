#!/usr/bin/env python3
"""Completeness verdict: sections, group coverage, and citation grounding.

Every arXiv id the report cites must exist in the collected corpus, so a
hallucinated or drifted citation fails the run instead of shipping.
"""

import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import RUN_DIR, STYLE_LIB, run

run(
    [STYLE_LIB, "verify",
     "--final", os.path.join(RUN_DIR, "author-style-imitation-review.md"),
     "--merged", os.path.join(RUN_DIR, "merged-review.json"),
     "--corpus", os.path.join(RUN_DIR, "source-corpus.json")],
    "verify",
)
