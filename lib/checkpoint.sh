#!/usr/bin/env bash
# checkpoint.sh — per-node checkpoint primitives for recipe resume.
#
# Implements roadmap Phase 3 item 10 (LobeHub deep-review): checkpoint
# / resume for recipes. The recursive-validate-impl recipe shipped
# at 2985cae has a recursive loop that, today, replays from node 0
# on every verifier failure even when nodes 1-3 already passed. That
# wastes 5-15x the LLM cost it should. This file is the primitive.
# Wiring into bin/mini-ork-execute (which dispatches each node) is a
# deliberate follow-up so reviewers can audit this shape first.
#
# Public API:
#   checkpoint_write   <node_id> <status> [<artifact_path>]
#       Records that node <node_id> finished with <status>
#       (success | failure | skipped) into $MINI_ORK_RUN_DIR/.checkpoint.json.
#       Optional artifact path lets the resume runner verify the
#       produced output still exists before skipping the node.
#       rc=0 on success.
#   checkpoint_can_resume <node_id>
#       Emits "yes" + the artifact_path if the node already finished
#       with status=success AND any recorded artifact still exists on
#       disk. Emits "no" otherwise. rc=0 always.
#   checkpoint_clear   [<node_id>]
#       Removes a node's checkpoint entry (or the entire file if no
#       node_id given). Used when the planner mutates the plan and
#       resume should NOT trust prior nodes.
#   checkpoint_summary
#       Emits a human-readable summary table on stdout.
#
# State file:
#   ${MINI_ORK_RUN_DIR}/.checkpoint.json
#   Shape: { "nodes": { "<node_id>": {"status": "...", "artifact_path": "...",
#                                     "completed_at": <unix_seconds>} }, ... }
#
# Why this exists in honesty terms:
#   Recursive recipes (recursive-self-improve, recursive-validate-impl)
#   loop over implement → verify cycles. Each iteration today re-runs
#   the planner + the lenses from scratch even though their output is
#   identical to last iteration's. Resume turns this into a pay-once
#   pattern; the divergence-kill safety net in workflow.yaml keeps
#   stuck loops from running indefinitely.

set -uo pipefail

_ckpt_resolve() {
  local _rd="${MINI_ORK_RUN_DIR:-}"
  if [ -z "$_rd" ]; then
    echo "checkpoint.sh: MINI_ORK_RUN_DIR unset; cannot persist" >&2
    return 2
  fi
  if [ ! -d "$_rd" ]; then
    mkdir -p "$_rd" 2>/dev/null || {
      echo "checkpoint.sh: failed to create $_rd" >&2
      return 2
    }
  fi
  printf '%s' "$_rd/.checkpoint.json"
}

checkpoint_write() {
  local _node="${1:-}"
  local _status="${2:-}"
  local _artifact="${3:-}"

  if [ -z "$_node" ] || [ -z "$_status" ]; then
    echo "checkpoint_write: usage: checkpoint_write <node_id> <status> [<artifact_path>]" >&2
    return 2
  fi
  case "$_status" in
    success|failure|skipped) ;;
    *) echo "checkpoint_write: status must be success|failure|skipped, got $_status" >&2; return 2 ;;
  esac

  local _path
  _path=$(_ckpt_resolve) || return $?

  if ! command -v python3 >/dev/null 2>&1; then
    echo "checkpoint_write: python3 unavailable" >&2
    return 2
  fi

  MO_CKPT_PATH="$_path" \
  MO_CKPT_NODE="$_node" \
  MO_CKPT_STATUS="$_status" \
  MO_CKPT_ART="$_artifact" \
  python3 - <<'PY'
import json, os, time

path = os.environ["MO_CKPT_PATH"]
node = os.environ["MO_CKPT_NODE"]
status = os.environ["MO_CKPT_STATUS"]
artifact = os.environ.get("MO_CKPT_ART") or None

data = {"nodes": {}}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict) and isinstance(loaded.get("nodes"), dict):
                data = loaded
    except Exception:
        pass

data.setdefault("nodes", {})[node] = {
    "status": status,
    "artifact_path": artifact,
    "completed_at": int(time.time()),
}

