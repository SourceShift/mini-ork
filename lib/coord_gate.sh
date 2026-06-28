#!/usr/bin/env bash
# coord_gate.sh — PreToolUse-style coordination gate for Track B6.
#
# Sits in front of any tool/file-edit dispatch and consults the lease registry
# (lib/coord_registry.sh) before the edit lands. Two modes:
#
#   advisory  — emit a visible "WAIT before editing <path>" nudge to stderr
#               and return 0 so the operation is NOT blocked. Designed for
#               noisy feedback without breaking pipelines.
#   strict    — return non-zero (rc=11) when the requested op would conflict
#               with an active coordination lease. Opt-in per scope: the
#               caller must export COORD_GATE_SCOPE=<path-prefix> AND set
#               COORD_GATE_MODE=strict to enable the deny path.
#
# Counters (incremented in-place under ${COORD_GATE_METRICS_FILE}):
#   coord_leases_held       — non-zero whenever an active lease covers the path
#   coord_queue_depth       — number of waiters currently in the registry
#   coord_deadlocks_broken  — bumped when a deadlock cycle aborts an acquire
#   coord_ttl_expirations   — bumped on each access that prunes >=1 expired lease
#
# Contention audit (bounded ring buffer under ${COORD_GATE_AUDIT_FILE}):
#   - Appends a record on every conflict / denied / deadlock event.
#   - Capped at COORD_GATE_AUDIT_MAX (default 64). When full, oldest record
#     is dropped so the file stays bounded regardless of contention volume.
#   - coord_gate_audit [N] returns the most recent N records (default all).
#
# Public API:
#   coord_gate_check <agent> <path> <mode>          → rc=0 advisory, rc=11 strict-deny
#   coord_gate_metrics                              → prints counter JSON to stdout
#   coord_gate_metrics_field <name> [default]       → prints integer value
#   coord_gate_audit [N]                            → prints JSON (most-recent first)
#   coord_gate_record_deadlock                      → bumps coord_deadlocks_broken
#   coord_gate_record_ttl_expiration [delta]        → bumps coord_ttl_expirations
#
# Environment knobs (all optional):
#   COORD_GATE_MODE=advisory|strict                 (default advisory)
#   COORD_GATE_SCOPE=<path-prefix>                  (strict mode only; default /)
#   COORD_GATE_METRICS_FILE=<path>                  (overrides default state dir)
#   COORD_GATE_AUDIT_FILE=<path>                    (overrides default state dir)
#   COORD_GATE_AUDIT_MAX=<int>                      (default 64)

set -Eeuo pipefail

COORD_GATE_DEFAULT_AUDIT_MAX="${COORD_GATE_DEFAULT_AUDIT_MAX:-64}"

_coord_gate_metrics_file() {
  if [ -n "${COORD_GATE_METRICS_FILE:-}" ]; then
    printf '%s\n' "${COORD_GATE_METRICS_FILE}"
    return 0
  fi
  local base=""
  if [ -n "${MINI_ORK_RUN_DIR:-}" ]; then
    base="${MINI_ORK_RUN_DIR}"
  elif [ -n "${MINI_ORK_HOME:-}" ]; then
    base="${MINI_ORK_HOME}"
  elif [ -n "${HOME:-}" ]; then
    base="${HOME}/.mini-ork"
  fi
  if [ -n "${base}" ]; then
    printf '%s/state/coord-gate/metrics.json\n' "${base}"
  else
    printf '/tmp/coord-gate/metrics.json\n'
  fi
}

_coord_gate_audit_file() {
  if [ -n "${COORD_GATE_AUDIT_FILE:-}" ]; then
    printf '%s\n' "${COORD_GATE_AUDIT_FILE}"
    return 0
  fi
  local base=""
  if [ -n "${MINI_ORK_RUN_DIR:-}" ]; then
    base="${MINI_ORK_RUN_DIR}"
  elif [ -n "${MINI_ORK_HOME:-}" ]; then
    base="${MINI_ORK_HOME}"
  elif [ -n "${HOME:-}" ]; then
    base="${HOME}/.mini-ork"
  fi
  if [ -n "${base}" ]; then
    printf '%s/state/coord-gate/audit.json\n' "${base}"
  else
    printf '/tmp/coord-gate/audit.json\n'
  fi
}

_coord_gate_lock_file() {
  printf '%s.lock\n' "$(_coord_gate_metrics_file)"
}

