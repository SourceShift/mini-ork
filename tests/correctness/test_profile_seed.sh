#!/usr/bin/env bash
# Regression coverage for recursive self-improve profile seeding.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LIB="$ROOT/bin/lib/profile-seed.sh"

PASS=0
FAIL=0
_ok() { echo "  [OK]   $*"; PASS=$((PASS + 1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }

TMPROOT="$(mktemp -d /tmp/mini-ork-profile-seed-XXXXXX)"
trap 'rm -rf "$TMPROOT"' EXIT

# shellcheck source=/dev/null
source "$LIB"

echo "── correctness: profile seed ──"

PROFILE1="$TMPROOT/run_profile-example.json"
printf '{"self_improve_resolved_base_sha":"abc123"}\n' > "$PROFILE1"
if mo_profile_seed_from_kickoff "$ROOT/recipes/recursive-self-improve/example-kickoff.md" "$PROFILE1" >/dev/null; then
  if jq -e '.success_criteria | length > 0' "$PROFILE1" >/dev/null; then
    _ok "profile_seed: success_criteria populated from example-kickoff"
  else
    _fail "profile_seed: success_criteria stayed empty"
  fi
  if jq -e '.profile_status != "needs_answers"' "$PROFILE1" >/dev/null; then
    _ok "profile_seed: profile_status flipped from needs_answers"
  else
    _fail "profile_seed: profile_status stayed needs_answers"
  fi
  if jq -e '.self_improve_resolved_base_sha == "abc123"' "$PROFILE1" >/dev/null; then
    _ok "profile_seed: preserves existing run_profile metadata"
  else
    _fail "profile_seed: lost existing run_profile metadata"
  fi
else
  _fail "profile_seed: example-kickoff invocation failed"
fi

MISSING="$TMPROOT/no-sections.md"
cat > "$MISSING" <<'MD'
# Sparse Kickoff

This intentionally has no structured profile sections.
MD

PROFILE2="$TMPROOT/run_profile-missing.json"
if mo_profile_seed_from_kickoff "$MISSING" "$PROFILE2" >/dev/null; then
  if jq -e '.profile_status == "needs_answers" and (.success_criteria | length == 0)' "$PROFILE2" >/dev/null; then
    _ok "profile_seed: fallback to needs_answers when sections missing"
  else
    _fail "profile_seed: sparse kickoff did not degrade to needs_answers"
  fi
else
  _fail "profile_seed: sparse kickoff crashed"
fi

echo
echo "Profile seed: $PASS OK / $FAIL FAIL"
[ "$FAIL" -eq 0 ] || exit 1
