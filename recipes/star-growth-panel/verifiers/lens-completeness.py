#!/usr/bin/env python3
# Completeness gate for the star-growth-panel recipe (four lenses).
#
# Grades three things:
#
#   lens-<lane>.md        the human report — five sections in order, at least
#                         six lane-scoped candidate blocks, every rating a
#                         valid enum, every candidate carrying a receipt.
#   context-<node>.json   the machine sidecar — one shared schema across all
#                         four lanes, with ids that match the report and a
#                         shortlist that is a permutation of them.
#   llm_calls             the four lenses were served by four DISTINCT model
#                         families, not four lane strings (see
#                         mini_ork.verify.panel_heterogeneity).
#
# plus the synthesis: required sections in order, 8–12 ranked items, every
# `sources` id resolving to a real candidate, and no candidate silently
# dropped (every sidecar id must appear somewhere in the synthesis).
#
# rc IS the verdict (0 = complete, 1 = miss); the JSON on stdout explains it.

from __future__ import annotations

import glob
import json
import os
import re
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
EVIDENCE = os.path.join(RUN_DIR, "verifier-lens-completeness.log")

# ── repo-root import shim (same shape as audit-judge-panel-4l) ──────────────
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

# (lane, node id, lane alias pinned by the workflow, candidate id prefix)
LANES = [
    ("growth", "growth_lens", "codex_lens", "G"),
    ("activation", "activation_lens", "glm_lens", "A"),
    ("ecosystem", "ecosystem_lens", "deepseek_lens", "E"),
    ("capability", "capability_lens", "minimax_lens", "K"),
]
SECTIONS = [
    "Evidence Read",
    "Candidate Features",
    "Ranked Shortlist",
    "mini-ork Gap Analysis",
    "Open Questions",
]
SYNTH_SECTIONS = [
    "Panel Summary",
    "Top 10 Adoption Plan",
    "Score Matrix",
    "Cross-Lens Consensus",
    "Rejected Or Not Worth It",
    "90-Day Sequence",
    "Questions For Human Decision",
]
VALID_IMPACT = {"high", "medium", "low"}
VALID_EFFORT = {"S", "M", "L"}
VALID_CONFIDENCE = {"high", "medium", "low"}

REQUIRED_SIDECAR_TOP = {
    "schema_version", "panel_id", "lane", "model", "candidates", "shortlist",
    "gap_analysis", "open_questions", "read_only_attestation",
}
REQUIRED_CANDIDATE = {
    "candidate_id", "name", "evidence", "star_impact", "effort", "fit",
    "why", "first_step",
}
REQUIRED_ATTESTATION = {"reference_modified", "target_modified",
                        "files_written"}

# Field values are read only from a line that STARTS with the field name
# (after an optional list marker), possibly wrapped in ** or backticks — the
# anchoring rule audit-judge-panel-4l paid for: an unanchored regex reads prose
# as a field value, and a wrapped continuation line is never a field.
_LINE_START = r"^(?:[ \t]*[-*+>][ \t]+(?:>[ \t]*)?)?"
_FIELD = r"[*`]*%s[*`]*[ \t]*:[ \t]*[*`]*[ \t]*([A-Za-z_]+)"

_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_CANDIDATE_RE = re.compile(r"^###\s+([GAEK])(\d+)\b[ \t]*[—:-]")
_TOP_RE = re.compile(r"^###\s+T(\d+)\b[ \t]*[—:-]")
_SOURCES_RE = re.compile(_LINE_START + r"[*`]*sources[*`]*[ \t]*:[ \t]*(.+)$",
                         re.I | re.M)
_IMPACT_RE = re.compile(_LINE_START + _FIELD % "star_impact", re.I | re.M)
_EFFORT_RE = re.compile(_LINE_START + _FIELD % "effort", re.I | re.M)
_FIT_RE = re.compile(_LINE_START + _FIELD % "(?:mini_ork_fit|fit)", re.I | re.M)
_CONFIDENCE_RE = re.compile(_LINE_START + _FIELD % "confidence", re.I | re.M)


