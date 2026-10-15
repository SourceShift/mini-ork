#!/usr/bin/env python3
# Completeness gate for the audit-judge-panel-4l recipe (four lanes).
#
# Grades three things:
#
#   judge-<lane>-audit.md   the human report — seven sections in order, every
#                           verdict/severity field a valid enum, real receipts.
#   context-<node>.json     the machine sidecar — ONE shared schema across all
#                           four lanes, covering exactly the finding and fix ids
#                           the KICKOFF declares (not a recipe-fixed F1..F6).
#   llm_calls               the four judges were served by four DISTINCT model
#                           FAMILIES, not four lane strings (R7a). The 3-lane
#                           panel's gate never looked: its run
#                           run-1791725633-31802 had opus_lens served by glm and
#                           laguna_lens falling back to minimax, so a "three
#                           family" panel was glm + minimax twice, and it passed.
#                           Comparing lane strings would not have caught it
#                           either — ``opus`` and ``opus55`` are one family.
#
# rc IS the verdict (0 = complete, 1 = miss); the JSON on stdout explains it.

# The runner execs this with its own interpreter, but an operator re-running
# the gate by hand gets whatever ``python3`` is first on PATH (3.9 on macOS);
# deferred annotations keep the ``X | None`` hints from failing at import there.
from __future__ import annotations

import glob
import json
import os
import re
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
EVIDENCE = os.path.join(RUN_DIR, "verifier-panel-completeness.log")

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

# (lane, node id, lane alias the workflow pins)
LANES = [
    ("opus", "opus_judge", "opus_lens"),
    ("glm", "glm_judge", "glm_lens"),
    ("deepseek", "deepseek_judge", "deepseek_lens"),
    ("minimax", "minimax_judge", "minimax_lens"),
]
SECTIONS = [
    "Discovery Evidence",
    "Finding Verdicts",
    "New Findings",
    "Fix Verdicts",
    "Proposed Solutions",
    "Recommended Fix Order",
    "Open Questions",
]
SYNTH_SECTIONS = [
    "Panel Verdict Matrix",
    "Consensus Findings",
    "Dissents",
    "Corrected Numbers",
    "New Findings",
    "Solution Plan",
    "Questions For Human Decision",
]
VALID_VERDICTS = {"confirmed", "partially_confirmed", "refuted", "unverifiable"}
VALID_SEVERITY = {"critical", "high", "medium", "low"}
REQUIRED_TOP = {
    "schema_version", "panel_id", "lane", "model", "findings", "new_findings",
    "fixes", "solutions", "fix_order", "counts", "open_questions",
    "read_only_attestation",
}
REQUIRED_FINDING = {"finding_id", "verdict", "severity", "evidence_checked",
                    "reasoning", "corrections"}
REQUIRED_FIX = {"fix_id", "finding_id", "verdict", "severity", "reasoning"}

# Receipts: SQL, sqlite3 invocations, file:line references.
EVIDENCE_RE = re.compile(r"sqlite3|SELECT |\bFROM [a-z_]+|\.py:\d+|:\d+\b", re.I)
# A FIELD value is read only from a line that STARTS with the field name (after
# an optional list/blockquote marker), possibly wrapped in ** or backticks —
# the anchoring audit-judge-panel-3l learned the hard way: an unanchored regex
# read "**Magnitude verdict:** F4's …" as ``verdict: F4``.
#
# A field is either at column 0 or behind a list/blockquote marker. An indented
# line with NO marker is the wrapped continuation of the item above, never a
# field, and only ``:`` separates a field from its value: the first live run
# failed on glm's wrapped prose ``  severity=None path for prose; …`` (a code
# reference) read as ``severity: None``.
_LINE_START = r"^(?:[ \t]*[-*+>][ \t]+(?:>[ \t]*)?)?"
_FIELD = r"[*`]*%s[*`]*[ \t]*:[ \t]*[*`]*[ \t]*([A-Za-z_]+)"
VERDICT_RE = re.compile(_LINE_START + _FIELD % "verdict", re.I | re.M)
SEVERITY_RE = re.compile(_LINE_START + _FIELD % "severity", re.I | re.M)

