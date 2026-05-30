#!/usr/bin/env bash
# group_evolver.sh — GroupEvolver: workflow topology evolution via typed mutations.
#
# Public API:
#   group_propose <group_perf_history_json>
#       → emits N WorkflowCandidate JSON objects on stdout (one per line)
#
# Selection score: selection_score = performance * sqrt(novelty)   [Ch 20]
# Novelty dimensions: topology + tool_sequence + failure_coverage
# Mutation types (typed):
#   add_node | remove_node | rewrite_node_prompt | add_verifier |
#   change_model_lane | reorder_edges | add_human_gate | split_by_task_type

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Typed mutations registry — weights control sampling probability.
_GROUP_MUTATION_TYPES=(
  add_node
  remove_node
  rewrite_node_prompt
  add_verifier
  change_model_lane
  reorder_edges
  add_human_gate
  split_by_task_type
)

# desc: Compute novelty score (0.0–1.0) across three dimensions for a candidate
#       workflow relative to existing workflows in group_perf_history_json.
#       Returns float on stdout.
_group_novelty_score() {
  local candidate_json="${1:?candidate_json required}"
  local history_json="${2:?history_json required}"
  python3 - "$candidate_json" "$history_json" <<'PY'
import json, sys, math

try:
    cand    = json.loads(sys.argv[1])
    history = json.loads(sys.argv[2])
    if not isinstance(history, list):
        history = []
except Exception:
    print("0.5")
    sys.exit(0)

def node_set(w):
    return set(w.get("nodes", {}).keys() if isinstance(w.get("nodes"), dict)
               else w.get("nodes", []))

def tool_seq(w):
    tools = []
    for n in (w.get("nodes", {}).values() if isinstance(w.get("nodes"), dict)
              else w.get("nodes", [])):
        if isinstance(n, dict):
            tools.extend(n.get("tools", []))
    return tuple(tools)

def failure_coverage(w):
    return set(w.get("failure_modes_handled", []))

c_nodes = node_set(cand)
c_tools = tool_seq(cand)
c_fail  = failure_coverage(cand)

if not history:
    print("1.0")
    sys.exit(0)

# Topology novelty: avg Jaccard distance from existing node sets
topo_dists = []
tool_dists = []
fail_dists = []
for h in history:
    h_nodes = node_set(h)
    h_tools = tool_seq(h)
    h_fail  = failure_coverage(h)
    # Jaccard distance
    union = c_nodes | h_nodes
    inter = c_nodes & h_nodes
    topo_dists.append(1.0 - (len(inter) / len(union)) if union else 0.0)
    # Tool sequence distance (normalized edit distance approximation)
    max_len = max(len(c_tools), len(h_tools), 1)
    common  = sum(1 for a, b in zip(c_tools, h_tools) if a == b)
    tool_dists.append(1.0 - common / max_len)
    # Failure coverage Jaccard distance
    fu = c_fail | h_fail
    fi = c_fail & h_fail
    fail_dists.append(1.0 - (len(fi) / len(fu)) if fu else 0.0)

avg_topo  = sum(topo_dists)  / len(topo_dists)
avg_tool  = sum(tool_dists)  / len(tool_dists)
avg_fail  = sum(fail_dists)  / len(fail_dists)

# Weighted combination: topology 50%, tool sequence 30%, failure coverage 20%
novelty = 0.50 * avg_topo + 0.30 * avg_tool + 0.20 * avg_fail
novelty = max(0.0, min(1.0, novelty))
print(f"{novelty:.6f}")
PY
}

