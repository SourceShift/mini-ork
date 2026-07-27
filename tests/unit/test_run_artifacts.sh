#!/usr/bin/env bash
# tests/unit/test_run_artifacts.sh — exercise the run_artifacts bash mirror at
# _mo_llm_persist_agent_transcript (lib/llm-dispatch.sh), the gzip-on-complete
# follow-up heredoc, and the Python prune helper.
#
# Strategy: a tmp DB with the run_artifacts table, a tmp run dir with two
# agent-<node>.{transcript.json,stream.jsonl} files, then drive
# _mo_llm_persist_agent_transcript + the embedded gzip heredoc directly.
# Tests (i)-(iv) from the plan.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/llm-dispatch.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

echo "── unit: run_artifacts bash mirror + retention ──"

[ -f "$LIB" ] || { _skip "lib/llm-dispatch.sh missing"; exit 0; }

# tmp DB + tmp run dir for hermetic runs.
WORK=$(mktemp -d /tmp/mo-runart-XXXXXX)
trap 'rm -rf "$WORK"' EXIT

DB="$WORK/state.db"
RUN_DIR="$WORK/run"
mkdir -p "$RUN_DIR"
export MINI_ORK_DB="$DB"
export MINI_ORK_RUN_DIR="$RUN_DIR"
export MINI_ORK_RUN_ID="run-bash-1"
export MO_NODE_ID="impl"

# ── (i) two-row registration when both files exist ─────────────────────────
sqlite3 "$DB" <<'SQL'
CREATE TABLE run_artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, node_id TEXT, call_id INTEGER,
  kind TEXT NOT NULL, rel_path TEXT NOT NULL,
  bytes INTEGER, sha256 TEXT, created_at INTEGER NOT NULL,
  UNIQUE(run_id, node_id, kind, rel_path)
);
SQL

SAFE_NODE="impl"
echo '{"text":"hello"}' > "$RUN_DIR/agent-${SAFE_NODE}.transcript.json"
printf 'event one\n' > "$RUN_DIR/agent-${SAFE_NODE}.stream.jsonl"

# Fake the out_file prefix the bash function expects.
OUT_FILE="$WORK/out"
cp "$RUN_DIR/agent-${SAFE_NODE}.transcript.json" "${OUT_FILE}.transcript.json"
cp "$RUN_DIR/agent-${SAFE_NODE}.stream.jsonl" "${OUT_FILE}.stream.jsonl"

# shellcheck source=lib/llm-dispatch.sh
source "$LIB"

# Drive the function — it should cp + register rows.
_mo_llm_persist_agent_transcript "$OUT_FILE" "test-model"

COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM run_artifacts WHERE run_id='run-bash-1'")
if [ "$COUNT" = "2" ]; then
  _ok "two rows registered after _mo_llm_persist_agent_transcript"
else
  _fail "expected 2 rows, got $COUNT"
fi

TURN_REL=$(sqlite3 "$DB" "SELECT rel_path FROM run_artifacts WHERE kind='turn_jsonl'")
TRANS_REL=$(sqlite3 "$DB" "SELECT rel_path FROM run_artifacts WHERE kind='transcript'")
# turn_jsonl rel_path may be either the raw .jsonl or the .jsonl.gz sibling,
# because the gzip-on-complete heredoc fires INSIDE the same function call.
case "$TURN_REL" in
  "agent-${SAFE_NODE}.stream.jsonl"|"agent-${SAFE_NODE}.stream.jsonl.gz")
    _ok "turn_jsonl rel_path is portable (=$TURN_REL)" ;;
  *) _fail "turn_jsonl rel_path = $TURN_REL" ;;
esac
[ "$TRANS_REL" = "agent-${SAFE_NODE}.transcript.json" ] && _ok "transcript rel_path matches agent-* basename" || _fail "transcript rel_path = $TRANS_REL"

