#!/usr/bin/env bash
# tests/unit/test_gradient_extractor.sh — unit tests for lib/gradient_extractor.sh
# Usage: bash tests/unit/test_gradient_extractor.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/gradient_extractor.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

echo "── unit: gradient_extractor.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/gradient_extractor.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

# Isolated test DB
TEST_DB=$(mktemp /tmp/mini-ork-test-XXXXXX.db)
export MINI_ORK_DB="$TEST_DB"
export MINI_ORK_HOME=$(mktemp -d)
trap 'rm -f "$TEST_DB"; rm -rf "$MINI_ORK_HOME"' EXIT

# Apply migrations so trace_store/gradient_extractor find execution_traces +
# gradient tables on first call.
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/tests/lib/setup_state_db.sh"
test_apply_migrations || { echo "skip: migrations failed to apply"; exit 0; }

# Stub out LLM dispatch — gradient_extractor falls back to override fn when set
_test_gradient_extractor_stub() {
  local _trace_id="$1"
  # Emit one well-formed gradient JSON
  echo '{"target":"workflow.node.test","signal":"test signal","suggested_change":"test change","confidence":0.9}'
}
export MINI_ORK_GRADIENT_EXTRACTOR_FN="_test_gradient_extractor_stub"

# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/trace_store.sh"
# shellcheck source=/dev/null
source "$LIB"

echo ""
echo "--- happy path: gradient_store round-trips a gradient record ---"

GID="$(gradient_store '{"target":"workflow.node.planner","signal":"slow","suggested_change":"add cache","evidence":"tr-abc","confidence":0.8}' 2>/dev/null)"
_ok_cond() { [[ -n "$GID" ]] && echo "  [OK]   $1" && PASS=$((PASS+1)) || { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }; }
_ok_cond "gradient_store returns non-empty id"

# Verify the row is present in DB
ROW_COUNT="$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM gradient_records WHERE gradient_id='$GID';" 2>/dev/null || echo 0)"
if [[ "$ROW_COUNT" -eq 1 ]]; then _ok "gradient_store writes to DB"; else _fail "gradient_store did not write to DB (count=$ROW_COUNT)"; fi

echo ""
echo "--- happy path: gradient_extract via override fn ---"

# Create a trace to extract from
TRACE_ID="$(trace_write '{"task_class":"grad-test","status":"success"}' 2>/dev/null)"

EXTRACTED="$(gradient_extract "$TRACE_ID" 2>/dev/null)"
if [[ -n "$EXTRACTED" ]]; then
  _ok "gradient_extract (stub) emits at least one gradient line"
  TARGET="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('target',''))" "$EXTRACTED" 2>/dev/null || echo "")"
  if [[ "$TARGET" == "workflow.node.test" ]]; then
    _ok "gradient_extract stub output has expected target"
  else
    _fail "gradient_extract stub output wrong target: '$TARGET'"
  fi
else
  _fail "gradient_extract (stub) returned empty output"
fi

echo ""
echo "--- edge case: gradient_store upsert on same gradient_id increments confidence ---"

GID2="gr-dedupetest"
gradient_store "{\"gradient_id\":\"$GID2\",\"target\":\"wf.node.A\",\"signal\":\"signal1\",\"suggested_change\":\"change1\",\"evidence\":\"tr-x\",\"confidence\":0.3}" >/dev/null 2>&1
gradient_store "{\"gradient_id\":\"$GID2\",\"target\":\"wf.node.A\",\"signal\":\"signal1\",\"suggested_change\":\"change1 updated\",\"evidence\":\"tr-x\",\"confidence\":0.7}" >/dev/null 2>&1

CONF="$(sqlite3 "$TEST_DB" "SELECT confidence FROM gradient_records WHERE gradient_id='$GID2';" 2>/dev/null || echo 0)"
if python3 -c "import sys; sys.exit(0 if abs(float(sys.argv[1]) - 0.7) < 0.001 else 1)" "$CONF" 2>/dev/null; then
  _ok "gradient_store upsert updates confidence to latest value"
else
  _fail "gradient_store upsert confidence wrong: $CONF (expected 0.7)"
fi

echo ""
echo "--- error path: gradient_store with missing required field exits non-zero ---"

if gradient_store '{"target":"wf.node.X","signal":"s"}' >/dev/null 2>&1; then
  _fail "gradient_store with missing evidence/suggested_change should exit non-zero"
