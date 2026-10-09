#!/usr/bin/env python3
# Completeness gate for the audit-judge-panel recipe.
#
# Two artifacts per lane, both graded:
#
#   judge-<lane>-audit.md      the human report — the five required sections,
#                              in order, each finding/fix block carrying a
#                              valid verdict + severity enum and a receipt.
#   context-<node>.json        the machine sidecar — ONE shared schema across
#                              BOTH lanes, so the synthesizer (and any later
#                              consumer) can join them mechanically instead of
#                              pattern-matching prose.
#
# Both halves are checked. The gate FAILS (non-zero rc) on any miss: an earlier
# version printed {"pass": false} and then `sys.exit(0)` unconditionally, so the
# runner — which reads rc, not the JSON — recorded a pass and the panel could
# not fail at all. A gate that cannot fail is theater, and it is why two lanes
# emitting disjoint sidecar schemas sailed through the first panel run.
#
# JSON verdict on stdout; rc mirrors it (0 = complete, 1 = miss).

import glob
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
    ("opus", "opus_judge", "judge-opus-audit.md"),
    ("minimax", "minimax_judge", "judge-minimax-audit.md"),
]
SECTIONS = [
    "Discovery Evidence",
    "Finding Verdicts",
    "Fix Verdicts",
    "Recommended Fix Order",
    "Open Questions",
]
VALID_VERDICTS = {"confirmed", "partially_confirmed", "refuted", "unverifiable"}
VALID_SEVERITY = {"critical", "high", "medium", "low"}

# Receipts: SQL fragments, file paths with :line, sqlite3 invocations.
EVIDENCE_RE = re.compile(
    r"sqlite3|SELECT |FROM llm_calls|FROM execution_traces|FROM gradient_records"
    r"|writeback\.py|llm_dispatch\.py|:[0-9]+",
    re.I,
)
# A FIELD value is only read from a line that STARTS with the field name —
# after an optional markdown list marker and/or blockquote marker — and may be
# wrapped in ``**`` or backticks on either side of the separator.
#
# The loose form this replaces (``[\*`]*verdict[\*`]*\s*[:=]\s*(\w+)``, unanchored)
# graded prose as a verdict field. It read the sentence
# ``**Magnitude verdict:** F4's dollar exposure is small (~$10.72)`` as
# ``verdict: F4`` and failed the whole panel with
# ``invalid_verdict_enum:['F4']`` on an otherwise valid report — a false
# negative that cost a full two-lane run. Anchoring at the line and forbidding
# words between the marker and the field name is the fix: ``**Magnitude
# verdict:**`` no longer reaches the field name, while ``- **verdict:**
# confirmed`` still does.
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
    # the two reports comparable line for line.
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
        # 6 findings + 6 fixes.
        fail(f"{report}:incomplete_verdict_coverage({len(verdicts)}/12)")
    if invalid_verdicts:
        fail(f"{report}:invalid_verdict_enum:{sorted(set(invalid_verdicts))[:3]}")
    if invalid_severity:
        fail(f"{report}:invalid_severity_enum:{sorted(set(invalid_severity))[:3]}")

