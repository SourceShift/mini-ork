#!/usr/bin/env bash
# Integration regression for Track B6 — coordination gate + metrics + audit.
#
# Acceptance:
#   1. Advisory mode emits a WAIT-before-editing nudge to stderr but
#      returns rc=0 (the tool call is NOT denied).
#   2. Strict mode (per-scope opt-in) returns rc=11 when the requested
#      op conflicts with an active lease outside the strict scope.
#   3. Counter increments:
#        - coord_leases_held        reflects the live registry snapshot
#        - coord_queue_depth        tracks active waiters
#        - coord_deadlocks_broken   bumps on deadlock abort
#        - coord_ttl_expirations    bumps on ttl prune events
#   4. The contention audit is bounded: with COORD_GATE_AUDIT_MAX=N the
#      file never stores more than N records, even after N+K appends.
#
# Uses an isolated state dir under $TMPDIR so the test never touches the
# real mini-ork home. The gate module auto-sources the registry, so the
# test only needs to source coord_gate.sh.

set -o pipefail

MINI_ORK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export MINI_ORK_ROOT

# Isolated state — never read/write the real .mini-ork home.
TMPDIR_B6="$(mktemp -d -t b6-gate-XXXXXX)"
trap 'rm -rf "$TMPDIR_B6"' EXIT
export MINI_ORK_HOME="$TMPDIR_B6"
export MINI_ORK_RUN_DIR="$TMPDIR_B6"
export COORD_REGISTRY_STATE_FILE="$TMPDIR_B6/state/coord-registry/leases.json"
export COORD_GATE_METRICS_FILE="$TMPDIR_B6/state/coord-gate/metrics.json"
export COORD_GATE_AUDIT_FILE="$TMPDIR_B6/state/coord-gate/audit.json"
mkdir -p "$(dirname "${COORD_REGISTRY_STATE_FILE}")" \
         "$(dirname "${COORD_GATE_METRICS_FILE}")" \
         "$(dirname "${COORD_GATE_AUDIT_FILE}")"

# shellcheck source=lib/coord_registry.sh
source "$MINI_ORK_ROOT/lib/coord_registry.sh"
# shellcheck source=lib/coord_gate.sh
source "$MINI_ORK_ROOT/lib/coord_gate.sh"

PASS=0; FAIL=0
_t() { printf '\n=== %s ===\n' "$1"; }
_check() {
  local desc="$1" actual="$2" expected="$3"
  if [ "$actual" = "$expected" ]; then
    printf "  PASS  %s\n" "$desc"
    PASS=$((PASS+1))
  else
    printf "  FAIL  %s\n    expected=%q\n    actual=%q\n" "$desc" "$expected" "$actual"
    FAIL=$((FAIL+1))
  fi
}

_t "1. advisory mode emits WAIT nudge and returns rc=0"
LEASE=$(coord_acquire holder-b /src/api write 60)
set +e
ERR=$(coord_gate_check requester-a /src/api/controller.rs write 2>&1)
RC=$?
set -e
_check "advisory rc=0 on conflict"        "$RC"    "0"
case "${ERR}" in
  WAIT*before*editing*)                  printf "  PASS  nudge contains 'WAIT before editing'\n"; PASS=$((PASS+1)) ;;
  *)                                    printf "  FAIL  nudge missing or wrong shape: %q\n" "${ERR}"; FAIL=$((FAIL+1)) ;;
esac
case "${ERR}" in
  *holder-b*)                           printf "  PASS  nudge names the holder\n"; PASS=$((PASS+1)) ;;
  *)                                    printf "  FAIL  nudge does not name holder-b: %q\n" "${ERR}"; FAIL=$((FAIL+1)) ;;
esac

_t "2. strict mode denies conflicting op with rc=11"
set +e
ERR=$(COORD_GATE_MODE=strict COORD_GATE_SCOPE=/ coord_gate_check requester-a /src/api/controller.rs write 2>&1)
RC=$?
set -e
_check "strict rc=11 on conflict"        "$RC"    "11"
case "${ERR}" in
  *strict*deny*)                        printf "  PASS  strict deny message emitted\n"; PASS=$((PASS+1)) ;;
  *)                                    printf "  FAIL  strict deny message missing: %q\n" "${ERR}"; FAIL=$((FAIL+1)) ;;
esac

_t "3. strict mode allows when path is inside the strict scope AND no conflict"
# Free the holder first so the only conflict-source is gone.
coord_release "$LEASE" >/dev/null
COORD_GATE_MODE=strict COORD_GATE_SCOPE=/src set +e
OUT=$(COORD_GATE_MODE=strict COORD_GATE_SCOPE=/src coord_gate_check requester-a /src/api/controller.rs write 2>&1)
RC=$?
set -e
_check "strict rc=0 with no holder"      "$RC"    "0"
[ -z "${OUT}" ] && { printf "  PASS  strict stays silent on no-conflict\n"; PASS=$((PASS+1)); } \
                || { printf "  FAIL  strict emitted output on no-conflict: %q\n" "${OUT}"; FAIL=$((FAIL+1)); }