# Negative: rel_path must NOT be absolute.
TURN_ABS=$(sqlite3 "$DB" "SELECT rel_path FROM run_artifacts WHERE kind='turn_jsonl' AND rel_path LIKE '/%'" | wc -l | tr -d ' ')
[ "$TURN_ABS" = "0" ] && _ok "no absolute rel_paths registered" || _fail "found $TURN_ABS absolute rel_paths"

# ── (ii) PRAGMA-gated no-op when run_artifacts table is absent ─────────────
DB2="$WORK/old.db"
sqlite3 "$DB2" "CREATE TABLE llm_calls(id INTEGER PRIMARY KEY, x TEXT);"
MINI_ORK_DB="$DB2" MINI_ORK_RUN_DIR="$RUN_DIR" \
  MINI_ORK_RUN_ID="run-old" MO_NODE_ID="oldnode" \
  _mo_llm_persist_agent_transcript "$OUT_FILE" "test-model" >/dev/null 2>&1
ROWS2=$(sqlite3 "$DB2" "SELECT COUNT(*) FROM run_artifacts" 2>/dev/null || echo 0)
[ "$ROWS2" = "0" ] && _ok "no-op when run_artifacts table absent" || _fail "unexpected rows on old DB: $ROWS2"

# ── (iii) gzip-on-complete rewrites rel_path to .gz ────────────────────────
# Force a fresh register on the real DB; the gzip heredoc updates rel_path.
GZIP_DB="$WORK/gz.db"
cp "$DB" "$GZIP_DB"
MINI_ORK_DB="$GZIP_DB" MINI_ORK_RUN_DIR="$RUN_DIR" \
  MINI_ORK_RUN_ID="run-gz" MO_NODE_ID="impl" \
  _mo_llm_persist_agent_transcript "$OUT_FILE" "test-model" >/dev/null 2>&1
GZ="$RUN_DIR/agent-${SAFE_NODE}.stream.jsonl.gz"
if [ -f "$GZ" ]; then
  _ok "gzip produced agent-impl.stream.jsonl.gz"
else
  _fail "gzip heredoc did not produce .gz sibling"
fi
NEW_REL=$(sqlite3 "$GZIP_DB" "SELECT rel_path FROM run_artifacts WHERE kind='turn_jsonl' AND run_id='run-gz'")
[ "$NEW_REL" = "agent-${SAFE_NODE}.stream.jsonl.gz" ] && _ok "rel_path rewritten to .gz suffix" || _fail "rel_path after gzip = $NEW_REL"
NEW_BYTES=$(sqlite3 "$GZIP_DB" "SELECT bytes FROM run_artifacts WHERE kind='turn_jsonl' AND run_id='run-gz'")
EXPECTED_BYTES=$(wc -c < "$GZ" | tr -d ' ')
[ "$NEW_BYTES" = "$EXPECTED_BYTES" ] && _ok "bytes match .gz file size" || _fail "bytes=$NEW_BYTES expected=$EXPECTED_BYTES"

# ── (iii-b) gzip UPDATE is scoped by run_id — no cross-run clobber ──────────
# rel_path is a bare basename shared across runs. On a FRESH db+run dir (so
# there's no pre-existing .gz row to trigger a masking UNIQUE-abort), a run's
# gzip must rewrite ONLY its own row, never another run's same-basename row.
X_DB="$WORK/xrun.db"
X_RUN="$WORK/xrun"; mkdir -p "$X_RUN"
sqlite3 "$X_DB" <<'SQL'
CREATE TABLE run_artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, node_id TEXT, call_id INTEGER,
  kind TEXT NOT NULL, rel_path TEXT NOT NULL,
  bytes INTEGER, sha256 TEXT, created_at INTEGER NOT NULL,
  UNIQUE(run_id, node_id, kind, rel_path)
);
SQL
# a different run's row, same basename, that must survive untouched
sqlite3 "$X_DB" "INSERT INTO run_artifacts(run_id, node_id, kind, rel_path, bytes, sha256, created_at) VALUES ('run-other','impl','turn_jsonl','agent-${SAFE_NODE}.stream.jsonl',999,'othersha',$(date +%s));"
printf 'fresh event\n' > "$X_RUN/agent-${SAFE_NODE}.stream.jsonl"
echo '{"text":"fresh"}' > "$X_RUN/agent-${SAFE_NODE}.transcript.json"
X_OUT="$WORK/xout"
cp "$X_RUN/agent-${SAFE_NODE}.stream.jsonl" "${X_OUT}.stream.jsonl"
cp "$X_RUN/agent-${SAFE_NODE}.transcript.json" "${X_OUT}.transcript.json"
MINI_ORK_DB="$X_DB" MINI_ORK_RUN_DIR="$X_RUN" \
  MINI_ORK_RUN_ID="run-fresh" MO_NODE_ID="impl" \
  _mo_llm_persist_agent_transcript "$X_OUT" "test-model" >/dev/null 2>&1