_coord_gate_ensure_dirs() {
  local mfile afile mdir adir
  mfile="$(_coord_gate_metrics_file)"
  afile="$(_coord_gate_audit_file)"
  mdir="$(dirname "${mfile}")"
  adir="$(dirname "${afile}")"
  mkdir -p "${mdir}" "${adir}"
  : >"$(_coord_gate_lock_file)"
}

# Atomic counter bump. Reads the metrics file under flock, increments the
# named counter, writes back. Creates the file with a fresh schema on first
# touch. We expose every counter at every increment so the JSON stays
# self-describing for tooling that reads it cold.
_coord_gate_bump() {
  local counter="$1"
  local delta="${2:-1}"
  _coord_gate_ensure_dirs
  local mfile lfile
  mfile="$(_coord_gate_metrics_file)"
  lfile="$(_coord_gate_lock_file)"
  COORD_GATE_METRICS_FILE="${mfile}" \
    python3 - "${counter}" "${delta}" "${mfile}" "${lfile}" <<'PY'
import fcntl, json, os, sys
counter, delta_s, mfile, lfile = sys.argv[1:5]
try:
    delta = int(delta_s)
except ValueError:
    delta = 1

default_schema = {
    "coord_leases_held": 0,
    "coord_queue_depth": 0,
    "coord_deadlocks_broken": 0,
    "coord_ttl_expirations": 0,
}

os.makedirs(os.path.dirname(mfile), exist_ok=True)
with open(lfile, "w", encoding="utf-8") as lf:
    fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
    try:
        data = {}
        if os.path.exists(mfile):
            try:
                with open(mfile, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, json.JSONDecodeError):
                data = {}
        merged = dict(default_schema)
        merged.update({k: int(v) for k, v in data.items() if isinstance(v, int)})
        merged[counter] = merged.get(counter, 0) + delta
        tmp = mfile + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, sort_keys=True)
            f.write("\n")
        os.replace(tmp, mfile)
    finally:
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
PY
}

# Audit append with bounded ring buffer. Keeps the most recent
# COORD_GATE_AUDIT_MAX records (default 64). Older records are dropped
# so the file cannot grow without bound under heavy contention.
_coord_gate_audit_append() {
  local record_json="$1"
  _coord_gate_ensure_dirs
  local afile lfile max_records
  afile="$(_coord_gate_audit_file)"
  lfile="$(_coord_gate_lock_file)"
  max_records="${COORD_GATE_AUDIT_MAX:-${COORD_GATE_DEFAULT_AUDIT_MAX}}"
  if ! [[ "${max_records}" =~ ^[0-9]+$ ]] || [ "${max_records}" -le 0 ]; then
    max_records="${COORD_GATE_DEFAULT_AUDIT_MAX}"
  fi
  COORD_GATE_AUDIT_FILE="${afile}" \
    python3 - "${record_json}" "${afile}" "${lfile}" "${max_records}" <<'PY'
import fcntl, json, os, sys
record_json, afile, lfile, max_records_s = sys.argv[1:5]
try:
    max_records = int(max_records_s)
except ValueError:
    max_records = 64

try:
    record = json.loads(record_json)
except (ValueError, TypeError):
    record = {"raw": record_json}

os.makedirs(os.path.dirname(afile), exist_ok=True)
with open(lfile, "w", encoding="utf-8") as lf:
    fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
    try:
        events = []
        max_seen = max_records
        if os.path.exists(afile):
            try:
                with open(afile, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and isinstance(loaded.get("events"), list):
                    events = loaded["events"]
                    if isinstance(loaded.get("max"), int):
                        max_seen = loaded["max"]
                elif isinstance(loaded, list):
                    events = loaded
            except (OSError, json.JSONDecodeError):
                events = []
        events.append(record)
        # Ring-buffer trim: drop oldest until we're at the cap.
        if len(events) > max_records:
            events = events[-max_records:]
        tmp = afile + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"events": events, "max": max_records}, f, sort_keys=True)
            f.write("\n")
        os.replace(tmp, afile)
    finally:
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
PY
}

