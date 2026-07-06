"""Python port of bin/mini-ork-execute — the executor (INCREMENTAL).

bin/mini-ork-execute is the 3449-line node-dispatch engine. Its orchestration
core (_dispatch_node + the DAG loop) drives every run and warrants dedicated
harsh-critic review before its default flips; this module lands the deterministic
HELPER LAYER first, behind a live-bash parity gate, so the risky core ports onto
verified foundations.

Ported here (all pure / transcribed verbatim):
    reward_from_status(status, verdict)     — status/verdict → GRPO reward
    dispatch_chain(node_type, lead)         — role-aware fallback lane chain (deduped)
    learning_static_lane(node_type, lane)   — static lane synthesis for unpinned nodes
    finish_reason_for_failure(rc, text)     — rc/text → finish reason
    infer_trace_code_region(payload)        — files_written → top-level code region
"""
from __future__ import annotations

import json
import os


def reward_from_status(status: str = "", verdict: str = "") -> str:
    v = (verdict or "").lower()
    if v in ("approve", "approved", "pass", "passed", "success", "ok"):
        return "1.0"
    if v in ("reject", "rejected", "fail", "failed", "request_changes", "needs_revision", "escalate"):
        return "0.0"
    s = (status or "").lower()
    if s in ("success", "published", "done", "approve", "approved", "pass", "passed"):
        return "1.0"
    if s in ("failure", "failed", "rolled_back", "blocked", "crash", "escalated", "reject", "rejected"):
        return "0.0"
    return "0.5"


_CODING_ROLES = {"implementer", "worker", "spec_author", "healer", "planner", "researcher",
                 "reflector", "replanner", "synthesizer", "bdd_runner"}
_REVIEW_ROLES = {"reviewer", "spec_reviewer", "verifier", "brain"}


def dispatch_chain(node_type: str, lead: str) -> str:
    """Lead lane + role-category fallback tail, comma-joined, order-preserving dedup."""
    tail = ""
    if node_type in _CODING_ROLES:
        tail = os.environ.get("MO_FALLBACK_CODING", "minimax,codex,sonnet")
    elif node_type in _REVIEW_ROLES:
        tail = os.environ.get("MO_FALLBACK_REVIEW", "opus,kimi,sonnet")
    if not tail:
        return lead
    seen = set()
    out = []
    for x in (lead + "," + tail).split(","):
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return ",".join(out)


def learning_static_lane(node_type: str, current_lane: str) -> str:
    frontier = os.environ.get("MO_FRONTIER_LANE", "opus_lens")
    cheap = os.environ.get("MO_CHEAP_LANE", "kimi_lens")
    # A recipe-pinned lane (current_lane != node_type) is explicit author intent
    # + the learning loop's exploration arm — keep it.
    if current_lane != node_type:
        return current_lane
    if node_type == "reviewer":
        return frontier
    if node_type in ("researcher", "implementer"):
        return cheap
    return current_lane


def finish_reason_for_failure(rc, text: str = "") -> str:
    rc = int(rc) if str(rc).lstrip("-").isdigit() else 1
    if rc == 124:
        return "timeout"
    if rc == 43 or "lane_fuse_open" in (text or ""):
        return "error"
    if "cost_circuit_open" in (text or ""):
        return "cost_limit"
    return "error"


def infer_trace_code_region(payload: str) -> str:
    """files_written → the top-level dir of the first in-repo relative file
    ('(root)' for root-level files). Verbatim transcription of the bash's
    embedded python; returns '' when nothing maps (bash prints nothing)."""
    try:
        data = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return ""
    run_dir = os.environ.get("MINI_ORK_RUN_DIR") or os.environ.get("RUN_DIR") or ""
    roots = [os.environ.get("MO_TARGET_CWD") or "", os.environ.get("MINI_ORK_ROOT") or "", os.getcwd()]
    roots = [os.path.abspath(r) for r in roots if r]

    def _decode_files(value):
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return []
            try:
                decoded = json.loads(s)
            except json.JSONDecodeError:
                return [s]
            return decoded if isinstance(decoded, list) else []
        return []

    def _relativize(path):
        if not isinstance(path, str):
            return None
        p = path.strip()
        if not p or "://" in p:
            return None
        if run_dir:
            run_abs = os.path.abspath(run_dir)
            p_abs = os.path.abspath(p) if os.path.isabs(p) else os.path.abspath(os.path.join(os.getcwd(), p))
            try:
                if os.path.commonpath([run_abs, p_abs]) == run_abs:
                    return None
            except ValueError:
                pass
        if os.path.isabs(p):
            p_abs = os.path.abspath(p)
            for root in roots:
                try:
                    if os.path.commonpath([root, p_abs]) == root:
                        return os.path.relpath(p_abs, root)
                except ValueError:
                    continue
            return None
        return p

    for raw in _decode_files(data.get("files_written")):
        rel = _relativize(raw)
        if not rel:
            continue
        rel = rel.replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        if not rel or rel.startswith("../"):
            continue
        return rel.split("/", 1)[0] if "/" in rel else "(root)"
    return ""