# ── structured sidecars (one shared schema across both lanes) ───────────────
REQUIRED_TOP = {
    "schema_version", "panel_id", "lane", "model",
    "findings", "fixes", "fix_order", "counts",
    "open_questions", "read_only_attestation",
}
REQUIRED_FINDING = {
    "finding_id", "verdict", "severity", "evidence_checked", "reasoning", "corrections",
}
REQUIRED_FIX = {"fix_id", "finding_id", "verdict", "severity", "reasoning"}
FINDING_IDS = [f"F{i}" for i in range(1, 7)]
FIX_IDS = [f"S{i}" for i in range(1, 7)]

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
    # ``lane`` may carry either the lane alias (``opus``) or the node id
    # (``opus_judge``) — both identify the same panel member, and a sidecar
    # copied from the other lane is what this check exists to catch. Requiring
    # one spelling exactly is how a valid run fails on a naming preference.
    if obj.get("lane") not in (None, lane, node):
        fail(f"{lane}:sidecar_lane_mismatch:{obj.get('lane')}")

    findings = obj.get("findings") or []
    fixes = obj.get("fixes") or []
    if [x.get("finding_id") for x in findings if isinstance(x, dict)] != FINDING_IDS:
        fail(f"{lane}:findings_not_F1..F6:{[x.get('finding_id') for x in findings]}")
    if [x.get("fix_id") for x in fixes if isinstance(x, dict)] != FIX_IDS:
        fail(f"{lane}:fixes_not_S1..S6:{[x.get('fix_id') for x in fixes]}")
    for x in findings:
        if isinstance(x, dict):
            miss = REQUIRED_FINDING - set(x)
            if miss:
                fail(f"{lane}:finding_{x.get('finding_id')}_missing:{sorted(miss)}")
            if str(x.get("verdict", "")).lower() not in VALID_VERDICTS:
                fail(f"{lane}:finding_{x.get('finding_id')}_bad_verdict:{x.get('verdict')}")
            if str(x.get("severity", "")).lower() not in VALID_SEVERITY:
                fail(f"{lane}:finding_{x.get('finding_id')}_bad_severity:{x.get('severity')}")
    for x in fixes:
        if isinstance(x, dict):
            miss = REQUIRED_FIX - set(x)
            if miss:
                # `fix_id` + `finding_id` together are what make the join work;
                # a fix carrying a finding id in the fix slot is the defect this
                # check exists to catch.
                fail(f"{lane}:fix_{x.get('fix_id')}_missing:{sorted(miss)}")
            if x.get("finding_id") not in FINDING_IDS:
                fail(f"{lane}:fix_{x.get('fix_id')}_finding_id_not_a_finding:{x.get('finding_id')}")
            if str(x.get("verdict", "")).lower() not in VALID_VERDICTS:
                fail(f"{lane}:fix_{x.get('fix_id')}_bad_verdict:{x.get('verdict')}")
            if str(x.get("severity", "")).lower() not in VALID_SEVERITY:
                fail(f"{lane}:fix_{x.get('fix_id')}_bad_severity:{x.get('severity')}")

    order = obj.get("fix_order") or []
    if not order or any(s not in FIX_IDS for s in order):
        fail(f"{lane}:fix_order_invalid:{order}")
    if sorted(order) != FIX_IDS:
        fail(f"{lane}:fix_order_not_a_permutation_of_S1..S6:{order}")

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
# keyed on one lane must see the other. Compare the key sets, not the values.
if len(sidecars) == 2:
    a, b = sidecars["opus"], sidecars["minimax"]
    if set(a) != set(b):
        fail(f"cross_lane_top_level_keys_differ:{sorted(set(a) ^ set(b))}")
    af = a.get("findings") or []
    bf = b.get("findings") or []
    if af and bf and set(af[0]) != set(bf[0]):
        fail(f"cross_lane_finding_keys_differ:{sorted(set(af[0]) ^ set(bf[0]))}")
    ax = a.get("fixes") or []
    bx = b.get("fixes") or []
    if ax and bx and set(ax[0]) != set(bx[0]):
        fail(f"cross_lane_fix_keys_differ:{sorted(set(ax[0]) ^ set(bx[0]))}")
    ev.write(f"cross_lane_parity: top={set(a) == set(b)} "
             f"finding={bool(af and bf and set(af[0]) == set(bf[0]))} "
             f"fix={bool(ax and bx and set(ax[0]) == set(bx[0]))}\n")

# ── synthesis ───────────────────────────────────────────────────────────────
synth = os.path.join(RUN_DIR, "synthesis.md")
if not os.path.isfile(synth):
    fail("MISSING_SYNTHESIS:synthesis.md")
else:
    synth_text = open(synth, encoding="utf-8", errors="replace").read()
    for needle in ("opus", "minimax", "F1", "F2", "F3", "F4", "F5", "F6",
                   "S1", "S2", "S3", "S4", "S5", "S6"):
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
