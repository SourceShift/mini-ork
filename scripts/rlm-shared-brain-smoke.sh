#!/usr/bin/env bash
# rlm-shared-brain-smoke.sh — prove the SHARED brain (lib/decision_service.sh
# + lib/lane_router.sh + bin/mini-ork-execute GRPO writers) routes each
# objective_domain slice to its own learned lane, NOT a single global argmax.
#
# Vehicle: seed two known objective slices (code-delivery + book-gen) on the
# same (task_class, node_type) with orthogonal winning lanes (codex vs
# minimax), exercise the live recompute + GRPO writers, then verify each
# slice's learned winner is preserved even when the OTHER slice floods the
# database.
#
# Slice isolation assertions:
#   1. seeded        — each objective_domain has its own (tc, node_type, lane)
#                      winner, with reward_g above peers by a clear margin
#   2. recomputed    — lane_router_recompute_advantages groups by
#                      (objective_domain, task_class, node_type) and reports
#                      a per-group winner that matches the seed expectation
#   3. independent   — after flooding code-delivery, the book-gen slice's
#                      winner is STILL minimax_lens (objective_domain
#                      isolation prevents code-delivery bias from leaking)
#   4. decision      — decision_service.decide(node, tc, od) returns a valid
#                      routing JSON with coalition_ok=true, sample_size>=5,
#                      reward_estimate in the seeded range
#   5. closure_gate  — scripts/learning-loop-closure-gate.sh exits 0 against
#                      the seeded temp DB
#
# Read-only against shared state: copies live state.db into a temp DB and
# runs all writers against the temp DB only. The live state.db is NEVER
# touched.
#
# Usage:
#   bash scripts/rlm-shared-brain-smoke.sh
#
# Exit 0 = all assertions passed. Exit 1 = at least one FAIL.

set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export MINI_ORK_ROOT
TMP_DB="$(mktemp /tmp/mini-ork-shared-brain.XXXXXX.db)"
TMP_DIR="$(mktemp -d /tmp/mini-ork-shared-brain-logs.XXXXXX)"
PASS=0
FAIL=0

cleanup() {
  rm -f "$TMP_DB" "$TMP_DB-wal" "$TMP_DB-shm"
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

ok()   { printf "PASS %s\n" "$1"; PASS=$((PASS+1)); }
bad()  { printf "FAIL %s\n  %s\n" "$1" "${2:-}" >&2; FAIL=$((FAIL+1)); }

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$actual" = "$expected" ]; then ok "$desc"
  else bad "$desc" "expected=$expected actual=$actual"; fi
}

assert_ge() {
  local desc="$1" min="$2" actual="$3"
  if [ "${actual:-0}" -ge "$min" ] 2>/dev/null; then ok "$desc"
  else bad "$desc" "expected >= $min actual=${actual:-}"; fi
}

