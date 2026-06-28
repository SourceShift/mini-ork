#!/usr/bin/env bash
# coord_registry.sh — Single-host lease registry for scoped path-prefix claims.
#
# Public API:
#   coord_acquire <agent> <path> <mode> [ttl_seconds]  → lease_id on stdout
#   coord_release <lease_id>                            → exit 0 on success
#   coord_renew   <agent> <lease_id> [ttl_seconds]     → exit 0 on success
#
# Defaults and limits (exportable so callers can introspect):
#   COORD_REGISTRY_DEFAULT_TTL  = 120   seconds (used when ttl is omitted)
#   COORD_REGISTRY_MAX_TTL      = 3600  seconds (1 hour; requested ttls cap here)
#
# Conflict semantics (many-readers-or-one-writer on path-prefix overlap):
#   - "read"  + "read"   on overlapping prefixes       → ALLOW (concurrent reads)
#   - "read"  + "write"  on overlapping prefixes       → BLOCK
#   - "write" + "write"  on overlapping prefixes       → BLOCK
#   - any mode on non-overlapping prefixes             → ALLOW
#   - A blocked acquisition records waiter -> holder edges. If adding those
#     edges creates a wait-for cycle and the requester is the cycle's lowest
#     priority participant, the requester is aborted with
#     {"status":"abort","reason":"deadlock"} instead of preempting a holder.
#
# Prefix overlap definition (component-aligned):
#   - Two paths overlap iff either is a component-aligned prefix of the
#     other. "src/api" overlaps "src/api/x.rs" (the former is a prefix of
#     the latter). "src/api" does NOT overlap "src/ap" (appending "/" forces
#     strict component alignment so "src/ap/" is not a prefix of "src/api/").
#   - Mode must be exactly "read" or "write"; anything else is rejected
#     so unknown modes cannot silently bypass conflicts.
#
# Time-bounded leases (TTL self-healing):
#   - Every coord_acquire / coord_release / coord_renew loads state and
#     prunes entries whose expires_at <= now BEFORE deciding what to do.
#     A crashed holder's claim heals automatically on the next read because
#     the expired lease is treated as free — no sweeper needed.
#   - coord_renew is permitted only for the current holder. If the supplied
#     agent does not match the stored agent, renew is rejected (rc=3) so a
#     leaked lease_id cannot indefinitely extend someone else's claim.
#   - Requested ttl values are silently capped at COORD_REGISTRY_MAX_TTL
#     so a misconfigured caller cannot pin the registry forever.
#
# Other invariants:
#   - Leases are time-bounded by absolute epoch (seconds).
#   - State lives at ${COORD_REGISTRY_STATE_FILE} if set, otherwise
#     ${MINI_ORK_RUN_DIR}/state/coord-registry/leases.json, otherwise
#     ${MINI_ORK_HOME}/state/coord-registry/leases.json, otherwise
#     ${HOME}/.mini-ork/state/coord-registry/leases.json.
#   - Cross-process safety on a single host uses flock on a sibling lockfile.
#   - Lease ids are opaque, non-empty, randomly generated. coord_release on
#     an unknown id returns non-zero (use this for cleanup paths).
#   - Agent priority is optional and only used for deadlock-cycle requester
#     abort decisions. Higher numbers are higher priority. Set
#     COORD_REGISTRY_AGENT_PRIORITIES to "agent-a=10,agent-b=20" or
#     COORD_REGISTRY_AGENT_PRIORITY_<sanitized agent> for focused tests.

# Default and maximum lease TTL in seconds. Exposed for callers that want
# to introspect (e.g. compute effective ttl without re-reading the source).
COORD_REGISTRY_DEFAULT_TTL="${COORD_REGISTRY_DEFAULT_TTL:-120}"
COORD_REGISTRY_MAX_TTL="${COORD_REGISTRY_MAX_TTL:-3600}"

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

