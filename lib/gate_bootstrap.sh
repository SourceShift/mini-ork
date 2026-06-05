#!/usr/bin/env bash
# gate_bootstrap.sh — auto-register the 4 oracle gates at framework boot.
#
# Per 3-subagent consensus (2026-06-05, recorded in
# docs/architecture/oracle-gates-wiring.md): the central wire-up fires
# all 4 oracle gates once-per-cycle at a single chokepoint inside
# bin/mini-ork-execute (modeled on the measure_topology call site).
# That chokepoint requires the gates to be REGISTERED in the gate_registry
# table before gate_run_all can dispatch them. This bootstrap runs at the
# top of the publisher case-branch and idempotently inserts the 4 gate
# records.
#
# Public API:
#   mo_bootstrap_oracle_gates  → registers the 4 gates if not already
#                                 present. Idempotent. rc=0 even on
#                                 partial failures (fail-open).

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

mo_bootstrap_oracle_gates() {
  if [ -z "${MINI_ORK_DB:-}" ] || [ ! -f "${MINI_ORK_DB:-/nonexistent}" ]; then
    return 0
  fi

  # shellcheck source=lib/gate_registry.sh
  source "$MINI_ORK_ROOT/lib/gate_registry.sh" 2>/dev/null || return 0
  if ! declare -f gate_register > /dev/null 2>&1; then
    return 0
  fi
  _gate_ensure_table 2>/dev/null || return 0

  # Idempotency: skip if all 4 already registered.
  local _existing
  _existing=$(python3 - "${MINI_ORK_DB}" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
try:
    c = con.execute(
        "SELECT COUNT(*) FROM gate_registry WHERE gate_id LIKE 'oracle-%'"
    ).fetchone()[0]
    print(c)
except Exception:
    print(0)
con.close()
PY
)
  if [ "${_existing:-0}" -ge 4 ]; then
    return 0
  fi

  local _root="$MINI_ORK_ROOT"
  # task_class_filter='*' would be literal-matched by gate_list's
  # `task_class_filter=?` clause (only matches context.task_class='*').
  # Pass empty string here, then NULL-out in the rename pass below — the
  # gate_list query `task_class_filter IS NULL OR ...=?` then treats NULL
  # as "applies to ALL task_classes" (framework-wide enforcement).
  gate_register "custom" "$_root/gates/coalition.sh"         "" --safety >/dev/null 2>&1 || true
  gate_register "custom" "$_root/gates/panel-health.sh"      "" --safety >/dev/null 2>&1 || true
  gate_register "custom" "$_root/gates/synthesis-promote.sh" "" --safety >/dev/null 2>&1 || true
  gate_register "custom" "$_root/gates/stability.sh"         ""           >/dev/null 2>&1 || true

  # Rename to stable gate_ids so future bootstrap calls see them.
  python3 - "${MINI_ORK_DB}" "$_root" <<'PY'
import sqlite3, sys
db, root = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
mapping = {
    f"{root}/gates/coalition.sh":         "oracle-coalition",
    f"{root}/gates/panel-health.sh":      "oracle-panel-health",
    f"{root}/gates/synthesis-promote.sh": "oracle-synthesis-promote",
    f"{root}/gates/stability.sh":         "oracle-stability",
}
try:
    for cond, new_id in mapping.items():
        cur = con.execute(
            "SELECT gate_id FROM gate_registry WHERE condition=? AND gate_id NOT LIKE 'oracle-%'",
            (cond,)
        ).fetchall()
        for (old_id,) in cur:
            con.execute(
                "UPDATE OR IGNORE gate_registry SET gate_id=? WHERE gate_id=?",
                (new_id, old_id)
            )
            con.execute("DELETE FROM gate_registry WHERE gate_id=?", (old_id,))
    # Empty-string task_class_filter → NULL so gate_list's "task_class_filter
    # IS NULL OR task_class_filter=?" treats it as "applies to all classes".
    con.execute("""
        UPDATE gate_registry SET task_class_filter=NULL
        WHERE gate_id LIKE 'oracle-%' AND task_class_filter=''
    """)
    con.commit()
except Exception:
    pass
con.close()
PY

  return 0
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  set -Eeuo pipefail
  _td=$(mktemp -d)
  trap 'rm -rf "$_td"' EXIT
  export MINI_ORK_DB="$_td/state.db"

  python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("""
CREATE TABLE IF NOT EXISTS gate_registry (
  gate_id           TEXT PRIMARY KEY,
  gate_type         TEXT NOT NULL,
  condition         TEXT NOT NULL,
  task_class_filter TEXT,
  safety            INTEGER NOT NULL DEFAULT 0,
  active            INTEGER NOT NULL DEFAULT 1,
  registered_at     INTEGER NOT NULL DEFAULT 0
);
""")
con.commit(); con.close()
PY

  echo "── self-test: cold call ──"
  mo_bootstrap_oracle_gates
  N1=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM gate_registry WHERE gate_id LIKE 'oracle-%';")
  [ "$N1" -eq 4 ] && echo "  [OK] cold registered $N1/4" || { echo "  [FAIL] got $N1 want 4"; exit 1; }

  echo "── self-test: warm call (idempotent) ──"
  mo_bootstrap_oracle_gates
  N2=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM gate_registry WHERE gate_id LIKE 'oracle-%';")
  [ "$N2" -eq 4 ] && echo "  [OK] warm stays at $N2/4" || { echo "  [FAIL] got $N2 want 4"; exit 1; }

  sqlite3 "$MINI_ORK_DB" "SELECT gate_id FROM gate_registry WHERE gate_id LIKE 'oracle-%' ORDER BY gate_id;"
  echo "self-test passed."
fi