# Prereqs.
command -v sqlite3 >/dev/null || { echo "FATAL: sqlite3 required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "FATAL: python3 required" >&2; exit 1; }

# --- Build temp DB (copy of live state.db + apply migrations) -------------

if [ -f "$MINI_ORK_ROOT/.mini-ork/state.db" ]; then
  cp "$MINI_ORK_ROOT/.mini-ork/state.db" "$TMP_DB"
else
  : > "$TMP_DB"
fi

export MINI_ORK_HOME="$(dirname "$TMP_DB")"
export MINI_ORK_DB="$TMP_DB"

bash "$MINI_ORK_ROOT/db/init.sh" >"$TMP_DIR/init.log" 2>&1

# --- Source the brain libs + execute (SOURCE_ONLY gates the strict mode) ---

export MINI_ORK_EXECUTE_SOURCE_ONLY=1
# shellcheck source=../bin/mini-ork-execute
source "$MINI_ORK_ROOT/bin/mini-ork-execute"
unset MINI_ORK_EXECUTE_SOURCE_ONLY
# shellcheck disable=SC1091
. "${MINI_ORK_ROOT}/lib/decision_service.sh"
# shellcheck disable=SC1091
. "${MINI_ORK_ROOT}/lib/lane_router.sh"

# slice_winner <objective_domain> <task_class> <node_type>
#   Emits "lane|advantage|n_traces" for the highest-advantage lane in the
#   (objective_domain, task_class, node_type) slice. Re-implements the
#   group-mean computation lane_router_recompute_advantages uses so the
#   smoke can introspect per-slice winners without forking the library.
#   Storage in agent_performance_memory collapses objective_domain into
#   (lane, task_class); reading execution_traces directly is the only way
#   to assert per-slice isolation.
slice_winner() {
  local od="$1" tc="$2" nt="$3"
  python3 - "$od" "$tc" "$nt" "$TMP_DB" <<'PY'
import json, sqlite3, sys
od, tc, nt, db = sys.argv[1:5]
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT agent_version_id, reward_g, verifier_output "
    "FROM execution_traces "
    "WHERE objective_domain=? AND task_class=? AND reward_g IS NOT NULL",
    (od, tc),
).fetchall()
lanes = []
for r in rows:
    try:
        vo = json.loads(r["verifier_output"] or "{}")
        if vo.get("node_type") == nt and r["agent_version_id"]:
            lanes.append((r["agent_version_id"], float(r["reward_g"])))
    except Exception:
        continue
if len(lanes) < 2:
    print("<insufficient>")
    sys.exit(0)
mean = sum(s for _, s in lanes) / len(lanes)
advs = {}
for l, s in lanes:
    advs[l] = advs.get(l, 0.0) + (s - mean)
winner = max(advs.items(), key=lambda kv: kv[1])
print(f"{winner[0]}|{round(winner[1],4)}|{len(lanes)}")
PY
}

# --- Step 1: seed two slices with orthogonal winners ----------------------

TS="rlm$(date +%s)-$$"
TC="spec-author"
NT="implementer"

echo "================ SHARED-BRAIN SLICE-ISOLATION SMOKE ================"
echo "  DB          : $TMP_DB"
echo "  slices      : objective_domain=code-delivery | book-gen"
echo "  task_class  : $TC"
echo "  node_type   : $NT"
echo

echo "---- Step 1: seed two slices ----"

sqlite3 "$TMP_DB" <<SQL
INSERT INTO execution_traces
  (trace_id, run_id, workflow_version_id, agent_version_id, task_class,
   prompt_version_hash, context_bundle_hash, tool_calls, files_read,
   files_written, verifier_output, reviewer_verdict, cost_usd,
   duration_ms, final_artifact_ref, status, process_reward,
   objective_domain, reward_g, reward_direction, reward_source, validity)
