#!/usr/bin/env python3
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _stage import HARNESS_LIB, input_file, run

run([HARNESS_LIB, "verify",
     "--outline", input_file("book_outline", "book-outline.json"),
     "--compendium", input_file("technique_compendium", "technique-compendium.md"),
     "--corpus", input_file("source_corpus", "source-corpus.json")], "verify")