# desc: Propose N WorkflowCandidate objects from group performance history.
#       group_perf_history_json is an array of:
#         { workflow_id, nodes, edges, performance, failure_modes_handled,
#           tool_sequence, model_lane, task_class, version_id }
#       Emits one WorkflowCandidate JSON per stdout line.
group_propose() {
  local history_json="${1:?group_perf_history_json required}"
  local n_candidates="${MINI_ORK_GROUP_CANDIDATES:-5}"

  python3 - "$history_json" "$n_candidates" \
    "${MINI_ORK_DB:-}" <<'PY'
import json, sys, math, random, copy, uuid, time

history_raw = sys.argv[1]
n           = int(sys.argv[2])

try:
    history = json.loads(history_raw)
    if not isinstance(history, list) or not history:
        print(json.dumps({
            "error": "empty or invalid history",
            "candidates": [],
        }))
        sys.exit(0)
except json.JSONDecodeError as e:
    print(f"group_propose: invalid JSON: {e}", file=sys.stderr)
    sys.exit(1)

MUTATION_TYPES = [
    "add_node", "remove_node", "rewrite_node_prompt", "add_verifier",
    "change_model_lane", "reorder_edges", "add_human_gate", "split_by_task_type",
]

MODEL_LANES = ["fast", "balanced", "quality", "reasoning"]

def selection_score(w):
    perf    = float(w.get("performance", 0.0))
    novelty = _novelty(w, history)
    return perf * math.sqrt(max(0.0, novelty))

def _novelty(cand, hist):
    c_nodes = set(cand.get("nodes", {}).keys() if isinstance(cand.get("nodes"), dict)
                  else cand.get("nodes", []))
    if not hist:
        return 1.0
    dists = []
    for h in hist:
        h_nodes = set(h.get("nodes", {}).keys() if isinstance(h.get("nodes"), dict)
                      else h.get("nodes", []))
        union = c_nodes | h_nodes
        inter = c_nodes & h_nodes
        dists.append(1.0 - len(inter) / len(union) if union else 0.0)
    return sum(dists) / len(dists) if dists else 0.5

def apply_mutation(workflow, mutation_type):
    w = copy.deepcopy(workflow)
    nodes = w.get("nodes", {})
    edges = w.get("edges", [])
    node_names = list(nodes.keys()) if isinstance(nodes, dict) else nodes

    if mutation_type == "add_node":
        new_name = f"node_{uuid.uuid4().hex[:6]}"
        if isinstance(nodes, dict):
            nodes[new_name] = {"tools": [], "model_lane": w.get("model_lane", "balanced")}
        w["mutation_applied"] = {"type": "add_node", "added": new_name}

    elif mutation_type == "remove_node" and len(node_names) > 1:
        rm = random.choice(node_names)
        if isinstance(nodes, dict):
            nodes.pop(rm, None)
        w["mutation_applied"] = {"type": "remove_node", "removed": rm}

    elif mutation_type == "rewrite_node_prompt" and node_names:
        target = random.choice(node_names)
        if isinstance(nodes, dict):
            nodes[target]["prompt_hash"] = f"ph-{uuid.uuid4().hex[:8]}"
        w["mutation_applied"] = {"type": "rewrite_node_prompt", "target": target}

    elif mutation_type == "add_verifier":
        verifier_name = f"verifier_{uuid.uuid4().hex[:6]}"
        if isinstance(nodes, dict):
            nodes[verifier_name] = {"role": "verifier", "tools": []}
        w["mutation_applied"] = {"type": "add_verifier", "added": verifier_name}

    elif mutation_type == "change_model_lane":
        new_lane = random.choice([l for l in MODEL_LANES if l != w.get("model_lane", "balanced")])
        w["model_lane"] = new_lane
        w["mutation_applied"] = {"type": "change_model_lane", "new_lane": new_lane}

    elif mutation_type == "reorder_edges" and len(edges) >= 2:
        random.shuffle(edges)
        w["edges"] = edges
        w["mutation_applied"] = {"type": "reorder_edges"}

    elif mutation_type == "add_human_gate":
        gate_name = f"human_gate_{uuid.uuid4().hex[:6]}"
        if isinstance(nodes, dict):
            nodes[gate_name] = {"role": "human_gate", "tools": []}
        w["mutation_applied"] = {"type": "add_human_gate", "added": gate_name}

    elif mutation_type == "split_by_task_type" and node_names:
        split_node = random.choice(node_names)
        branch_a   = f"{split_node}_branch_a"
        branch_b   = f"{split_node}_branch_b"
        if isinstance(nodes, dict):
            orig = nodes.pop(split_node, {})
            nodes[branch_a] = {**orig, "task_type_filter": "primary"}
            nodes[branch_b] = {**orig, "task_type_filter": "secondary"}
        w["mutation_applied"] = {"type": "split_by_task_type", "split": split_node}
    else:
        w["mutation_applied"] = {"type": mutation_type, "note": "no-op (preconditions not met)"}

    return w

# Rank history by selection score and pick parents via weighted sampling
scored = sorted(history, key=lambda w: selection_score(w), reverse=True)
parents = scored[:max(1, len(scored) // 2 + 1)]  # top half

candidates = []
now = int(time.time())
for i in range(n):
    parent = random.choice(parents)
    mutation = random.choice(MUTATION_TYPES)
    child   = apply_mutation(parent, mutation)
    cid     = f"wc-{uuid.uuid4().hex[:16]}"
    child.update({
        "candidate_id": cid,
        "parent_id":    parent.get("workflow_id", parent.get("candidate_id", "")),
        "mutation_type": mutation,
        "proposed_at":  now,
        "selection_score": round(selection_score(parent), 6),
        "novelty_estimated": round(_novelty(child, history), 6),
    })
    candidates.append(child)

for c in candidates:
    print(json.dumps(c))
PY
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "group_evolver.sh — source me and call group_propose <group_perf_history_json>"
fi
