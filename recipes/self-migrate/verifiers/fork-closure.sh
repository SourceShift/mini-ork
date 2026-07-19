#!/usr/bin/env bash
# verifiers/fork-closure.sh — deterministic no-dangling-runtime-edge gate.
#
# The LLM integration map is useful analysis, but fork retirement must not rely
# on an LLM noticing every caller. This gate inspects the migrated worktree and
# requires both the retired entrypoint and all executable/runtime references to
# be absent. Historical documentation is intentionally outside this runtime
# gate and is updated during the completion audit.

set -uo pipefail
RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
REPO_ROOT="${MO_TARGET_CWD:-${MINI_ORK_ROOT:-$(pwd)}}"
FORK="${MO_FORK:?MO_FORK required}"
ENTRYPOINT="$REPO_ROOT/bin/mini-ork-$FORK"
NEEDLE="bin/mini-ork-$FORK"
DYNAMIC_PATTERN="_bin\\([^)]*['\"]${FORK}['\"]"
EVIDENCE="$RUN_DIR/verifier-fork-closure.log"

pass=true
reasons=()

if [ -e "$ENTRYPOINT" ]; then
  pass=false
  reasons+=("legacy entrypoint still exists: $ENTRYPOINT")
fi

search_roots=()
for rel in bin lib mini_ork scripts tests gates web ui; do
  [ -e "$REPO_ROOT/$rel" ] && search_roots+=("$rel")
done

: >"$EVIDENCE"
if [ "${#search_roots[@]}" -gt 0 ] && (
  cd "$REPO_ROOT"
  rg -n --fixed-strings --hidden --glob '!.git/**' -- "$NEEDLE" "${search_roots[@]}"
) >"$EVIDENCE" 2>&1; then
  pass=false
  reasons+=("runtime references to $NEEDLE remain — see verifier-fork-closure.log")
fi

if [ -d "$REPO_ROOT/mini_ork" ] && (
  cd "$REPO_ROOT"
  rg -n --regexp "$DYNAMIC_PATTERN" mini_ork
) >>"$EVIDENCE" 2>&1; then
  pass=false
  reasons+=("dynamic _bin(..., '$FORK') references remain — see verifier-fork-closure.log")
fi

python3 - "$pass" "$EVIDENCE" "$FORK" "${reasons[@]:-}" <<'PY'
import json
import sys

pass_str, evidence, fork = sys.argv[1], sys.argv[2], sys.argv[3]
reasons = [reason for reason in sys.argv[4:] if reason]
print(json.dumps({
    "name": "fork-closure",
    "fork": fork,
    "pass": pass_str == "true",
    "evidence": evidence,
    "reasons": reasons,
}))
PY

[ "$pass" = true ]
