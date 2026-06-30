#!/usr/bin/env bash
# tests/unit/test_repo_integrity_guard.sh — unit tests for lib/repo_integrity_guard.sh
#                                       and the tag-advisory downgrade in .githooks/pre-push.
#
# Usage: bash tests/unit/test_repo_integrity_guard.sh
#
# Self-contained: spins up temp git repos; no external network, no LLM
# providers, no real remote. Mirrors the test_<lib>.sh skeleton used
# elsewhere in tests/unit/.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/repo_integrity_guard.sh"
PRE_PUSH="$MINI_ORK_ROOT/.githooks/pre-push"
CLAIM_CHECK_SRC="$MINI_ORK_ROOT/scripts/readme-claim-check.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

echo "── unit: repo_integrity_guard.sh + pre-push tag-advisory ──"

[ -f "$LIB" ] || { _fail "lib/repo_integrity_guard.sh missing"; echo "── Results: $PASS OK $FAIL FAIL ──"; exit 1; }
[ -f "$PRE_PUSH" ] || { _fail ".githooks/pre-push missing"; echo "── Results: $PASS OK $FAIL FAIL ──"; exit 1; }

# shellcheck source=/dev/null
source "$LIB"

TMP_ROOT="$(mktemp -d /tmp/mo-rig-test-XXXXXX)"
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

_setup_repo() {
  local d="$1"
  mkdir -p "$d"
  git -C "$d" init -q -b main
  git -C "$d" config user.email "rig-test@example.com"
  git -C "$d" config user.name  "RigTest"
  git -C "$d" config commit.gpgsign false
}

# ── (a) cold-start ──────────────────────────────────────────────────────────
echo ""
echo "--- (a) cold-start: records current tip, no log row ---"
A_DIR="$TMP_ROOT/a"
_setup_repo "$A_DIR"
echo "a" > "$A_DIR/f.txt"
git -C "$A_DIR" add f.txt
git -C "$A_DIR" commit -q -m "initial"
LKG="$A_DIR/.mini-ork/last-known-good-ref.main"
LOG="$A_DIR/.mini-ork/repo-integrity-guard.log"

(
  cd "$A_DIR"
  unset MO_REPO_INTEGRITY_GUARD_DISABLED
  source "$LIB"
  repo_integrity_check_and_heal
)
rc=$?
[ "$rc" -eq 0 ] && _ok "guard exited 0 on cold-start" || _fail "guard exit=$rc"
[ -s "$LKG" ] && _ok "last-known-good-ref.<branch> populated" || _fail "LKG missing/empty: $LKG"
[ ! -s "$LOG" ] && _ok "no log row written on cold-start" || _fail "unexpected log row at cold-start"
TIP=$(git -C "$A_DIR" rev-parse HEAD)
[ "$(cat "$LKG")" = "$TIP" ] && _ok "LKG contains current tip" || _fail "LKG != HEAD (LKG=$(cat "$LKG"), HEAD=$TIP)"

# ── (b) foreign clobber heal ────────────────────────────────────────────────
echo ""
echo "--- (b) foreign clobber is detected and healed ---"
B_DIR="$TMP_ROOT/b"
_setup_repo "$B_DIR"
echo "good" > "$B_DIR/f.txt"
git -C "$B_DIR" add f.txt
git -C "$B_DIR" commit -q -m "good"
GOOD_SHA=$(git -C "$B_DIR" rev-parse HEAD)
GOOD_CT=$(git -C "$B_DIR" show -s --format=%ct "$GOOD_SHA")
LOG="$B_DIR/.mini-ork/repo-integrity-guard.log"

# Run guard to record the good SHA.
(
  cd "$B_DIR"; unset MO_REPO_INTEGRITY_GUARD_DISABLED; source "$LIB"
  repo_integrity_check_and_heal
) >/dev/null 2>&1

# Build an UNRELATED commit (no parent → empty tree) with a PAST date.
EMPTY_TREE=$(git -C "$B_DIR" mktree < /dev/null)
PAST=$((GOOD_CT - 86400))
OLDER_SHA=$(GIT_AUTHOR_DATE="@$PAST" GIT_COMMITTER_DATE="@$PAST" \
  git -C "$B_DIR" commit-tree "$EMPTY_TREE" -m "older-unrelated")

# Force-reset the branch ref to the unrelated older commit.
git -C "$B_DIR" reset --hard "$OLDER_SHA" >/dev/null 2>&1
[ "$(git -C "$B_DIR" rev-parse HEAD)" = "$OLDER_SHA" ] \
  && _ok "test fixture: branch reset onto unrelated older commit" \
  || _fail "could not reset branch to unrelated older commit"

# Run guard — should heal.
(
  cd "$B_DIR"; unset MO_REPO_INTEGRITY_GUARD_DISABLED; source "$LIB"
  repo_integrity_check_and_heal
) >/dev/null 2>&1