# Snapshot the current registry and bump the relevant counters in one
# pass. Always touches the four counters (with +0 when the event didn't
# happen) so the JSON schema stays stable for downstream consumers.
_coord_gate_observe_registry() {
  local mfile afile registry_state
  mfile="$(_coord_gate_metrics_file)"
  afile="$(_coord_gate_audit_file)"
  # Resolve the registry state file the same way coord_registry.sh does,
  # so the gate reads the live state regardless of which env knob is set.
  if [ -n "${COORD_REGISTRY_STATE_FILE:-}" ]; then
    registry_state="${COORD_REGISTRY_STATE_FILE}"
  elif declare -F _coord_registry_state_file >/dev/null 2>&1; then
    registry_state="$(_coord_registry_state_file)"
  else
    registry_state=""
  fi
  COORD_GATE_METRICS_FILE="${mfile}" \
    COORD_GATE_AUDIT_FILE="${afile}" \
    python3 - "${mfile}" "$(_coord_gate_lock_file)" "${registry_state}" <<'PY' 2>/dev/null
import fcntl, json, os, sys, time
mfile, lfile, state_file = sys.argv[1], sys.argv[2], sys.argv[3]

default_schema = {
    "coord_leases_held": 0,
    "coord_queue_depth": 0,
    "coord_deadlocks_broken": 0,
    "coord_ttl_expirations": 0,
}

now = int(time.time())
leases = {}
waits = {}
if state_file and os.path.exists(state_file):
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            leases = data.get("leases", {}) if isinstance(data.get("leases"), dict) else {}
            waits = data.get("waits", {}) if isinstance(data.get("waits"), dict) else {}
    except (OSError, json.JSONDecodeError):
        leases, waits = {}, {}

expired_before = 0
if isinstance(leases, dict):
    for rec in leases.values():
        try:
            if int(rec.get("expires_at", 0)) <= now:
                expired_before += 1
        except (TypeError, ValueError):
            pass

active = {lid: rec for lid, rec in leases.items() if int(rec.get("expires_at", 0)) > now}
# A wait is "live" when at least one of its BLOCKERS is an active holder — a
# blocked requester need not itself hold a lease. The old check (waiter must be
# an active holder) undercounted pure waiters to 0 during contention (frc-b6 fix).
_active_agents = {r.get("agent", "") for r in active.values()}
live_waits = {w: bs for w, bs in waits.items() if any(b in _active_agents for b in (bs or []))}

os.makedirs(os.path.dirname(mfile), exist_ok=True)
with open(lfile, "w", encoding="utf-8") as lf:
    fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
    try:
        merged = dict(default_schema)
        if os.path.exists(mfile):
            try:
                with open(mfile, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    merged.update({k: int(v) for k, v in loaded.items() if isinstance(v, int)})
            except (OSError, json.JSONDecodeError):
                pass
        # Snapshot-derived counters overwrite the stored value so the
        # gate's view of "leases held / queue depth" stays in sync with
        # the registry. Callers bump deadlock + ttl deltas explicitly
        # through coord_gate_record_* so those survive observe().
        merged["coord_leases_held"] = len(active)
        merged["coord_queue_depth"] = len(live_waits)
        if merged.get("coord_deadlocks_broken", 0) < 0:
            merged["coord_deadlocks_broken"] = 0
        if merged.get("coord_ttl_expirations", 0) < 0:
            merged["coord_ttl_expirations"] = 0
        tmp = mfile + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, sort_keys=True)
            f.write("\n")
        os.replace(tmp, mfile)
    finally:
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
PY
}

# desc: Probe the registry for a conflict without acquiring. Prints
#       "rc<TAB>holder" to stdout where rc is 0 (no conflict) or 1
#       (conflict) and holder is the conflicting agent (empty when rc=0).
#       Never errors — registry I/O failures are treated as "no conflict"
#       so the gate stays fail-open in the advisory path.
_coord_gate_probe_conflict() {
  local agent="$1" path="$2" mode="$3"
  local state_file
  state_file="${COORD_REGISTRY_STATE_FILE:-}"
  if [ -z "${state_file}" ]; then
    state_file="$(_coord_registry_state_file 2>/dev/null || true)"
  fi
  if [ -z "${state_file}" ] || [ ! -f "${state_file}" ]; then
    printf '0\t\n'
    return 0
  fi
  COORD_REGISTRY_STATE_FILE="${state_file}" \
    python3 - "${agent}" "${path}" "${mode}" "${state_file}" <<'PY' 2>/dev/null
import json, os, sys, time
agent, path, mode, state_file = sys.argv[1:5]

def normalize(p):
    p = p.strip()
    while "//" in p:
        p = p.replace("//", "/")
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    if not p.startswith("/"):
        p = "/" + p
    return p

def overlaps(a, b):
    if a == b:
        return True
    # Don't double the root: "/" + "/" == "//" made a root lease overlap
    # nothing, letting a "/"-scoped conflict bypass strict deny (frc-b6 fix).
    a_slash = a if a.endswith("/") else a + "/"
    b_slash = b if b.endswith("/") else b + "/"
    return a_slash.startswith(b_slash) or b_slash.startswith(a_slash)

def conflicts(existing_mode, new_mode):
    return not (existing_mode == "read" and new_mode == "read")

if not os.path.exists(state_file):
    print("0\t")
    sys.exit(0)

try:
    with open(state_file, "r", encoding="utf-8") as f:
        data = json.load(f)
except (OSError, json.JSONDecodeError):
    print("0\t")
    sys.exit(0)

now = int(time.time())
leases = data.get("leases", {}) if isinstance(data.get("leases"), dict) else {}
npath = normalize(path)
holder = ""
for rec in leases.values():
    if int(rec.get("expires_at", 0)) <= now:
        continue
    if not overlaps(rec.get("path", ""), npath):
        continue
    if not conflicts(rec.get("mode", "write"), mode):
        continue
    holder = str(rec.get("agent", ""))
    break
if holder:
    print(f"1\t{holder}")
else:
    print("0\t")
PY
  return 0
}