_coord_registry_state_file() {
  if [ -n "${COORD_REGISTRY_STATE_FILE:-}" ]; then
    printf '%s\n' "${COORD_REGISTRY_STATE_FILE}"
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
    printf '%s/state/coord-registry/leases.json\n' "${base}"
  else
    printf '/tmp/coord-registry/leases.json\n'
  fi
}

_coord_registry_lock_file() {
  printf '%s.lock\n' "$(_coord_registry_state_file)"
}

_coord_registry_ensure_state_dir() {
  local state_file lock_file state_dir
  state_file="$(_coord_registry_state_file)"
  state_dir="$(dirname "${state_file}")"
  lock_file="$(_coord_registry_lock_file)"
  mkdir -p "${state_dir}"
  : >"${lock_file}"
}

# _coord_registry_effective_ttl <requested_ttl>
#   Prints the ttl value to use. An empty/non-numeric/non-positive value
#   falls back to COORD_REGISTRY_DEFAULT_TTL. A value above
#   COORD_REGISTRY_MAX_TTL is capped silently so a misconfigured caller
#   cannot pin the registry forever.
_coord_registry_effective_ttl() {
  local requested="${1:-}"
  local default_ttl="${COORD_REGISTRY_DEFAULT_TTL:-120}"
  local max_ttl="${COORD_REGISTRY_MAX_TTL:-3600}"
  if ! [[ "${requested}" =~ ^[0-9]+$ ]] || [ "${requested}" -le 0 ]; then
    printf '%s\n' "${default_ttl}"
    return 0
  fi
  if [ "${requested}" -gt "${max_ttl}" ]; then
    printf '%s\n' "${max_ttl}"
    return 0
  fi
  printf '%s\n' "${requested}"
}