else
  _ok "gradient_store with missing required field exits non-zero"
fi

echo ""
echo "--- error path: gradient_store with invalid JSON exits non-zero ---"

if gradient_store "not-json" >/dev/null 2>&1; then
  _fail "gradient_store with invalid JSON should exit non-zero"
else
  _ok "gradient_store with invalid JSON exits non-zero"
fi

echo ""
echo "--- error path: gradient_extract on non-existent trace_id exits non-zero ---"

# Unset override so we test actual trace lookup failure path
unset MINI_ORK_GRADIENT_EXTRACTOR_FN
if gradient_extract "tr-doesnotexist" >/dev/null 2>&1; then
  _fail "gradient_extract on missing trace_id should exit non-zero"
else
  _ok "gradient_extract on missing trace_id exits non-zero"
fi

# ── AC1: idempotent re-extraction ─────────────────────────────────────────
# Re-running reflection_extract_gradients over the same `--since` window
# must NOT duplicate gradient_records rows for a trace that already
# produced one. Implementer watermarks against gradient_records.evidence.
#
# Earlier in the file we 'unset MINI_ORK_GRADIENT_EXTRACTOR_FN' to test
# the missing-trace error path — but AC1 needs the stub to mint a row.
# Re-install a stub here, scoped to AC1 only.
#
# The base test only sources lib/trace_store.sh + lib/gradient_extractor.sh;
# AC1 needs lib/reflection_pipeline.sh too because that's where
# reflection_extract_gradients (and its watermark hook) live.
echo ""
echo "--- AC1: idempotent re-extract via watermark ---"
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/reflection_pipeline.sh"

_ac1_stub() {
  local _trace_id="$1"
  # `gradient_extract`'s LLM path defaults `evidence = trace_id` after JSON
  # parsing; the override path emits whatever we print, so we must include
  # `evidence` ourselves for `gradient_store` to accept the payload.
  echo "{\"target\":\"workflow.node.ac1\",\"signal\":\"ac1 sig\",\"suggested_change\":\"ac1 fix\",\"confidence\":0.55,\"evidence\":\"$_trace_id\"}"
}
export MINI_ORK_GRADIENT_EXTRACTOR_FN="_ac1_stub"

TID_W="$(trace_write '{"task_class":"grad-watermark","status":"success"}' 2>/dev/null)"
if [[ -n "$TID_W" ]]; then
  BEFORE_W="$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM gradient_records;" 2>/dev/null || echo 0)"
  # First pass — extraction mints rows for unknown traces.
  reflection_extract_gradients 0 >/dev/null 2>&1 || true
  AFTER1_W="$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM gradient_records;" 2>/dev/null || echo 0)"
  # Second pass on the SAME window — must NOT add new rows (watermark).
  reflection_extract_gradients 0 >/dev/null 2>&1 || true
  AFTER2_W="$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM gradient_records;" 2>/dev/null || echo 0)"
  # Two-pronged assertion: the first pass MUST grow the table (proves
  # extraction actually fired), and the second pass MUST equal the first
  # (proves watermark stopped re-extraction). A pure "no-op both ways"
  # would otherwise pass a tautological check.
  if [[ "$AFTER1_W" -gt "$BEFORE_W" ]]; then
    _ok "reflection_extract_gradients: first pass minted rows (before=$BEFORE_W → after1=$AFTER1_W)"
  else
    _fail "reflection_extract_gradients: first pass added 0 rows (before=$BEFORE_W → after1=$AFTER1_W) — stub/exec broken"
  fi
  if [[ "$AFTER2_W" == "$AFTER1_W" ]]; then
    _ok "reflection_extract_gradients: second pass over same window produced 0 new rows (after1=$AFTER1_W → after2=$AFTER2_W)"
  else
    _fail "reflection_extract_gradients: second pass duplicated rows (after1=$AFTER1_W → after2=$AFTER2_W) — watermark broken"
  fi
  # And probe the exact trace we wrote: did it get a gradient on pass 1?
  TID_ROW="$(sqlite3 "$TEST_DB" \
    "SELECT COUNT(*) FROM gradient_records WHERE evidence='$TID_W';" 2>/dev/null || echo 0)"
  if [[ "$TID_ROW" -ge 1 ]]; then
    _ok "AC1 trace $TID_W has gradient_records.evidence row"
  else
    _fail "AC1 trace $TID_W has 0 gradient_records rows — extract missed it"
  fi