with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
}

checkpoint_can_resume() {
  local _node="${1:-}"
  if [ -z "$_node" ]; then
    echo "checkpoint_can_resume: usage: checkpoint_can_resume <node_id>" >&2
    echo "no"
    return 0
  fi

  local _path
  _path=$(_ckpt_resolve) || { echo "no"; return 0; }

  if [ ! -f "$_path" ]; then
    echo "no"
    return 0
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    echo "no"
    return 0
  fi

  MO_CKPT_PATH="$_path" \
  MO_CKPT_NODE="$_node" \
  python3 - <<'PY'
import json, os, sys
try:
    data = json.load(open(os.environ["MO_CKPT_PATH"], encoding="utf-8"))
except Exception:
    print("no"); sys.exit(0)
node = (data.get("nodes") or {}).get(os.environ["MO_CKPT_NODE"])
if not node or node.get("status") != "success":
    print("no"); sys.exit(0)
artifact = node.get("artifact_path")
if artifact and not os.path.exists(artifact):
    # Artifact recorded but missing on disk — resume would be unsafe.
    print("no"); sys.exit(0)
# Emit yes + the artifact_path (or empty) on the same line for callers
# that grep "^yes" before parsing.
print("yes\t" + (artifact or ""))
PY
}

checkpoint_clear() {
  local _node="${1:-}"
  local _path
  _path=$(_ckpt_resolve) || return $?
  if [ ! -f "$_path" ]; then
    return 0
  fi
  if [ -z "$_node" ]; then
    rm -f "$_path"
    return 0
  fi

  MO_CKPT_PATH="$_path" MO_CKPT_NODE="$_node" python3 - <<'PY'
import json, os
try:
    data = json.load(open(os.environ["MO_CKPT_PATH"], encoding="utf-8"))
except Exception:
    data = {"nodes": {}}
data.setdefault("nodes", {}).pop(os.environ["MO_CKPT_NODE"], None)
with open(os.environ["MO_CKPT_PATH"], "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
}

checkpoint_summary() {
  local _path
  _path=$(_ckpt_resolve) || return $?
  if [ ! -f "$_path" ]; then
    echo "no checkpoint file at $_path"
    return 0
  fi
  MO_CKPT_PATH="$_path" python3 - <<'PY'
import json, os, time
try:
    data = json.load(open(os.environ["MO_CKPT_PATH"], encoding="utf-8"))
except Exception:
    print("checkpoint file unparseable")
    raise SystemExit(0)
nodes = data.get("nodes") or {}
if not nodes:
    print("(no nodes recorded)")
    raise SystemExit(0)
print(f"{'node_id':<24} {'status':<10} {'age_s':<8} artifact")
print("-" * 80)
now = int(time.time())
for nid, n in sorted(nodes.items()):
    age = now - (n.get("completed_at") or 0)
    art = n.get("artifact_path") or ""
    print(f"{nid:<24} {n.get('status', '?'):<10} {age:<8} {art}")
PY
}

# Self-test: write 3 nodes, query resume on each, clear one, summary.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _selftest_dir=$(mktemp -d)
  trap 'rm -rf "$_selftest_dir"' EXIT
  export MINI_ORK_RUN_DIR="$_selftest_dir"

  echo "--- write 3 nodes (2 success, 1 failure) ---"
  touch "$_selftest_dir/plan.json"
  checkpoint_write planner success "$_selftest_dir/plan.json"
  touch "$_selftest_dir/lens-out.md"
  checkpoint_write researcher success "$_selftest_dir/lens-out.md"
  checkpoint_write verifier failure

  echo "--- can_resume on success+artifact-present (expect yes) ---"
  checkpoint_can_resume planner

  echo "--- can_resume on failure (expect no) ---"
  checkpoint_can_resume verifier

  echo "--- can_resume on success but artifact deleted (expect no) ---"
  rm -f "$_selftest_dir/lens-out.md"
  checkpoint_can_resume researcher

  echo "--- summary ---"
  checkpoint_summary

  echo "--- clear verifier + summary ---"
  checkpoint_clear verifier
  checkpoint_summary
fi
