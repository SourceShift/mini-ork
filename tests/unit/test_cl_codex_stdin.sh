#!/usr/bin/env bash
# Regression: cl_codex.sh must accept the prompt from STDIN when no positional
# prompt is given (the wrapper-stdin contract the Python dispatch layer drives —
# prompt never on argv ⇒ E2BIG-proof from the caller all the way in). Existing
# argv callers must still work unchanged.
set -uo pipefail
ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PASS=0; FAIL=0
ok(){ echo "  [OK]   $1"; PASS=$((PASS+1)); }
bad(){ echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

echo "── unit: cl_codex.sh stdin prompt mode ──"

# Stub codex: capture the prompt it receives (the final positional arg, after
# `--`) so we can assert what flowed through the wrapper.
cat > "$TMP/codex" <<'STUB'
#!/usr/bin/env bash
last_msg=""; prev=""
for a in "$@"; do
  [ "$prev" = "--output-last-message" ] && last_msg="$a"
  prev="$a"
done
printf '%s' "${@: -1}" > "$CODEX_PROMPT_CAPTURE"
printf '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n'
[ -n "$last_msg" ] && printf 'ANSWER' > "$last_msg"
exit 0
STUB
chmod +x "$TMP/codex"

# MO_TARGET_CWD pinned to $TMP (not $ROOT): without it the cwd-guard's PWD
# fallback resolves to this repo's own checkout (which has bin/mini-ork) and
# refuses the dispatch — this test isn't exercising the guard, just stdin
# prompt handling, so it needs a non-framework target cwd.
run_codex(){ MO_TARGET_CWD="$TMP" PATH="$TMP:$PATH" CODEX_PROMPT_CAPTURE="$TMP/cap" bash "$ROOT/lib/providers/cl_codex.sh" "$@"; }

# 1. prompt via stdin, NO positional prompt → wrapper reads stdin
: > "$TMP/cap"
printf '%s' "HELLO-VIA-STDIN" | run_codex --print --output-format text >/dev/null 2>&1
[ "$(cat "$TMP/cap")" = "HELLO-VIA-STDIN" ] && ok "prompt read from stdin reached codex" || bad "stdin prompt lost (got '$(cat "$TMP/cap")')"

# 2. positional prompt still works (backward compat) — argv wins over stdin
: > "$TMP/cap"
printf '%s' "IGNORED-STDIN" | run_codex --print --output-format text "ARGV-PROMPT" >/dev/null 2>&1
[ "$(cat "$TMP/cap")" = "ARGV-PROMPT" ] && ok "positional prompt still honored (argv wins)" || bad "argv prompt regressed (got '$(cat "$TMP/cap")')"

# 3. no prompt at all (no argv, stdin is empty/closed) → clean exit 2, not a hang
run_codex --print --output-format text </dev/null >/dev/null 2>&1
[ "$?" -eq 2 ] && ok "no prompt anywhere → exit 2 (no hang)" || bad "expected exit 2 with no prompt"

echo "── Results: $PASS OK  $FAIL FAIL ──"
[ "$FAIL" -eq 0 ]
