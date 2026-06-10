#!/usr/bin/env python3
"""Tolerant reviewer-verdict extraction.

Reviewer LLMs are asked to "Respond with JSON: {"verdict": ...}" but
routinely surround the JSON with prose (preamble like "Artifact read and
verified..." or trailing chat) — same failure class as D-011/D-016 in
bin/mini-ork-plan. A strict json.load on the review file then yields
verdict=unknown, which cascades: verifier fails review_verdict, FAIL_COUNT
increments, rollback fires, and a passing run is marked failed
(observed: run-1781105320-64712).

Strategy: strict parse first; on failure, balanced-brace scan the text and
take the LAST parseable object containing a "verdict" key (the answer
follows any schema echo from the prompt).

CLI: python3 extract_verdict.py FILE  → prints verdict or "unknown"; exit 0.
"""

from __future__ import annotations

import json
import sys


def _balanced_objects(s: str):
    i = 0
    while True:
        start = s.find("{", i)
        if start < 0:
            return
        depth = 0
        in_str = False
        esc = False
        for j in range(start, len(s)):
            c = s[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield s[start : j + 1]
                    i = j + 1
                    break
        else:
            return


def extract_review(text: str) -> dict | None:
    """Return the review dict (must contain a 'verdict' key), or None."""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "verdict" in obj:
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    found = None
    for chunk in _balanced_objects(text):
        try:
            obj = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("verdict"), str):
            found = obj
    return found


def extract_verdict(text: str) -> str:
    obj = extract_review(text)
    if obj is None:
        return "unknown"
    verdict = obj.get("verdict", "unknown")
    return verdict.strip() if isinstance(verdict, str) and verdict.strip() else "unknown"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("unknown")
        sys.exit(0)
    try:
        with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
            print(extract_verdict(f.read()))
    except OSError:
        print("unknown")
    sys.exit(0)
