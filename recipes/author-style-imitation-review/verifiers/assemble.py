#!/usr/bin/env python3
"""Append the deterministic appendices (ranked table, per-group evidence)."""

import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import RUN_DIR, STYLE_LIB, input_file, run

run(
    [STYLE_LIB, "assemble",
     "--report", input_file("technique_report", "technique-report.md"),
     "--merged", input_file("merged_review", "merged-review.json"),
     "--corpus", input_file("source_corpus", "source-corpus.json"),
     "--out", os.path.join(RUN_DIR, "author-style-imitation-review.md")],
    "assemble",
)
