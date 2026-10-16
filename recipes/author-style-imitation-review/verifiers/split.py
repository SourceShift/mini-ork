#!/usr/bin/env python3
"""Validate the clusterer's grouping and write one bounded file per group."""

import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import RUN_DIR, STYLE_LIB, input_file, run

run(
    [STYLE_LIB, "split",
     "--clusters", input_file("technique_clusters", "clusters.json"),
     "--catalog", input_file("technique_catalog", "technique-catalog.json"),
     "--papers", input_file("paper_index", "paper-index.json"),
     "--out-dir", os.path.join(RUN_DIR, "groups"),
     "--unclustered-out", os.path.join(RUN_DIR, "unclustered.json")],
    "split",
)
