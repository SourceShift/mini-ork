#!/usr/bin/env bash
# Regression: cl_codex.sh must never pass the (multi-MB) codex JSONL stream or
# the assistant body through exec's env/argv — that hits ARG_MAX and dies with
# E2BIG "Argument list too long" (observed fleet-wide 2026-06-30: every codex
# dispatch failed at the usage-harvest python3 call). The wrapper now passes a
# FILE PATH (short) and reads it in python / via stdin. We stub `codex` to emit
# a ~1.5MB agent_message + several turn.completed events, then assert the
# wrapper produces clean output + usage/turns/cost sidecars without dying.
set -uo pipefail
ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PASS=0; FAIL=0
ok(){ echo "  [OK]   $1"; PASS=$((PASS+1)); }
bad(){ echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

echo "── unit: cl_codex.sh ARG_MAX/E2BIG regression ──"

# --- stub codex CLI --------------------------------------------------------
# Honors --output-last-message <path>. Emits thread.started + 2 turn.completed
# (usage) + one big agent_message on stdout (→ stream file). Writes the big
# assistant body to the last-message file UNLESS STUB_NO_LASTMSG=1, which forces
# cl_codex's else-branch that reconstructs the body from the stream (site 279).
cat > "$TMP/codex" <<'STUB'
#!/usr/bin/env bash
last_msg=""
while [ $# -gt 0 ]; do
  case "$1" in
    --output-last-message) last_msg="$2"; shift 2 ;;
    *) shift ;;
  esac
done
big="$(head -c 1500000 /dev/zero | tr '\0' 'x')"
printf '{"type":"thread.started","thread_id":"th-1"}\n'
printf '{"type":"turn.completed","usage":{"input_tokens":1000,"output_tokens":500,"cached_input_tokens":200}}\n'
printf '{"type":"turn.completed","usage":{"input_tokens":2000,"output_tokens":700,"cached_input_tokens":100}}\n'
printf '{"type":"item.completed","item":{"type":"agent_message","text":"FINAL_ANSWER %s"}}\n' "$big"
if [ -z "${STUB_NO_LASTMSG:-}" ] && [ -n "$last_msg" ]; then
  printf 'FINAL_ANSWER %s' "$big" > "$last_msg"
fi
exit 0
STUB
chmod +x "$TMP/codex"

run_wrapper(){ # $1=format ; rest via env already set by caller
  # MO_TARGET_CWD pinned to $TMP (not $ROOT): without it the cwd-guard's PWD
  # fallback resolves to this repo's own checkout (which has bin/mini-ork)
  # and refuses the dispatch — this test isn't exercising the guard, just
  # the wrapper's E2BIG handling, so it needs a non-framework target cwd.
  MO_TARGET_CWD="$TMP" PATH="$TMP:$PATH" bash "$ROOT/lib/providers/cl_codex.sh" --print --output-format "$1" "do the thing"
}

# ── Scenario A: last-message present (sites 187 harvest + 311 clean + 356) ──
USAGE="$TMP/a.tokens"; TURNS="$TMP/a.turns.jsonl"; COST="$TMP/a.cost"
OUT="$(MO_USAGE_FILE="$USAGE" MO_TURNS_FILE="$TURNS" MO_COST_FILE="$COST" run_wrapper text 2>"$TMP/a.err")"
RC=$?
grep -qi "argument list too long" "$TMP/a.err" && bad "E2BIG triggered (scenario A)" || ok "no ARG_MAX/E2BIG with 1.5MB stream (A)"
[ "$RC" -eq 0 ] && ok "wrapper exit 0 (A)" || bad "wrapper rc=$RC (A): $(tail -1 "$TMP/a.err")"
grep -q "$(printf '3000\t1200')" "$USAGE" 2>/dev/null && ok "usage totals harvested (in=3000 out=1200)" || bad "usage wrong: $(cat "$USAGE" 2>/dev/null)"
[ -s "$TURNS" ] && ok "turns sidecar written ($(wc -l <"$TURNS" | tr -d ' ') turns)" || bad "turns sidecar empty"
[ -s "$COST" ] && ok "cost sidecar written ($(cat "$COST"))" || bad "cost sidecar empty"
printf '%s' "$OUT" | grep -q "FINAL_ANSWER" && ok "clean text output carries assistant body (A)" || bad "clean output missing body (A)"

# ── Scenario B: empty last-message → reconstruct from stream (site 279) ──────
OUTB="$(STUB_NO_LASTMSG=1 run_wrapper text 2>"$TMP/b.err")"
RCB=$?
grep -qi "argument list too long" "$TMP/b.err" && bad "E2BIG triggered (scenario B / site 279)" || ok "no E2BIG reconstructing body from stream (B)"
[ "$RCB" -eq 0 ] && ok "wrapper exit 0 (B)" || bad "wrapper rc=$RCB (B): $(tail -1 "$TMP/b.err")"
printf '%s' "$OUTB" | grep -q "FINAL_ANSWER" && ok "reconstructed body carries assistant text (B)" || bad "reconstructed body missing (B)"

# ── Scenario C: json envelope (site 356 — CLEAN via stdin, not argv) ─────────
OUTJ="$(run_wrapper json 2>"$TMP/c.err")"
grep -qi "argument list too long" "$TMP/c.err" && bad "E2BIG in json envelope (site 356)" || ok "no E2BIG in json envelope path (C)"
printf '%s' "$OUTJ" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("model")=="codex" and "FINAL_ANSWER" in d.get("result",""); print("ok")' >/dev/null 2>&1 \
  && ok "json envelope valid + carries body (C)" || bad "json envelope invalid (C)"

echo "── Results: $PASS OK  $FAIL FAIL ──"
[ "$FAIL" -eq 0 ]
