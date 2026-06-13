#!/usr/bin/env bash
# verifier_rubric.sh — verifier rubric scoring + ground-truth feedback.
#
# Implements roadmap Phase 3 item 9 (LobeHub deep-review): rubric
# tables + ground-truth feedback chain on verifier dispatches.
# Pairs with migration 0025_verifier_rubrics.sql which provides the
# DDL. This file is the public CRUD surface.
#
# Public API:
#   rubric_register     <rubric_id> <name> <task_class> <axes_json>
#       Inserts (or upserts) a verifier_rubrics row.
#       rc=0 on success.
#   rubric_get          <rubric_id>
#       Emits the rubric row as JSON on stdout. "null" on miss.
#   verifier_result_record <run_id> <verifier_name> <verdict> [<rubric_id>] [<confidence>] [<scored_axes_json>]
#       Inserts a verifier_results row. Returns the new result_id on
#       stdout. Verdict must be one of pass | fail | indeterminate
#       | vacuous (matches the table CHECK).
#   verifier_result_annotate <result_id> <kind> <annotator> [<notes>]
#       kind ∈ false_positive | false_negative
#       Sets the matching flag + annotated_by + annotated_at. Refuses
#       to set both is_false_positive=1 AND is_false_negative=1 on
#       the same row (matches the CHECK).
#   verifier_chain_repair <result_id> <repair_run_id>
#       Attaches a repair_run_id to a failed-verifier result so the
#       self-improve loop can count repair chains.
#   verifier_fp_rate <verifier_name> [<window_seconds>]
#       Emits the false-positive rate for a verifier as a float on
#       stdout (e.g. "0.12"). Useful as a quality signal in the
#       self-improve gradient extractor.
#
# Why this exists in honesty terms:
#   Today the framework reports verifier verdicts but has no place
#   to record "this was actually a false positive, my fault for
#   trusting the panel". The new schema closes that loop: the
#   operator annotates after the fact, the self-improve gradient
#   extractor reads the annotations, and the verifier prompts
#   evolve away from the patterns that produce wrong verdicts.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

_rubric_ensure_db() {
  if [ -z "${MINI_ORK_DB:-}" ]; then
    echo "verifier_rubric.sh: MINI_ORK_DB env unset; cannot persist" >&2
    return 2
  fi
  if [ ! -f "$MINI_ORK_DB" ]; then
    echo "verifier_rubric.sh: state.db not found at $MINI_ORK_DB; run mini-ork init first" >&2
    return 2
  fi
  return 0
}

_rubric_uuid() {
  # 12-char hex id is enough for an operator-visible primary key
  # and avoids the openssl rand dependency.
  python3 -c "import secrets; print(secrets.token_hex(6))"
}

rubric_register() {
  local _id="${1:-}"
  local _name="${2:-}"
  local _task_class="${3:-}"
  local _axes_json="${4:-}"

  if [ -z "$_id" ] || [ -z "$_name" ]; then
    echo "rubric_register: usage: rubric_register <rubric_id> <name> <task_class> <axes_json>" >&2
    return 2
  fi
  _rubric_ensure_db || return $?

  MO_RUB_DB="$MINI_ORK_DB" \
  MO_RUB_ID="$_id" \
  MO_RUB_NAME="$_name" \
  MO_RUB_TC="$_task_class" \
  MO_RUB_AXES="$_axes_json" \
  python3 - <<'PY'
import os, sqlite3, sys, time

con = sqlite3.connect(os.environ["MO_RUB_DB"])
con.execute("PRAGMA busy_timeout=5000")
now = int(time.time())
con.execute("""
    INSERT INTO verifier_rubrics
        (rubric_id, name, description, task_class, axes_json, created_at, updated_at, is_active)
    VALUES (?, ?, NULL, ?, ?, ?, ?, 1)
    ON CONFLICT(rubric_id) DO UPDATE SET
        name=excluded.name,
        task_class=excluded.task_class,
        axes_json=excluded.axes_json,
        updated_at=excluded.updated_at,
        is_active=1
""", (
    os.environ["MO_RUB_ID"],
    os.environ["MO_RUB_NAME"],
    os.environ["MO_RUB_TC"] or None,
    os.environ["MO_RUB_AXES"] or None,
    now, now,
))
con.commit()
con.close()
PY
}

