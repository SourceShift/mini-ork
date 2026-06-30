#!/usr/bin/env bash
# Regression: .githooks/post-commit must restore the branch ref when a racing
# process clobbers HEAD onto an older/foreign commit right after a commit
# (observed 2026-06-30: a stray `git reset` to refs/codex/curated-sync — a
# foreign commit — orphaned a just-pushed fix). The original watchdog only
# guarded working-tree files ("HEAD is intact"); this asserts the HEAD-clobber
# guard restores the branch tip, and that it does NOT fight a legitimate
# follow-up commit.
set -uo pipefail
ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
HOOK="$ROOT/.githooks/post-commit"
PASS=0; FAIL=0
ok(){ echo "  [OK]   $1"; PASS=$((PASS+1)); }
bad(){ echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
echo "── unit: post-commit HEAD-clobber guard ──"

# --- build a throwaway repo with the hook installed ------------------------
R="$TMP/repo"; mkdir -p "$R"
HK="$TMP/hooks"; mkdir -p "$HK"; cp "$HOOK" "$HK/post-commit"; chmod +x "$HK/post-commit"
(
  cd "$R"
  git init -q
  git config user.email t@t; git config user.name t
  git config core.hooksPath "$HK"
  # Fast watchdog: short window, sub-second poll.
  export MO_REVERSION_GUARD_WATCH_S=12 MO_REVERSION_GUARD_POLL_S=1
  echo a > f.txt; git add f.txt; git commit -q -m A      # commit A (older)
  A=$(git rev-parse HEAD)
  sleep 1.1                                              # ensure B's ct > A's ct
  echo b > f.txt; git add f.txt; git commit -q -m B      # commit B — arms the watchdog
  B=$(git rev-parse HEAD)
  echo "$A" > "$TMP/A"; echo "$B" > "$TMP/B"
)
A=$(cat "$TMP/A"); B=$(cat "$TMP/B")

# --- Scenario 1: clobber HEAD onto the older commit A; guard must restore B --
( cd "$R"; git reset --hard "$A" >/dev/null 2>&1 )
[ "$(cd "$R"; git rev-parse HEAD)" = "$A" ] && ok "clobber applied (branch at older A)" || bad "clobber setup failed"

# poll up to ~8s for the detached watchdog to heal the ref
restored=""
for _ in $(seq 1 16); do
  sleep 0.5
  if [ "$(cd "$R"; git rev-parse HEAD)" = "$B" ]; then restored=1; break; fi
done
[ -n "$restored" ] && ok "watchdog restored branch ref to our commit B" || bad "branch NOT restored (still $(cd "$R"; git rev-parse --short HEAD))"
grep -q "restored-HEAD-clobbered-from-" "$R/.mini-ork/file-reversion-guard.log" 2>/dev/null \
  && ok "recovery logged" || bad "no recovery log line"

# --- Scenario 2: a legitimate follow-up commit must NOT be reverted ----------
R2="$TMP/repo2"; mkdir -p "$R2"
(
  cd "$R2"
  git init -q; git config user.email t@t; git config user.name t
  git config core.hooksPath "$HK"
  export MO_REVERSION_GUARD_WATCH_S=6 MO_REVERSION_GUARD_POLL_S=1
  echo a > f.txt; git add f.txt; git commit -q -m A
  echo b > f.txt; git add f.txt; git commit -q -m B     # arms watchdog on B
  echo c > f.txt; git add f.txt; git commit -q -m C     # legit follow-up (descends from B)
  git rev-parse HEAD > "$TMP/C"
)
C=$(cat "$TMP/C")
sleep 4   # let the (B-armed) watchdog run its window
[ "$(cd "$R2"; git rev-parse HEAD)" = "$C" ] && ok "legit follow-up commit C preserved (no false restore)" || bad "guard wrongly reverted a legit commit"

echo "── Results: $PASS OK  $FAIL FAIL ──"
[ "$FAIL" -eq 0 ]
