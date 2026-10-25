#!/usr/bin/env python3
"""Render the cycle's machine brief into the two artifacts the lenses read.

This is where provenance is removed. A lens that can see `source` learns that
this gap came from `db:L10` rather than `probe:garden`, and starts favouring
gap sources instead of judging the evidence in front of it. The queue already
ranked by severity; the panel's job is diagnosis, not re-ranking.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (RUN_DIR, as_list, brief_path, fail, read_json, write_json)

STAGE = "brief"
PROVENANCE_KEYS = ("source", "evidence_key", "signature", "candidate_id")


def main() -> None:
    brief = read_json(brief_path(), STAGE)
    candidate = brief.get("candidate")
    constraints = brief.get("constraints") or {}
    if not isinstance(candidate, dict):
        fail(STAGE, "brief.candidate is missing or not an object")
    title = str(candidate.get("title") or "").strip()
    if not title:
        fail(STAGE, "brief.candidate.title is empty; there is nothing to diagnose")
    command = str(constraints.get("verification_command")
                  or candidate.get("success_command") or "").strip()
    if not command:
        fail(STAGE, "the candidate has no success command; a fix would be unverifiable")
    repo_root = str(constraints.get("repo_root") or "").strip()
    if not repo_root or not os.path.isdir(repo_root):
        fail(STAGE, f"constraints.repo_root {repo_root!r} is not a directory")

    evidence = {k: v for k, v in (candidate.get("evidence") or {}).items()
                if k not in PROVENANCE_KEYS}

    lines = [
        f"# RSI gap: {title}",
        "",
        f"Domain: **{candidate.get('domain', '?')}** · kind: `{candidate.get('kind', '?')}` · "
        f"severity: {candidate.get('severity', '?')}",
        "",
        "## Evidence",
        "",
        "```json",
        json.dumps(evidence, indent=2, sort_keys=True),
        "```",
        "",
        "## What the harvester recorded",
        "",
        str(candidate.get("detail") or "(no detail)").strip(),
        "",
        f"- Files the evidence points at: {', '.join(as_list(candidate.get('files'))) or '(none)'}",
        f"- Suggested direction: {str(candidate.get('fix_hint') or '(none)').strip()}",
        f"- Success looks like: {str(candidate.get('success_metric') or '(none)').strip()}",
        "",
        "## Hard constraints",
        "",
        f"- The repository root is {repo_root}.",
        f"- The gap is closed when `{command}` reports success where it now does not.",
        f"- At most {constraints.get('max_files', 5)} files may change.",
        "- Paths are repo-relative. Do not propose an absolute path or one containing `..`.",
        "",
        "## Where the run lives",
        "",
        f"- Run directory: {RUN_DIR}",
        "- Write answers as JSON only; prose outside the JSON block is discarded.",
        "",
    ]
    with open(os.path.join(RUN_DIR, "brief.md"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))

    write_json("candidates.json", {
        "cycle_id": brief.get("cycle_id", ""),
        "selected": candidate.get("candidate_id", ""),
        "candidates": brief.get("candidates") or [],
        "candidate": candidate,
        "constraints": constraints,
    }, "rsi-cycle-brief")


if __name__ == "__main__":
    main()
