#!/usr/bin/env bash
# tests/integration/test_d011_planner_json_sanitize.sh
# D-011/D-052 regression: planner JSON sanitization must strip markdown fences,
# leading/trailing prose, and prompt-template JSON before json.loads.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

_extract() {
  python3 -c "
import json, sys
txt = sys.stdin.read()

def objects(s):
    i = 0
    while True:
        start = s.find('{', i)
        if start < 0:
            return
        depth = 0
        in_str = False
        esc = False
        for j in range(start, len(s)):
            c = s[j]
            if in_str:
                if esc:
                    esc = False
                elif c == '\\\\':
                    esc = True
                elif c == '\"':
                    in_str = False
                continue
            if c == '\"':
                in_str = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    yield s[start:j+1]
                    i = j + 1
                    break
        else:
            return

def contains_placeholder(v):
    if isinstance(v, str):
        s = v.strip()
        return s.startswith('<') and s.endswith('>')
    if isinstance(v, list):
        return any(contains_placeholder(x) for x in v)
    if isinstance(v, dict):
        return any(contains_placeholder(x) for x in v.values())
    return False

def is_plan(obj):
    if not isinstance(obj, dict):
        return False
    if not isinstance(obj.get('verifier_contract'), dict):
        return False
    if not obj.get('verifier_contract', {}).get('checks'):
        return False
    if contains_placeholder(obj):
        return False
    return any(k in obj for k in ('objective', 'decomposition', 'artifact_contract'))

first = None
for chunk in objects(txt):
    if first is None:
        first = chunk
    try:
        parsed = json.loads(chunk)
    except Exception:
        continue
    if is_plan(parsed):
        sys.stdout.write(json.dumps(parsed, separators=(',', ':')))
        sys.exit(0)
sys.stdout.write(first if first is not None else txt)
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
{"objective":"x","decomposition":[{"id":"n1","node_type":"researcher"}],"artifact_contract":{"outputs":[],"success_verifiers":[]},"verifier_contract":{"checks":[{"id":"c1"}]}}
```' | _extract | python3 -c "import sys,json; p=json.load(sys.stdin); print(p['decomposition'][0]['node_type'])")
if [ "$OUT" = "researcher" ]; then _ok "nested JSON parses after strip"; else _fail "nested JSON broken (got: $OUT)"; fi

# Case 6: Codex transcript includes prompt-template JSON before real plan JSON.
OUT=$(cat <<'EOF' | _extract | python3 -c "import sys,json; p=json.load(sys.stdin); print(p['decomposition'][0]['target_file'])"
user
{"objective":"<one sentence>","decomposition":[{"node_type":"implementer","target_file":"<path/to/doc.md>"}],"artifact_contract":"ref provided","verifier_contract":{"checks":[{"kind":"grep","file":"<path/to/doc.md>","pattern":"<extended-regex>"}]}}
codex
{"objective":"Add operator note","decomposition":[{"node_type":"implementer","target_file":"docs/README.md"}],"artifact_contract":{"outputs":["docs/README.md"],"success_verifiers":[]},"verifier_contract":{"checks":[{"kind":"grep","file":"docs/README.md","pattern":"Operator note"}]}}
EOF
)
if [ "$OUT" = "docs/README.md" ]; then _ok "prompt-template JSON skipped"; else _fail "template JSON was selected (got: $OUT)"; fi

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