def _without_fences(text: str) -> str:
    out, in_fence = [], False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return "\n".join(out)


def heading_index(text: str, heading: str) -> int:
    m = re.search(r"^#{1,4}\s+(?:\d+\.\s*)?" + re.escape(heading) + r"\b",
                  text, re.M | re.I)
    return m.start() if m else -1


def _read(path: str) -> str:
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def candidate_blocks(text: str, prefix: str) -> list[dict]:
    """``[{id, body}]`` for every ``### <prefix><n> — …`` block."""
    lines = _without_fences(text).splitlines()
    out: list[dict] = []
    current: dict | None = None
    for line in lines:
        m = _CANDIDATE_RE.match(line)
        if m:
            if m.group(1) != prefix:
                current = None
                continue
            current = {"id": m.group(1) + m.group(2), "body": []}
            out.append(current)
            continue
        if line.startswith("#"):
            current = None
            continue
        if current is not None:
            current["body"].append(line)
    return out


def check_report(lane: str, prefix: str, fail, ev) -> dict[str, str]:
    path = os.path.join(RUN_DIR, f"lens-{lane}.md")
    name = os.path.basename(path)
    text = _read(path)
    if not text:
        fail(f"MISSING_REPORT:{name}")
        return {}
    idx = [heading_index(text, s) for s in SECTIONS]
    order_ok = all(i >= 0 for i in idx) and idx == sorted(idx)
    blocks = candidate_blocks(text, prefix)
    ids = [b["id"] for b in blocks]
    ev.write(f"{name}: lines={text.count(chr(10))} candidates={len(ids)} "
             f"sections_ordered={order_ok}\n")
    if text.count("\n") < 40:
        fail(f"{name}:too_short({text.count(chr(10))})")
    if not order_ok:
        absent = [s for s, i in zip(SECTIONS, idx) if i < 0]
        fail(f"{name}:sections_missing_or_out_of_order:{absent or 'reorder'}")
    if len(ids) < 6:
        fail(f"{name}:too_few_candidates({len(ids)}/6)")
    if len(set(ids)) != len(ids):
        fail(f"{name}:duplicate_candidate_ids")
    for b in blocks:
        body = "\n".join(b["body"])
        if not re.search(_LINE_START + r"[*`]*evidence[*`]*[ \t]*:[ \t]*\S",
                         body, re.I | re.M):

            fail(f"{name}:candidate_{b['id']}_missing_evidence")
        if not re.search(_LINE_START + r"[*`]*first_step[*`]*[ \t]*:[ \t]*\S",
                         body, re.I | re.M):
            fail(f"{name}:candidate_{b['id']}_missing_first_step")
        for label, rx, allowed in (
            ("star_impact", _IMPACT_RE, VALID_IMPACT),
            ("effort", _EFFORT_RE, VALID_EFFORT),
            ("fit", _FIT_RE, VALID_IMPACT),
        ):
            vals = rx.findall(body)
            if not vals:
                fail(f"{name}:candidate_{b['id']}_missing_{label}")
            elif vals[0] not in allowed:
                fail(f"{name}:candidate_{b['id']}_bad_{label}:{vals[0]}")
    # Ranked shortlist must cover every candidate, never silently omit one.
    shortlist_section = text[idx[2]:] if idx[2] >= 0 else ""
    for cid in ids:
        if not re.search(r"\b" + re.escape(cid) + r"\b", shortlist_section):
            fail(f"{name}:shortlist_omits_{cid}")
    return {b["id"]: "\n".join(b["body"]) for b in blocks}


