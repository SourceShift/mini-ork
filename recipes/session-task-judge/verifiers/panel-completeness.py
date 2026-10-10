#!/usr/bin/env python3
# Completeness gate for the session-task-judge recipe.
#
# Two artifacts per lane, both graded:
#
#   judge-<lane>-tasks.md      the human report — the five required sections,
#                              in order, each task/solution block carrying a
#                              valid verdict + severity enum and a receipt.
#   context-<node>.json        the machine sidecar — ONE shared schema across
#                              all THREE lanes, so the synthesizer (and any
#                              later consumer) can join them mechanically
#                              instead of pattern-matching prose.
#
# Both halves are checked. The gate FAILS (non-zero rc) on any miss — rc IS
# the verdict, the runner reads rc not the JSON (inherited hard lesson from
# the audit panel: a gate that printed {"pass": false} and exited 0 could not
# fail at all).
#
# JSON verdict on stdout; rc mirrors it (0 = complete, 1 = miss).

import glob
import itertools
import json
import os
import re
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
EVIDENCE = os.path.join(RUN_DIR, "verifier-panel-completeness.log")
ev = open(EVIDENCE, "w", encoding="utf-8")

missing = []


def fail(why: str) -> None:
    missing.append(why)


# ── markdown reports ────────────────────────────────────────────────────────
LANES = [
    # (lane, node id, report name) — the sidecar name is what
    # `_researcher_output_file()` derives for a non-`_lens` researcher node.
    ("minimax", "minimax_judge", "judge-minimax-tasks.md"),
    ("glm", "glm_judge", "judge-glm-tasks.md"),
    ("deepseek", "deepseek_judge", "judge-deepseek-tasks.md"),
]
SECTIONS = [
    "Discovery Evidence",
    "Task Verdicts",
    "Solution Verdicts",
    "Recommended Execution Order",
    "Open Questions",
]
TASK_VERDICTS = {"positive_impact", "partially_positive", "stale", "unverifiable"}
SOLUTION_VERDICTS = {"sound", "partially_sound", "unsound", "unverifiable"}
VALID_VERDICTS = TASK_VERDICTS | SOLUTION_VERDICTS
VALID_SEVERITY = {"critical", "high", "medium", "low"}

# Receipts: git invocations, run-dir ids, repo paths with :line, kickoffs and
# recipes refs. Broader than the audit panel's SQL-shaped set because these
# judges verify against git + filesystem state, not a state DB.
EVIDENCE_RE = re.compile(
    r"git (log|show|status|diff|rev-parse|branch|ls-remote)"
    r"|run-1[0-9]{9}"
    r"|salvage"
    r"|kickoffs/"
    r"|recipes/"
    r"|\.mini-ork/"
    r"|[A-Za-z_/.-]+:[0-9]+",
    re.I,
)
# A FIELD value is only read from a line that STARTS with the field name —
# after an optional markdown list marker and/or blockquote marker — and may be
# wrapped in ``**`` or backticks on either side of the separator. Anchoring at
# the line is what keeps prose ("the severity of the stale task...") from
# being graded as a field value (inherited from the audit panel gate).
_LINE_START = r"^[ \t]*(?:[-*+>][ \t]+)?(?:>[ \t]*)?"
_FIELD = r"[*`]*%s[*`]*[ \t]*[:=][ \t]*[*`]*[ \t]*([A-Za-z_]+)"
VERDICT_RE = re.compile(_LINE_START + _FIELD % "verdict", re.I | re.M)
SEVERITY_RE = re.compile(_LINE_START + _FIELD % "severity", re.I | re.M)


def heading_index(text: str, heading: str) -> int:
    """Index of the first `#… ## Heading` line whose text starts with heading."""
    m = re.search(r"^#{1,4}\s+(?:\d+\.\s*)?" + re.escape(heading) + r"\b",
                  text, re.M | re.I)
    return m.start() if m else -1


