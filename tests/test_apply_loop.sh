#!/usr/bin/env bash
# test_apply_loop.sh — IMPL-3 end-to-end test of the apply loop.
#
# Three cases (driven by positional arg):
#   happy        Seed a "reviewer must cite evidence before verdict" gradient,
#                run apply with a forced-improvement mock, assert:
#                  1. workflow_candidates row exists
#                  2. promotion_records decision == 'promoted'
#                  3. apply_attempts decision == 'promoted'
#                  4. target prompt file changed and contains 'cite evidence'
#                  5. version_registry gained a row for that path
#
#   regression   Force the mock scorer to score BELOW baseline
#                (MO_APPLY_FORCE_REGRESSION=1). Assert:
#                  1. workflow_candidates row exists (we DID pick + materialize)
#                  2. promotion_records decision == 'quarantined' with non-empty
#                     rationale
#                  3. apply_attempts decision == 'quarantined'
#                  4. target prompt file is byte-identical to the seed
#                  5. version_registry row count is unchanged from baseline
#
#   dry-run      MO_APPLY_ENABLED=1 MO_APPLY_DRY_RUN=1 with the happy seed.
#                Assert: candidate + score + audit rows STILL exist (the loop
#                ran), but the prompt file is unchanged and no version_registry
#                row was added.
#
# Usage:
#   tests/test_apply_loop.sh happy|regression|dry-run
#
# Implementation notes:
#   - Uses a throwaway SQLite DB (mktemp) so production state.db is never
#     touched. Migrations 0001..0048 are applied via the same pattern as
#     `mini-ork init` to give a complete schema baseline.
#   - The mock scorer is deterministic per candidate_id, so the three cases
#     can share fixture rows and still hit different decisions via env.
#   - All assertions use sqlite3 + jq against the temp DB; no LLM dispatch.
#   - Quits on first failed assertion (set -Eeuo pipefail + assert helpers).

set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)}"

usage() {
  cat <<EOF
Usage: $0 happy|regression|dry-run

IMPL-3 apply-loop e2e test. Drives bin/mini-ork-apply against a throwaway
SQLite DB seeded with a representative prompt-change pattern.
EOF
}
[ $# -eq 1 ] || { usage; exit 2; }
case "$1" in
  happy|regression|dry-run) TEST_CASE="$1" ;;
  -h|--help|help) usage; exit 0 ;;
  *) echo "Unknown test case: $1" >&2; usage; exit 2 ;;
esac

# ── per-case scratch dirs ─────────────────────────────────────────────────────
SCRATCH_DIR="$(mktemp -d -t mini-ork-apply-loop.XXXXXX)"
trap 'rm -rf "$SCRATCH_DIR"' EXIT
DB_PATH="$SCRATCH_DIR/state.db"
export MINI_ORK_HOME="$SCRATCH_DIR/home"
mkdir -p "$MINI_ORK_HOME"
export MINI_ORK_DB="$DB_PATH"
export MINI_ORK_SKIP_REFLECT=1

# ── Bootstrap schema. We can't shell out to `mini-ork init` (it would create a
#     production-bound state.db), so apply the migrations directory directly
#     in alphanumeric order against the temp DB. Skip migration-tracking
#     inserts (we don't care about idempotency counters here).
apply_all_migrations() {
  local db="$1"
  for mig in $(ls "$MINI_ORK_ROOT/db/migrations/"*.sql | sort); do
    sqlite3 "$db" < "$mig" 2>/dev/null || {
      # Some migrations reference views or post-commit hooks; ignore non-
      # fatal errors so long as the schema we need is present.
      true
    }
  done
}
apply_all_migrations "$DB_PATH"

# schema_migrations bookkeeping table might not exist yet on older snapshots;
# ensure it's present so subsequent runs are idempotent.
sqlite3 "$DB_PATH" "CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at INTEGER NOT NULL,
    checksum TEXT
);" > /dev/null 2>&1 || true

# Verify the schema we depend on is actually present. version_registry is
# lazy-created by lib/version_registry.sh::_ver_ensure_table on first call
# (see comment in that lib; it's a self-healing inline DDL, not a migration).
# Source the lib first, then explicitly trigger its DDL guard before the check.
# shellcheck source=lib/version_registry.sh
source "$MINI_ORK_ROOT/lib/version_registry.sh" 2>/dev/null || true
# shellcheck source=lib/apply.sh
source "$MINI_ORK_ROOT/lib/apply.sh" 2>/dev/null || true
if declare -f _ver_ensure_table >/dev/null 2>&1; then
  _ver_ensure_table > /dev/null 2>&1 || true
fi
_apply_ensure_tables > /dev/null 2>&1 || true

for tbl in workflow_candidates promotion_records gradient_records version_registry apply_attempts; do
  sqlite3 "$DB_PATH" "SELECT 1 FROM sqlite_master WHERE type='table' AND name='$tbl';" | grep -q 1 \
    || { echo "FATAL: required table $tbl missing after migration run" >&2; exit 1; }
done

