#!/usr/bin/env bash
# tests/integration/test_dispatch_telemetry_gate.sh — regression suite for the
# four defects diagnosed on researcher run-1781095892-69202 (2026-06-10):
#
#   1. Executable-lane (codex) transcripts carried zero tokens + a false
#      "text-output fallback" marker even when the wrapper harvested real
#      usage into the MO_TURNS_FILE sidecar → _mo_llm_write_exec_transcript.
#   2. <z-insight> protocol blocks (inherited by spawned CLIs from the
#      operator's global agent config) polluted deliverable output
#      → _mo_llm_strip_protocol_blocks.
#   3. codex CLI exposes no billing figure → cl_codex.sh estimates cost from
#      harvested tokens at env-overridable list rates (MO_COST_FILE sidecar).
#   4. the native executor dispatched plans the planner had already declared
#      blocked (plan_status=needs_answers) → pre-dispatch execute gate.

set -uo pipefail

ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT="$ROOT"

OK=0
FAIL=0
_ok()   { OK=$((OK + 1));   echo "  [OK]   $1"; }
_fail() { FAIL=$((FAIL + 1)); echo "  [FAIL] $1"; }

TMP=$(mktemp -d -t mo-telemetry-gate.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

# ── 1+2. exec transcript merge + protocol strip ──────────────────────────────
echo "── exec-lane transcript: sidecar tokens + protocol strip ──"
(
  cd "$TMP"
  printf '%s\n' '{"ok":true}' '<z-insight>{"leak":1}</z-insight>' > out.txt
  printf '%s\n' '{"turn_index":0,"input_tokens":1000,"output_tokens":50,"cache_read_input_tokens":400,"model":"codex","session_id":"s1"}' > out.txt.turns.jsonl
  MINI_ORK_LLM_SOURCE_ONLY=1 source "$ROOT/lib/llm-dispatch.sh" 2>/dev/null || true
  _mo_llm_strip_protocol_blocks out.txt
  _mo_llm_write_exec_transcript out.txt codex
)
if ! grep -q '<z-insight>' "$TMP/out.txt"; then
  _ok "z-insight block stripped from deliverable"
else
  _fail "z-insight block survived strip"
fi
python3 - "$TMP/out.txt.transcript.json" <<'PY' && _ok "exec transcript carries sidecar tokens, no fallback marker" || _fail "exec transcript wrong shape"
import json, sys
d = json.load(open(sys.argv[1]))
assert "fallback" not in d, "false fallback marker"
t = d["turns"][0]
assert t["input_tokens"] == 1000 and t["output_tokens"] == 50, "tokens not merged"
assert t["text"].strip() == '{"ok":true}', f"text wrong: {t['text']!r}"
assert d["totals"] == {"input_tokens": 1000, "output_tokens": 50}
PY

# Missing sidecar → graceful fallback to plain-text transcript
(
  cd "$TMP"
  rm -f out2.txt.transcript.json
  printf 'plain body\n' > out2.txt
  MINI_ORK_LLM_SOURCE_ONLY=1 source "$ROOT/lib/llm-dispatch.sh" 2>/dev/null || true
  _mo_llm_write_exec_transcript out2.txt codex
)
if python3 -c "
import json, sys
d = json.load(open('$TMP/out2.txt.transcript.json'))
assert d.get('fallback') == 'text-output'
" 2>/dev/null; then
  _ok "no sidecar → text-output fallback preserved"
else
  _fail "no-sidecar fallback broken"
fi

# ── 3. cl_codex.sh cost estimation via stub codex ────────────────────────────
echo "── cl_codex.sh: usage harvest + estimated cost sidecar ──"
mkdir -p "$TMP/stubbin"
cat > "$TMP/stubbin/codex" <<'EOF'
#!/usr/bin/env bash
LAST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --output-last-message) LAST="$2"; shift 2;;
    *) shift;;
  esac
done
echo '{"type":"thread.started","thread_id":"t-1"}'
echo '{"type":"turn.completed","usage":{"input_tokens":1000000,"cached_input_tokens":0,"output_tokens":100000}}'
[ -n "$LAST" ] && printf 'body\n' > "$LAST"
EOF
chmod +x "$TMP/stubbin/codex"
# MO_TARGET_CWD pinned to $TMP: without it the cwd-guard's PWD fallback
# resolves to this repo's own checkout (which has bin/mini-ork) and refuses
# the dispatch — this test exercises telemetry sidecars, not the guard.
PATH="$TMP/stubbin:$PATH" \
  MO_TARGET_CWD="$TMP" \
  MO_USAGE_FILE="$TMP/u.tokens" MO_TURNS_FILE="$TMP/t.jsonl" MO_COST_FILE="$TMP/c.cost" \
  MO_CODEX_USD_PER_MTOK_IN=1.0 MO_CODEX_USD_PER_MTOK_OUT=10.0 \
  "$ROOT/lib/providers/cl_codex.sh" --print --output-format text "p" >/dev/null 2>&1
