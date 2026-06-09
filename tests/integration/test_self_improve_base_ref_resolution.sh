#!/usr/bin/env bash
# Regression coverage for recursive self-improve worktree base-ref resolution.
set -uo pipefail

REAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNNER="$REAL_ROOT/bin/mini-ork-self-improve"
MIGRATION="$REAL_ROOT/db/migrations/0017_self_improve_learning.sql"

TMPROOT="$(mktemp -d /tmp/mini-ork-base-ref-test-XXXXXX)"
trap 'rm -rf "$TMPROOT"' EXIT

PASS=0
FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS + 1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }

_make_root() {
  local root="$1"
  mkdir -p "$root/db/migrations"
  cp "$MIGRATION" "$root/db/migrations/0017_self_improve_learning.sql"
  git -C "$root" init -q
  git -C "$root" config user.email "test@example.local"
  git -C "$root" config user.name "mini-ork test"
  git -C "$root" config commit.gpgsign false
  git -C "$root" checkout -q -B main
  printf 'main\n' > "$root/state.txt"
  git -C "$root" add state.txt
  git -C "$root" commit -q -m main
}

_run_dry() {
  local root="$1" home="$2" base_ref="${3:-}"
  mkdir -p "$home"
  if [ -n "$base_ref" ]; then
    MINI_ORK_ROOT="$root" MINI_ORK_HOME="$home" MINI_ORK_DB="$home/state.db" \
      MINI_ORK_SELF_IMPROVE_BASE_REF="$base_ref" \
      "$RUNNER" --dry-run --max-iters 1 --soft-cap-hours 1 --hard-cap-hours 1
  else
    MINI_ORK_ROOT="$root" MINI_ORK_HOME="$home" MINI_ORK_DB="$home/state.db" \
      "$RUNNER" --dry-run --max-iters 1 --soft-cap-hours 1 --hard-cap-hours 1
  fi
}

_profile_value() {
  python3 - "$1" "$2" <<'PY'
import glob, json, sys
home, key = sys.argv[1:]
paths = sorted(glob.glob(f"{home}/runs/*/run_profile.json"))
if not paths:
    raise SystemExit("missing run_profile.json")
print(json.load(open(paths[-1], encoding="utf-8")).get(key, ""))
PY
}

_last_run_notes() {
  sqlite3 "$1/state.db" \
    "SELECT notes FROM self_improve_runs ORDER BY started_at DESC LIMIT 1;" 2>/dev/null || true
}

echo "── self-improve explicit base-ref resolution ──"

ROOT1="$TMPROOT/root-happy"
HOME1="$TMPROOT/home-happy"
mkdir -p "$ROOT1"
_make_root "$ROOT1"
main_sha=$(git -C "$ROOT1" rev-parse main)
if _run_dry "$ROOT1" "$HOME1" main >/tmp/mini-ork-base-ref-happy.out 2>&1; then
  resolved_sha=$(_profile_value "$HOME1" self_improve_resolved_base_sha)
  notes=$(_last_run_notes "$HOME1")
  if [ "$resolved_sha" = "$main_sha" ] && echo "$notes" | grep -q "base_ref=main@$main_sha"; then
    _ok "MINI_ORK_SELF_IMPROVE_BASE_REF=main resolves to git rev-parse main"
  else
    _fail "expected resolved sha/notes for $main_sha, got sha=$resolved_sha notes=$notes"
  fi
else
  _fail "dry-run failed for explicit main base"
  sed -n '1,40p' /tmp/mini-ork-base-ref-happy.out
fi

ROOT2="$TMPROOT/root-drift"
HOME2="$TMPROOT/home-drift"
mkdir -p "$ROOT2"
_make_root "$ROOT2"
main_sha=$(git -C "$ROOT2" rev-parse main)
git -C "$ROOT2" checkout -q -b audit/ahead
printf 'audit\n' >> "$ROOT2/state.txt"
git -C "$ROOT2" add state.txt
git -C "$ROOT2" commit -q -m audit-ahead
audit_sha=$(git -C "$ROOT2" rev-parse HEAD)
if _run_dry "$ROOT2" "$HOME2" >/tmp/mini-ork-base-ref-drift.out 2>&1; then
  resolved_sha=$(_profile_value "$HOME2" self_improve_resolved_base_sha)
  notes=$(_last_run_notes "$HOME2")
  if [ "$resolved_sha" = "$main_sha" ] && [ "$resolved_sha" != "$audit_sha" ] \
     && echo "$notes" | grep -q "base_ref=main@$main_sha"; then
    _ok "ambient audit branch is ignored; iter worktree resolves from main"
  else
    _fail "expected main sha $main_sha instead of audit sha $audit_sha, got sha=$resolved_sha notes=$notes"
  fi
else
  _fail "dry-run failed for drift guard"
  sed -n '1,40p' /tmp/mini-ork-base-ref-drift.out
fi

ROOT3="$TMPROOT/root-fallback"
HOME3="$TMPROOT/home-fallback"
mkdir -p "$ROOT3"
_make_root "$ROOT3"
git -C "$ROOT3" checkout -q -b audit/fallback
printf 'fallback\n' >> "$ROOT3/state.txt"
git -C "$ROOT3" add state.txt
git -C "$ROOT3" commit -q -m fallback-audit
ambient_sha=$(git -C "$ROOT3" rev-parse HEAD)
out=$(_run_dry "$ROOT3" "$HOME3" nonexistent 2>&1)
rc=$?
if [ "$rc" -eq 0 ] && echo "$out" | grep -q 'WARN: base ref "nonexistent"' \
   && echo "$out" | grep -q 'falling back to ambient branch'; then
  resolved_sha=$(_profile_value "$HOME3" self_improve_resolved_base_sha)
  fallback=$(_profile_value "$HOME3" self_improve_base_ref_fallback)
  if [ "$resolved_sha" = "$ambient_sha" ] && [ "$fallback" = "True" ]; then
    _ok "unavailable base ref warns loudly and falls back to ambient branch"
  else
    _fail "fallback profile mismatch: sha=$resolved_sha fallback=$fallback"
  fi
else
  _fail "unavailable base ref did not warn and complete"
  echo "$out" | sed -n '1,40p'
fi

echo
echo "Self-improve base-ref resolution: $PASS OK / $FAIL FAIL"
[ "$FAIL" -eq 0 ]