def check_sidecar(lane: str, node: str, prefix: str, report_ids: list[str],
                  fail, ev) -> dict | None:
    path = os.path.join(RUN_DIR, f"context-{node}.json")
    if not os.path.isfile(path):
        hits = [p for p in sorted(glob.glob(os.path.join(RUN_DIR,
                                                         "context-*.json")))
                if os.path.basename(p).startswith(f"context-{lane}")]
        if not hits:
            fail(f"MISSING_SIDECAR:context-{node}.json")
            return None
        path = hits[0]
    try:
        obj = json.loads(_read(path))
    except ValueError as exc:
        fail(f"{lane}:sidecar_not_json:{exc}")
        return None
    if not isinstance(obj, dict):
        fail(f"{lane}:sidecar_not_an_object")
        return None

    absent = REQUIRED_SIDECAR_TOP - set(obj)
    if absent:
        fail(f"{lane}:sidecar_missing_keys:{sorted(absent)}")
    for key in ("schema_version", "panel_id", "lane", "model"):
        if not obj.get(key):
            fail(f"{lane}:sidecar_empty_{key}")
    if obj.get("lane") not in (None, lane, node):
        fail(f"{lane}:sidecar_lane_mismatch:{obj.get('lane')}")

    cands = [x for x in (obj.get("candidates") or []) if isinstance(x, dict)]
    ids: list[str] = []
    for x in cands:
        cid = x.get("candidate_id")
        if isinstance(cid, str):
            ids.append(cid)
    ev.write(f"context-{node}.json: candidates={len(cands)} ids={ids}\n")
    if len(cands) < 6:
        fail(f"{lane}:sidecar_too_few_candidates({len(cands)}/6)")
    if len(set(ids)) != len(ids):
        fail(f"{lane}:sidecar_duplicate_ids")
    if any(not (isinstance(i, str) and re.fullmatch(prefix + r"\d+", i))
           for i in ids):
        fail(f"{lane}:sidecar_bad_id_prefix:{ids}")
    if report_ids and ids != report_ids:
        fail(f"{lane}:sidecar_ids_differ_from_report:{ids} != {report_ids}")
    for x in cands:
        cid = x.get("candidate_id")
        miss = REQUIRED_CANDIDATE - set(x)
        if miss:
            fail(f"{lane}:candidate_{cid}_missing:{sorted(miss)}")
        if str(x.get("star_impact", "")) not in VALID_IMPACT:
            fail(f"{lane}:candidate_{cid}_bad_star_impact:{x.get('star_impact')}")
        if str(x.get("effort", "")) not in VALID_EFFORT:
            fail(f"{lane}:candidate_{cid}_bad_effort:{x.get('effort')}")
        if str(x.get("fit", "")) not in VALID_IMPACT:
            fail(f"{lane}:candidate_{cid}_bad_fit:{x.get('fit')}")
        if not (isinstance(x.get("evidence"), list) and x["evidence"]):
            fail(f"{lane}:candidate_{cid}_empty_evidence")

    order = [x for x in (obj.get("shortlist") or []) if isinstance(x, str)]
    if sorted(order) != sorted(ids) or len(order) != len(ids):
        fail(f"{lane}:shortlist_not_a_permutation_of_candidates:{order}")

    att = obj.get("read_only_attestation")
    if not isinstance(att, dict):
        fail(f"{lane}:read_only_attestation_missing")
    else:
        miss = REQUIRED_ATTESTATION - set(att)
        if miss:
            fail(f"{lane}:attestation_missing:{sorted(miss)}")
        if att.get("reference_modified") is not False:
            fail(f"{lane}:attestation_reference_modified_not_false")
        if att.get("target_modified") is not False:
            fail(f"{lane}:attestation_target_modified_not_false")
    return obj


def check_served_models(fail, ev) -> None:
    run_id = os.environ.get("MINI_ORK_RUN_ID") or os.path.basename(
        RUN_DIR.rstrip("/"))
    aliases = [alias for _l, _n, alias, _p in LANES]
    result = panel_heterogeneity.check_panel(run_id, aliases)
    if result["skipped"]:
        ev.write(f"served_models: skipped ({result['skipped']})\n")
        return
    ev.write(f"served_models: served={json.dumps(result['served'])} "
             f"families={json.dumps(result['families'])}\n")
    for problem in result["problems"]:
        fail(f"served_models:{problem}")