rubric_get() {
  local _id="${1:-}"
  if [ -z "$_id" ]; then
    echo "rubric_get: usage: rubric_get <rubric_id>" >&2
    return 2
  fi
  _rubric_ensure_db || return $?
  MO_RUB_DB="$MINI_ORK_DB" MO_RUB_ID="$_id" python3 - <<'PY'
import json, os, sqlite3, sys
con = sqlite3.connect(os.environ["MO_RUB_DB"])
con.row_factory = sqlite3.Row
row = con.execute(
    "SELECT * FROM verifier_rubrics WHERE rubric_id=?",
    (os.environ["MO_RUB_ID"],)
).fetchone()
if not row:
    print("null"); sys.exit(0)
print(json.dumps(dict(row)))
PY
}

verifier_result_record() {
  local _run_id="${1:-}"
  local _verifier="${2:-}"
  local _verdict="${3:-}"
  local _rubric_id="${4:-}"
  local _confidence="${5:-}"
  local _scored_axes="${6:-}"

  if [ -z "$_run_id" ] || [ -z "$_verifier" ] || [ -z "$_verdict" ]; then
    echo "verifier_result_record: usage: verifier_result_record <run_id> <verifier_name> <verdict> [<rubric_id>] [<confidence>] [<scored_axes_json>]" >&2
    return 2
  fi
  _rubric_ensure_db || return $?

  local _result_id
  _result_id="vr-$(_rubric_uuid)"

  MO_RES_DB="$MINI_ORK_DB" \
  MO_RES_ID="$_result_id" \
  MO_RES_RUN="$_run_id" \
  MO_RES_VER="$_verifier" \
  MO_RES_VERDICT="$_verdict" \
  MO_RES_RUBRIC="$_rubric_id" \
  MO_RES_CONF="$_confidence" \
  MO_RES_AXES="$_scored_axes" \
  python3 - <<'PY'
import os, sqlite3, sys
con = sqlite3.connect(os.environ["MO_RES_DB"])
con.execute("PRAGMA busy_timeout=5000")
con.execute("""
    INSERT INTO verifier_results
        (result_id, run_id, verifier_name, rubric_id, verdict,
         confidence, scored_axes_json)
    VALUES (?, ?, ?, ?, ?, ?, ?)
""", (
    os.environ["MO_RES_ID"],
    os.environ["MO_RES_RUN"],
    os.environ["MO_RES_VER"],
    os.environ["MO_RES_RUBRIC"] or None,
    os.environ["MO_RES_VERDICT"],
    float(os.environ["MO_RES_CONF"]) if os.environ.get("MO_RES_CONF") else None,
    os.environ["MO_RES_AXES"] or None,
))
con.commit()
con.close()
PY
  echo "$_result_id"
}

verifier_result_annotate() {
  local _result_id="${1:-}"
  local _kind="${2:-}"
  local _annotator="${3:-}"
  local _notes="${4:-}"

  if [ -z "$_result_id" ] || [ -z "$_kind" ] || [ -z "$_annotator" ]; then
    echo "verifier_result_annotate: usage: verifier_result_annotate <result_id> <false_positive|false_negative> <annotator> [<notes>]" >&2
    return 2
  fi
  case "$_kind" in
    false_positive|false_negative) ;;
    *) echo "verifier_result_annotate: kind must be false_positive or false_negative" >&2; return 2 ;;
  esac
  _rubric_ensure_db || return $?

  MO_RES_DB="$MINI_ORK_DB" \
  MO_RES_ID="$_result_id" \
  MO_RES_KIND="$_kind" \
  MO_RES_ANNOT="$_annotator" \
  MO_RES_NOTES="$_notes" \
  python3 - <<'PY'
import os, sqlite3, sys, time
con = sqlite3.connect(os.environ["MO_RES_DB"])
con.execute("PRAGMA busy_timeout=5000")
col = "is_false_positive" if os.environ["MO_RES_KIND"] == "false_positive" else "is_false_negative"
now = int(time.time())
try:
    con.execute(f"""
        UPDATE verifier_results
           SET {col}=1, annotated_by=?, annotated_at=?, notes=COALESCE(?, notes)
         WHERE result_id=?
    """, (
        os.environ["MO_RES_ANNOT"], now,
        os.environ["MO_RES_NOTES"] or None,
        os.environ["MO_RES_ID"],
    ))
    con.commit()