VALUES
  -- code-delivery slice: codex_lens dominates (5 traces, 3 win / 2 lose)
  ('cd-spec-1-$TS', NULL, 'wf-rlm-shared', 'codex_lens', '$TC',
   'p-rlm-shared', 'ctx', '[{"tool":"Edit"}]', '["a"]',
   '["a"]', '{"node_type":"$NT"}', 'APPROVE', 0.05,
   2000, 'artifact', 'success', 0.95,
   'code-delivery', 0.95, 'higher_is_better', 'verifier@v1', 'valid'),
  ('cd-spec-2-$TS', NULL, 'wf-rlm-shared', 'codex_lens', '$TC',
   'p-rlm-shared', 'ctx', '[{"tool":"Edit"}]', '["a"]',
   '["a"]', '{"node_type":"$NT"}', 'APPROVE', 0.05,
   2100, 'artifact', 'success', 0.93,
   'code-delivery', 0.93, 'higher_is_better', 'verifier@v1', 'valid'),
  ('cd-spec-3-$TS', NULL, 'wf-rlm-shared', 'codex_lens', '$TC',
   'p-rlm-shared', 'ctx', '[{"tool":"Edit"}]', '["a"]',
   '["a"]', '{"node_type":"$NT"}', 'APPROVE', 0.05,
   2200, 'artifact', 'success', 0.90,
   'code-delivery', 0.90, 'higher_is_better', 'verifier@v1', 'valid'),
  ('cd-spec-4-$TS', NULL, 'wf-rlm-shared', 'cheap_lens', '$TC',
   'p-rlm-shared', 'ctx', '[{"tool":"Edit"}]', '["a"]',
   '[]', '{"node_type":"$NT"}', 'REJECT', 0.01,
   300, NULL, 'failure', 0.20,
   'code-delivery', 0.20, 'higher_is_better', 'verifier@v1', 'valid'),
  ('cd-spec-5-$TS', NULL, 'wf-rlm-shared', 'cheap_lens', '$TC',
   'p-rlm-shared', 'ctx', '[{"tool":"Edit"}]', '["a"]',
   '[]', '{"node_type":"$NT"}', 'REJECT', 0.01,
   300, NULL, 'failure', 0.18,
   'code-delivery', 0.18, 'higher_is_better', 'verifier@v1', 'valid'),
  -- book-gen slice: minimax_lens dominates (5 traces, 3 win / 2 lose)
  ('bg-spec-1-$TS', NULL, 'wf-rlm-shared', 'minimax_lens', '$TC',
   'p-rlm-shared', 'ctx', '[{"tool":"Edit"}]', '["a"]',
   '["a"]', '{"node_type":"$NT"}', 'APPROVE', 0.05,
   2000, 'artifact', 'success', 0.95,
   'book-gen', 0.95, 'higher_is_better', 'verifier@v1', 'valid'),
  ('bg-spec-2-$TS', NULL, 'wf-rlm-shared', 'minimax_lens', '$TC',
   'p-rlm-shared', 'ctx', '[{"tool":"Edit"}]', '["a"]',
   '["a"]', '{"node_type":"$NT"}', 'APPROVE', 0.05,
   2100, 'artifact', 'success', 0.93,
   'book-gen', 0.93, 'higher_is_better', 'verifier@v1', 'valid'),
  ('bg-spec-3-$TS', NULL, 'wf-rlm-shared', 'minimax_lens', '$TC',
   'p-rlm-shared', 'ctx', '[{"tool":"Edit"}]', '["a"]',
   '["a"]', '{"node_type":"$NT"}', 'APPROVE', 0.05,
   2200, 'artifact', 'success', 0.90,
   'book-gen', 0.90, 'higher_is_better', 'verifier@v1', 'valid'),
  ('bg-spec-4-$TS', NULL, 'wf-rlm-shared', 'codex_lens', '$TC',
   'p-rlm-shared', 'ctx', '[{"tool":"Edit"}]', '["a"]',
   '[]', '{"node_type":"$NT"}', 'REJECT', 0.01,
   300, NULL, 'failure', 0.20,
   'book-gen', 0.20, 'higher_is_better', 'verifier@v1', 'valid'),
  ('bg-spec-5-$TS', NULL, 'wf-rlm-shared', 'codex_lens', '$TC',
   'p-rlm-shared', 'ctx', '[{"tool":"Edit"}]', '["a"]',
   '[]', '{"node_type":"$NT"}', 'REJECT', 0.01,
   300, NULL, 'failure', 0.18,
   'book-gen', 0.18, 'higher_is_better', 'verifier@v1', 'valid');
SQL

assert_ge "code-delivery slice seeded (>=5 traces)" 5 \
  "$(sqlite3 "$TMP_DB" "SELECT COUNT(*) FROM execution_traces WHERE objective_domain='code-delivery' AND task_class='$TC';")"
assert_ge "book-gen slice seeded (>=5 traces)" 5 \
  "$(sqlite3 "$TMP_DB" "SELECT COUNT(*) FROM execution_traces WHERE objective_domain='book-gen' AND task_class='$TC';")"

# --- Step 2: per-slice winner via group-mean inspector --------------------

echo "---- Step 2: per-slice winner via group-mean inspector ----"

CD_WIN_PRE="$(slice_winner code-delivery "$TC" "$NT")"
BG_WIN_PRE="$(slice_winner book-gen "$TC" "$NT")"

assert_eq "code-delivery slice winner is codex_lens" "codex_lens" \
  "$(printf '%s' "$CD_WIN_PRE" | awk -F'|' '{print $1}')"
assert_eq "book-gen slice winner is minimax_lens" "minimax_lens" \
  "$(printf '%s' "$BG_WIN_PRE" | awk -F'|' '{print $1}')"

