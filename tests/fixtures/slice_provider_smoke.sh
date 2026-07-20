#!/usr/bin/env bash
# tests/fixtures/slice_provider_smoke.sh — capture context_assemble output
# across two regimes (under-budget, over-budget) so the slice-provider
# refactor can be proven byte-for-byte compatible with the prior 64K
# truncate. Writes sha256 sums + full payloads to a side-by-side
# baseline dir; verifier diffs the post-refactor output against this.
#
# Usage:
#   SLICE_BASELINE_DIR=/path/to/out \
#     bash tests/fixtures/slice_provider_smoke.sh
#
# Why two regimes: under-budget exercises the "tokens_used <= budget"
# branch (no _truncated marker). Over-budget exercises the trim loops
# (pop prior_similar_runs, then pop known_failure_modes). Together
# they cover both paths the default provider must reproduce.
#
# Byte-for-byte note: `assembled_at` is `int(time.time())` — it changes
# on every run. We zero it before hashing so the comparison isolates
# the refactor's behaviour from clock drift. tokens_estimated depends
# on serialized length which is stable across refactors; it is left
# in the diff.
set -uo pipefail

_mask_dynamic() {
  python3 - "$1" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
d.pop("assembled_at", None)
with open(sys.argv[1], "w") as f:
    json.dump(d, f, sort_keys=True)
PY
}

WT_ROOT="${WT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
OUT_DIR="${SLICE_BASELINE_DIR:-/tmp/slice-provider-baseline-$$}"
mkdir -p "$OUT_DIR"

# ── Build a populated test DB ──────────────────────────────────────────
DB="$(mktemp /tmp/slice-provider-db-XXXXXX)"
export MINI_ORK_DB="$DB"
export MINI_ORK_HOME="$(mktemp -d)"
# shellcheck source=/dev/null
source "$WT_ROOT/tests/lib/setup_state_db.sh"
test_apply_migrations

# shellcheck source=/dev/null
source "$WT_ROOT/lib/trace_store.sh"

# Populate 30 execution_traces — enough to blow the 200-token budget.
for i in $(seq 1 30); do
  trace_write "$(cat <<JSON
{
  "task_class": "smoke-task",
  "status": "success",
  "cost_usd": 0.1234,
  "duration_ms": 4567,
  "trace_id": "smoke-trace-$i"
}
JSON
)" >/dev/null 2>&1
done

# Also seed gradient_records to exercise the failure_modes trim branch.
python3 - "$DB" <<'PY'
import sqlite3, sys
db = sys.argv[1]
con = sqlite3.connect(db)
for i in range(15):
    con.execute(
        "INSERT INTO gradient_records (target, signal, suggested_change, evidence, confidence, created_at, task_class) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            f"smoke.target.{i}",
            f"smoke signal variant {i} with enough text to consume tokens " * 3,
            f"smoke suggested fix {i} with extra padding " * 4,
            f"smoke evidence {i}",
            0.7 + (i * 0.01),
            1700000000 + i,
            "smoke-task",
        ),
    )
con.commit()
con.close()
PY

# ── Case 1: under-budget (no truncation) ─────────────────────────────
BRIEF_A="$(mktemp /tmp/slice-brief-A-XXXXXX)"
cat >"$BRIEF_A" <<'JSON'
{"task_class":"smoke-task","title":"Under-budget smoke"}
JSON
export MINI_ORK_CTX_BUDGET_TOKENS=200000
PACK_A="$(MINI_ORK_DB="$DB" MINI_ORK_HOME="$MINI_ORK_HOME" \
  PYTHONPATH="$WT_ROOT:${PYTHONPATH:-}" python3 -m mini_ork.context_assembler \
    assemble "$BRIEF_A" "under_node")"
echo "$PACK_A" > "$OUT_DIR/under-budget.json"
_mask_dynamic "$OUT_DIR/under-budget.json"
sha256sum "$OUT_DIR/under-budget.json" > "$OUT_DIR/under-budget.sha256"
rm -f "$BRIEF_A"

# ── Case 2: over-budget (truncation path) ─────────────────────────────
BRIEF_B="$(mktemp /tmp/slice-brief-B-XXXXXX)"
cat >"$BRIEF_B" <<'JSON'
{"task_class":"smoke-task","title":"Over-budget smoke"}
JSON
export MINI_ORK_CTX_BUDGET_TOKENS=300
PACK_B="$(MINI_ORK_DB="$DB" MINI_ORK_HOME="$MINI_ORK_HOME" \
  PYTHONPATH="$WT_ROOT:${PYTHONPATH:-}" python3 -m mini_ork.context_assembler \
    assemble "$BRIEF_B" "over_node")"
echo "$PACK_B" > "$OUT_DIR/over-budget.json"
_mask_dynamic "$OUT_DIR/over-budget.json"
sha256sum "$OUT_DIR/over-budget.json" > "$OUT_DIR/over-budget.sha256"
rm -f "$BRIEF_B"

# ── Cleanup ──────────────────────────────────────────────────────────
rm -f "$DB"
rm -rf "$MINI_ORK_HOME"

# ── Report ───────────────────────────────────────────────────────────
echo "slice_provider_smoke: baseline captured at $OUT_DIR"
cat "$OUT_DIR/under-budget.sha256"
cat "$OUT_DIR/over-budget.sha256"
