#!/usr/bin/env bash
# D-016 + D-017 regression: balanced-brace parser extracts FIRST top-level
# JSON object, ignoring trailing meta-blocks (z-insight, etc.).
# D-017: planner prompt instructs strict node_type enum compliance.
set -uo pipefail

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

_extract() {
  python3 -c "
import sys
txt = sys.stdin.read()
i = txt.find('{')
if i < 0:
    sys.stdout.write(txt); sys.exit(0)
depth, in_str, esc, end = 0, False, False, -1
for j in range(i, len(txt)):
    c = txt[j]
    if in_str:
        if esc: esc = False
        elif c == '\\\\': esc = True
        elif c == '\"': in_str = False
        continue
    if c == '\"': in_str = True
    elif c == '{': depth += 1
    elif c == '}':
        depth -= 1
        if depth == 0: end = j; break
sys.stdout.write(txt[i:end+1] if end > 0 else txt[i:])
"
}

echo "── d-016: balanced-brace extracts first JSON, ignores trailing z-insight ──"

OUT=$(printf '%s' '{"objective":"x"}
<z-insight>
{"domain":"other","foo":{"bar":1}}
</z-insight>' | _extract)
EXPECTED='{"objective":"x"}'
if [ "$OUT" = "$EXPECTED" ]; then
  _ok "extracted first JSON, ignored z-insight block"
else
  _fail "extracted: $OUT (expected: $EXPECTED)"
fi

# Case 2: nested braces inside the plan
OUT=$(printf '%s' '{"a":1,"nested":{"b":2,"c":{"d":3}}}<garbage>{}' | _extract)
EXPECTED='{"a":1,"nested":{"b":2,"c":{"d":3}}}'
if [ "$OUT" = "$EXPECTED" ]; then _ok "nested braces handled"; else _fail "nested: $OUT"; fi

# Case 3: string literal containing brace
OUT=$(printf '%s' '{"a":"value with } in string","b":2}{extra}' | _extract)
EXPECTED='{"a":"value with } in string","b":2}'
if [ "$OUT" = "$EXPECTED" ]; then _ok "string-literal braces ignored"; else _fail "strlit: $OUT"; fi

# Case 4: markdown fenced
OUT=$(printf '%s' '```json
{"a":1}
```
trailing' | _extract)
EXPECTED='{"a":1}'
if [ "$OUT" = "$EXPECTED" ]; then _ok "fenced JSON stripped"; else _fail "fenced: $OUT"; fi

echo ""
echo "── d-017: refactor-audit planner prompt instructs strict enum ──"
PROMPT_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/recipes/refactor-audit/prompts/planner.md"
if grep -q "DO NOT invent new node_type" "$PROMPT_FILE"; then
  _ok "planner prompt forbids inventing node_types"
else
  _fail "planner prompt missing 'DO NOT invent' guard (D-017 regression)"
fi
if grep -q "USE FOR ALL 4 LENSES" "$PROMPT_FILE"; then
  _ok "planner prompt maps lenses → researcher node_type"
else
  _fail "planner prompt missing lens-to-researcher mapping (D-017 regression)"
fi

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