AFTER_HEAD=$(git -C "$B_DIR" rev-parse refs/heads/main)
[ "$AFTER_HEAD" = "$GOOD_SHA" ] \
  && _ok "branch ref restored to good SHA after heal" \
  || _fail "branch ref not restored (got=$AFTER_HEAD, want=$GOOD_SHA)"

log_rows=$(grep -c '' "$LOG" 2>/dev/null || echo 0)
[ "$log_rows" -eq 1 ] && _ok "exactly one TSV log row after heal" || _fail "expected 1 log row, got $log_rows"
[ -s "$LOG" ] && grep -q "$GOOD_SHA" "$LOG" \
  && _ok "log row references good SHA" \
  || _fail "log row missing good SHA"
[ -s "$LOG" ] && grep -q "$OLDER_SHA" "$LOG" \
  && _ok "log row references old clobbered tip SHA" \
  || _fail "log row missing old clobbered tip SHA"

LKG_AFTER=$(cat "$B_DIR/.mini-ork/last-known-good-ref.main")
[ "$LKG_AFTER" = "$GOOD_SHA" ] \
  && _ok "LKG unchanged after heal (still good SHA)" \
  || _fail "LKG modified on heal (got=$LKG_AFTER, want=$GOOD_SHA)"

# ── (c) legitimate fast-forward — no heal, LKG advances ────────────────────
echo ""
echo "--- (c) legitimate fast-forward — no false positive ---"
C_DIR="$TMP_ROOT/c"
_setup_repo "$C_DIR"
echo "first" > "$C_DIR/f.txt"
git -C "$C_DIR" add f.txt
git -C "$C_DIR" commit -q -m "first"
FIRST_SHA=$(git -C "$C_DIR" rev-parse HEAD)

(
  cd "$C_DIR"; unset MO_REPO_INTEGRITY_GUARD_DISABLED; source "$LIB"
  repo_integrity_check_and_heal
) >/dev/null 2>&1

# Build a newer commit (future date) on top — a legitimate advance.
FUTURE=$(( $(date +%s) + 86400 ))
echo "newer" > "$C_DIR/f.txt"
git -C "$C_DIR" add f.txt
GIT_AUTHOR_DATE="@$FUTURE" GIT_COMMITTER_DATE="@$FUTURE" \
  git -C "$C_DIR" commit -q -m "newer"
NEW_SHA=$(git -C "$C_DIR" rev-parse HEAD)

(
  cd "$C_DIR"; unset MO_REPO_INTEGRITY_GUARD_DISABLED; source "$LIB"
  repo_integrity_check_and_heal
) >/dev/null 2>&1

[ "$(git -C "$C_DIR" rev-parse refs/heads/main)" = "$NEW_SHA" ] \
  && _ok "branch tip unchanged on legitimate advance" \
  || _fail "branch tip moved unexpectedly"

c_rows=$(grep -c '' "$C_DIR/.mini-ork/repo-integrity-guard.log" 2>/dev/null || echo 0)
[ "$c_rows" -eq 0 ] && _ok "no log row on legitimate advance" \
  || _fail "expected 0 log rows, got $c_rows"

[ "$(cat "$C_DIR/.mini-ork/last-known-good-ref.main")" = "$NEW_SHA" ] \
  && _ok "LKG advanced to new tip" \
  || _fail "LKG not advanced to new tip"

# ── (d) disabled escape hatch ──────────────────────────────────────────────
echo ""
echo "--- (d) MO_REPO_INTEGRITY_GUARD_DISABLED=1 short-circuits ---"
D_DIR="$TMP_ROOT/d"
_setup_repo "$D_DIR"
echo "x" > "$D_DIR/f.txt"
git -C "$D_DIR" add f.txt
git -C "$D_DIR" commit -q -m "init" >/dev/null 2>&1

(
  cd "$D_DIR"
  export MO_REPO_INTEGRITY_GUARD_DISABLED=1
  source "$LIB"
  repo_integrity_check_and_heal
)
d_rc=$?
[ "$d_rc" -eq 0 ] && _ok "guard exits 0 when disabled" || _fail "guard exit=$d_rc when disabled"
[ ! -e "$D_DIR/.mini-ork/last-known-good-ref.main" ] \
  && _ok "no LKG file written when disabled" \
  || _fail "LKG file written despite disabled flag"
[ ! -e "$D_DIR/.mini-ork/repo-integrity-guard.log" ] \
  && _ok "no log file written when disabled" \
  || _fail "log file written despite disabled flag"

# ── (e) not in worktree ─────────────────────────────────────────────────────
echo ""
echo "--- (e) not in a worktree: exits 0, no writes ---"
E_DIR="$TMP_ROOT/empty"
mkdir -p "$E_DIR"