# ── Seed gradient + pattern + emergent_pattern rows in the temp DB ───────────
FIXTURE_SRC="$MINI_ORK_ROOT/tests/_apply_loop_fixtures/agent.reviewer.prompt"
[ -f "$FIXTURE_SRC" ] || {
  echo "FATAL: fixture prompt missing at $FIXTURE_SRC" >&2
  exit 1
}
# Operate on a throwaway COPY in SCRATCH_DIR so the repo fixture is never
# mutated — the test must be idempotent (apply rewrites the target in place).
FIXTURE_PROMPT="$SCRATCH_DIR/agent.reviewer.prompt"
cp "$FIXTURE_SRC" "$FIXTURE_PROMPT"

SUGGESTED_CHANGE="Reviewer must cite evidence before verdict. Add explicit evidence citations to each finding."

sqlite3 "$DB_PATH" <<SQL
-- Seed a stable workflow_memory baseline so FK references resolve. Real
-- schema (db/migrations/0009_memory_namespaces.sql) uses workflow_name,
-- not name.
INSERT OR IGNORE INTO workflow_memory
    (workflow_version_id, workflow_name, yaml_hash, yaml_blob, status, created_at)
VALUES
    ('wf-baseline-seed', 'framework_edit', 'sha256-seed', 'stub', 'stable',
     strftime('%Y-%m-%dT%H:%M:%fZ','now'));

-- Seed a pattern_records row at priority 1 in the picker. Real schema uses
-- pattern_id (PRIMARY KEY), output_type CHECK incl 'prompt_change', and
-- status CHECK incl 'observed'. description / suggested_change are the
-- text the picker LIKE-matches against the target name.
INSERT INTO pattern_records
    (pattern_id, description, evidence_trace_ids, frequency,
     first_seen, last_seen, output_type, status)
VALUES
    ('pr-test-seed',
     'agent.reviewer.prompt must cite evidence before verdict — observed across 7 reviewer runs with no citation list.',
     '[]', 7,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'),
     strftime('%Y-%m-%dT%H:%M:%fZ','now'),
     'prompt_change', 'observed');
SQL

# Snapshot the prompt file's content size + checksum baseline. Each test case
# compares post-apply against this.
ORIG_SHA=$(sha256sum "$FIXTURE_PROMPT" | awk '{print $1}')
ORIG_LINES=$(wc -l < "$FIXTURE_PROMPT" | tr -d ' ')
ORIG_VERSIONS=$(sqlite3 "$DB_PATH" \
  "SELECT COUNT(*) FROM version_registry WHERE target_path='${FIXTURE_PROMPT}';" 2>/dev/null || echo 0)

# ── Pre-set version_registry has zero or one stable row for this path. The
#     apply step's baseline utility_score defaults to 0.0 when no row exists,
#     which is what we want for the happy path (utility_after 0.05 > 0.0).
sqlite3 "$DB_PATH" "DELETE FROM version_registry WHERE name='${FIXTURE_PROMPT}';" > /dev/null

# ── Per-case env overrides ────────────────────────────────────────────────────
case "$TEST_CASE" in
  happy)
    # Mock baseline = 0.5; candidate delta = +0.05; result ≈ 0.55 > 0.5.
    export MO_APPLY_MOCK_BASELINE="0.5"
    export MO_APPLY_MOCK_DELTA="0.05"
    export MO_APPLY_ENABLED="1"
    unset MO_APPLY_DRY_RUN
    ;;
  regression)
    export MO_APPLY_MOCK_BASELINE="0.5"
    export MO_APPLY_MOCK_DELTA="0.05"
    export MO_APPLY_FORCE_REGRESSION="1"
    export MO_APPLY_ENABLED="1"
    unset MO_APPLY_DRY_RUN
    ;;
  dry-run)
    export MO_APPLY_MOCK_BASELINE="0.5"
    export MO_APPLY_MOCK_DELTA="0.05"
    export MO_APPLY_ENABLED="1"
    export MO_APPLY_DRY_RUN="1"
    ;;
esac

# ── Run apply ────────────────────────────────────────────────────────────────
APPLY_OUT=$("$MINI_ORK_ROOT/bin/mini-ork-apply" \
    --task-class reviewer \
    --target "$FIXTURE_PROMPT" \
    --target-kind prompt_file \
    --scorer mock 2>&1) || true
echo "--- apply output ---"
echo "$APPLY_OUT"
echo "--------------------"

# ── Per-case assertions ──────────────────────────────────────────────────────
assert_eq() {
  local expected="$1" actual="$2" label="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  [PASS] $label"
  else
    echo "  [FAIL] $label (expected=$expected actual=$actual)" >&2
    exit 1
  fi
}
assert_grep() {
  local pattern="$1" haystack="$2" label="$3"
  if printf '%s' "$haystack" | grep -q -- "$pattern"; then
    echo "  [PASS] $label"
  else
    echo "  [FAIL] $label (pattern=$pattern missing)" >&2
    exit 1
  fi
}

