#!/usr/bin/env python3
import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import HARNESS_LIB, RUN_DIR, input_file, input_many, run

groups = input_many("group_extractions", ".json")
argv = [HARNESS_LIB, "compendium", "--corpus", input_file("source_corpus", "source-corpus.json"),
        "--compendium-out", os.path.join(RUN_DIR, "technique-compendium.md"),
        "--techniques-out", os.path.join(RUN_DIR, "techniques.json")]
for path in groups:
    argv += ["--group", path]
run(argv, "compendium", expect=(len(groups), 10))