except sqlite3.IntegrityError as exc:
    sys.stderr.write(f"verifier_result_annotate: refused: {exc}\n")
    sys.exit(1)
con.close()
PY
}

verifier_chain_repair() {
  local _result_id="${1:-}"
  local _repair_run_id="${2:-}"
  if [ -z "$_result_id" ] || [ -z "$_repair_run_id" ]; then
    echo "verifier_chain_repair: usage: verifier_chain_repair <result_id> <repair_run_id>" >&2
    return 2
  fi
  _rubric_ensure_db || return $?
  MO_RES_DB="$MINI_ORK_DB" \
  MO_RES_ID="$_result_id" \
  MO_RES_REPAIR="$_repair_run_id" \
  python3 - <<'PY'
import os, sqlite3
con = sqlite3.connect(os.environ["MO_RES_DB"])
con.execute("PRAGMA busy_timeout=5000")
con.execute(
    "UPDATE verifier_results SET repair_run_id=? WHERE result_id=?",
    (os.environ["MO_RES_REPAIR"], os.environ["MO_RES_ID"])
)
con.commit()
con.close()
PY
}

verifier_fp_rate() {
  local _verifier="${1:-}"
  local _window_seconds="${2:-0}"
  if [ -z "$_verifier" ]; then
    echo "verifier_fp_rate: usage: verifier_fp_rate <verifier_name> [<window_seconds>]" >&2
    return 2
  fi
  _rubric_ensure_db || return $?
  MO_RES_DB="$MINI_ORK_DB" \
  MO_RES_VER="$_verifier" \
  MO_RES_WIN="$_window_seconds" \
  python3 - <<'PY'
import os, sqlite3, sys, time

con = sqlite3.connect(os.environ["MO_RES_DB"])
window = int(os.environ["MO_RES_WIN"])
cutoff = (int(time.time()) - window) if window > 0 else 0
total = con.execute(
    "SELECT COUNT(*) FROM verifier_results WHERE verifier_name=? AND created_at>=?",
    (os.environ["MO_RES_VER"], cutoff)
).fetchone()[0]
if total == 0:
    print("0.0"); sys.exit(0)
fps = con.execute(
    "SELECT COUNT(*) FROM verifier_results WHERE verifier_name=? AND created_at>=? AND is_false_positive=1",
    (os.environ["MO_RES_VER"], cutoff)
).fetchone()[0]
print(f"{fps/total:.4f}")
PY
}

# Self-test: round-trip register → record → annotate → fp_rate.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _selftest_dir=$(mktemp -d)
  trap 'rm -rf "$_selftest_dir"' EXIT

  export MINI_ORK_DB="$_selftest_dir/state.db"
  python3 -c "
import sqlite3
con = sqlite3.connect('$MINI_ORK_DB')
con.executescript(open('$MINI_ORK_ROOT/db/migrations/0025_verifier_rubrics.sql').read())
con.commit()
"
  echo "--- register rubric ---"
  rubric_register "test-rubric" "Test rubric" "framework_edit" '{"axes":["clarity","scope"]}'
  rubric_get "test-rubric" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'name={d[\"name\"]} task_class={d[\"task_class\"]}')"

  echo "--- record a passing result ---"
  pass_id=$(verifier_result_record "run-001" "static-check" "pass" "test-rubric" "0.92")
  echo "pass_id=$pass_id"

  echo "--- record a failing result ---"
  fail_id=$(verifier_result_record "run-002" "static-check" "fail" "test-rubric" "0.61")
  echo "fail_id=$fail_id"

  echo "--- annotate fail as false positive ---"
  verifier_result_annotate "$fail_id" "false_positive" "operator-amir" "verifier was wrong; ran the test manually + it passed"

  echo "--- fp rate ---"
  echo "fp_rate=$(verifier_fp_rate static-check)"

  echo "--- chain a repair run ---"
  verifier_chain_repair "$fail_id" "run-002-repair"
  python3 -c "
import sqlite3
c = sqlite3.connect('$MINI_ORK_DB')
c.row_factory = sqlite3.Row
row = c.execute('SELECT repair_run_id FROM verifier_results WHERE result_id=?', ('$fail_id',)).fetchone()
print(f'repair chained: {dict(row)}')
"
fi