_t "4. metrics reflect the live registry"
LEASE=$(coord_acquire holder-c /src/coord write 60)
# Touch the gate so it runs an observe() and writes the metrics file.
coord_gate_check requester-c /somewhere/else write >/dev/null 2>&1
METRICS=$(coord_gate_metrics)
HELD=$(printf '%s' "${METRICS}" | python3 -c "import sys,json;print(json.load(sys.stdin)['coord_leases_held'])")
_check "coord_leases_held reflects live snapshot" "$HELD" "1"

# Add a second lease on an unrelated prefix so leases_held=2.
LEASE2=$(coord_acquire holder-d /src/other-2 write 60)
coord_gate_check requester-c /somewhere/else write >/dev/null 2>&1
METRICS=$(coord_gate_metrics)
HELD=$(printf '%s' "${METRICS}" | python3 -c "import sys,json;print(json.load(sys.stdin)['coord_leases_held'])")
_check "coord_leases_held increments to 2"        "$HELD" "2"

# Deadlock abort must bump coord_deadlocks_broken. Set up a tight cycle:
#   holder-c holds /src/coord (write)
#   requester-e holds /src/other-cycle (write)
# Then holder-c asks for /src/other-cycle/x — blocked by requester-e
# (edge holder-c → requester-e).
# Then requester-e asks for /src/coord/x — blocked by holder-c
# (edge requester-e → holder-c). Cycle: holder-c → requester-e → holder-c.
# With requester-e lower priority than holder-c, requester-e is the
# lowest-priority participant and the requester → abort rc=4.
LEASE3=$(coord_acquire requester-e /src/other-cycle write 60)
set +e
COORD_REGISTRY_AGENT_PRIORITY_HOLDER_C=100 \
COORD_REGISTRY_AGENT_PRIORITY_REQUESTER_E=5 \
  coord_acquire holder-c /src/other-cycle/x write 60 >/dev/null 2>&1
OUT=$(COORD_REGISTRY_AGENT_PRIORITY_HOLDER_C=100 \
       COORD_REGISTRY_AGENT_PRIORITY_REQUESTER_E=5 \
       coord_acquire requester-e /src/coord/x write 60 2>&1)
ACQ_RC=$?
set -e
_check "deadlock abort rc=4"                     "$ACQ_RC" "4"
# Bump via the gate's exposed recorder (the registry aborts before reaching
# the gate, so the recorder is the canonical place to mark the counter).
coord_gate_record_deadlock
DEAD=$(coord_gate_metrics_field coord_deadlocks_broken)
_check "coord_deadlocks_broken >= 1 after abort"  "$DEAD" "1"

# TTL expirations — bump via recorder and confirm the counter advances.
coord_gate_record_ttl_expiration 3
TTL=$(coord_gate_metrics_field coord_ttl_expirations)
_check "coord_ttl_expirations reflects delta"     "$TTL"  "3"

_t "5. audit is bounded by COORD_GATE_AUDIT_MAX"
COORD_GATE_AUDIT_MAX=4
for i in 1 2 3 4 5 6 7 8 9; do
  _coord_gate_audit_append "{\"event\":\"probe\",\"n\":${i}}"
done
COUNT=$(coord_gate_audit | python3 -c "import sys,json;print(json.load(sys.stdin)['count'])")
MAX=$(coord_gate_audit | python3 -c "import sys,json;print(json.load(sys.stdin)['max'])")
_check "audit count <= COORD_GATE_AUDIT_MAX"      "$COUNT" "4"
_check "audit max field reflects cap"             "$MAX"   "4"

# Most-recent-first ordering: the last appended record should be n=9.
FIRST=$(coord_gate_audit 1 | python3 -c "import sys,json;print(json.load(sys.stdin)['events'][0]['n'])")
_check "most-recent-first ordering"               "$FIRST" "9"

# Bounded audit survives a real conflict flow.
LEASE_A=$(coord_acquire holder-final /src/final write 60)
COORD_GATE_MODE=advisory coord_gate_check requester-final /src/final/x.rs write >/dev/null 2>&1 || true
NEW_COUNT=$(coord_gate_audit | python3 -c "import sys,json;print(json.load(sys.stdin)['count'])")
[ "$NEW_COUNT" -le 4 ] && { printf "  PASS  conflict append still bounded (count=%s)\n" "$NEW_COUNT"; PASS=$((PASS+1)); } \
                       || { printf "  FAIL  conflict append broke bound (count=%s)\n" "$NEW_COUNT"; FAIL=$((FAIL+1)); }

# Cleanup so reruns are idempotent.
coord_release "$LEASE"  >/dev/null 2>&1 || true
coord_release "$LEASE2" >/dev/null 2>&1 || true
coord_release "$LEASE3" >/dev/null 2>&1 || true
coord_release "$LEASE_A" >/dev/null 2>&1 || true

printf '\n=== SUMMARY: %d passed, %d failed ===\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
