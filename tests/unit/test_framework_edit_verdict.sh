#!/usr/bin/env bash
# tests/unit/test_framework_edit_verdict.sh — verifier verdict.json contract.
# Direct unit coverage for the fix in recipes/framework-edit/verifiers/:
#   * static-check.py and test.py must WRITE $RUN_DIR/verdict.json (not assert
#     its pre-existence as a self-defeating gating check).
#   * Either verifier may run first; the shared helper must merge keys
#     tolerantly and produce the schema declared in artifact_contract.yaml:
#       { files_changed:int, tests_pass:bool, static_pass:bool, pass:bool }
#   * pass = static_pass && tests_pass (defensive: missing keys -> false).
#
# Positive case: clean diff (new README.md) -> verdict.json exists with all
#   four keys and pass:true after both verifiers run.
# Negative case: diff adds a .sh containing broken bash (`if [`) -> static
#   check fails; verdict.json has static_pass:false and pass:false.
set -uo pipefail

MINI_ORK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export MINI_ORK_ROOT

STATIC_CHECK="$MINI_ORK_ROOT/recipes/framework-edit/verifiers/static-check.py"
TEST_V="$MINI_ORK_ROOT/recipes/framework-edit/verifiers/test.py"
HELPER="$MINI_ORK_ROOT/recipes/framework-edit/verifiers/_verdict_merge.py"

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

# Parse a JSON file's key via python; asserts $2 == $3.
_assert_field() {
  local f="$1" key="$2" expected="$3"
  local got
  got="$(python3 -c "import json,sys; v=json.load(open(sys.argv[1])); print(v.get(sys.argv[2], '<MISSING>'))" "$f" "$key" 2>/dev/null)"
  if [ "$got" = "$expected" ]; then
    _ok "$key == $expected"
  else
    _fail "$key: expected '$expected' got '$got'"
  fi
}

# Assert a JSON file's key matches a python expression on its value
# (e.g. "isinstance(v['files_changed'], int) and v['files_changed']==1").
_assert_expr() {
  local f="$1" expr="$2" desc="$3"
  if python3 -c "import json,sys; v=json.load(open(sys.argv[1])); assert $expr" "$f" 2>/dev/null; then
    _ok "$desc"
  else
    _fail "$desc"
  fi
}

# Build a scratch git repo with one tracked file under $1. The verifier
# scripts call `git archive HEAD` on the repo root, so we need at least
# one commit and at least one tracked file.
_make_scratch_repo() {
  local repo="$1"
  mkdir -p "$repo"
  git -C "$repo" init -q -b main
  git -C "$repo" config user.email "test@local"
  git -C "$repo" config user.name "test"
  printf '# placeholder\n' > "$repo/README.md"
  git -C "$repo" add README.md
  git -C "$repo" commit -q -m "init"
}

# Build a unified diff for adding $2 with content $3 inside $1, writing
# it to $4. Caller must have already committed a baseline file so the
# diff has a real "--- a/" anchor.
_build_add_diff() {
  local repo="$1" target_rel="$2" content="$3" out="$4"
  local target_abs="$repo/$target_rel"
  mkdir -p "$(dirname "$target_abs")"
  printf '%s\n' "$content" > "$target_abs"
  git -C "$repo" add -N "$target_rel"  # intent-to-add so diff is unidirectional
  git -C "$repo" diff -- "$target_rel" > "$out"
  git -C "$repo" reset -q -- "$target_rel"
  rm -f "$target_abs"
}

# Run a verifier against a synthetic mini-ork run dir backed by $repo.
_run_verifier() {
  local run_dir="$1" repo="$2" verifier="$3"
  (
    cd "$repo"
    MINI_ORK_RUN_DIR="$run_dir" \
    MINI_ORK_ROOT="$repo" \
      python3 "$verifier" > "$run_dir/verifier.stdout" 2>&1
  )
}

echo "== helper sanity =="

# 1) Helper exposes write_verdict
if grep -qE '^def write_verdict' "$HELPER"; then
  _ok "_verdict_merge.py defines write_verdict()"
else
  _fail "_verdict_merge.py missing write_verdict() definition"
fi

# 2) Static-check imports the helper
if grep -q '_verdict_merge' "$STATIC_CHECK"; then
  _ok "static-check.py imports _verdict_merge"
else
  _fail "static-check.py does not import _verdict_merge"
fi

# 3) Test verifier imports the helper
if grep -q '_verdict_merge' "$TEST_V"; then
  _ok "test.py imports _verdict_merge"
else
  _fail "test.py does not import _verdict_merge"
fi

# 4) Neither verifier asserts verdict.json pre-existence anymore
if grep -q 'artifact-verdict-json-exists' "$STATIC_CHECK" "$TEST_V"; then
  _fail "self-defeating existence check still present"
else
  _ok "self-defeating existence check removed"
fi

# 5) Both call write_verdict
if grep -q 'write_verdict' "$STATIC_CHECK" "$TEST_V"; then
  _ok "both verifiers invoke write_verdict"
else
  _fail "at least one verifier never calls write_verdict"
fi

