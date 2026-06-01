#!/usr/bin/env bash
# workflow_lifecycle.sh — bootstrap workflow_memory baseline rows + store
# workflow_candidates from group_propose output.
#
# v0.2-pt32 (Phase E gap closure, 2026-06-01):
# Previously group_propose emitted candidate JSON to stdout only — nothing
# persisted to workflow_candidates. AND workflow_memory was empty so even
# a candidate INSERT would fail the FK. This lib closes both gaps:
#
#   workflow_memory_ensure_baseline <recipe_name> <task_class>
#     Creates ONE workflow_memory row per recipe if missing. yaml_blob
#     comes from recipes/<recipe>/workflow.yaml. Status defaults to
#     'promoted' (baseline).
#     Emits workflow_version_id on stdout.
#
#   workflow_candidate_store <candidate_json>
#     Persists a candidate from group_propose's stdout into the
#     workflow_candidates table. Auto-creates the baseline workflow_memory
#     row if missing. Emits candidate_id on stdout.
#
# Schema constraint reminder (from migration 0011):
#   workflow_candidates.base_workflow_version_id REFERENCES workflow_memory.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# desc: Ensure a workflow_memory baseline row exists for a recipe. Idempotent.
#       Args: <recipe_name> [task_class]
#       Emits: workflow_version_id on stdout
workflow_memory_ensure_baseline() {
  local recipe="${1:?recipe_name required}"
  local task_class="${2:-$recipe}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$MINI_ORK_ROOT" "$recipe" "$task_class" <<'PY'
import sqlite3, sys, hashlib, os, time

db, root, recipe, task_class = sys.argv[1:5]

# Read the recipe's workflow.yaml as the canonical baseline blob
yaml_path = os.path.join(root, 'recipes', recipe, 'workflow.yaml')
if not os.path.isfile(yaml_path):
    sys.stderr.write(f"workflow_memory_ensure_baseline: no workflow.yaml at {yaml_path}\n")
    sys.exit(1)

with open(yaml_path) as f:
    yaml_blob = f.read()

yaml_hash = hashlib.sha256(yaml_blob.encode('utf-8')).hexdigest()
version_id = f"{recipe}_v0.1.0"

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
try:
    cur = con.execute("SELECT workflow_version_id FROM workflow_memory WHERE workflow_version_id = ?", (version_id,))
    if cur.fetchone():
        print(version_id)
        sys.exit(0)
    con.execute("""
        INSERT INTO workflow_memory
        (workflow_version_id, workflow_name, base_version_id, yaml_hash, yaml_blob, mutations, status)
        VALUES (?, ?, NULL, ?, ?, '[]', 'promoted')
    """, (version_id, recipe, yaml_hash, yaml_blob))
    con.commit()
    print(version_id)
finally:
    con.close()
PY
}

# desc: Persist a workflow_candidate from group_propose's stdout JSON.
#       Args: <candidate_json>
#       Emits: candidate_id on stdout
workflow_candidate_store() {
  local payload="${1:?candidate_json required}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$MINI_ORK_ROOT" "$payload" <<'PY'
import sqlite3, sys, json, hashlib, os, time, uuid

db, root, raw = sys.argv[1:4]

try:
    p = json.loads(raw)
except json.JSONDecodeError as e:
    sys.stderr.write(f"workflow_candidate_store: invalid JSON: {e}\n")
    sys.exit(1)

if not isinstance(p, dict):
    sys.stderr.write(f"workflow_candidate_store: expected object, got {type(p).__name__}\n")
    sys.exit(1)

task_class = p.get('task_class') or 'generic'
recipe = task_class.replace('_', '-')  # convention: task_class=refactor_audit → recipe dir=refactor-audit
# If recipe dir doesn't exist, try the literal task_class name
recipe_dir = os.path.join(root, 'recipes', recipe)
if not os.path.isdir(recipe_dir):
    recipe = task_class
    recipe_dir = os.path.join(root, 'recipes', recipe)
if not os.path.isdir(recipe_dir):
    sys.stderr.write(f"workflow_candidate_store: no recipes/ dir for task_class={task_class}\n")
    sys.exit(1)

# Reuse workflow_memory_ensure_baseline logic inline
yaml_path = os.path.join(recipe_dir, 'workflow.yaml')
if not os.path.isfile(yaml_path):
    sys.stderr.write(f"workflow_candidate_store: no workflow.yaml at {yaml_path}\n")
    sys.exit(1)
with open(yaml_path) as f:
    yaml_blob = f.read()
yaml_hash = hashlib.sha256(yaml_blob.encode('utf-8')).hexdigest()
version_id = f"{recipe}_v0.1.0"

candidate_id = p.get('candidate_id') or f"wc-{uuid.uuid4().hex[:12]}"
mutations = json.dumps([p.get('mutation_applied', {})]) if p.get('mutation_applied') else '[]'

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
try:
    # 1. Ensure baseline workflow_memory row
    cur = con.execute("SELECT workflow_version_id FROM workflow_memory WHERE workflow_version_id = ?", (version_id,))
    if not cur.fetchone():
        con.execute("""
            INSERT INTO workflow_memory
            (workflow_version_id, workflow_name, base_version_id, yaml_hash, yaml_blob, mutations, status)
            VALUES (?, ?, NULL, ?, ?, '[]', 'promoted')
        """, (version_id, recipe, yaml_hash, yaml_blob))

    # 2. Insert workflow_candidate
    con.execute("""
        INSERT INTO workflow_candidates
        (candidate_id, base_workflow_version_id, mutations, status, utility_delta, created_by)
        VALUES (?, ?, ?, 'candidate', 0.0, 'evolution_engine')
        ON CONFLICT(candidate_id) DO NOTHING
    """, (candidate_id, version_id, mutations))
    con.commit()
    print(candidate_id)
finally:
    con.close()
PY
}

# When sourced for self-test:
if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "workflow_lifecycle.sh — source me and call workflow_memory_ensure_baseline / workflow_candidate_store" >&2
fi