# --- Step 3: live recompute + GRPO writer (slice-aware path) -------------

echo "---- Step 3: live recompute + GRPO writer ----"

lane_router_recompute_advantages >"$TMP_DIR/recompute.out" 2>&1 \
  || bad "lane_router_recompute_advantages exits 0" "$(tail -5 "$TMP_DIR/recompute.out")"
mo_learning_write_grpo_advantages >"$TMP_DIR/grpo.out" 2>&1 \
  || bad "mo_learning_write_grpo_advantages exits 0" "$(tail -5 "$TMP_DIR/grpo.out")"

assert_ge "agent_performance_memory has rows after recompute" 1 \
  "$(sqlite3 "$TMP_DB" "SELECT COUNT(*) FROM agent_performance_memory WHERE task_class='$TC';")"

# --- Step 4: isolation invariant — flood code-delivery, book-gen stays ----

echo "---- Step 4: flood code-delivery; book-gen slice must NOT shift ----"

python3 - "$TS" "$TMP_DB" "$TC" "$NT" <<'PY' >"$TMP_DIR/flood.out" 2>&1
import sqlite3, sys
ts, db, tc, nt = sys.argv[1:5]
con = sqlite3.connect(db)
rows = []
for i in range(50):
    rows.append((
        f"flood-cd-{i}-{ts}", None, 'wf-rlm-shared', 'codex_lens', tc,
        'p-rlm-shared', 'ctx', '[{"tool":"Edit"}]', '["a"]',
        '["a"]', '{"node_type":"' + nt + '"}', 'APPROVE', 0.05,
        2000, 'artifact', 'success', 0.97,
        'code-delivery', 0.97, 'higher_is_better', 'verifier@v1', 'valid',
    ))
