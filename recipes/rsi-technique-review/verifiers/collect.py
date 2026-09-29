#!/usr/bin/env python3
import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import FRONTIER_LIB, RECIPE_DIR, RUN_DIR, run

plan = os.environ.get("MINI_ORK_COLLECTION_PLAN") or os.path.join(RECIPE_DIR, "collection-plan.json")
run([FRONTIER_LIB, "collect", "--plan", plan, "--output", os.path.join(RUN_DIR, "source-corpus.json")], "collect")
