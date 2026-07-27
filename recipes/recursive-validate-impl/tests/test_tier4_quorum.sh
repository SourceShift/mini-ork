#!/usr/bin/env bash
# tests/test_tier4_quorum.sh — unit test for tier4-panel-quorum.py verifier.
#
# Covers: 2-of-4 (fail), 4-of-4 (pass), default quorum, override quorum,
# size-threshold semantics. Self-contained; no network; runs in <2s.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERIFIER="$SCRIPT_DIR/../verifiers/tier4-panel-quorum.py"

if [ ! -f "$VERIFIER" ]; then
  echo "FAIL: verifier not found at $VERIFIER" >&2
  exit 1
fi

PASS=0
FAIL=0
_assert() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  [OK]   $label  (got: $actual)"
    PASS=$((PASS + 1))
  else
    echo "  [FAIL] $label  expected=$expected actual=$actual" >&2
    FAIL=$((FAIL + 1))
  fi
}

_mk_lens() {
  local dir="$1" lens="$2" size="${3:-500}"
  python3 -c "open('$dir/tier4-$lens.md','w').write('x' * $size)"
}

# Test 1: 2 of 4 present, default quorum=3 → fail
T1=$(mktemp -d)
_mk_lens "$T1" codex
_mk_lens "$T1" minimax
out=$(MINI_ORK_RUN_DIR="$T1" python3 "$VERIFIER")
_assert "T1.pass=false (2/4 < 3 quorum)"   "false" "$(echo "$out" | python3 -c 'import json,sys;print(json.load(sys.stdin)["pass"])' | tr 'TF' 'tf')"
_assert "T1.verdict=fail"                  "fail" "$(echo "$out" | python3 -c 'import json,sys;print(json.load(sys.stdin)["verdict"])')"
_assert "T1.missing_count=2"                "2"    "$(echo "$out" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["missing"]))')"
rm -rf "$T1"

# Test 2: 4 of 4 present, default quorum=3 → pass
T2=$(mktemp -d)
for lens in glm kimi codex minimax; do _mk_lens "$T2" "$lens"; done
out=$(MINI_ORK_RUN_DIR="$T2" python3 "$VERIFIER")
_assert "T2.pass=true (4/4 >= 3 quorum)"   "true" "$(echo "$out" | python3 -c 'import json,sys;print(json.load(sys.stdin)["pass"])' | tr 'TF' 'tf')"
_assert "T2.verdict=pass"                   "pass" "$(echo "$out" | python3 -c 'import json,sys;print(json.load(sys.stdin)["verdict"])')"
_assert "T2.missing_count=0"                "0"    "$(echo "$out" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["missing"]))')"
rm -rf "$T2"

# Test 3: 3 of 4 present, default quorum=3 → pass (exactly at threshold)
T3=$(mktemp -d)
for lens in glm codex minimax; do _mk_lens "$T3" "$lens"; done
out=$(MINI_ORK_RUN_DIR="$T3" python3 "$VERIFIER")
_assert "T3.pass=true (3/4 == 3 quorum)"   "true" "$(echo "$out" | python3 -c 'import json,sys;print(json.load(sys.stdin)["pass"])' | tr 'TF' 'tf')"
rm -rf "$T3"

# Test 4: override quorum=4 with only 3 present → fail
T4=$(mktemp -d)
for lens in glm codex minimax; do _mk_lens "$T4" "$lens"; done
out=$(MINI_ORK_RUN_DIR="$T4" MO_TIER4_QUORUM=4 python3 "$VERIFIER")
_assert "T4.pass=false (3/4 < 4 override quorum)" "false" "$(echo "$out" | python3 -c 'import json,sys;print(json.load(sys.stdin)["pass"])' | tr 'TF' 'tf')"
rm -rf "$T4"

# Test 5: tier4-glm.md exists but size=50 bytes (below MIN_SIZE) → counts as missing
T5=$(mktemp -d)
_mk_lens "$T5" glm 50
_mk_lens "$T5" kimi
_mk_lens "$T5" codex
_mk_lens "$T5" minimax
out=$(MINI_ORK_RUN_DIR="$T5" python3 "$VERIFIER")
_assert "T5.glm in missing (size below threshold)" "true" "$(echo "$out" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(str("glm" in d["missing"]).lower())')"
_assert "T5.quorum_met=3 (kimi+codex+minimax)" "3" "$(echo "$out" | python3 -c 'import json,sys;print(json.load(sys.stdin)["quorum_met"])')"
rm -rf "$T5"

echo
echo "tier4_quorum tests: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