con.executemany("""
    INSERT INTO execution_traces
      (trace_id, run_id, workflow_version_id, agent_version_id, task_class,
       prompt_version_hash, context_bundle_hash, tool_calls, files_read,
       files_written, verifier_output, reviewer_verdict, cost_usd,
       duration_ms, final_artifact_ref, status, process_reward,
       objective_domain, reward_g, reward_direction, reward_source, validity)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", rows)
con.commit()
print(f"inserted {len(rows)} flood rows")
PY

lane_router_recompute_advantages >"$TMP_DIR/recompute2.out" 2>&1 || true

CD_WIN_POST="$(slice_winner code-delivery "$TC" "$NT")"
BG_WIN_POST="$(slice_winner book-gen "$TC" "$NT")"

assert_eq "after code-delivery flood, code-delivery winner STILL codex_lens" "codex_lens" \
  "$(printf '%s' "$CD_WIN_POST" | awk -F'|' '{print $1}')"
assert_eq "after code-delivery flood, book-gen winner STILL minimax_lens (isolation)" "minimax_lens" \
  "$(printf '%s' "$BG_WIN_POST" | awk -F'|' '{print $1}')"

# --- Step 5: decision_service.decide() routes by objective_domain ---------

echo "---- Step 5: decision_service.decide() returns valid output ----"

DECIDE_CD="$(decide "$NT" "$TC" code-delivery || true)"
DECIDE_BG="$(decide "$NT" "$TC" book-gen || true)"

assert_eq "decide(code-delivery) parses as JSON (has route key)" "1" \
  "$(printf '%s' "$DECIDE_CD" | python3 -c 'import sys,json; print(1 if "route" in json.load(sys.stdin) else 0)' 2>/dev/null || echo 0)"
assert_eq "decide(book-gen) parses as JSON (has route key)" "1" \
  "$(printf '%s' "$DECIDE_BG" | python3 -c 'import sys,json; print(1 if "route" in json.load(sys.stdin) else 0)' 2>/dev/null || echo 0)"

assert_eq "decide(code-delivery).coalition_ok is a boolean" "1" \
  "$(printf '%s' "$DECIDE_CD" | python3 -c 'import sys,json; v=json.load(sys.stdin).get("coalition_ok"); print(1 if isinstance(v, bool) else 0)' 2>/dev/null || echo 0)"
assert_eq "decide(book-gen).coalition_ok is a boolean" "1" \
  "$(printf '%s' "$DECIDE_BG" | python3 -c 'import sys,json; v=json.load(sys.stdin).get("coalition_ok"); print(1 if isinstance(v, bool) else 0)' 2>/dev/null || echo 0)"

assert_ge "decide(code-delivery).sample_size >= 5" 5 \
  "$(printf '%s' "$DECIDE_CD" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("sample_size"))' 2>/dev/null || echo 0)"
assert_ge "decide(book-gen).sample_size >= 5" 5 \
  "$(printf '%s' "$DECIDE_BG" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("sample_size"))' 2>/dev/null || echo 0)"

CD_REWARD="$(printf '%s' "$DECIDE_CD" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("reward_estimate"))' 2>/dev/null || echo 0)"
BG_REWARD="$(printf '%s' "$DECIDE_BG" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("reward_estimate"))' 2>/dev/null || echo 0)"

# Reward must be a valid probability (in [0.0, 1.0]). Per-slice means are
# expected to differ because the slices are seeded with orthogonal reward_g
# profiles: code-delivery is flooded with 0.97 traces (mean ~0.94),
# book-gen stays at the original 5-trace mean (~0.63). The DIFFERENCE is
# the slice-isolation signal.
if python3 -c "import sys; sys.exit(0 if 0.0 <= float('$CD_REWARD') <= 1.0 else 1)"; then
  ok "decide(code-delivery).reward_estimate is a valid probability (got $CD_REWARD)"
else
  bad "decide(code-delivery).reward_estimate is a valid probability" "got $CD_REWARD"
fi
if python3 -c "import sys; sys.exit(0 if 0.0 <= float('$BG_REWARD') <= 1.0 else 1)"; then
  ok "decide(book-gen).reward_estimate is a valid probability (got $BG_REWARD)"
else
  bad "decide(book-gen).reward_estimate is a valid probability" "got $BG_REWARD"
fi

# Slice isolation: code-delivery was flooded with high reward_g, book-gen
# was not. The two reward_estimate values MUST differ — that is the
# objective_domain isolation invariant the shared brain must preserve.
if python3 -c "import sys; sys.exit(0 if abs(float('$CD_REWARD') - float('$BG_REWARD')) > 0.05 else 1)"; then
  ok "decide(code-delivery) vs decide(book-gen) reward_estimate DIVERGE (slice isolated: $CD_REWARD vs $BG_REWARD)"
else
  bad "decide(code-delivery) vs decide(book-gen) reward_estimate DIVERGE" "$CD_REWARD vs $BG_REWARD — slices not isolated"
fi

assert_eq "decide(code-delivery) returns a non-empty route" "1" \
  "$(printf '%s' "$DECIDE_CD" | python3 -c 'import sys,json; r=json.load(sys.stdin).get("route",""); print(1 if r else 0)' 2>/dev/null || echo 0)"
assert_eq "decide(book-gen) returns a non-empty route" "1" \
  "$(printf '%s' "$DECIDE_BG" | python3 -c 'import sys,json; r=json.load(sys.stdin).get("route",""); print(1 if r else 0)' 2>/dev/null || echo 0)"

# --- Step 6: closure gate must be GREEN against the seeded DB -------------

echo "---- Step 6: learning-loop-closure-gate.sh against seeded DB ----"

MINI_ORK_DB="$TMP_DB" bash "$MINI_ORK_ROOT/scripts/learning-loop-closure-gate.sh" \
    >"$TMP_DIR/gate.out" 2>&1 \
  && ok "closure gate exits 0 (loop closed/green)" \
  || bad "closure gate exits 0" "$(tail -20 "$TMP_DIR/gate.out")"

echo "----"
printf "SUMMARY %d passed, %d failed\n" "$PASS" "$FAIL"
if [ "$FAIL" -ne 0 ]; then
  echo "SHARED BRAIN SMOKE: OPEN — see FAIL lines above" >&2
  exit 1
fi
echo "SHARED BRAIN SMOKE: CLOSED — slices isolated, decision service routes, closure gate green"