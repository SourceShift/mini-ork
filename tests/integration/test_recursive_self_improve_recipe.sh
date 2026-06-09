#!/usr/bin/env bash
# Integration smoke for the recursive-self-improve recipe.
# Covers: recipe scaffolding, workflow lane routing, verifier behavior,
# agents override, migration apply, outer runner dry-run.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
export PATH="$MINI_ORK_ROOT/bin:$PATH"
export MINI_ORK_DRY_RUN=1

TMPROOT="$(mktemp -d /tmp/mini-ork-self-improve-test-XXXXXX)"
trap 'rm -rf "$TMPROOT"' EXIT
cd "$TMPROOT" || exit 1
git init -q
git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init

export MINI_ORK_HOME="$TMPROOT/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"

PASS=0
FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS + 1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }
_assert() {
  local label="$1"; shift
  if "$@"; then _ok "$label"; else _fail "$label"; fi
}

mini-ork init >/dev/null 2>&1 || true

RECIPE_DIR="$MINI_ORK_ROOT/recipes/recursive-self-improve"
WORKFLOW="$RECIPE_DIR/workflow.yaml"
AGENTS_OVR="$MINI_ORK_ROOT/config/agents.recursive-self-improve.yaml"
MIGRATION="$MINI_ORK_ROOT/db/migrations/0017_self_improve_learning.sql"

echo "── recursive-self-improve scaffolding ──"
_assert "task_class.yaml exists"            test -f "$RECIPE_DIR/task_class.yaml"
_assert "workflow.yaml exists"              test -f "$WORKFLOW"
_assert "artifact_contract.yaml exists"     test -f "$RECIPE_DIR/artifact_contract.yaml"
_assert "agents override exists"            test -f "$AGENTS_OVR"
_assert "migration exists"                  test -f "$MIGRATION"
_assert "bottleneck-scan prompt exists"     test -f "$RECIPE_DIR/prompts/bottleneck-scan.md"
_assert "perf-lens prompt exists"           test -f "$RECIPE_DIR/prompts/lens-minimax-perf.md"
_assert "correctness-lens prompt exists"    test -f "$RECIPE_DIR/prompts/lens-kimi-correctness.md"
_assert "arch-lens prompt exists"           test -f "$RECIPE_DIR/prompts/lens-codex-arch.md"
_assert "arxiv-researcher prompt exists"    test -f "$RECIPE_DIR/prompts/arxiv-researcher.md"
_assert "opus-synthesis prompt exists"      test -f "$RECIPE_DIR/prompts/opus-synthesis.md"
_assert "implementer prompt exists"         test -f "$RECIPE_DIR/prompts/implementer.md"
_assert "bottlenecks-found verifier exec"   test -x "$RECIPE_DIR/verifiers/bottlenecks-found.sh"
_assert "self-tests-pass verifier exec"     test -x "$RECIPE_DIR/verifiers/self-tests-pass.sh"
_assert "no-regression verifier exec"       test -x "$RECIPE_DIR/verifiers/no-regression.sh"
_assert "outer runner exec"                 test -x "$MINI_ORK_ROOT/bin/mini-ork-self-improve"

echo
echo "── workflow lane routing ──"
python3 - "$WORKFLOW" <<'PY'
import sys, yaml
wf = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
nodes = wf.get("nodes") or []
by_name = {n["name"]: n for n in nodes}

expected = {
    "bottleneck_lens":    ("researcher",   "planner"),
    "perf_lens":          ("researcher",   "minimax_lens"),
    "correctness_lens":   ("researcher",   "kimi_lens"),
    "arch_lens":          ("researcher",   "codex_lens"),
    "arxiv_lens":         ("researcher",   "codex_lens"),
    "opus_synthesizer":   ("reviewer",     "opus_lens"),
    "implementer":        ("implementer",  "codex_lens"),
}
ok = True
for name, (typ, lane) in expected.items():
    n = by_name.get(name)
    if not n:
        print(f"MISSING_NODE: {name}"); ok = False; continue
    if n.get("type") != typ:
        print(f"BAD_TYPE: {name} expected={typ} got={n.get('type')}"); ok = False
    if n.get("model_lane") != lane:
        print(f"BAD_LANE: {name} expected={lane} got={n.get('model_lane')}"); ok = False

