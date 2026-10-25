#!/usr/bin/env python3
"""Render the cycle's critic brief: the diff, the evidence, the gates, the metric.

The diff is capped. A cycle's diff is one bounded change, but a bad run can
produce something enormous, and a truncated diff with a loud marker is far better
than a silently shortened one or a prompt that never fits: a critic that cannot
see the whole diff must know that.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import RUN_DIR, as_list, brief_path, fail, read_json

STAGE = "brief"
MAX_DIFF_CHARS = 60_000


def main() -> None:
    brief = read_json(brief_path(), STAGE)
    candidate = brief.get("candidate") or {}
    diff = str(brief.get("diff") or "")
    if not diff.strip():
        fail(STAGE, "the brief carries no diff; there is nothing to review")
    truncated = len(diff) > MAX_DIFF_CHARS
    shown = diff[:MAX_DIFF_CHARS] if truncated else diff

    gates = brief.get("gates") or []
    gate_lines = [f"- {g.get('id', '?')} {g.get('name', '')}: "
                  f"{'pass' if g.get('passed') else 'FAIL'}"
                  f"{' (' + str(g.get('code')) + ')' if g.get('code') else ''}"
                  f"{' — ' + str(g.get('detail')) if g.get('detail') else ''}"
                  for g in gates] or ["- (no gate results were recorded)"]

    lines = [
        f"# RSI review: {candidate.get('title', '(untitled candidate)')}",
        "",
        "## The gap",
        "",
        str(candidate.get("detail") or candidate.get("title") or "").strip(),
        "",
        f"- Success command: `{candidate.get('success_command', '?')}`",
        f"- Success metric: {str(candidate.get('success_metric') or '(none)').strip()}",
        f"- Evidence: ```json {json.dumps(candidate.get('evidence') or {}, sort_keys=True)} ```",
        "",
        "## Deterministic gates this diff already passed",
        "",
        *gate_lines,
        "",
        "These are necessary, not sufficient. Every one of them can be satisfied by "
        "a change that measures differently without fixing anything. That is the "
        "question you are here to answer.",
        "",
        "## Before / after",
        "",
        "```json",
        json.dumps(brief.get("metric") or {}, indent=2, sort_keys=True),
        "```",
        "",
        "## Files changed",
        "",
        *[f"- `{path}`" for path in as_list(brief.get("changed_files"))],
        "",
        "## Diff",
        "",
        "```diff",
        shown,
        "```",
        "",
    ]
    if truncated:
        lines += [
            f"> The diff was truncated at {MAX_DIFF_CHARS} characters "
            f"({len(diff)} total). Anything past this point is NOT shown. If your "
            "verdict depends on code you cannot see, say so and do not approve.",
            "",
        ]

    path = os.path.join(RUN_DIR, "critic-brief.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    print(json.dumps({"verifier": "rsi-critic-brief", "pass": True,
                      "diff_chars": len(diff), "truncated": truncated}))


if __name__ == "__main__":
    main()
