#!/usr/bin/env bash
# tests/integration/test_gradient_dedup.sh — cross-target gradient dedup in gradient_store
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
export PATH="$MINI_ORK_ROOT/bin:$PATH"

TMPROOT=$(mktemp -d /tmp/ork-int-test-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT
cd "$TMPROOT"
git init -q
export MINI_ORK_HOME="$TMPROOT/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"

mini-ork init >/dev/null 2>&1

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/gradient_extractor.sh"

_count() { sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM gradient_records;"; }

# Seed a trace so evidence -> task_class resolution works
sqlite3 "$MINI_ORK_DB" "INSERT INTO execution_traces (trace_id, task_class, status) VALUES ('tr-test-1', 'obs_smoke', 'success');" 2>/dev/null || true

echo "── integration: gradient dedup ──"

# 1. First record inserts cleanly
GID1=$(gradient_store '{"target":"workflow.node.execute","signal":"run_id, workflow_version_id, prompt_version_hash, and context_bundle_hash are all empty on the execute trace","suggested_change":"Stamp run lineage fields when writing the trace","evidence":"tr-test-1","confidence":0.8}' 2>/dev/null)
[ -n "$GID1" ] && [ "$(_count)" = "1" ] \
  && _ok "first record inserted ($GID1)" \
  || _fail "first record insert (gid=$GID1 count=$(_count))"

# 2. Near-duplicate under a DIFFERENT target is absorbed, not inserted
GID2=$(gradient_store '{"target":"workflow.node.obs_smoke","signal":"run_id, workflow_version_id, prompt_version_hash, and context_bundle_hash are all empty on the obs_smoke trace","suggested_change":"Stamp run lineage fields when writing the trace","evidence":"tr-test-1","confidence":0.9}' 2>"$TMPROOT/dedup.err")
if [ "$GID2" = "$GID1" ] && [ "$(_count)" = "1" ] && grep -q "dedup sim=" "$TMPROOT/dedup.err"; then
  _ok "near-dup absorbed into existing record (same gid, count still 1)"
else
  _fail "near-dup not absorbed (gid1=$GID1 gid2=$GID2 count=$(_count))"
fi

# 3. Absorption keeps the MAX confidence
CONF=$(sqlite3 "$MINI_ORK_DB" "SELECT confidence FROM gradient_records WHERE gradient_id='$GID1';")
[ "$CONF" = "0.9" ] \
  && _ok "absorbed record carries max confidence (0.9)" \
  || _fail "confidence not raised to max (got $CONF)"

# 4. A genuinely distinct learning still inserts
GID3=$(gradient_store '{"target":"agent.reviewer.prompt","signal":"Reviewer emitted a bare verdict without reading the artifact under review","suggested_change":"Require the reviewer to quote at least one line from the artifact","evidence":"tr-test-1","confidence":0.7}' 2>/dev/null)
[ -n "$GID3" ] && [ "$GID3" != "$GID1" ] && [ "$(_count)" = "2" ] \
  && _ok "distinct learning inserted as new record" \
  || _fail "distinct learning blocked (gid=$GID3 count=$(_count))"

# 5. MO_GRADIENT_DEDUP_SIM=0 disables dedup
GID4=$(MO_GRADIENT_DEDUP_SIM=0 gradient_store '{"target":"workflow.node.verify","signal":"run_id, workflow_version_id, prompt_version_hash, and context_bundle_hash are all empty on the verify trace","suggested_change":"Stamp run lineage fields when writing the trace","evidence":"tr-test-1","confidence":0.6}' 2>/dev/null)
[ -n "$GID4" ] && [ "$GID4" != "$GID1" ] && [ "$(_count)" = "3" ] \
  && _ok "MO_GRADIENT_DEDUP_SIM=0 disables dedup" \
  || _fail "dedup not disabled by env (gid=$GID4 count=$(_count))"

# 6. Explicit gradient_id bypasses dedup (upsert contract preserved)
GID5=$(gradient_store "{\"gradient_id\":\"$GID4\",\"target\":\"workflow.node.verify\",\"signal\":\"run_id, workflow_version_id, prompt_version_hash, and context_bundle_hash are all empty on the verify trace\",\"suggested_change\":\"Stamp run lineage fields when writing the trace\",\"evidence\":\"tr-test-1\",\"confidence\":0.95}" 2>/dev/null)
CONF5=$(sqlite3 "$MINI_ORK_DB" "SELECT confidence FROM gradient_records WHERE gradient_id='$GID4';")
[ "$GID5" = "$GID4" ] && [ "$CONF5" = "0.95" ] && [ "$(_count)" = "3" ] \
  && _ok "explicit gradient_id upserts in place (no dedup detour)" \
  || _fail "explicit gradient_id upsert broken (gid=$GID5 conf=$CONF5 count=$(_count))"

echo ""
echo "── Results: $PASS OK  $FAIL FAIL ──"
[ "$FAIL" -eq 0 ]