if [ "$(cat "$TMP/u.tokens" 2>/dev/null)" = "$(printf '1000000\t100000')" ]; then
  _ok "usage sidecar harvested from turn.completed"
else
  _fail "usage sidecar wrong: $(cat "$TMP/u.tokens" 2>/dev/null)"
fi
# 1M in @ $1/M + 100k out @ $10/M = $2.00
if python3 -c "assert abs(float(open('$TMP/c.cost').read()) - 2.0) < 1e-6" 2>/dev/null; then
  _ok "estimated cost sidecar = \$2.00 at injected rates"
else
  _fail "cost sidecar wrong: $(cat "$TMP/c.cost" 2>/dev/null)"
fi

# ── 4. execute pre-dispatch gate ──────────────────────────────────────────────
echo "── native execute: needs_answers plan refused before dispatch ──"
HOME_DIR="$TMP/home"
mkdir -p "$HOME_DIR/runs/run-gate"
cat > "$HOME_DIR/runs/run-gate/plan.json" <<'EOF'
{"plan_status":"needs_answers","blocked_by":"run_profile","human_questions":["q1"],"decomposition":[]}
EOF
sqlite3 "$HOME_DIR/state.db" "
CREATE TABLE task_runs(id TEXT PRIMARY KEY, status TEXT, verdict TEXT CHECK (verdict IN ('APPROVE','REQUEST_CHANGES','ESCALATE','CRASH') OR verdict IS NULL), updated_at INTEGER, ended_at INTEGER, notes TEXT, trace_id TEXT, created_at INTEGER, recipe TEXT, cost_usd REAL);
CREATE TABLE run_events(event_id TEXT, run_id TEXT, event_type TEXT, payload_json TEXT, created_at INTEGER);
INSERT INTO task_runs(id,status,created_at) VALUES('run-gate','planned',strftime('%s','now'));
"
# Pin MINI_ORK_DRY_RUN=0: the gate deliberately skips under dry-run
# (execute:299), and CI exports MINI_ORK_DRY_RUN=1 globally — without the
# pin this test asserts different behavior depending on ambient env.
MINI_ORK_HOME="$HOME_DIR" MINI_ORK_RUN_ID="run-gate" MINI_ORK_DRY_RUN=0 \
  "$ROOT/bin/mini-ork" execute "$HOME_DIR/runs/run-gate/plan.json" >/dev/null 2>&1
rc=$?
[ "$rc" -eq 6 ] && _ok "gate exits 6" || _fail "gate rc=$rc (want 6)"
row=$(sqlite3 "$HOME_DIR/state.db" "SELECT status||'|'||verdict FROM task_runs WHERE id='run-gate';")
[ "$row" = "failed|ESCALATE" ] && _ok "task_run marked failed/ESCALATE (schema-valid verdict)" || _fail "task_run row: $row"
ev=$(sqlite3 "$HOME_DIR/state.db" "SELECT count(*) FROM run_events WHERE event_type='execute_blocked';")
[ "$ev" = "1" ] && _ok "execute_blocked event emitted" || _fail "execute_blocked events: $ev"
[ -f "$HOME_DIR/runs/run-gate/blocked.json" ] && _ok "blocked.json artifact written" || _fail "blocked.json missing"

# Override env lets the plan through (dry-run so nothing dispatches)
MINI_ORK_HOME="$HOME_DIR" MINI_ORK_RUN_ID="run-gate" MINI_ORK_EXECUTE_GATE=0 \
  "$ROOT/bin/mini-ork" execute "$HOME_DIR/runs/run-gate/plan.json" --dry-run >/dev/null 2>&1
[ $? -eq 0 ] && _ok "MINI_ORK_EXECUTE_GATE=0 bypasses gate" || _fail "gate override broken"

echo ""
echo "── Results: $OK OK  $FAIL FAIL ──"
[ "$FAIL" -eq 0 ]