# Three deterministic verifiers must be present
expected_verifiers = {"bottlenecks_found", "self_tests_pass", "no_regression"}
got_verifiers = {n["name"] for n in nodes if n.get("type") == "verifier"}
if not expected_verifiers.issubset(got_verifiers):
    print(f"MISSING_VERIFIERS: {expected_verifiers - got_verifiers}"); ok = False

sys.exit(0 if ok else 1)
PY
if [ $? -eq 0 ]; then _ok "lane routing + verifier roster"; else _fail "lane routing"; fi

echo
echo "── agents override ──"
python3 - "$AGENTS_OVR" <<'PY'
import sys, yaml
y = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
lanes = y.get("lanes", {})
expect = {"minimax_lens": "minimax", "kimi_lens": "kimi",
          "codex_lens": "codex", "opus_lens": "opus", "reviewer": "opus"}
bad = [(k,v,lanes.get(k)) for k,v in expect.items() if lanes.get(k) != v]
if bad:
    for k,want,got in bad: print(f"BAD_LANE: {k} want={want} got={got}")
    sys.exit(1)
sys.exit(0)
PY
if [ $? -eq 0 ]; then _ok "agents override binds opus + minimax/kimi/codex"; else _fail "agents override"; fi

echo
echo "── migration apply (idempotent) ──"
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$MINI_ORK_DB" < "$MIGRATION" >/dev/null 2>&1 && \
    sqlite3 "$MINI_ORK_DB" < "$MIGRATION" >/dev/null 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then _ok "migration applied twice without error"; else _fail "migration"; fi
  # All three tables present
  tables=$(sqlite3 "$MINI_ORK_DB" \
    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('self_improve_runs','learning_record','self_improve_arxiv_refs') ORDER BY name;")
  expected=$'learning_record\nself_improve_arxiv_refs\nself_improve_runs'
  if [ "$tables" = "$expected" ]; then _ok "all 3 tables created"; else _fail "tables missing: got=$tables"; fi
else
  _fail "sqlite3 missing; cannot verify migration"
fi

echo
echo "── verifier behavior ──"
mkdir -p "$TMPROOT/run"
export MINI_ORK_RUN_DIR="$TMPROOT/run"

# Regression for the staging-filter bug (iter-1 of run #4 leaked
# lens-*.md files into commit 5f2d96b at worktree root):
# runner must filter workflow-internal artifacts out of the iter
# commit via explicit `git add` pathspec exclusions.
RUNNER="$MINI_ORK_ROOT/bin/mini-ork-self-improve"
if grep -qE "':!lens-\*\.md'" "$RUNNER" \
   && grep -qE "':!synthesis\.md\*'" "$RUNNER" \
   && grep -qE "':!context-\*\.json'" "$RUNNER"; then
  _ok "runner staging filter excludes lens-*.md + synthesis.md + context-*.json"
else
  _fail "runner staging filter missing workflow-artifact exclusions"
fi
if grep -q '_self_improve_record_success' "$RUNNER"; then
  _ok "runner records success row in learning_record on successful iter"
else
  _fail "runner missing learning_record success bookkeeping"
fi

# Regression for iter-1 file-name mismatch: verifier must look for
# lens-bottleneck.md + lens-arxiv.md (matches dispatcher's _lens
# naming heuristic), not the old bottleneck-scan.md / arxiv-refs.md
# names. Inline assertion against the verifier source.
if grep -q 'lens-bottleneck.md' "$RECIPE_DIR/verifiers/bottlenecks-found.sh" \
   && grep -q 'lens-arxiv.md' "$RECIPE_DIR/verifiers/bottlenecks-found.sh"; then
  _ok "bottlenecks-found verifier references lens-bottleneck.md + lens-arxiv.md"
else
  _fail "bottlenecks-found verifier missing lens-*.md file names — will fail vs dispatcher output"
fi

# Empty run dir → bottlenecks-found should fail
out=$(bash "$RECIPE_DIR/verifiers/bottlenecks-found.sh" 2>/dev/null)
if echo "$out" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d['pass'] is False else 1)"; then
  _ok "bottlenecks-found fails on empty run dir"
else
  _fail "bottlenecks-found should fail on empty run dir"
fi

