#!/usr/bin/env python3
import os
from _stage import REVIEW_LIB, RUN_DIR, input_file, run

run([REVIEW_LIB, "assemble",
     "--report", input_file("impact_report", "impact-report.md"),
     "--merged", input_file("merged_review", "merged-review.json"),
     "--corpus", input_file("source_corpus", "source-corpus.json"),
     "--out", os.path.join(RUN_DIR, "rsi-technique-review.md")], "assemble")
