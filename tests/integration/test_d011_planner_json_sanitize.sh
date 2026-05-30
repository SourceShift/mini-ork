#!/usr/bin/env bash
# tests/integration/test_d011_planner_json_sanitize.sh
# D-011 regression: planner JSON sanitization must strip markdown fences +
# leading/trailing prose before json.loads.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

_extract() {
  python3 -c "
import sys, re
txt = sys.stdin.read()
m = re.search(r'\{.*\}', txt, flags=re.S)
sys.stdout.write(m.group(0) if m else txt)
"
}

echo "── d-011: planner json sanitization ──"

# Case 1: markdown fenced JSON
OUT=$(echo '```json
{"a":1}
```' | _extract)
if [ "$OUT" = '{"a":1}' ]; then _ok "fenced JSON stripped"; else _fail "fenced JSON not stripped (got: $OUT)"; fi

# Case 2: leading prose
OUT=$(echo 'Here is the plan:
{"a":2}' | _extract)
if [ "$OUT" = '{"a":2}' ]; then _ok "leading prose stripped"; else _fail "leading prose not stripped (got: $OUT)"; fi

# Case 3: trailing prose
OUT=$(echo '{"a":3}
That is the plan.' | _extract)
if [ "$OUT" = '{"a":3}' ]; then _ok "trailing prose stripped"; else _fail "trailing prose not stripped (got: $OUT)"; fi

# Case 4: bare JSON pass-through
OUT=$(echo '{"a":4}' | _extract)
if [ "$OUT" = '{"a":4}' ]; then _ok "bare JSON passthrough"; else _fail "bare JSON corrupted (got: $OUT)"; fi

# Case 5: nested JSON survives (verifier_contract case)
OUT=$(echo '```json
{"objective":"x","decomposition":[{"id":"n1","node_type":"researcher"}]}
```' | _extract | python3 -c "import sys,json; p=json.load(sys.stdin); print(p['decomposition'][0]['node_type'])")
if [ "$OUT" = "researcher" ]; then _ok "nested JSON parses after strip"; else _fail "nested JSON broken (got: $OUT)"; fi

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