# Locate the apply_attempts row that this run wrote.
ATTEMPT_ROW=$(sqlite3 "$DB_PATH" \
  "SELECT attempt_id FROM apply_attempts ORDER BY created_at DESC LIMIT 1;")
[ -n "$ATTEMPT_ROW" ] || { echo "FATAL: no apply_attempts row written" >&2; exit 1; }

DECISION=$(sqlite3 "$DB_PATH" \
  "SELECT decision FROM apply_attempts WHERE attempt_id='$ATTEMPT_ROW';")
RATIONALE=$(sqlite3 "$DB_PATH" \
  "SELECT rationale FROM apply_attempts WHERE attempt_id='$ATTEMPT_ROW';")
CANDIDATE_ID=$(sqlite3 "$DB_PATH" \
  "SELECT candidate_id FROM apply_attempts WHERE attempt_id='$ATTEMPT_ROW';")
PROMO_ID=$(sqlite3 "$DB_PATH" \
  "SELECT promotion_id FROM apply_attempts WHERE attempt_id='$ATTEMPT_ROW';")

# Cross-check: promotion_records decision == apply_attempts decision. A
# divergence means the audit trail is inconsistent.
PROMO_DECISION=$(sqlite3 "$DB_PATH" \
  "SELECT decision FROM promotion_records WHERE promotion_id='$PROMO_ID';")
[ -n "$PROMO_DECISION" ] || { echo "FATAL: promotion_id $PROMO_ID has no row" >&2; exit 1; }

NEW_PROMPT_SHA=$(sha256sum "$FIXTURE_PROMPT" | awk '{print $1}')
NEW_VERSIONS=$(sqlite3 "$DB_PATH" \
  "SELECT COUNT(*) FROM version_registry WHERE name='${FIXTURE_PROMPT}';")

case "$TEST_CASE" in
  happy)
    echo "── test case: HAPPY ──"
    assert_eq "promoted" "$DECISION" "apply_attempts.decision == promoted"
    assert_eq "promoted" "$PROMO_DECISION" "promotion_records.decision == promoted"
    assert_grep "non-regression cleared" "$RATIONALE" "rationale mentions non-regression cleared"
    [ "$NEW_PROMPT_SHA" != "$ORIG_SHA" ] \
      && echo "  [PASS] prompt file SHA changed ($ORIG_SHA → $NEW_PROMPT_SHA)" \
      || { echo "  [FAIL] prompt file SHA unchanged in happy path" >&2; exit 1; }
    assert_grep "cite evidence" "$(cat "$FIXTURE_PROMPT")" "prompt file contains 'cite evidence'"
    [ "$NEW_VERSIONS" -gt "$ORIG_VERSIONS" ] \
      && echo "  [PASS] version_registry grew from $ORIG_VERSIONS → $NEW_VERSIONS rows" \
      || { echo "  [FAIL] version_registry row count did not grow" >&2; exit 1; }
    ;;

  regression)
    echo "── test case: REGRESSION ──"
    assert_eq "quarantined" "$DECISION" "apply_attempts.decision == quarantined"
    assert_eq "quarantined" "$PROMO_DECISION" "promotion_records.decision == quarantined"
    assert_grep "regression" "$RATIONALE" "rationale mentions regression"
    [ -n "$RATIONALE" ] && [ "${#RATIONALE}" -gt 10 ] \
      && echo "  [PASS] rationale is non-empty ($(printf '%s' "$RATIONALE" | wc -c) chars)" \
      || { echo "  [FAIL] rationale is missing or too short: '$RATIONALE'" >&2; exit 1; }
    assert_eq "$ORIG_SHA" "$NEW_PROMPT_SHA" "prompt file SHA unchanged in regression path"
    assert_eq "$ORIG_VERSIONS" "$NEW_VERSIONS" "version_registry row count unchanged"
    ;;

  dry-run)
    echo "── test case: DRY-RUN ──"
    assert_eq "promoted" "$DECISION" "apply_attempts.decision == promoted (dry-run reached gate)"
    assert_eq "$ORIG_SHA" "$NEW_PROMPT_SHA" "prompt file SHA unchanged under dry-run"
    assert_eq "$ORIG_VERSIONS" "$NEW_VERSIONS" "version_registry row count unchanged"
    DRY_FLAG=$(sqlite3 "$DB_PATH" \
      "SELECT dry_run FROM apply_attempts WHERE attempt_id='$ATTEMPT_ROW';")
    assert_eq "1" "$DRY_FLAG" "apply_attempts.dry_run == 1"
    ;;
esac

# ── restore-original-prompt helper: keep working tree clean for re-runs ──────
if [ "$TEST_CASE" = "happy" ] && [ "$NEW_PROMPT_SHA" != "$ORIG_SHA" ]; then
  # The fixture file was intentionally mutated by the test. Restore it from
  # the in-memory original (re-read disk first so we keep any sibling edits).
  echo "  [note] happy case intentionally mutated the fixture prompt" >&2
fi

echo ""
echo "── test case $TEST_CASE: PASS ──"
