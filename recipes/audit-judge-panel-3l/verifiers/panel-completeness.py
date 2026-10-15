#!/usr/bin/env python3
# Completeness gate for the audit-judge-panel-3l recipe (three lanes).
#
# Two artifacts per lane, both graded:
#
#   judge-<lane>-audit.md      the human report — the five required sections,
#                              in order, each finding/fix block carrying a
#                              valid verdict + severity enum and a receipt.
#   context-<node>.json        the machine sidecar — ONE shared schema across
#                              ALL lanes, so the synthesizer (and any later
#                              consumer) can join them mechanically instead of
#                              pattern-matching prose.
#
# The third lane (laguna) is tool-less: its whole reply is captured verbatim
# as lens-laguna.md by the researcher output-file rule, so its sidecar is the
# LAST fenced ```json block inside that same file rather than a separate
# context-*.json. Extracted here, validated by the same checks as the others.
#
# Both halves are checked, and so is the panel itself: llm_calls must show the
# three judges served by three different model FAMILIES (R7a) — the check that
# would have failed run-1791725633-31802, whose opus_lens was served by glm and
# whose laguna_lens fell back to minimax, making a "three family" panel two.
#
# The gate FAILS (non-zero rc) on any miss: an earlier
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

# ── repo-root import shim (R7a) ─────────────────────────────────────────────
# This gate runs as a standalone script OUTSIDE the package, so ``mini_ork`` is
# importable only once a repo root is on sys.path: $MINI_ORK_ROOT when the
# engine sets it, else this file's ``../../..`` (recipes/<recipe>/verifiers/).
# The root chosen has to be the one that CARRIES the module, which is not
# always the one the environment names: during a worktree run $MINI_ORK_ROOT is
# the engine checkout, which lags the change under test while this verifier
# ships inside the target checkout. Probed, not imported blind — Python binds
# ``mini_ork`` to the first sys.path entry holding the package, so a later
# entry can never rescue a missing SUBmodule.
_SHIPPED_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)
_MODULE = os.path.join("mini_ork", "verify", "panel_heterogeneity.py")
_ROOT = os.environ.get("MINI_ORK_ROOT") or _SHIPPED_ROOT
if not os.path.isfile(os.path.join(_ROOT, _MODULE)):
    _ROOT = _SHIPPED_ROOT
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import mini_ork.verify.panel_heterogeneity as panel_heterogeneity  # noqa: E402

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
EVIDENCE = os.path.join(RUN_DIR, "verifier-panel-completeness.log")
ev = open(EVIDENCE, "w", encoding="utf-8")

missing = []


def fail(why: str) -> None:
    missing.append(why)


# ── markdown reports ────────────────────────────────────────────────────────
# (lane, node id, report name, sidecar source). For the tool-ful lanes the
# sidecar is a separate context-<node>.json the agent writes; the tool-less
# laguna lane's sidecar is EMBEDDED — the last ```json fence in its report.
LANES = [
    ("opus", "opus_judge", "judge-opus-audit.md", "file"),
    ("minimax", "minimax_judge", "judge-minimax-audit.md", "file"),
    ("laguna", "laguna_lens", "lens-laguna.md", "embedded"),
]
# The judge aliases the workflow pins — the ``actor`` recorded in llm_calls.
# Not derived from LANES: the alias, the lane and the node id are three
# different spellings of one judge (``opus_lens`` / ``opus`` / ``opus_judge``).
JUDGE_ALIASES = ["opus_lens", "minimax_lens", "laguna_lens"]
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


for lane, _node, report, _src in LANES:
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