_H2_RE = re.compile(r"^##(?!#)\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_ID_LINE = r"^\s*(?:#{3,6}\s+|[-*+]\s+|\d+[.)]\s+)?(?:\*\*|__|`)?(%s\d+)\b"
_F_ID_RE = re.compile(_ID_LINE % "F")
_S_ID_RE = re.compile(_ID_LINE % "S")


# ── kickoff → declared ids ──────────────────────────────────────────────────

def kickoff_text(run_dir: str) -> str:
    """The kickoff as the run received it: the context-v2 snapshot when there is
    one (taken before any node ran), else the file run_profile.json names, else
    ``$MINI_ORK_KICKOFF``. Empty when none can be read."""
    try:
        with open(os.path.join(run_dir, "context-pack.v2.json"), encoding="utf-8") as fh:
            snap = (json.load(fh) or {}).get("kickoff_snapshot") or ""
        if snap.strip():
            return snap
    except (OSError, ValueError, AttributeError):
        pass
    candidates = []
    try:
        with open(os.path.join(run_dir, "run_profile.json"), encoding="utf-8") as fh:
            candidates.append((json.load(fh) or {}).get("kickoff_path") or "")
    except (OSError, ValueError, AttributeError):
        pass
    candidates.append(os.environ.get("MINI_ORK_KICKOFF", ""))
    for path in candidates:
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
    return ""


def declared_ids(text: str) -> tuple[list[str], list[str]]:
    """``(finding ids, fix ids)`` in first-appearance order.

    Finding ids are items starting with ``F<n>`` inside a ``## …finding…``
    section; fix ids are items starting with ``S<n>`` inside a ``## …fix…`` or
    ``## …solution…`` section. An item is a list bullet, a numbered line or a
    ``###`` heading. Fenced code is ignored, so an example never declares ids.
    """
    findings: list[str] = []
    fixes: list[str] = []
    section = ""
    in_fence = False
    for line in (text or "").splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _H2_RE.match(line)
        if m:
            section = m.group(1).lower()
            continue
        if "finding" in section:
            hit = _F_ID_RE.match(line)
            if hit and hit.group(1) not in findings:
                findings.append(hit.group(1))
        elif "fix" in section or "solution" in section:
            hit = _S_ID_RE.match(line)
            if hit and hit.group(1) not in fixes:
                fixes.append(hit.group(1))
    return findings, fixes


# ── reports ─────────────────────────────────────────────────────────────────

def _without_fences(text: str) -> str:
    """``text`` with every fenced code block blanked out (line count kept)."""
    out, in_fence = [], False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return "\n".join(out)


def heading_index(text: str, heading: str) -> int:
    """Index of the first heading line whose text starts with ``heading``."""
    m = re.search(r"^#{1,4}\s+(?:\d+\.\s*)?" + re.escape(heading) + r"\b",
                  text, re.M | re.I)
    return m.start() if m else -1


def check_report(path: str, n_items: int, fail, ev) -> None:
    name = os.path.basename(path)
    if not os.path.isfile(path):
        fail(f"MISSING_REPORT:{name}")
        return
    text = open(path, encoding="utf-8", errors="replace").read()
    lines = text.count("\n")
    evidence_hits = sum(1 for line in text.splitlines() if EVIDENCE_RE.search(line))
    prose = _without_fences(text)  # a quoted snippet is a receipt, not a field
    verdicts = VERDICT_RE.findall(prose)
    severities = SEVERITY_RE.findall(prose)
    idx = [heading_index(text, s) for s in SECTIONS]
    order_ok = all(i >= 0 for i in idx) and idx == sorted(idx)
    ev.write(f"{name}: lines={lines} evidence_hits={evidence_hits} "
             f"verdict_fields={len(verdicts)} severity_fields={len(severities)} "
             f"sections_ordered={order_ok}\n")
    if lines < 40:
        fail(f"{name}:too_short({lines})")
    if evidence_hits < 5:
        fail(f"{name}:missing_discovery_evidence({evidence_hits})")
    if not order_ok:
        absent = [s for s, i in zip(SECTIONS, idx) if i < 0]
        fail(f"{name}:sections_missing_or_out_of_order:{absent or 'reorder'}")
    if len(verdicts) < n_items:
        fail(f"{name}:incomplete_verdict_coverage({len(verdicts)}/{n_items})")
    bad_v = sorted({v for v in verdicts if v.lower() not in VALID_VERDICTS})
    bad_s = sorted({s for s in severities if s.lower() not in VALID_SEVERITY})
    if bad_v:
        fail(f"{name}:invalid_verdict_enum:{bad_v[:3]}")
    if bad_s:
        fail(f"{name}:invalid_severity_enum:{bad_s[:3]}")


# ── sidecars ────────────────────────────────────────────────────────────────

def _ids_of(value) -> list:
    return value if isinstance(value, list) else [value]


def check_sidecar(lane: str, node: str, f_ids: list[str], s_ids: list[str],
                  fail) -> dict | None:
    path = os.path.join(RUN_DIR, f"context-{node}.json")
    if not os.path.isfile(path):
        hits = [p for p in sorted(glob.glob(os.path.join(RUN_DIR, "context-*.json")))
                if os.path.basename(p).startswith(f"context-{lane}")]
        if not hits:
            fail(f"MISSING_SIDECAR:context-{node}.json")
            return None
        path = hits[0]
    try:
        obj = json.loads(open(path, encoding="utf-8", errors="replace").read())
    except ValueError as exc:
        fail(f"{lane}:sidecar_not_json:{exc}")
        return None
    if not isinstance(obj, dict):
        fail(f"{lane}:sidecar_not_an_object")
        return None

    absent = REQUIRED_TOP - set(obj)
    if absent:
        fail(f"{lane}:sidecar_missing_keys:{sorted(absent)}")
    for key in ("schema_version", "panel_id", "lane", "model"):
        if not obj.get(key):
            fail(f"{lane}:sidecar_empty_{key}")
    if obj.get("lane") not in (None, lane, node):
        fail(f"{lane}:sidecar_lane_mismatch:{obj.get('lane')}")

    findings = [x for x in (obj.get("findings") or []) if isinstance(x, dict)]
    fixes = [x for x in (obj.get("fixes") or []) if isinstance(x, dict)]
    if [x.get("finding_id") for x in findings] != f_ids:
        fail(f"{lane}:findings_not_kickoff_ids:{[x.get('finding_id') for x in findings]}")
    if [x.get("fix_id") for x in fixes] != s_ids:
        fail(f"{lane}:fixes_not_kickoff_ids:{[x.get('fix_id') for x in fixes]}")
    for x in findings:
        fid = x.get("finding_id")
        miss = REQUIRED_FINDING - set(x)
        if miss:
            fail(f"{lane}:finding_{fid}_missing:{sorted(miss)}")
        if str(x.get("verdict", "")).lower() not in VALID_VERDICTS:
            fail(f"{lane}:finding_{fid}_bad_verdict:{x.get('verdict')}")
        if str(x.get("severity", "")).lower() not in VALID_SEVERITY:
            fail(f"{lane}:finding_{fid}_bad_severity:{x.get('severity')}")
    for x in fixes:
        sid = x.get("fix_id")
        miss = REQUIRED_FIX - set(x)
        if miss:
            fail(f"{lane}:fix_{sid}_missing:{sorted(miss)}")
        linked = [i for i in _ids_of(x.get("finding_id")) if i]
        if not linked or any(i not in f_ids for i in linked):
            fail(f"{lane}:fix_{sid}_finding_id_not_a_finding:{x.get('finding_id')}")
        if str(x.get("verdict", "")).lower() not in VALID_VERDICTS:
            fail(f"{lane}:fix_{sid}_bad_verdict:{x.get('verdict')}")
        if str(x.get("severity", "")).lower() not in VALID_SEVERITY:
            fail(f"{lane}:fix_{sid}_bad_severity:{x.get('severity')}")

    new = obj.get("new_findings")
    sols = obj.get("solutions")
    if not isinstance(new, list):
        fail(f"{lane}:new_findings_not_a_list")
        new = []
    if not isinstance(sols, list):
        fail(f"{lane}:solutions_not_a_list")
        sols = []
    n_ids = {x.get("new_finding_id") for x in new if isinstance(x, dict)}
    for x in new:
        if isinstance(x, dict) and str(x.get("severity", "")).lower() not in VALID_SEVERITY:
            fail(f"{lane}:new_finding_{x.get('new_finding_id')}_bad_severity:{x.get('severity')}")
    known = set(f_ids) | n_ids
    for x in sols:
        if not isinstance(x, dict):
            continue
        bad = [a for a in _ids_of(x.get("addresses") or []) if a not in known]
        if bad:
            fail(f"{lane}:solution_{x.get('solution_id')}_addresses_unknown:{bad}")

    order = obj.get("fix_order") or []
    if sorted(order) != sorted(s_ids) or len(order) != len(s_ids):
        fail(f"{lane}:fix_order_not_a_permutation_of_kickoff_fixes:{order}")

    counts = obj.get("counts") or {}
    hist = counts.get("severity_histogram") if isinstance(counts, dict) else None
    if not isinstance(hist, dict) or set(hist) != VALID_SEVERITY:
        fail(f"{lane}:counts_missing_severity_histogram:{sorted(hist or {})}")
    elif sum(v for v in hist.values() if isinstance(v, int)) != len(f_ids):
        fail(f"{lane}:severity_histogram_does_not_sum_to_{len(f_ids)}:{hist}")

    att = obj.get("read_only_attestation")
    if not isinstance(att, dict) or att.get("worktree_modified") is not False:
        fail(f"{lane}:read_only_attestation_missing_or_false_expected")
    return obj


# ── served model families ───────────────────────────────────────────────────

def _expected_models() -> dict[str, set[str]]:
    """``MO_PANEL_EXPECT_MODELS="opus_lens=opus|opus55,glm_lens=glm"`` → map."""
    out: dict[str, set[str]] = {}
    for part in (os.environ.get("MO_PANEL_EXPECT_MODELS") or "").split(","):
        alias, _, models = part.partition("=")
        if alias.strip() and models.strip():
            out[alias.strip()] = {m.strip() for m in models.split("|") if m.strip()}
    return out


def check_served_models(fail, ev) -> None:
    """The four judges must be four different model FAMILIES.

    Reads the judge calls of THIS run from ``llm_calls`` (``actor`` = the
    pinned lens alias, ``model_id`` = the lane that actually served it) and
    grades the family each served lane resolves to — the check the raw
    lane-string comparison could not make. A lens whose first lane failed and
    was served by a different tail lane is a fallback failure; two aliases on
    one family (``opus`` + ``opus55``, or the operator overlay remapping a
    lens onto another judge's lane) are a same-family failure. Every problem is
    a gate failure prefixed ``served_models:``. No DB, no table, or no rows for
    this run is recorded as skipped — tests and dry runs have none — but any
    rows present are enforced."""
    run_id = os.environ.get("MINI_ORK_RUN_ID") or os.path.basename(RUN_DIR.rstrip("/"))
    aliases = [alias for _l, _n, alias in LANES]
    result = panel_heterogeneity.check_panel(run_id, aliases, expect=_expected_models())
    if result["skipped"]:
        ev.write(f"served_models: skipped ({result['skipped']})\n")
        return
    ev.write(f"served_models: served={json.dumps(result['served'])} "
             f"families={json.dumps(result['families'])}\n")
    for problem in result["problems"]:
        fail(f"served_models:{problem}")


# ── synthesis ───────────────────────────────────────────────────────────────

def check_synthesis(f_ids: list[str], s_ids: list[str], fail) -> None:
    path = os.path.join(RUN_DIR, "synthesis.md")
    if not os.path.isfile(path):
        fail("MISSING_SYNTHESIS:synthesis.md")
        return
    text = open(path, encoding="utf-8", errors="replace").read()
    idx = [heading_index(text, s) for s in SYNTH_SECTIONS]
    if not (all(i >= 0 for i in idx) and idx == sorted(idx)):
        absent = [s for s, i in zip(SYNTH_SECTIONS, idx) if i < 0]
        fail(f"synthesis.md:sections_missing_or_out_of_order:{absent or 'reorder'}")
    for needle in [lane for lane, _n, _a in LANES] + f_ids + s_ids:
        if not re.search(r"\b" + re.escape(needle) + r"\b", text, re.I):
            fail(f"synthesis.md:missing_{needle}")


def main() -> int:
    missing: list[str] = []
    fail = missing.append
    with open(EVIDENCE, "w", encoding="utf-8") as ev:
        f_ids, s_ids = declared_ids(kickoff_text(RUN_DIR))
        ev.write(f"kickoff_ids: findings={f_ids} fixes={s_ids}\n")
        if not f_ids:
            fail("kickoff_declares_no_findings: need F<n> items under a '## …Findings…' heading")
        if not s_ids:
            fail("kickoff_declares_no_fixes: need S<n> items under a '## …fixes…' heading")
        for lane, _node, _alias in LANES:
            check_report(os.path.join(RUN_DIR, f"judge-{lane}-audit.md"),
                         len(f_ids) + len(s_ids), fail, ev)
        sidecars = {}
        for lane, node, _alias in LANES:
            obj = check_sidecar(lane, node, f_ids, s_ids, fail)
            if obj is not None:
                sidecars[lane] = obj
        # Every lane already must carry REQUIRED_TOP (check_sidecar), which is
        # all a mechanical join needs. A lane that adds provenance keys on top
        # (deepseek wrote head_commit + kickoff_path on the first live run)
        # breaks nothing, so extras are recorded, not failed.
        for lane in sorted(sidecars):
            extra = sorted(set(sidecars[lane]) - REQUIRED_TOP)
            if extra:
                ev.write(f"{lane}: extra sidecar keys (allowed): {extra}\n")
        check_served_models(fail, ev)
        check_synthesis(f_ids, s_ids, fail)
    print(json.dumps({"verifier": "panel-completeness", "pass": not missing,
                      "evidence_path": EVIDENCE, "missing": missing}, indent=2))
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