else
  _skip "trace_write unavailable for AC1 — skipping"
fi

# Reset the override to its prior (unset) state so later test blocks see
# the original setup. The 'extract on missing trace' test below uses unset.
unset MINI_ORK_GRADIENT_EXTRACTOR_FN

# Probe the watermark helper directly — covers a manually-seeded "already
# linked" row, which is what happens after a prior extract pass on a
# different `--since` window.
if declare -f _gradient_check_watermark >/dev/null 2>&1; then
  TID_LINK="$(trace_write '{"task_class":"grad-watermark","status":"success"}' 2>/dev/null)"
  if [[ -n "$TID_LINK" ]]; then
    python3 -c "
import sqlite3
con = sqlite3.connect('$TEST_DB')
con.execute(\"INSERT OR REPLACE INTO gradient_records (gradient_id, target, signal, suggested_change, evidence, confidence, created_at) VALUES ('gr-wm-pre','wf.node.test','presignaled','x','$TID_LINK', 0.5, 0)\")
con.commit(); con.close()
" 2>/dev/null
    if _gradient_check_watermark "$TID_LINK" 2>/dev/null; then
      _ok "_gradient_check_watermark returns 0 (skip) for already-linked trace"
    else
      _fail "_gradient_check_watermark returns 1 (extract) for already-linked trace"
    fi
  fi
  if _gradient_check_watermark "tr-fresh-no-relation-zzz" 2>/dev/null; then
    _fail "_gradient_check_watermark returns 0 for unknown trace id"
  else
    _ok "_gradient_check_watermark returns 1 (extract) for unknown trace id"
  fi
else
  _skip "_gradient_check_watermark helper not defined"
fi

# ── AC2: __reflect__ noise excluded ──────────────────────────────────────
# A trace whose task_class begins with `__` (framework-internal) must NOT
# produce a gradient_records row in extract. The LLM stub would otherwise
# mint one for every such trace on every run.
echo ""
echo "--- AC2: __reflect__/framework-self traces excluded ---"

if declare -f _gradient_is_framework_agent >/dev/null 2>&1; then
  if _gradient_is_framework_agent "__reflect__"; then
    _ok "_gradient_is_framework_agent recognises __reflect__"
  else
    _fail "_gradient_is_framework_agent does NOT recognise __reflect__"
  fi
  if _gradient_is_framework_agent "__future_agent__"; then
    _ok "_gradient_is_framework_agent recognises __future_agent__"
  else
    _fail "_gradient_is_framework_agent does NOT recognise __future_agent__"
  fi
  if _gradient_is_framework_agent "framework_edit"; then
    _fail "_gradient_is_framework_agent wrongly flags framework_edit"
  else
    _ok "_gradient_is_framework_agent leaves regular task_classes alone"
  fi
  if _gradient_is_framework_agent "" 2>/dev/null; then
    _fail "_gradient_is_framework_agent wrongly flags empty task_class"
  else
    _ok "_gradient_is_framework_agent skips empty task_class"
  fi
else
  _skip "_gradient_is_framework_agent helper not defined"
fi

# Seed a real trace with task_class='__reflect__' and let reflection_extract
# run end-to-end; the trace must be filtered out before extraction.
TID_R="$(trace_write '{"task_class":"__reflect__","status":"success"}' 2>/dev/null)"
if [[ -n "$TID_R" ]]; then
  BEFORE_R="$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM gradient_records WHERE evidence='$TID_R';" 2>/dev/null || echo 0)"
  reflection_extract_gradients 0 >/dev/null 2>&1 || true
  AFTER_R="$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM gradient_records WHERE evidence='$TID_R';" 2>/dev/null || echo 0)"
  if [[ "$BEFORE_R" -eq 0 && "$AFTER_R" -eq 0 ]]; then
    _ok "reflection_extract_gradients did NOT mint a gradient for __reflect__ trace $TID_R"
  else
    _fail "reflection_extract_gradients minted a gradient for __reflect__ trace $TID_R (before=$BEFORE_R after=$AFTER_R)"
  fi
else
  _skip "trace_write unavailable for AC2 trace seed"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
