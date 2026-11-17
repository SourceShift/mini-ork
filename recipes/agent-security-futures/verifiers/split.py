#!/usr/bin/env python3
import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import REVIEW_LIB, RUN_DIR, input_file, run

run([REVIEW_LIB, "split",
     "--clusters", input_file("technique_clusters", "clusters.json"),
     "--catalog", input_file("technique_catalog", "technique-catalog.json"),
     "--papers", input_file("paper_index", "paper-index.json"),
     "--out-dir", os.path.join(RUN_DIR, "groups"),
     "--unclustered-out", os.path.join(RUN_DIR, "unclustered.json")], "split")
