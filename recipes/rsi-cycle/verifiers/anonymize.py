#!/usr/bin/env python3
"""Reduce four lens answers to an anonymous panel.

Ordering is by content hash, not by filename, so the synthesiser cannot infer
which lane said what from position. Filenames track lanes; hashes do not.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (as_list, fail, first, input_many, read_json, write_json)

STAGE = "anonymize"
VIEW_KEYS = ("view", "summary", "answer", "diagnosis", "assessment")
MIN_LENSES = 3


def main() -> None:
    paths = input_many("lenses", ".json")
    if len(paths) < MIN_LENSES:
        fail(STAGE, f"{len(paths)} lens answer(s) arrived; at least {MIN_LENSES} are "
                     f"required for a panel (a run with two is one opinion twice)")

    entries = []
    for path in paths:
        answer = read_json(path, STAGE)
        view = str(first(answer, *VIEW_KEYS, default="")).strip()
        if not view:
            fail(STAGE, f"{os.path.basename(path)} has no view/summary field")
        entries.append({
            "view": view,
            "files": as_list(first(answer, "files", "files_to_touch", "proposed_files")),
            "risks": as_list(first(answer, "risks", "concerns", "blocking_findings")),
            "confidence": answer.get("confidence"),
        })

    # Content-hash order: stable across runs, independent of which lane served.
    entries.sort(key=lambda e: hashlib.sha256(
        (e["view"] + json.dumps(e["files"])).encode("utf-8")).hexdigest())
    for index, entry in enumerate(entries, start=1):
        entry["id"] = f"L{index}"

    write_json("lens-panel.json", {"lenses": entries, "count": len(entries)},
               "rsi-cycle-anonymize")


if __name__ == "__main__":
    main()