for lane, _node, report in LANES:
    f = os.path.join(RUN_DIR, report)
    if not os.path.isfile(f):
        fail(f"MISSING_REPORT:{report}")
        ev.write(f"MISSING: {report}\n")
        continue
    text = open(f, encoding="utf-8", errors="replace").read()
    lines = text.count("\n")
    evidence_hits = sum(1 for line in text.splitlines() if EVIDENCE_RE.search(line))

    verdicts = VERDICT_RE.findall(text)
    severities = SEVERITY_RE.findall(text)
    invalid_verdicts = [v for v in verdicts if v.lower() not in VALID_VERDICTS]
    invalid_severity = [s for s in severities if s.lower() not in VALID_SEVERITY]

    # The five sections must be present AND in order — the contract that makes
    # the three reports comparable line for line.
    idx = [heading_index(text, s) for s in SECTIONS]
    order_ok = all(i >= 0 for i in idx) and idx == sorted(idx)

    ev.write(
        f"{report}: lines={lines} evidence_hits={evidence_hits} "
        f"verdict_blocks={len(verdicts)} severity_blocks={len(severities)} "
        f"sections_ordered={order_ok}\n"
    )
    if lines < 30:
        fail(f"{report}:too_short")
    if evidence_hits < 3:
        fail(f"{report}:missing_discovery_evidence")
    if not order_ok:
        absent = [s for s, i in zip(SECTIONS, idx) if i < 0]
        fail(f"{report}:sections_missing_or_out_of_order:{absent or 'reorder'}")
    if len(verdicts) < 12:
        # 6 tasks + 6 solutions.
        fail(f"{report}:incomplete_verdict_coverage({len(verdicts)}/12)")
    if invalid_verdicts:
        fail(f"{report}:invalid_verdict_enum:{sorted(set(invalid_verdicts))[:3]}")
    if invalid_severity:
        fail(f"{report}:invalid_severity_enum:{sorted(set(invalid_severity))[:3]}")

# ── structured sidecars (one shared schema across all three lanes) ──────────
REQUIRED_TOP = {
    "schema_version", "panel_id", "lane", "model",
    "tasks", "solutions", "execution_order", "counts",
    "open_questions", "read_only_attestation",
}
REQUIRED_TASK = {
    "task_id", "verdict", "severity", "evidence_checked", "reasoning", "corrections",
}
REQUIRED_SOLUTION = {"solution_id", "task_id", "verdict", "severity", "reasoning"}
TASK_IDS = [f"T{i}" for i in range(1, 7)]
SOLUTION_IDS = [f"P{i}" for i in range(1, 7)]

sidecars: dict[str, dict] = {}


def check_lane_json(lane: str, node: str) -> None:
    match = os.path.join(RUN_DIR, "context-*.json")
    cand = os.path.join(RUN_DIR, f"context-{node}.json")
    if not os.path.isfile(cand):
        hits = [p for p in sorted(glob.glob(match)) if node.split("_")[0] in os.path.basename(p)]
        if not hits:
            fail(f"MISSING_SIDECAR:context-{node}.json")
            ev.write(f"MISSING sidecar: context-{node}.json\n")
            return
        cand = hits[0]
    raw = open(cand, encoding="utf-8", errors="replace").read()
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        fail(f"{lane}:sidecar_not_json:{exc}")
        return
    if not isinstance(obj, dict):
        fail(f"{lane}:sidecar_not_an_object")
        return
    sidecars[lane] = obj

    absent = REQUIRED_TOP - set(obj)
    if absent:
        fail(f"{lane}:sidecar_missing_keys:{sorted(absent)}")
    for key in ("schema_version", "panel_id", "lane", "model"):
        if not obj.get(key):
            fail(f"{lane}:sidecar_empty_{key}")
    # ``lane`` may carry either the lane alias (``glm``) or the node id
    # (``glm_judge``) — both identify the same panel member, and a sidecar
    # copied from ANOTHER lane is what this check exists to catch. Requiring
    # one spelling exactly is how a valid run fails on a naming preference.
    if obj.get("lane") not in (None, lane, node):
        fail(f"{lane}:sidecar_lane_mismatch:{obj.get('lane')}")

    tasks = obj.get("tasks") or []
    solutions = obj.get("solutions") or []
    if [x.get("task_id") for x in tasks if isinstance(x, dict)] != TASK_IDS:
        fail(f"{lane}:tasks_not_T1..T6:{[x.get('task_id') for x in tasks]}")
    if [x.get("solution_id") for x in solutions if isinstance(x, dict)] != SOLUTION_IDS:
        fail(f"{lane}:solutions_not_P1..P6:{[x.get('solution_id') for x in solutions]}")
    for x in tasks:
        if isinstance(x, dict):
            miss = REQUIRED_TASK - set(x)
            if miss:
                fail(f"{lane}:task_{x.get('task_id')}_missing:{sorted(miss)}")
            if str(x.get("verdict", "")).lower() not in TASK_VERDICTS:
                fail(f"{lane}:task_{x.get('task_id')}_bad_verdict:{x.get('verdict')}")
            if str(x.get("severity", "")).lower() not in VALID_SEVERITY:
                fail(f"{lane}:task_{x.get('task_id')}_bad_severity:{x.get('severity')}")
    for x in solutions:
        if isinstance(x, dict):
            miss = REQUIRED_SOLUTION - set(x)
            if miss:
                # `solution_id` + `task_id` together are what make the join
                # work; a solution carrying a task id in the solution slot is
                # the defect this check exists to catch.
                fail(f"{lane}:solution_{x.get('solution_id')}_missing:{sorted(miss)}")
            if x.get("task_id") not in TASK_IDS:
                fail(f"{lane}:solution_{x.get('solution_id')}_task_id_not_a_task:{x.get('task_id')}")
            if str(x.get("verdict", "")).lower() not in SOLUTION_VERDICTS:
                fail(f"{lane}:solution_{x.get('solution_id')}_bad_verdict:{x.get('verdict')}")
            if str(x.get("severity", "")).lower() not in VALID_SEVERITY:
                fail(f"{lane}:solution_{x.get('solution_id')}_bad_severity:{x.get('severity')}")

    order = obj.get("execution_order") or []
    if not order or any(t not in TASK_IDS for t in order):
        fail(f"{lane}:execution_order_invalid:{order}")
    elif sorted(order) != TASK_IDS:
        fail(f"{lane}:execution_order_not_a_permutation_of_T1..T6:{order}")

    counts = obj.get("counts") or {}
    hist = (counts.get("severity_histogram") or {}) if isinstance(counts, dict) else {}
    if not isinstance(hist, dict) or set(hist) != VALID_SEVERITY:
        fail(f"{lane}:counts_missing_severity_histogram:{sorted(hist)}")
    elif sum(v for v in hist.values() if isinstance(v, int)) != 6:
        fail(f"{lane}:severity_histogram_does_not_sum_to_6:{hist}")

    att = obj.get("read_only_attestation")
    if not isinstance(att, dict) or att.get("worktree_modified") is not False:
        fail(f"{lane}:read_only_attestation_missing_or_false_expected")


