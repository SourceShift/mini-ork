#!/usr/bin/env python3
"""Assign every technique to a model-defined family, then write one file per group.

The clusterer defines the families; the assignment is mechanical and lives in
lib/style_pipeline.py. Both stages run here so the DAG keeps one deterministic
node after the clusterer and the assignment stays replayable by hand with the
argv printed below.
"""

import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import RUN_DIR, STYLE_LIB, input_file, run

run(
    [STYLE_LIB, "assign",
     "--families", input_file("technique_clusters", "families.json"),
     "--catalog", input_file("technique_catalog", "technique-catalog.json"),
     "--clusters-out", os.path.join(RUN_DIR, "clusters.json"),
     "--audit-out", os.path.join(RUN_DIR, "cluster-assignment.json")],
    "assign",
)

run(
    [STYLE_LIB, "split",
     "--clusters", os.path.join(RUN_DIR, "clusters.json"),
     "--catalog", input_file("technique_catalog", "technique-catalog.json"),
     "--papers", input_file("paper_index", "paper-index.json"),
     "--out-dir", os.path.join(RUN_DIR, "groups"),
     "--unclustered-out", os.path.join(RUN_DIR, "unclustered.json")],
    "split",
)
