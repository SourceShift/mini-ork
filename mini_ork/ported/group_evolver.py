"""Parity gate: mini_ork.ported.group_evolver vs lib/group_evolver.sh.

Faithful port of ``group_propose``. Pure-compute: accepts a workflow-history
JSON list, emits N WorkflowCandidate dicts. Bash wrapper is the authoritative
oracle; the parity test (tests/unit/test_group_evolver_py.py) diffs against
the live bash invocation byte-for-byte after stripping the nondeterministic
envelope (candidate_id, parent_id, proposed_at).

Public API:
  propose(history, n_candidates=5, *, seed=None) -> list[dict]
  novelty_score(candidate, history) -> float       # bash's _group_novelty_score
"""
from __future__ import annotations

import copy
import json
import math
import os
import random
import time
import uuid

__all__ = ["propose", "novelty_score"]

MUTATION_TYPES = [
    "add_node", "remove_node", "rewrite_node_prompt", "add_verifier",
    "change_model_lane", "reorder_edges", "add_human_gate", "split_by_task_type",
]
MODEL_LANES = ["fast", "balanced", "quality", "reasoning"]


def _nodes(w):  # node names as a set (handles dict-or-list node storage)
    n = w.get("nodes", {})
    return set(n.keys() if isinstance(n, dict) else (n or []))


def _tools(w):  # flattened tool sequence across all nodes
    n = w.get("nodes", {})
    items = n.values() if isinstance(n, dict) else (n or [])
    out = []
    for x in items:
        if isinstance(x, dict):
            out.extend(x.get("tools", []))
    return tuple(out)


def _novelty(cand, hist):  # bash lines 143-155: nodes-only Jaccard distance
    cn = _nodes(cand)
    if not hist:
        return 1.0
    ds = []
    for h in hist:
        hn = _nodes(h)
        u = cn | hn
        ds.append(1.0 - len(cn & hn) / len(u) if u else 0.0)
    return sum(ds) / len(ds) if ds else 0.5


def novelty_score(candidate, history):  # bash _group_novelty_score (3-dim)
    if not isinstance(history, list):
        history = []
    cn, ct, cf = _nodes(candidate), _tools(candidate), set(candidate.get("failure_modes_handled", []) or [])
    if not history:
        return 1.0
    td, tl, fd = [], [], []
    for h in history:
        hn, ht, hf = _nodes(h), _tools(h), set(h.get("failure_modes_handled", []) or [])
        u, i = cn | hn, cn & hn
        td.append(1.0 - len(i) / len(u) if u else 0.0)
        ml = max(len(ct), len(ht), 1)
        tl.append(1.0 - sum(1 for a, b in zip(ct, ht) if a == b) / ml)
        fu, fi = cf | hf, cf & hf
        fd.append(1.0 - len(fi) / len(fu) if fu else 0.0)
    nv = 0.50 * sum(td) / len(td) + 0.30 * sum(tl) / len(tl) + 0.20 * sum(fd) / len(fd)
    return max(0.0, min(1.0, nv))


def _score(w, hist):  # perf * sqrt(max(0, novelty))
    return float(w.get("performance", 0.0)) * math.sqrt(max(0.0, _novelty(w, hist)))


def apply_mutation(workflow, mutation_type):  # bash lines 157-215 verbatim
    w = copy.deepcopy(workflow)
    nodes = w.get("nodes", {})
    edges = w.get("edges", [])
    nn = list(nodes.keys()) if isinstance(nodes, dict) else list(nodes or [])
    if mutation_type == "add_node":
        nm = f"node_{uuid.uuid4().hex[:6]}"
        if isinstance(nodes, dict):
            nodes[nm] = {"tools": [], "model_lane": w.get("model_lane", "balanced")}
        w["mutation_applied"] = {"type": "add_node", "added": nm}
    elif mutation_type == "remove_node" and len(nn) > 1:
        rm = random.choice(nn)
        if isinstance(nodes, dict):
            nodes.pop(rm, None)
        w["mutation_applied"] = {"type": "remove_node", "removed": rm}
    elif mutation_type == "rewrite_node_prompt" and nn:
        tgt = random.choice(nn)
        if isinstance(nodes, dict):
            nodes[tgt]["prompt_hash"] = f"ph-{uuid.uuid4().hex[:8]}"
        w["mutation_applied"] = {"type": "rewrite_node_prompt", "target": tgt}
    elif mutation_type == "add_verifier":
        vn = f"verifier_{uuid.uuid4().hex[:6]}"
        if isinstance(nodes, dict):
            nodes[vn] = {"role": "verifier", "tools": []}
        w["mutation_applied"] = {"type": "add_verifier", "added": vn}
    elif mutation_type == "change_model_lane":
        cur = w.get("model_lane", "balanced")
        nl = random.choice([l for l in MODEL_LANES if l != cur])
        w["model_lane"] = nl
        w["mutation_applied"] = {"type": "change_model_lane", "new_lane": nl}
    elif mutation_type == "reorder_edges" and len(edges) >= 2:
        random.shuffle(edges)
        w["edges"] = edges
        w["mutation_applied"] = {"type": "reorder_edges"}
    elif mutation_type == "add_human_gate":
        gn = f"human_gate_{uuid.uuid4().hex[:6]}"
        if isinstance(nodes, dict):
            nodes[gn] = {"role": "human_gate", "tools": []}
        w["mutation_applied"] = {"type": "add_human_gate", "added": gn}
    elif mutation_type == "split_by_task_type" and nn:
        sp = random.choice(nn)
        ba, bb = f"{sp}_branch_a", f"{sp}_branch_b"
        if isinstance(nodes, dict):
            orig = nodes.pop(sp, {})
            nodes[ba] = {**orig, "task_type_filter": "primary"}
            nodes[bb] = {**orig, "task_type_filter": "secondary"}
        w["mutation_applied"] = {"type": "split_by_task_type", "split": sp}
    else:
        w["mutation_applied"] = {"type": mutation_type, "note": "no-op (preconditions not met)"}
    return w


def propose(history_json_or_list, n_candidates=None, *, seed=None):
    """Mirror ``group_propose`` from lib/group_evolver.sh.

    Returns N WorkflowCandidate dicts; bash emits one per stdout line via
    ``json.dumps(c)`` (default separators). Parity test compares NDJSON
    byte-equivalence after stripping candidate_id, parent_id, proposed_at.
    """
    if seed is not None:
        random.seed(seed)
    if n_candidates is None:
        n_candidates = int(os.environ.get("MINI_ORK_GROUP_CANDIDATES", "5"))
    if isinstance(history_json_or_list, str):
        try:
            history = json.loads(history_json_or_list)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"group_propose: invalid JSON: {e}") from e
    else:
        history = history_json_or_list
    if not isinstance(history, list) or not history:
        return []
    scored = sorted(history, key=lambda w: _score(w, history), reverse=True)
    parents = scored[:max(1, len(scored) // 2 + 1)]
    out, now = [], int(time.time())
    for _ in range(n_candidates):
        parent = random.choice(parents)
        mut = random.choice(MUTATION_TYPES)
        child = apply_mutation(parent, mut)
        child.update({
            "candidate_id":      f"wc-{uuid.uuid4().hex[:16]}",
            "parent_id":         parent.get("workflow_id", parent.get("candidate_id", "")),
            "mutation_type":     mut,
            "proposed_at":       now,
            "selection_score":   round(_score(parent, history), 6),
            "novelty_estimated": round(_novelty(child, history), 6),
        })
        out.append(child)
    return out