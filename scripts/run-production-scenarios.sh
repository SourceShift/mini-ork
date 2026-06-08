#!/usr/bin/env bash
# Run production-style mini-ork scenarios through the real markdown entrypoint.
#
# Default mode is dry-run. Set MO_PROD_SCENARIO_MODE=live to allow real LLM
# dispatch. This script intentionally calls `bin/mini-ork run`; it is not a
# unit-test harness around internal functions.
set -uo pipefail

ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MODE="${MO_PROD_SCENARIO_MODE:-dry-run}"
MD_ONLY=0
FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --md-only) MD_ONLY=1; shift ;;
    *) FILTER="$1"; shift ;;
  esac
done

case "$MODE" in
  dry-run) DRY_RUN=1 ;;
  live) DRY_RUN=0 ;;
  *) echo "MO_PROD_SCENARIO_MODE must be dry-run or live" >&2; exit 2 ;;
esac

SCENARIO_DIR="$ROOT/docs/production-validation/kickoffs"

scenario_rows() {
  cat <<EOF
code-fix|$SCENARIO_DIR/code-fix-real-bug.md
docs|$SCENARIO_DIR/docs-real-edit.md
bdd-first-delivery|$SCENARIO_DIR/bdd-settings-page.md
refactor-audit|$SCENARIO_DIR/refactor-audit-provider-roster.md
research-synthesis|$SCENARIO_DIR/research-synthesis-heterogeneous-review.md
blog-post|$SCENARIO_DIR/blog-post-launch.md
db-migration|$SCENARIO_DIR/db-migration-user-profile.md
ops-runbook|$SCENARIO_DIR/ops-runbook-symlink-hang.md
ui-audit|$SCENARIO_DIR/ui-audit-readme-cli.md
EOF
}

expected_task_class() {
  local recipe="$1"
  local yaml="$ROOT/recipes/$recipe/task_class.yaml"
  if [ -f "$yaml" ]; then
    python3 - "$yaml" "$recipe" <<'PY'
import sys, yaml
path, recipe = sys.argv[1:3]
try:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    print((data.get("name") or recipe.replace("-", "_")).strip())
except Exception:
    print(recipe.replace("-", "_"))
PY
  else
    printf '%s\n' "${recipe//-/_}"
  fi
}

PASS=0
FAIL=0
SKIP=0

echo "mini-ork production scenarios"
echo "root: $ROOT"
echo "mode: $MODE"
[ "$MD_ONLY" -eq 1 ] && echo "entrypoint: mini-ork run <kickoff.md>"
[ -n "$FILTER" ] && echo "filter: $FILTER"
echo ""

while IFS='|' read -r recipe kickoff; do
  [ -n "$recipe" ] || continue
  if [ -n "$FILTER" ] && [ "$FILTER" != "$recipe" ]; then
    SKIP=$((SKIP + 1))
    continue
  fi

  echo "==> $recipe"
  if [ ! -f "$kickoff" ]; then
    echo "  [FAIL] kickoff missing: $kickoff"
    FAIL=$((FAIL + 1))
    continue
  fi

  tmp_project=$(mktemp -d /tmp/mini-ork-prod-scenario-XXXXXX)
  (
    set -euo pipefail
    cd "$tmp_project"
    git init -q
    export MINI_ORK_HOME="$tmp_project/.mini-ork"
    export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
    export MINI_ORK_DRY_RUN="$DRY_RUN"
    export MINI_ORK_NO_COLOR=1
    "$ROOT/bin/mini-ork" init >/dev/null
    if [ "$MD_ONLY" -eq 1 ]; then
      "$ROOT/bin/mini-ork" run "$kickoff"
    else
      "$ROOT/bin/mini-ork" run "$recipe" "$kickoff"
    fi
  ) >"$tmp_project/output.log" 2>&1
  rc=$?

  expected_class=$(expected_task_class "$recipe")
  actual_class=$(grep -E '^task_class=' "$tmp_project/output.log" | tail -1 | cut -d= -f2- || true)

  if [ "$rc" -eq 0 ] && grep -q '"verdict"' "$tmp_project/output.log" && [ "$actual_class" = "$expected_class" ]; then
    echo "  [OK] rc=0 and verify verdict emitted"
    echo "  task_class: $actual_class"
    run_dir=$(grep -E '^plan_path=' "$tmp_project/output.log" | tail -1 | cut -d= -f2- | xargs dirname 2>/dev/null || true)
    [ -n "$run_dir" ] && echo "  run_dir: $run_dir"
    PASS=$((PASS + 1))
  else
    echo "  [FAIL] rc=$rc, task_class=$actual_class expected=$expected_class, or missing verify verdict"
    sed -n '1,80p' "$tmp_project/output.log" | sed 's/^/    /'
    FAIL=$((FAIL + 1))
  fi

  if [ "$MODE" = "dry-run" ]; then
    rm -rf "$tmp_project"
  else
    echo "  retained: $tmp_project"
  fi
  echo ""
done < <(scenario_rows)

echo "Results: $PASS OK  $SKIP SKIP  $FAIL FAIL"
[ "$FAIL" -eq 0 ] || exit 1