# 6) py_compile on the three ported files + bash -n on this test
if python3 -m py_compile "$HELPER" "$STATIC_CHECK" "$TEST_V" \
   && bash -n "${BASH_SOURCE[0]}" 2>/dev/null; then
  _ok "py_compile clean on helper + static-check + test; bash -n clean on this test"
else
  _fail "py_compile/bash -n reported a syntax error"
fi

echo
echo "== positive: clean diff -> verdict.json pass:true =="

POS_DIR="$(mktemp -d -t fwedit-pos-XXXXXX)"
POS_RUN="$POS_DIR/run"
POS_REPO="$POS_DIR/repo"
mkdir -p "$POS_RUN"
_make_scratch_repo "$POS_REPO"
_build_add_diff "$POS_REPO" "NEW_README.md" "hello from mini-ork verdict test" "$POS_RUN/framework-edit.diff"

# Sanity: diff has the expected anchors.
if grep -q '^diff --git' "$POS_RUN/framework-edit.diff" \
   && grep -q '^+++ b/NEW_README.md' "$POS_RUN/framework-edit.diff"; then
  _ok "scratch diff well-formed"
else
  _fail "scratch diff malformed"
fi

# Run static-check FIRST (most common ordering).
_run_verifier "$POS_RUN" "$POS_REPO" "$STATIC_CHECK"

if [ -f "$POS_RUN/verdict.json" ]; then
  _ok "verdict.json exists after static-check"
else
  _fail "verdict.json missing after static-check"
fi

_assert_expr "$POS_RUN/verdict.json" \
  "set(v.keys())=={'files_changed','tests_pass','static_pass','pass'}" \
  "verdict.json has exactly the four required keys (after static-check only)"

_assert_field "$POS_RUN/verdict.json" "files_changed" "1"
_assert_field "$POS_RUN/verdict.json" "static_pass" "True"
_assert_field "$POS_RUN/verdict.json" "tests_pass" "False"
_assert_field "$POS_RUN/verdict.json" "pass" "False"

# Run test.sh SECOND.
_run_verifier "$POS_RUN" "$POS_REPO" "$TEST_V"

_assert_field "$POS_RUN/verdict.json" "files_changed" "1"
_assert_field "$POS_RUN/verdict.json" "static_pass" "True"
_assert_field "$POS_RUN/verdict.json" "tests_pass" "True"
_assert_field "$POS_RUN/verdict.json" "pass" "True"

# Schema strictness (DoD requires exact shape: int + 3 bools).
_assert_expr "$POS_RUN/verdict.json" \
  "isinstance(v['files_changed'], int) and isinstance(v['tests_pass'], bool) and isinstance(v['static_pass'], bool) and isinstance(v['pass'], bool)" \
  "verdict.json schema: files_changed:int, others:bool"

echo
echo "== positive: test-first ordering still yields pass:true =="

POS2_DIR="$(mktemp -d -t fwedit-pos2-XXXXXX)"
POS2_RUN="$POS2_DIR/run"
POS2_REPO="$POS2_DIR/repo"
mkdir -p "$POS2_RUN"
_make_scratch_repo "$POS2_REPO"
_build_add_diff "$POS2_REPO" "NEW_README.md" "second ordering smoke" "$POS2_RUN/framework-edit.diff"

_run_verifier "$POS2_RUN" "$POS2_REPO" "$TEST_V"
_assert_field "$POS2_RUN/verdict.json" "tests_pass" "True"
_assert_field "$POS2_RUN/verdict.json" "static_pass" "False"
_assert_field "$POS2_RUN/verdict.json" "pass" "False"

_run_verifier "$POS2_RUN" "$POS2_REPO" "$STATIC_CHECK"
_assert_field "$POS2_RUN/verdict.json" "files_changed" "1"
_assert_field "$POS2_RUN/verdict.json" "static_pass" "True"
_assert_field "$POS2_RUN/verdict.json" "tests_pass" "True"
_assert_field "$POS2_RUN/verdict.json" "pass" "True"

echo
echo "== negative: broken-bash diff -> static_pass:false, pass:false =="

NEG_DIR="$(mktemp -d -t fwedit-neg-XXXXXX)"
NEG_RUN="$NEG_DIR/run"
NEG_REPO="$NEG_DIR/repo"
mkdir -p "$NEG_RUN"
_make_scratch_repo "$NEG_REPO"
# `if [` is unfinished bash — bash -n will reject it.
_build_add_diff "$NEG_REPO" "broken.sh" "if [" "$NEG_RUN/framework-edit.diff"

_run_verifier "$NEG_RUN" "$NEG_REPO" "$STATIC_CHECK"

if [ -f "$NEG_RUN/verdict.json" ]; then
  _ok "verdict.json still produced even when static check fails"
else
  _fail "verdict.json missing after failing static check"
fi

_assert_field "$NEG_RUN/verdict.json" "static_pass" "False"
_assert_field "$NEG_RUN/verdict.json" "pass" "False"

echo
echo "== contract: implementer prompt untouched =="

if (cd "$MINI_ORK_ROOT" && git status --porcelain -- recipes/framework-edit/prompts/implementer.md) | grep -q .; then
  _fail "implementer.md has working-tree changes (forbidden: implementer must not own verdict.json)"
else
  _ok "implementer.md working-tree clean (verdict.json stays verifier-owned)"
fi

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]