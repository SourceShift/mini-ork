#!/usr/bin/env python3
import os
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import HARNESS_LIB, RECIPE_DIR, RUN_DIR, input_file, run

config = os.environ.get("MINI_ORK_BOOK_CONFIG") or os.path.join(RECIPE_DIR, "book.json")
run([HARNESS_LIB, "book-spec",
     "--techniques", input_file("techniques", "techniques.json"),
     "--corpus", input_file("source_corpus", "source-corpus.json"),
     "--config", config,
     "--outline-out", os.path.join(RUN_DIR, "book-outline.json"),
     "--brief-out", os.path.join(RUN_DIR, "book-brief.md")], "book-spec")