(
  cd "$E_DIR"
  unset MO_REPO_INTEGRITY_GUARD_DISABLED
  source "$LIB"
  repo_integrity_check_and_heal
)
e_rc=$?
[ "$e_rc" -eq 0 ] && _ok "guard exits 0 outside a worktree" \
  || _fail "guard exit=$e_rc outside worktree"
[ ! -e "$E_DIR/.mini-ork" ] \
  && _ok "no .mini-ork/ created outside worktree" \
  || _fail ".mini-ork/ created outside worktree"

# ── (f) pre-push tag vs main drift ──────────────────────────────────────────
echo ""
echo "--- (f) pre-push tag-advisory vs main hard-block ---"
[ -f "$CLAIM_CHECK_SRC" ] || { _skip "scripts/readme-claim-check.sh missing — skip pre-push subtest"; }

F_DIR="$TMP_ROOT/f"
_setup_repo "$F_DIR"
mkdir -p "$F_DIR/.githooks" "$F_DIR/scripts"
cp "$PRE_PUSH"        "$F_DIR/.githooks/pre-push"
cp "$CLAIM_CHECK_SRC" "$F_DIR/scripts/readme-claim-check.sh"
chmod +x "$F_DIR/.githooks/pre-push" "$F_DIR/scripts/readme-claim-check.sh"

# Write a README whose claimed count cannot match the empty temp repo —
# guaranteed Layer 1 DRIFT.
cat > "$F_DIR/README.md" <<'EOF'
# F test repo

5 framework primitives
EOF
git -C "$F_DIR" add .githooks scripts README.md
git -C "$F_DIR" commit -q -m "fixture"

# Sanity: confirm readme-claim-check.sh actually fails in this temp repo.
(
  cd "$F_DIR"
  bash "$F_DIR/scripts/readme-claim-check.sh" >/dev/null 2>&1
)
sanity_rc=$?
[ "$sanity_rc" -ne 0 ] \
  && _ok "fixture: scripts/readme-claim-check.sh fails as designed" \
  || _fail "fixture L1 did not fail (rc=$sanity_rc) — cannot exercise the downgrade"

# Tag push → advisory exit 0
tag_out=$(
  cd "$F_DIR" \
    && MO_README_DRIFT_SKIP=0 \
       bash "$F_DIR/.githooks/pre-push" \
            <<< "abc123 def456 refs/tags/v9.9.9 0000000000000000000000000000000000000000" \
       2>&1
)
tag_rc=$?
[ "$tag_rc" -eq 0 ] \
  && _ok "tag push exits 0 with README drift (advisory)" \
  || _fail "tag push exit=$tag_rc (expected 0)"
echo "$tag_out" | grep -q "advisory" \
  && _ok "tag push output contains 'advisory' marker" \
  || _fail "tag push output missing 'advisory' marker; got:\n$tag_out"

# Main push → hard-block exit 1
main_out=$(
  cd "$F_DIR" \
    && MO_README_DRIFT_SKIP=0 \
       bash "$F_DIR/.githooks/pre-push" \
            <<< "abc123 def456 refs/heads/main 0000000000000000000000000000000000000000" \
       2>&1
)
main_rc=$?
[ "$main_rc" -eq 1 ] \
  && _ok "main push hard-blocks with README drift (exit 1)" \
  || _fail "main push exit=$main_rc (expected 1)"
echo "$main_out" | grep -q "BLOCKED" \
  && _ok "main push output mentions BLOCKED" \
  || _fail "main push output missing BLOCKED; got:\n$main_out"

# ── (g) slash-named branch — LKG filename must be sanitized ─────────────────
echo ""
echo "--- (g) slash branch (feat/x): LKG filename sanitized, no subdir error ---"
G_DIR="$TMP_ROOT/g"
_setup_repo "$G_DIR"
echo "g" > "$G_DIR/f.txt"
git -C "$G_DIR" add f.txt
git -C "$G_DIR" commit -q -m "initial"
git -C "$G_DIR" switch -q -c feat/slash-case
G_ERR="$( cd "$G_DIR" && repo_integrity_check_and_heal 2>&1 1>/dev/null )"
[ -z "$G_ERR" ] \
  && _ok "guard runs clean on slash branch (no stderr)" \
  || _fail "guard emitted error on slash branch: $G_ERR"
[ -s "$G_DIR/.mini-ork/last-known-good-ref.feat__slash-case" ] \
  && _ok "LKG written with sanitized name (feat__slash-case)" \
  || _fail "sanitized LKG file missing: $(ls "$G_DIR"/.mini-ork/last-known-good-ref.* 2>/dev/null)"
[ ! -d "$G_DIR/.mini-ork/last-known-good-ref.feat" ] \
  && _ok "no stray last-known-good-ref.feat/ subdir created" \
  || _fail "slash leaked into a subdir path"

# ── summary ────────────────────────────────────────────────────────────────
echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
exit 0