# desc: Acquire a scoped, time-bounded path-prefix lease for an agent.
#       Prints a non-empty lease id on stdout. Returns non-zero on conflict
#       or invalid arguments.
#       ttl_seconds is optional; when omitted/empty/non-positive the
#       default COORD_REGISTRY_DEFAULT_TTL is used, and any value above
#       COORD_REGISTRY_MAX_TTL is capped silently.
coord_acquire() {
  local agent="${1:-}"
  local path="${2:-}"
  local mode="${3:-}"
  local ttl="${4:-}"
  if [ -z "${agent}" ] || [ -z "${path}" ] || [ -z "${mode}" ]; then
    printf 'coord_acquire: usage: coord_acquire <agent> <path> <mode> [ttl_seconds]\n' >&2
    return 2
  fi
  if [ "${mode}" != "read" ] && [ "${mode}" != "write" ]; then
    printf 'coord_acquire: mode must be "read" or "write" (got %s)\n' "${mode}" >&2
    return 2
  fi
  # Only reject an explicit ttl when the caller passed one — empty/missing
  # falls back to default inside _coord_registry_effective_ttl.
  if [ -n "${ttl}" ] && { ! [[ "${ttl}" =~ ^[0-9]+$ ]] || [ "${ttl}" -le 0 ]; }; then
    printf 'coord_acquire: ttl must be a positive integer (got %s)\n' "${ttl}" >&2
    return 2
  fi
  local effective_ttl
  effective_ttl="$(_coord_registry_effective_ttl "${ttl}")"
  _coord_registry_ensure_state_dir
  local state_file lock_file
  state_file="$(_coord_registry_state_file)"
  lock_file="$(_coord_registry_lock_file)"
  COORD_REGISTRY_STATE_FILE="${state_file}" \
    python3 - "${agent}" "${path}" "${mode}" "${effective_ttl}" "${state_file}" "${lock_file}" <<'PY'
import errno
import fcntl
import json
import os
import secrets
import sys
import time

agent, path, mode, ttl_s, state_file, lock_file = sys.argv[1:7]


def normalize(p: str) -> str:
    p = p.strip()
    while "//" in p:
        p = p.replace("//", "/")
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    if not p.startswith("/"):
        p = "/" + p
    return p


def overlaps(a: str, b: str) -> bool:
    if a == b:
        return True
    # Append a single trailing slash for prefix-boundary safety, but do NOT
    # double the root ("/" + "/" == "//" made root overlap nothing) (frc-b2 fix).
    a_slash = a if a.endswith("/") else a + "/"
    b_slash = b if b.endswith("/") else b + "/"
    return a_slash.startswith(b_slash) or b_slash.startswith(a_slash)


def mode_conflicts(existing_mode: str, new_mode: str) -> bool:
    # Many-readers-or-one-writer on path-prefix overlap: R+R is the only
    # non-conflicting combination. Everything else (R+W, W+R, W+W) blocks.
    if existing_mode == "read" and new_mode == "read":
        return False
    return True


def load(path: str) -> dict:
    if not os.path.exists(path):
        return {"leases": {}, "waits": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"leases": {}, "waits": {}}
    if not isinstance(data, dict) or not isinstance(data.get("leases"), dict):
        return {"leases": {}, "waits": {}}
    if not isinstance(data.get("waits"), dict):
        data["waits"] = {}
    return data


def save(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def priority_for(agent_name: str) -> int:
    priorities = os.environ.get("COORD_REGISTRY_AGENT_PRIORITIES", "")
    for item in priorities.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key.strip() != agent_name:
            continue
        try:
            return int(value.strip())
        except ValueError:
            return 0
    # Uppercase the sanitized suffix so env vars (conventionally upper-case,
    # e.g. COORD_REGISTRY_AGENT_PRIORITY_REQUESTER_E) match lower/mixed-case
    # agent names like "requester-e" (frc-b4/b5 critic fix). Without this the
    # lookup silently missed and every agent defaulted to priority 0.
    env_key = "COORD_REGISTRY_AGENT_PRIORITY_" + "".join(
        c.upper() if c.isalnum() else "_" for c in agent_name
    )
    try:
        return int(os.environ.get(env_key, "0"))
    except ValueError:
        return 0


def prune_waits(waits: dict, active: dict) -> dict:
    active_agents = {str(rec.get("agent", "")) for rec in active.values()}
    pruned = {}
    for waiter, blockers in waits.items():
        if waiter not in active_agents:
            continue
        live_blockers = sorted({b for b in blockers if b in active_agents and b != waiter})
        if live_blockers:
            pruned[waiter] = live_blockers
    return pruned


def find_requester_cycle(graph: dict, requester: str) -> list[str] | None:
    stack: list[str] = []
    seen: set[str] = set()

    def visit(node: str) -> list[str] | None:
        if node == requester and stack:
            return stack + [node]
        if node in seen:
            return None
        seen.add(node)
        stack.append(node)
        for nxt in graph.get(node, []):
            cycle = visit(nxt)
            if cycle:
                return cycle
        stack.pop()
        return None

    return visit(requester)


now = int(time.time())
expires_at = now + int(ttl_s)
npath = normalize(path)

os.makedirs(os.path.dirname(lock_file), exist_ok=True)
with open(lock_file, "w", encoding="utf-8") as lf:
    fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
    try:
        data = load(state_file)
        leases = data["leases"]
        # Self-heal: drop expired entries before evaluating conflicts so
        # a crashed holder's claim is free on the next read.
        active = {
            lid: rec
            for lid, rec in leases.items()
            if int(rec.get("expires_at", 0)) > now
        }
        waits = prune_waits(data.get("waits", {}), active)
        blockers = []
        for rec in active.values():
            existing_path = rec.get("path", "")
            existing_mode = rec.get("mode", "write")
            if not overlaps(existing_path, npath):
                continue
            if not mode_conflicts(existing_mode, mode):
                continue
            blocker = str(rec.get("agent", ""))
            if blocker:
                blockers.append(blocker)
        if blockers:
            waits[agent] = sorted({b for b in blockers if b != agent})
            cycle = find_requester_cycle(waits, agent)
            if cycle:
                participants = sorted(set(cycle))
                # Wound-wait: the victim is the deterministic lowest-priority
                # participant (tie-broken by name). A process can only abort
                # ITSELF, so the requester aborts iff it is that victim; any
                # other (lower-priority) waiter self-wounds when it next
                # evaluates. The old `requester_priority <= lowest_priority`
                # left the deadlock intact whenever the requester was NOT the
                # lowest, and the `<=` caused equal-priority livelock (frc-b4).
                victim = min(participants, key=lambda a: (priority_for(a), a))
                if victim == agent:
                    waits.pop(agent, None)
                    save(state_file, {"leases": active, "waits": waits})
                    sys.stdout.write(json.dumps({
                        "status": "abort",
                        "reason": "deadlock",
                        "agent": agent,
                        "cycle": participants,
                    }, sort_keys=True) + "\n")
                    sys.exit(4)
            save(state_file, {"leases": active, "waits": waits})
            sys.stderr.write(
                "coord_acquire: conflict with active lease "
                f"held by {', '.join(sorted(set(blockers)))} "
                f"on path {npath} (mode={mode})\n"
            )
            sys.exit(1)
        lease_id = secrets.token_hex(8)
        active[lease_id] = {
            "lease_id": lease_id,
            "agent": agent,
            "path": npath,
            "mode": mode,
            "acquired_at": now,
            "expires_at": expires_at,
        }
        waits.pop(agent, None)
        save(state_file, {"leases": active, "waits": waits})
        sys.stdout.write(lease_id + "\n")
    finally:
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
PY
}

# desc: Release a lease by id. Returns 0 on success, 1 if the id is unknown
#       or malformed. Expired leases are pruned on touch.
coord_release() {
  local lease_id="${1:-}"
  if [ -z "${lease_id}" ]; then
    printf 'coord_release: usage: coord_release <lease_id>\n' >&2
    return 2
  fi
  if ! [[ "${lease_id}" =~ ^[A-Fa-f0-9]+$ ]]; then
    printf 'coord_release: malformed lease id\n' >&2
    return 1
  fi
  _coord_registry_ensure_state_dir
  local state_file lock_file
  state_file="$(_coord_registry_state_file)"
  lock_file="$(_coord_registry_lock_file)"
  COORD_REGISTRY_STATE_FILE="${state_file}" \
    python3 - "${lease_id}" "${state_file}" "${lock_file}" <<'PY'
import fcntl
import json
import os
import sys
import time

lease_id, state_file, lock_file = sys.argv[1:4]


def load(path: str) -> dict:
    if not os.path.exists(path):
        return {"leases": {}, "waits": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"leases": {}, "waits": {}}
    if not isinstance(data, dict) or not isinstance(data.get("leases"), dict):
        return {"leases": {}, "waits": {}}
    if not isinstance(data.get("waits"), dict):
        data["waits"] = {}
    return data


def save(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


now = int(time.time())
with open(lock_file, "w", encoding="utf-8") as lf:
    fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
    try:
        data = load(state_file)
        leases = data["leases"]
        active = {
            lid: rec
            for lid, rec in leases.items()
            if int(rec.get("expires_at", 0)) > now
        }
        if lease_id not in active:
            sys.stderr.write(f"coord_release: unknown lease id {lease_id}\n")
            sys.exit(1)
        released_agent = str(active[lease_id].get("agent", ""))
        del active[lease_id]
        active_agents = {str(rec.get("agent", "")) for rec in active.values()}
        waits = {}
        for waiter, blockers in data.get("waits", {}).items():
            if waiter == released_agent or waiter not in active_agents:
                continue
            live_blockers = sorted({
                b for b in blockers if b in active_agents and b != released_agent
            })
            if live_blockers:
                waits[waiter] = live_blockers
        save(state_file, {"leases": active, "waits": waits})
    finally:
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
PY
}

# desc: Renew an active lease owned by <agent>. Extends expires_at to
#       now + ttl_seconds (capped at COORD_REGISTRY_MAX_TTL). ttl_seconds
#       is optional and defaults to COORD_REGISTRY_DEFAULT_TTL.
#       Exit codes:
#         0  — lease extended
#         1  — lease id unknown, malformed, or already expired (pruned on read)
#         2  — usage / argument validation failure
#         3  — caller is not the current holder (a leaked lease_id cannot
#              be used to indefinitely extend someone else's claim)
coord_renew() {
  local agent="${1:-}"
  local lease_id="${2:-}"
  local ttl="${3:-}"
  if [ -z "${agent}" ] || [ -z "${lease_id}" ]; then
    printf 'coord_renew: usage: coord_renew <agent> <lease_id> [ttl_seconds]\n' >&2
    return 2
  fi
  if ! [[ "${lease_id}" =~ ^[A-Fa-f0-9]+$ ]]; then
    printf 'coord_renew: malformed lease id\n' >&2
    return 1
  fi
  if [ -n "${ttl}" ] && { ! [[ "${ttl}" =~ ^[0-9]+$ ]] || [ "${ttl}" -le 0 ]; }; then
    printf 'coord_renew: ttl must be a positive integer (got %s)\n' "${ttl}" >&2
    return 2
  fi
  local effective_ttl
  effective_ttl="$(_coord_registry_effective_ttl "${ttl}")"
  _coord_registry_ensure_state_dir
  local state_file lock_file
  state_file="$(_coord_registry_state_file)"
  lock_file="$(_coord_registry_lock_file)"
  COORD_REGISTRY_STATE_FILE="${state_file}" \
    python3 - "${agent}" "${lease_id}" "${effective_ttl}" "${state_file}" "${lock_file}" <<'PY'
import fcntl
import json
import os
import sys
import time

agent, lease_id, ttl_s, state_file, lock_file = sys.argv[1:6]


def load(path: str) -> dict:
    if not os.path.exists(path):
        return {"leases": {}, "waits": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"leases": {}, "waits": {}}
    if not isinstance(data, dict) or not isinstance(data.get("leases"), dict):
        return {"leases": {}, "waits": {}}
    if not isinstance(data.get("waits"), dict):
        data["waits"] = {}
    return data


def save(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


now = int(time.time())
new_expires_at = now + int(ttl_s)

os.makedirs(os.path.dirname(lock_file), exist_ok=True)
with open(lock_file, "w", encoding="utf-8") as lf:
    fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
    try:
        data = load(state_file)
        leases = data["leases"]
        # Self-heal: expired entries are pruned before lookup, so renew on
        # an expired lease returns "unknown" (rc=1) without ever mutating
        # state — that's the crashed-holder heal property.
        active = {
            lid: rec
            for lid, rec in leases.items()
            if int(rec.get("expires_at", 0)) > now
        }
        if lease_id not in active:
            sys.stderr.write(f"coord_renew: unknown or expired lease id {lease_id}\n")
            sys.exit(1)
        rec = active[lease_id]
        if rec.get("agent") != agent:
            sys.stderr.write(
                "coord_renew: caller is not the current holder "
                f"(held by {rec.get('agent')})\n"
            )
            sys.exit(3)
        rec["expires_at"] = new_expires_at
        active_agents = {str(rec.get("agent", "")) for rec in active.values()}
        waits = {}
        for waiter, blockers in data.get("waits", {}).items():
            if waiter not in active_agents:
                continue
            live_blockers = sorted({b for b in blockers if b in active_agents and b != waiter})
            if live_blockers:
                waits[waiter] = live_blockers
        save(state_file, {"leases": active, "waits": waits})
    finally:
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
PY
}
