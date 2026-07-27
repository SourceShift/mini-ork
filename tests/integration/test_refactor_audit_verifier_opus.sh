#!/usr/bin/env bash
# Regression: refactor-audit verifier must match the durable
# glm/kimi/codex/opus lens roster.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT

TMPROOT="$(mktemp -d /tmp/mini-ork-refactor-verifier-XXXXXX)"
trap 'rm -rf "$TMPROOT"' EXIT
export MINI_ORK_RUN_DIR="$TMPROOT/run"
mkdir -p "$MINI_ORK_RUN_DIR"

PASS=0
FAIL=0
_ok() { echo "  [OK]   $*"; PASS=$((PASS + 1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }

write_lens() {
  local lens="$1"
  local file="$MINI_ORK_RUN_DIR/lens-${lens}.md"
  local i
  for i in $(seq 1 12); do
    printf 'src/%s-file-%02d.ts:%d: finding from %s lens\n' "$lens" "$i" "$i" "$lens" >> "$file"
  done
}

for lens in glm kimi codex opus; do
  write_lens "$lens"
done

cat > "$MINI_ORK_RUN_DIR/synthesis.md" <<'MD'
# Refactor synthesis

The synthesis cross-references lens-glm, lens-kimi, lens-codex, and
lens-opus findings before ranking the final recommendations.
MD

OUT="$(python3 "$MINI_ORK_ROOT/recipes/refactor-audit/verifiers/lens-completeness.py" 2>&1)"
RC=$?
if [ "$RC" -eq 0 ]; then
  _ok "lens-completeness exits 0"
else
  _fail "lens-completeness exited $RC"
fi

VERDICT="$(printf '%s' "$OUT" | jq -r '.pass // false' 2>/dev/null || echo false)"
if [ "$VERDICT" = "true" ]; then
  _ok "lens-completeness accepts glm/kimi/codex/opus roster"
else
  _fail "lens-completeness rejected current roster: $OUT"
fi

if printf '%s' "$OUT" | grep -q 'lens-minimax'; then
  _fail "verifier output still references stale lens-minimax"
else
  _ok "verifier output does not reference stale lens-minimax"
fi

echo
echo "-- Results: ${PASS} OK  ${FAIL} FAIL --"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
