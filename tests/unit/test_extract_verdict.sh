#!/usr/bin/env bash
# tests/unit/test_extract_verdict.sh — tolerant reviewer-verdict extraction.
# Regression for run-1781105320-64712: reviewer emitted preamble prose before
# its verdict JSON → strict json.load gave verdict=unknown → verifier failed
# review_verdict → rollback fired → passing run marked failed.
# Usage: bash tests/unit/test_extract_verdict.sh
set -uo pipefail

MINI_ORK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export MINI_ORK_ROOT
EXTRACT="$MINI_ORK_ROOT/lib/extract_verdict.py"

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

_assert_verdict() {
  local label="$1" expected="$2" content="$3"
  local f="$TEST_DIR/review.json"
  printf '%s' "$content" > "$f"
  local got
  got=$(python3 "$EXTRACT" "$f")
  if [ "$got" = "$expected" ]; then
    _ok "$label (verdict=$got)"
  else
    _fail "$label: expected '$expected' got '$got'"
  fi
}

echo "== extract_verdict.py =="

_assert_verdict "pure JSON" "pass" \
  '{"verdict": "pass", "notes": []}'

# The exact shape that killed run-1781105320-64712
_assert_verdict "preamble prose before JSON" "pass" \
'Artifact read and verified: 5 lines, markdown header present. Contract met.

{"verdict": "pass", "notes": ["lens-tiny.md exists", "shape ok"]}'

_assert_verdict "markdown-fenced JSON" "fail" \
'```json
{"verdict": "fail", "notes": ["missing section"]}
```'

# Prompt schema echoed first; the real answer follows — LAST object wins
_assert_verdict "schema echo then answer" "needs_revision" \
'The requested format was {"verdict": "pass|fail|needs_revision", "notes": []}
{"verdict": "needs_revision", "notes": ["section 2 thin"]}'

_assert_verdict "trailing chat after JSON" "pass" \
'{"verdict": "pass", "notes": []}

Let me know if you need anything else!'

_assert_verdict "no JSON at all" "unknown" \
'I reviewed the artifact and it looks good.'

_assert_verdict "JSON without verdict key" "unknown" \
'{"notes": ["looks fine"], "score": 9}'

_assert_verdict "empty file" "unknown" ''

_assert_verdict "nested braces in notes" "pass" \
'{"verdict": "pass", "notes": ["object literal {a: {b: 1}} handled"]}'

echo "== lens-exists.py verifier tolerance =="

RUN_DIR="$TEST_DIR/run"
mkdir -p "$RUN_DIR"
printf '# Tiny Lens\n\n- point one\n- point two\n- point three\n' > "$RUN_DIR/lens-tiny.md"
printf 'Preamble chatter.\n\n{"verdict": "pass", "notes": []}\n' > "$RUN_DIR/review-tiny_reviewer.json"

if MINI_ORK_RUN_DIR="$RUN_DIR" MINI_ORK_DB="" \
   python3 "$MINI_ORK_ROOT/recipes/obs-smoke/verifiers/lens-exists.py" > "$TEST_DIR/verifier.out" 2>&1; then
  if grep -q '"review_verdict": true' "$TEST_DIR/verifier.out" \
     && grep -q '"review_json_strict": false' "$TEST_DIR/verifier.out"; then
    _ok "verifier passes preamble review + records strict-parse miss"
  else
    _fail "verifier passed but checks missing: $(cat "$TEST_DIR/verifier.out")"
  fi
else
  _fail "verifier rejected preamble review: $(cat "$TEST_DIR/verifier.out")"
fi

# Garbage review must still fail
printf 'no json here at all\n' > "$RUN_DIR/review-tiny_reviewer.json"
if MINI_ORK_RUN_DIR="$RUN_DIR" MINI_ORK_DB="" \
   python3 "$MINI_ORK_ROOT/recipes/obs-smoke/verifiers/lens-exists.py" > "$TEST_DIR/verifier2.out" 2>&1; then
  _fail "verifier accepted verdict-less review"
else
  _ok "verifier still fails verdict-less review"
fi

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
