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
EVIDENCE="$RUN_DIR/verifier-fork-closure.log"

pass=true
reasons=()
mkdir -p "$RUN_DIR"

search_roots=()
for rel in bin lib mini_ork scripts tests gates web ui; do
  [ -e "$REPO_ROOT/$rel" ] && search_roots+=("$rel")
done

: >"$EVIDENCE"
if [ "$FORK" = "cli" ]; then
  ENTRYPOINT="$REPO_ROOT/bin/mini-ork"
  if [ ! -x "$ENTRYPOINT" ]; then
    pass=false
    reasons+=("public CLI launcher is missing or not executable: $ENTRYPOINT")
  elif ! python3 - "$ENTRYPOINT" >>"$EVIDENCE" 2>&1 <<'PY'
import ast
import sys

path = sys.argv[1]
source = open(path, encoding="utf-8").read()
if not source.startswith("#!/usr/bin/env python3\n"):
    raise SystemExit("launcher does not have the Python shebang")
for forbidden in ("MINI_ORK_RUNTIME", "runtime-select", "BASH_SOURCE", "source "):
    if forbidden in source:
        raise SystemExit(f"launcher retains Bash delegation token: {forbidden}")
tree = ast.parse(source, filename=path)
imports_cli = any(
    isinstance(node, ast.ImportFrom)
    and node.module == "mini_ork.cli.main"
    and any(alias.name == "main" for alias in node.names)
    for node in ast.walk(tree)
)
if not imports_cli:
    raise SystemExit("launcher does not import mini_ork.cli.main.main")
print("CLI launcher is executable, Python-only, and delegates to the native dispatcher")
PY
  then
    pass=false
    reasons+=("public CLI launcher is not Python-only — see verifier-fork-closure.log")
  fi

  if (
    cd "$REPO_ROOT"
    rg -n --regexp "_bin\\([^)]*['\"]cli['\"]|mini-ork-cli" mini_ork tests scripts bin lib gates
  ) >>"$EVIDENCE" 2>&1; then
    pass=false
    reasons+=("suffixed or dynamic CLI runtime references remain — see verifier-fork-closure.log")
  fi
else
  ENTRYPOINT="$REPO_ROOT/bin/mini-ork-$FORK"
  NEEDLE="bin/mini-ork-$FORK"
  DYNAMIC_PATTERN="_bin\\([^)]*['\"]${FORK}['\"]|['\"]bin['\"][[:space:]]*/[[:space:]]*['\"]mini-ork-${FORK}['\"]"
  if [ -e "$ENTRYPOINT" ]; then
    pass=false
    reasons+=("legacy entrypoint still exists: $ENTRYPOINT")
  fi
  if [ "${#search_roots[@]}" -gt 0 ] && (
    cd "$REPO_ROOT"
    rg -n --fixed-strings --hidden --glob '!.git/**' -- "$NEEDLE" "${search_roots[@]}"
  ) >"$EVIDENCE" 2>&1; then
    pass=false
    reasons+=("runtime references to $NEEDLE remain — see verifier-fork-closure.log")
  fi
  if [ -d "$REPO_ROOT/mini_ork" ] && (
    cd "$REPO_ROOT"
    rg -n --regexp "$DYNAMIC_PATTERN" mini_ork tests scripts bin lib
  ) >>"$EVIDENCE" 2>&1; then
    pass=false
    reasons+=("dynamic runtime references for '$FORK' remain — see verifier-fork-closure.log")
  fi
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
