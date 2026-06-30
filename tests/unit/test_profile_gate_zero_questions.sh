#!/usr/bin/env bash
# Regression: a planner profile that flags needs_answers but asks ZERO
# human_questions must NOT dead-end the gate. The planner asked nothing (it had
# everything), so there is nothing to answer — normalize needs_answers → ready
# rather than blocking dispatch on answers that cannot exist.
set -uo pipefail
ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PASS=0; FAIL=0
ok(){ echo "  [OK]   $1"; PASS=$((PASS+1)); }
bad(){ echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# shellcheck source=/dev/null
. "$ROOT/lib/profile_gate.sh"

_status_in(){ python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("profile_status",""))' "$1"; }
_mkprofile(){ printf '%s\n' "$2" > "$1"; }

echo "── unit: profile gate — needs_answers + 0 questions ──"

# Case 1: needs_answers + [] questions → normalized to ready, file rewritten
P1="$TMP/p1.json"
_mkprofile "$P1" '{"profile_status":"needs_answers","human_questions":[],"confidence":0.9}'
out="$(mo_profile_normalize_zero_questions "$P1")"
[ "$out" = "ready" ] && ok "0-questions needs_answers → echoes ready" || bad "echoed '$out' (want ready)"
[ "$(_status_in "$P1")" = "ready" ] && ok "profile file rewritten to ready (downstream agrees)" || bad "file not rewritten ($(_status_in "$P1"))"
[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("confidence"))' "$P1")" = "0.9" ] && ok "confidence preserved (gate stays independent)" || bad "confidence mutated"

# Case 2: needs_answers + REAL questions → unchanged (block still legitimate)
P2="$TMP/p2.json"
_mkprofile "$P2" '{"profile_status":"needs_answers","human_questions":["What is the target repo?"],"confidence":0.4}'
out="$(mo_profile_normalize_zero_questions "$P2")"
[ "$out" = "needs_answers" ] && ok "needs_answers WITH questions stays needs_answers" || bad "wrongly normalized ('$out')"
[ "$(_status_in "$P2")" = "needs_answers" ] && ok "profile file untouched when real questions exist" || bad "file wrongly rewritten"

# Case 3: already ready → unchanged
P3="$TMP/p3.json"
_mkprofile "$P3" '{"profile_status":"ready","human_questions":[],"confidence":1.0}'
out="$(mo_profile_normalize_zero_questions "$P3")"
[ "$out" = "ready" ] && ok "already-ready profile stays ready" || bad "ready mutated ('$out')"

# Case 4: missing path → no-op, empty echo (caller keeps its status)
out="$(mo_profile_normalize_zero_questions "$TMP/does-not-exist.json")"
[ -z "$out" ] && ok "missing profile → empty echo (no-op)" || bad "missing profile echoed '$out'"

echo "── Results: $PASS OK  $FAIL FAIL ──"
[ "$FAIL" -eq 0 ]