# desc: PreToolUse-style gate. Returns 0 in advisory mode always (with a
#       nudge on conflict), rc=11 in strict mode when the requested op
#       conflicts with an active lease outside the strict scope or with a
#       non-overlapping lease inside it.
#
# Exit codes:
#   0   advisory: no conflict, or conflict with nudge emitted
#   11  strict:   conflict denied
#   2   usage/argument error
coord_gate_check() {
  local agent="${1:-}"
  local path="${2:-}"
  local mode="${3:-}"
  if [ -z "${agent}" ] || [ -z "${path}" ] || [ -z "${mode}" ]; then
    printf 'coord_gate_check: usage: coord_gate_check <agent> <path> <mode>\n' >&2
    return 2
  fi
  if [ "${mode}" != "read" ] && [ "${mode}" != "write" ]; then
    printf 'coord_gate_check: mode must be "read" or "write" (got %s)\n' "${mode}" >&2
    return 2
  fi
  local gate_mode="${COORD_GATE_MODE:-advisory}"
  local gate_scope="${COORD_GATE_SCOPE:-/}"
  if [ "${gate_mode}" != "advisory" ] && [ "${gate_mode}" != "strict" ]; then
    printf 'coord_gate_check: COORD_GATE_MODE must be advisory or strict (got %s)\n' "${gate_mode}" >&2
    return 2
  fi

  _coord_gate_observe_registry

  local probe_out probe_rc probe_holder
  probe_out="$(_coord_gate_probe_conflict "${agent}" "${path}" "${mode}" 2>/dev/null || printf '0\t\n')"
  probe_rc="${probe_out%%	*}"
  probe_holder="${probe_out#*	}"
  if [ "${probe_rc}" != "1" ]; then
    return 0
  fi

  # Build the audit record with json.dumps so paths/holders containing quotes
  # or backslashes can't produce invalid JSON / injection (frc-b6 critic fix).
  local audit_record
  audit_record="$(MODE="${gate_mode}" APATH="${path}" RMODE="${mode}" \
    HOLDER="${probe_holder}" SCOPE="${gate_scope}" TS="$(date +%s)" python3 -c '
import json, os
print(json.dumps({
    "event": "conflict",
    "mode": os.environ["MODE"],
    "path": os.environ["APATH"],
    "requested_mode": os.environ["RMODE"],
    "holder": os.environ["HOLDER"],
    "scope": os.environ["SCOPE"],
    "ts": int(os.environ["TS"]),
}, sort_keys=True))')"
  _coord_gate_audit_append "${audit_record}"

  if [ "${gate_mode}" = "advisory" ]; then
    printf 'WAIT before editing %s — coordination lease held by %s (mode=%s, scope=%s)\n' \
      "${path}" "${probe_holder}" "${mode}" "${gate_scope}" >&2
    return 0
  fi

  # strict: DENY the conflict when the requested path is INSIDE the strict
  # scope; fall back to advisory (nudge, allow) for paths OUTSIDE the scope.
  # The old condition was inverted — it denied out-of-scope and allowed
  # in-scope conflicts, so a strict op on a scoped path sailed through
  # (frc-b6 critic fix). Root scope "/" means strict applies everywhere.
  local _scope="${gate_scope%/}"
  if [ "${gate_scope}" = "/" ] || [ "${path}" = "${_scope}" ] || [[ "${path}" == "${_scope}"/* ]]; then
    printf 'coord_gate_check: strict deny for %s (mode=%s, holder=%s, scope=%s)\n' \
      "${path}" "${mode}" "${probe_holder}" "${gate_scope}" >&2
    return 11
  fi
  # out of scope → advisory fallback
  printf 'WAIT before editing %s — coordination lease held by %s (mode=%s, out of strict scope=%s)\n' \
    "${path}" "${probe_holder}" "${mode}" "${gate_scope}" >&2
  return 0
}

# desc: Print current metrics JSON to stdout. Always includes all four
#       counters (defaulting to 0) so downstream tooling sees a stable
#       schema even on a fresh install.
coord_gate_metrics() {
  _coord_gate_ensure_dirs
  _coord_gate_observe_registry
  local mfile
  mfile="$(_coord_gate_metrics_file)"
  if [ -f "${mfile}" ]; then
    cat "${mfile}"
  else
    printf '{"coord_leases_held":0,"coord_queue_depth":0,"coord_deadlocks_broken":0,"coord_ttl_expirations":0}\n'
  fi
}

# desc: Read a single metrics field by name. Returns the integer value or
#       the supplied default when the file/field is missing.
coord_gate_metrics_field() {
  local field="$1"
  local default="${2:-0}"
  _coord_gate_ensure_dirs
  local mfile
  mfile="$(_coord_gate_metrics_file)"
  if [ ! -f "${mfile}" ]; then
    printf '%s\n' "${default}"
    return 0
  fi
  python3 - "${mfile}" "${field}" "${default}" <<'PY' 2>/dev/null
import json, sys
mfile, field, default = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(mfile, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and field in data:
        print(int(data[field]))
    else:
        print(default)
except (OSError, json.JSONDecodeError, ValueError, TypeError):
    print(default)
PY
}

# desc: Bump coord_deadlocks_broken. Called from the registry layer when
#       an acquire aborts on a wait-for cycle. Exposed so the registry
#       can hook into the gate's metrics without importing bash internals.
coord_gate_record_deadlock() {
  _coord_gate_bump coord_deadlocks_broken 1
}

# desc: Bump coord_ttl_expirations by N (default 1). Called by callers
#       that observe expired leases; the gate's own observe path also
#       sets the live value, so this is a delta over the snapshot.
coord_gate_record_ttl_expiration() {
  local delta="${1:-1}"
  _coord_gate_bump coord_ttl_expirations "${delta}"
}

# desc: Return the most recent N audit records (default all), most-recent
#       first. Returns a JSON object — the bounded `events` buffer plus a
#       `count` field for sanity-check probes.
coord_gate_audit() {
  local n="${1:-0}"
  _coord_gate_ensure_dirs
  local afile
  afile="$(_coord_gate_audit_file)"
  if [ ! -f "${afile}" ]; then
    printf '{"events":[],"count":0,"max":%s}\n' "${COORD_GATE_DEFAULT_AUDIT_MAX}"
    return 0
  fi
  python3 - "${afile}" "${n}" <<'PY' 2>/dev/null
import json, sys
afile, n_s = sys.argv[1], sys.argv[2]
try:
    n = int(n_s) if n_s else 0
except ValueError:
    n = 0
try:
    with open(afile, "r", encoding="utf-8") as f:
        data = json.load(f)
except (OSError, json.JSONDecodeError):
    print(json.dumps({"events": [], "count": 0, "max": 0}))
    sys.exit(0)
events = data.get("events", []) if isinstance(data, dict) else data
if not isinstance(events, list):
    events = []
recent = events[::-1]
if n > 0:
    recent = recent[:n]
max_seen = data.get("max", 0) if isinstance(data, dict) else 0
print(json.dumps({"events": recent, "count": len(events), "max": max_seen}, sort_keys=True))
PY
}

# Auto-source the registry if we were loaded standalone, so the gate can
# reach _coord_registry_state_file without forcing every caller to source
# both libraries explicitly.
if [ -z "${_COORD_REGISTRY_SOURCED:-}" ]; then
  _coord_gate_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -f "${_coord_gate_root}/coord_registry.sh" ]; then
    # shellcheck source=coord_registry.sh
    source "${_coord_gate_root}/coord_registry.sh"
  fi
fi