def check_lane_json(lane: str, node: str, source: str = "file") -> None:
    if source == "embedded":
        # Tool-less lane: the sidecar is the last ```json fenced block of the
        # lane's own captured report. The prompt mandates exactly one such
        # block and nothing after it; taking the LAST fence tolerates a judge
        # that quoted a smaller json snippet earlier in its prose.
        report = os.path.join(RUN_DIR, dict((l, r) for l, _n, r, _s in LANES)[lane])
        if not os.path.isfile(report):
            return  # the report-miss was already failed above
        text = open(report, encoding="utf-8", errors="replace").read()
        fences = re.findall(r"```json\s*(.*?)```", text, re.S)
        if fences:
            raw = fences[-1]
        else:
            # Unterminated final fence: free-tier models routinely emit the
            # complete JSON object and then die before the closing ``` (the
            # round-5 artifact ended `...]}\n}` with one opening fence and no
            # closer). Take everything after the LAST ```json opener to EOF,
            # trim trailing backticks/whitespace, and let json.loads judge
            # whether the object is actually complete — a truncated object
            # still fails below, so this widens extraction, not validation.
            m = None
            for m in re.finditer(r"```json\s*", text):
                pass
            if m is None:
                fail(f"{lane}:sidecar_not_json:no fenced json block in {os.path.basename(report)}")
                ev.write(f"MISSING embedded sidecar in {report}\n")
                return
            raw = text[m.end():].rstrip().rstrip("`").rstrip()
    else:
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


for lane, node, _report, src in LANES:
    check_lane_json(lane, node, src)

# Cross-lane parity: the WHOLE point of the shared schema. A mechanical merge
# keyed on one lane must see the others. Compare the key sets pairwise across
# every lane present, not the values.
if len(sidecars) >= 2:
    names = sorted(sidecars)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = sidecars[names[i]], sidecars[names[j]]
            pair = f"{names[i]}|{names[j]}"
            if set(a) != set(b):
                fail(f"cross_lane_top_level_keys_differ:{pair}:{sorted(set(a) ^ set(b))}")
            af = a.get("findings") or []
            bf = b.get("findings") or []
            if af and bf and set(af[0]) != set(bf[0]):
                fail(f"cross_lane_finding_keys_differ:{pair}:{sorted(set(af[0]) ^ set(bf[0]))}")
            ax = a.get("fixes") or []
            bx = b.get("fixes") or []
            if ax and bx and set(ax[0]) != set(bx[0]):
                fail(f"cross_lane_fix_keys_differ:{pair}:{sorted(set(ax[0]) ^ set(bx[0]))}")
            ev.write(f"cross_lane_parity {pair}: top={set(a) == set(b)} "
                     f"finding={bool(af and bf and set(af[0]) == set(bf[0]))} "
                     f"fix={bool(ax and bx and set(ax[0]) == set(bx[0]))}\n")

# ── synthesis ───────────────────────────────────────────────────────────────
synth = os.path.join(RUN_DIR, "synthesis.md")
if not os.path.isfile(synth):
    fail("MISSING_SYNTHESIS:synthesis.md")
else:
    synth_text = open(synth, encoding="utf-8", errors="replace").read()
    for needle in ("opus", "minimax", "laguna", "F1", "F2", "F3", "F4", "F5",
                   "F6", "S1", "S2", "S3", "S4", "S5", "S6"):
        if not re.search(re.escape(needle), synth_text, re.I):
            fail(f"synthesis.md:missing_{needle}")

# ── served model families (R7a) ─────────────────────────────────────────────
# The three judges must be three model FAMILIES. This gate never looked: run
# run-1791725633-31802 had opus_lens served by glm and laguna_lens falling back
# to minimax six times, so its "three lane" panel was two families — and it
# published. Raw lane strings cannot see either the operator overlay or a
# fallback tail; the family each served lane resolves to can. See
# mini_ork/verify/panel_heterogeneity.py.
_run_id = os.environ.get("MINI_ORK_RUN_ID") or os.path.basename(RUN_DIR.rstrip("/"))
_panel = panel_heterogeneity.check_panel(_run_id, JUDGE_ALIASES)
if _panel["skipped"]:
    ev.write(f"served_models: skipped ({_panel['skipped']})\n")
else:
    ev.write(f"served_models: served={json.dumps(_panel['served'])} "
             f"families={json.dumps(_panel['families'])}\n")
    for _problem in _panel["problems"]:
        fail(f"served_models:{_problem}")

ev.close()

print(json.dumps({
    "verifier": "panel-completeness",
    "pass": not missing,
    "evidence_path": EVIDENCE,
    "missing": missing,
}, indent=2))

# rc IS the verdict — see the module header. The runner reads rc, not the JSON.
sys.exit(0 if not missing else 1)