X_SELF=$(sqlite3 "$X_DB" "SELECT rel_path FROM run_artifacts WHERE run_id='run-fresh' AND kind='turn_jsonl'")
OTHER_REL=$(sqlite3 "$X_DB" "SELECT rel_path FROM run_artifacts WHERE run_id='run-other' AND kind='turn_jsonl'")
OTHER_SHA=$(sqlite3 "$X_DB" "SELECT sha256 FROM run_artifacts WHERE run_id='run-other' AND kind='turn_jsonl'")
if [ "$X_SELF" = "agent-${SAFE_NODE}.stream.jsonl.gz" ] \
   && [ "$OTHER_REL" = "agent-${SAFE_NODE}.stream.jsonl" ] && [ "$OTHER_SHA" = "othersha" ]; then
  _ok "gzip UPDATE scoped by run_id (own row .gz, other run's row untouched)"
else
  _fail "cross-run clobber: self=$X_SELF other_rel=$OTHER_REL other_sha=$OTHER_SHA"
fi

# ── (iv) prune removes only turn_jsonl older than TTL ─────────────────────
PRUNE_DB="$WORK/prune.db"
sqlite3 "$PRUNE_DB" <<'SQL'
CREATE TABLE run_artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, node_id TEXT, call_id INTEGER,
  kind TEXT NOT NULL, rel_path TEXT NOT NULL,
  bytes INTEGER, sha256 TEXT, created_at INTEGER NOT NULL,
  UNIQUE(run_id, node_id, kind, rel_path)
);
SQL
ANCIENT=$(($(date +%s) - 60 * 86400))
sqlite3 "$PRUNE_DB" "INSERT INTO run_artifacts(run_id, node_id, kind, rel_path, created_at) VALUES ('r', 'n', 'turn_jsonl', 'old.stream.jsonl.gz', $ANCIENT);"
sqlite3 "$PRUNE_DB" "INSERT INTO run_artifacts(run_id, node_id, kind, rel_path, created_at) VALUES ('r', 'n', 'evidence_bundle', 'old.evidence.json', $ANCIENT);"
DELETED=$(MINI_ORK_RUN_DIR="$WORK/empty" python3 -c "
import sys
sys.path.insert(0, '$MINI_ORK_ROOT')
from mini_ork.dispatch.retention import prune_old_trajectories
print(prune_old_trajectories('$PRUNE_DB', ttl_days=30))
")
[ "$DELETED" = "1" ] && _ok "prune deleted exactly 1 row" || _fail "prune deleted=$DELETED expected=1"
EVIDENCE_LEFT=$(sqlite3 "$PRUNE_DB" "SELECT COUNT(*) FROM run_artifacts WHERE kind='evidence_bundle'")
[ "$EVIDENCE_LEFT" = "1" ] && _ok "evidence_bundle row preserved" || _fail "evidence_bundle count=$EVIDENCE_LEFT"
TURN_LEFT=$(sqlite3 "$PRUNE_DB" "SELECT COUNT(*) FROM run_artifacts WHERE kind='turn_jsonl'")
[ "$TURN_LEFT" = "0" ] && _ok "turn_jsonl row removed" || _fail "turn_jsonl count=$TURN_LEFT"

echo "── results: $PASS pass / $FAIL fail / $SKIP skip ──"
[ "$FAIL" = "0" ]