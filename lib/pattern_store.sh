#!/usr/bin/env bash
# pattern_store.sh — PatternRecord storage and emergence hooks.
#
# Public API:
#   pattern_store    <json_payload>
#   pattern_query    [--min-frequency N] [--output-type T]
#   pattern_on_new   <hook_fn_name>   ← register callback fired on new pattern
#
# Schema fields: pattern_id, description, evidence_trace_ids (json),
#   frequency, first_seen, last_seen, output_type, cluster_id

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Registry of on_new hooks (array of function names).
_PATTERN_ON_NEW_HOOKS=()

_pattern_ensure_table() {
  # v0.2-pt10 G-003 DDL session guard
  [ "${_MO_PATTERN_SCHEMA_INIT:-0}" = "1" ] && return 0
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA busy_timeout=5000")
con.execute("""
    CREATE TABLE IF NOT EXISTS pattern_records (
        pattern_id         TEXT PRIMARY KEY,
        description        TEXT NOT NULL,
        evidence_trace_ids TEXT NOT NULL DEFAULT '[]',
        frequency          INTEGER NOT NULL DEFAULT 1,
        first_seen         INTEGER NOT NULL,
        last_seen          INTEGER NOT NULL,
        output_type        TEXT NOT NULL
                               CHECK(output_type IN (
                                   'adr','verifier_addition','workflow_change',
                                   'prompt_change','best_practice_rule','other'
                               )),
        cluster_id         TEXT
    )
""")
con.commit()
con.close()
PY
  _MO_PATTERN_SCHEMA_INIT=1
  export _MO_PATTERN_SCHEMA_INIT
}

# desc: Store or upsert a pattern record. If pattern_id already exists,
#       increments frequency and updates last_seen + merges evidence_trace_ids.
#       Returns pattern_id on stdout.
pattern_store() {
  local payload="${1:?json_payload required}"
  _pattern_ensure_table

  local pattern_id
  pattern_id="$(python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$payload" <<'PY'
import sqlite3, json, sys, uuid, time

db = sys.argv[1]
try:
    p = json.loads(sys.argv[2])
except json.JSONDecodeError as e:
    print(f"pattern_store: invalid JSON: {e}", file=sys.stderr)
    sys.exit(1)

pid     = p.get("pattern_id") or f"pat-{uuid.uuid4().hex[:12]}"
now     = int(time.time())
output_type = p.get("output_type", "other")

valid_types = {"adr","verifier_addition","workflow_change",
               "prompt_change","best_practice_rule","other"}
if output_type not in valid_types:
    output_type = "other"

new_evidence = p.get("evidence_trace_ids", [])
if isinstance(new_evidence, str):
    try:
        new_evidence = json.loads(new_evidence)
    except Exception:
        new_evidence = []

con = sqlite3.connect(db)
existing = con.execute(
    "SELECT frequency, evidence_trace_ids, first_seen FROM pattern_records WHERE pattern_id=?",
    (pid,)
).fetchone()

is_new = existing is None

if is_new:
    con.execute("""
        INSERT INTO pattern_records
            (pattern_id, description, evidence_trace_ids, frequency, first_seen, last_seen, output_type, cluster_id)
        VALUES (?,?,?,?,?,?,?,?)
    """, (
        pid,
        p.get("description", ""),
        json.dumps(new_evidence),
        int(p.get("frequency", 1)),
        int(p.get("first_seen", now)),
        now,
        output_type,
        p.get("cluster_id"),
    ))
else:
    freq       = existing[0] + 1
    old_ev     = json.loads(existing[1]) if existing[1] else []
    merged_ev  = list(dict.fromkeys(old_ev + new_evidence))  # dedupe, preserve order
    first_seen = existing[2]
    con.execute("""
        UPDATE pattern_records
        SET frequency=?, evidence_trace_ids=?, last_seen=?, output_type=?, cluster_id=?
        WHERE pattern_id=?
    """, (freq, json.dumps(merged_ev), now, output_type, p.get("cluster_id"), pid))

con.commit()
con.close()
# Tag new/updated so caller can fire hooks
print(f"{pid}|{'new' if is_new else 'updated'}")
PY
)"

  local pid="${pattern_id%%|*}"
  local is_new="${pattern_id##*|}"

  if [[ "$is_new" == "new" && ${#_PATTERN_ON_NEW_HOOKS[@]} -gt 0 ]]; then
    for hook_fn in "${_PATTERN_ON_NEW_HOOKS[@]}"; do
      if declare -f "$hook_fn" > /dev/null 2>&1; then
        "$hook_fn" "$pid" "$payload" || true
      else
        echo "pattern_store: hook function '$hook_fn' not defined, skipping" >&2
      fi
    done
  fi

  echo "$pid"
}

# desc: Query pattern records with optional filters. Emits JSON array on stdout.
#       Flags: --min-frequency N, --output-type T
pattern_query() {
  local min_freq=1
  local output_type=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --min-frequency) min_freq="$2";   shift 2 ;;
      --output-type)   output_type="$2"; shift 2 ;;
      *) shift ;;
    esac
  done

  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$min_freq" "$output_type" <<'PY'
import sqlite3, json, sys
db, mf, ot = sys.argv[1], int(sys.argv[2]), sys.argv[3]
clauses, params = ["frequency >= ?"], [mf]
if ot:
    clauses.append("output_type = ?"); params.append(ot)
sql = ("SELECT * FROM pattern_records WHERE " + " AND ".join(clauses)
       + " ORDER BY frequency DESC")
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
rows = con.execute(sql, params).fetchall()
con.close()
print(json.dumps([dict(r) for r in rows]))
PY
}

# desc: Register a callback function name to be invoked when a NEW pattern is
#       stored. The function receives (pattern_id, original_payload_json).
pattern_on_new() {
  local hook_fn="${1:?hook function name required}"
  _PATTERN_ON_NEW_HOOKS+=("$hook_fn")
  echo "pattern_on_new: registered hook '${hook_fn}'" >&2
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "pattern_store.sh — source me and call pattern_store / pattern_query / pattern_on_new"
fi