# Polluted synthesis → still fails
cat > "$MINI_ORK_RUN_DIR/lens-bottleneck.md" <<'MD'
# Scan
| 1 | perf | x | high | a:1 | minimax_lens |
MD
cat > "$MINI_ORK_RUN_DIR/lens-arxiv.md" <<'MD'
# Arxiv refs
MD
cat > "$MINI_ORK_RUN_DIR/synthesis.md" <<'MD'
# Synthesis
★ Insight ─── polluted
| 1 | foo | perf | bar | x | 0.9 |
MD
out=$(bash "$RECIPE_DIR/verifiers/bottlenecks-found.sh" 2>/dev/null)
if echo "$out" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d['pass'] is False else 1)"; then
  _ok "bottlenecks-found rejects ★ Insight envelope leak"
else
  _fail "bottlenecks-found should reject polluted synthesis"
fi

# Clean synthesis → passes
cat > "$MINI_ORK_RUN_DIR/synthesis.md" <<'MD'
# Synthesis — iter 1

## Ranked patch plan

| 1 | perf-bottle | perf | drop redundant LLM call | trace=x | 0.85 |
MD
out=$(bash "$RECIPE_DIR/verifiers/bottlenecks-found.sh" 2>/dev/null)
if echo "$out" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d['pass'] is True else 1)"; then
  _ok "bottlenecks-found accepts clean synthesis with 1 ranked patch"
else
  _fail "bottlenecks-found should accept clean synthesis"
fi

# Converged shortcut
cat > "$MINI_ORK_RUN_DIR/lens-bottleneck.md" <<'MD'
# Scan
## Status: converged
MD
out=$(bash "$RECIPE_DIR/verifiers/bottlenecks-found.sh" 2>/dev/null)
if echo "$out" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if (d['pass'] and d['converged']) else 1)"; then
  _ok "bottlenecks-found short-circuits on convergence"
else
  _fail "convergence shortcut"
fi

# self-tests-pass against an empty worktree → fail (vacuous)
mkdir -p "$TMPROOT/wt-empty"
git -C "$TMPROOT/wt-empty" init -q
export MINI_ORK_SELF_IMPROVE_WORKTREE="$TMPROOT/wt-empty"
out=$(bash "$RECIPE_DIR/verifiers/self-tests-pass.sh" 2>/dev/null)
if echo "$out" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d['pass'] is False else 1)"; then
  _ok "self-tests-pass refuses vacuous (no test suites)"
else
  _fail "self-tests-pass should refuse vacuous"
fi
unset MINI_ORK_SELF_IMPROVE_WORKTREE

echo
echo "── outer runner dispatches via mini-ork run (not mini-ork-execute --recipe) ──"
# Regression for iter-1 bug: mini-ork-execute has no --recipe / --kickoff flags.
# The runner must call `mini-ork run recursive-self-improve <kickoff.md>`.
if grep -q 'run recursive-self-improve' "$MINI_ORK_ROOT/bin/mini-ork-self-improve"; then
  _ok "runner dispatches via 'mini-ork run recursive-self-improve' (lifecycle walks classify→plan→execute→verify)"
else
  _fail "runner missing 'mini-ork run recursive-self-improve' invocation"
fi
if grep -q -- '--recipe recursive-self-improve' "$MINI_ORK_ROOT/bin/mini-ork-self-improve"; then
  _fail "runner still uses 'mini-ork-execute --recipe' — will hit 'Unknown flag' rc=2"
else
  _ok "runner does not pass --recipe to mini-ork-execute (which has no such flag)"
fi

echo
echo "── outer runner --dry-run ──"
out=$("$MINI_ORK_ROOT/bin/mini-ork-self-improve" --dry-run --max-iters 1 --soft-cap-hours 1 --hard-cap-hours 1 2>&1 || true)
if echo "$out" | grep -q "dry-run"; then
  _ok "outer runner --dry-run smoke ran"
else
  _fail "outer runner --dry-run smoke"
  echo "$out" | head -30
fi

echo
echo "── invalid caps rejected ──"
out=$("$MINI_ORK_ROOT/bin/mini-ork-self-improve" --soft-cap-hours 5 --hard-cap-hours 3 --dry-run 2>&1 || true)
if echo "$out" | grep -q "invalid cap hours"; then
  _ok "outer runner rejects soft > hard"
else
  _fail "outer runner accepted soft > hard"
fi

echo
echo "Recursive-self-improve smoke: $PASS OK / $FAIL FAIL"
[ "$FAIL" -eq 0 ]