def check_synthesis(all_ids: set[str], fail, ev) -> None:
    path = os.path.join(RUN_DIR, "synthesis.md")
    text = _read(path)
    if not text:
        fail("MISSING_SYNTHESIS:synthesis.md")
        return
    idx = [heading_index(text, s) for s in SYNTH_SECTIONS]
    if not (all(i >= 0 for i in idx) and idx == sorted(idx)):
        absent = [s for s, i in zip(SYNTH_SECTIONS, idx) if i < 0]
        fail(f"synthesis.md:sections_missing_or_out_of_order:{absent or 'reorder'}")
    for lane, _n, _a, _p in LANES:
        if lane not in text.lower():
            fail(f"synthesis.md:does_not_name_lane_{lane}")

    prose = _without_fences(text)
    tops = [m.group(1) for m in map(_TOP_RE.match, prose.splitlines()) if m]
    ev.write(f"synthesis.md: top_items={tops}\n")
    if not (8 <= len(tops) <= 12):
        fail(f"synthesis.md:top_item_count({len(tops)})_not_in_8..12")
    if len(set(tops)) != len(tops):
        fail("synthesis.md:duplicate_top_item_ids")

    sources_lists = [m.group(1) for m in _SOURCES_RE.finditer(prose)]
    if len(sources_lists) < len(tops):
        fail(f"synthesis.md:sources_lines({len(sources_lists)})<top_items")
    cited: set[str] = set()
    for raw in sources_lists:
        for token in re.findall(r"\b([GAEK]\d+)\b", raw):
            cited.add(token)
            if token not in all_ids:
                fail(f"synthesis.md:sources_unknown_candidate:{token}")
    if not cited:
        fail("synthesis.md:no_source_candidate_ids_cited")

    # No silent drops: every candidate must appear somewhere in the synthesis
    # (top plan or rejected section) — the gate cannot judge the reason, only
    # that the id was considered.
    for cid in sorted(all_ids):
        if not re.search(r"\b" + re.escape(cid) + r"\b", text):
            fail(f"synthesis.md:candidate_dropped:{cid}")

    bad_i = {v for v in _IMPACT_RE.findall(prose) if v not in VALID_IMPACT}
    bad_e = {v for v in _EFFORT_RE.findall(prose) if v not in VALID_EFFORT}
    bad_c = {v for v in _CONFIDENCE_RE.findall(prose) if v not in VALID_CONFIDENCE}
    if bad_i:
        fail(f"synthesis.md:invalid_star_impact:{sorted(bad_i)}")
    if bad_e:
        fail(f"synthesis.md:invalid_effort:{sorted(bad_e)}")
    if bad_c:
        fail(f"synthesis.md:invalid_confidence:{sorted(bad_c)}")


def main() -> int:
    missing: list[str] = []
    fail = missing.append
    with open(EVIDENCE, "w", encoding="utf-8") as ev:
        all_ids: set[str] = set()
        for lane, node, _alias, prefix in LANES:
            report_ids = list(check_report(lane, prefix, fail, ev))
            obj = check_sidecar(lane, node, prefix, report_ids, fail, ev)
            if obj is not None:
                all_ids.update(x["candidate_id"] for x in
                               (obj.get("candidates") or [])
                               if isinstance(x, dict)
                               and isinstance(x.get("candidate_id"), str))
            else:
                all_ids.update(report_ids)
        check_served_models(fail, ev)
        check_synthesis(all_ids, fail, ev)
    print(json.dumps({"verifier": "lens-completeness", "pass": not missing,
                      "evidence_path": EVIDENCE, "missing": missing},
                     indent=2))
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