for lane, node, _report in LANES:
    check_lane_json(lane, node)

# Cross-lane parity: the WHOLE point of the shared schema. A mechanical merge
# keyed on one lane must see the other two. Compare the key sets, not the
# values — pairwise over all three lanes so any one schema drift is caught
# twice, naming both drift partners.
if len(sidecars) == 3:
    for a_lane, b_lane in itertools.combinations(sorted(sidecars), 2):
        a, b = sidecars[a_lane], sidecars[b_lane]
        tag = f"{a_lane}~{b_lane}"
        if set(a) != set(b):
            fail(f"cross_lane_{tag}_top_level_keys_differ:{sorted(set(a) ^ set(b))}")
        at, bt = a.get("tasks") or [], b.get("tasks") or []
        if at and bt and set(at[0]) != set(bt[0]):
            fail(f"cross_lane_{tag}_task_keys_differ:{sorted(set(at[0]) ^ set(bt[0]))}")
        ax, bx = a.get("solutions") or [], b.get("solutions") or []
        if ax and bx and set(ax[0]) != set(bx[0]):
            fail(f"cross_lane_{tag}_solution_keys_differ:{sorted(set(ax[0]) ^ set(bx[0]))}")
    ev.write("cross_lane_parity: pairwise over 3 lanes checked\n")

# ── synthesis ───────────────────────────────────────────────────────────────
synth = os.path.join(RUN_DIR, "synthesis.md")
if not os.path.isfile(synth):
    fail("MISSING_SYNTHESIS:synthesis.md")
else:
    synth_text = open(synth, encoding="utf-8", errors="replace").read()
    for needle in ("minimax", "glm", "deepseek",
                   "T1", "T2", "T3", "T4", "T5", "T6",
                   "P1", "P2", "P3", "P4", "P5", "P6"):
        if not re.search(re.escape(needle), synth_text, re.I):
            fail(f"synthesis.md:missing_{needle}")

ev.close()

print(json.dumps({
    "verifier": "panel-completeness",
    "pass": not missing,
    "evidence_path": EVIDENCE,
    "missing": missing,
}, indent=2))

# rc IS the verdict — see the module header. The runner reads rc, not the JSON.
sys.exit(0 if not missing else 1)
