#!/usr/bin/env bash
# Regression: .githooks/reference-transaction must REJECT a ref update that moves
# HEAD / a branch (or creates a ref) to a commit FOREIGN to this repo — the exact
# cross-repo corruption a consuming repo's drifted lane caused (reset --hard to
# refs/codex/curated-sync = 3fdeeb4, an unrelated repo's commit). Legitimate
# history (commits, resets within the repo's own graph) must pass.
set -uo pipefail
ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
HOOK="$ROOT/.githooks/reference-transaction"
PASS=0; FAIL=0
ok(){ echo "  [OK]   $1"; PASS=$((PASS+1)); }
bad(){ echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
echo "── unit: reference-transaction foreign-ref guard ──"

# git must support the reference-transaction hook (>= 2.28)
if ! git --version | awk '{print $3}' | awk -F. '{exit !($1>2 || ($1==2 && $2>=28))}'; then
  echo "  [SKIP] git $(git --version | awk '{print $3}') has no reference-transaction hook"
  echo "── Results: 0 OK  0 FAIL ──"; exit 0
fi

R="$TMP/repo"; HK="$TMP/hooks"; mkdir -p "$R" "$HK"
cp "$HOOK" "$HK/reference-transaction"; chmod +x "$HK/reference-transaction"
(
  cd "$R"
  git init -q
  git config user.email t@t; git config user.name t
  git config core.hooksPath "$HK"
  echo a > f; git add f; git commit -q -m A
  echo b > f; git add f; git commit -q -m B
)
A=$(cd "$R"; git rev-parse HEAD~1)
B=$(cd "$R"; git rev-parse HEAD)
# a FOREIGN root commit (empty tree, no parent) — shares no history with A/B
EMPTY_TREE=$(cd "$R"; git hash-object -w -t tree /dev/null)
FOREIGN=$(cd "$R"; printf 'Fix DigitalOcean dark mode logo\n' | git commit-tree "$EMPTY_TREE")

# 1. creating refs/codex/* to a foreign commit is REJECTED
( cd "$R"; git update-ref refs/codex/curated-sync "$FOREIGN" ) 2>/dev/null \
  && bad "foreign refs/codex/* creation was allowed" \
  || ok "foreign refs/codex/* creation rejected"
[ -z "$(cd "$R"; git rev-parse --verify --quiet refs/codex/curated-sync)" ] \
  && ok "refs/codex/curated-sync not created" || bad "ref leaked into repo"

# 2. reset --hard onto the foreign commit is REJECTED (HEAD unchanged)
( cd "$R"; git reset --hard "$FOREIGN" ) >/dev/null 2>&1 || true
[ "$(cd "$R"; git rev-parse HEAD)" = "$B" ] \
  && ok "foreign reset --hard rejected (HEAD held at B)" || bad "HEAD was clobbered to foreign"

# 3. a LEGITIMATE reset within the repo's own history is ALLOWED
( cd "$R"; git reset --hard "$A" ) >/dev/null 2>&1
[ "$(cd "$R"; git rev-parse HEAD)" = "$A" ] \
  && ok "legit reset to an in-history commit allowed" || bad "legit reset was blocked"

# 4. a normal new commit is ALLOWED (shares history)
( cd "$R"; git reset --hard "$B" >/dev/null 2>&1; echo c > f; git add f; git commit -q -m C )
[ "$(cd "$R"; git log --oneline | wc -l | tr -d ' ')" -ge 3 ] \
  && ok "normal commit allowed" || bad "normal commit blocked"

# 5. the escape hatch lets a deliberate foreign graft through
( cd "$R"; MO_ALLOW_FOREIGN_REF=1 git reset --hard "$FOREIGN" ) >/dev/null 2>&1 || true
[ "$(cd "$R"; git rev-parse HEAD)" = "$FOREIGN" ] \
  && ok "MO_ALLOW_FOREIGN_REF=1 bypass works" || bad "escape hatch did not work"

echo "── Results: $PASS OK  $FAIL FAIL ──"
[ "$FAIL" -eq 0 ]
