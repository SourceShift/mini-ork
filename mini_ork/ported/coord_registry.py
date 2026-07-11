"""coord_registry — Python port of lib/coord_registry.sh.

Faithful port of the three public functions in ``lib/coord_registry.sh``:
``coord_acquire``, ``coord_release``, ``coord_renew``. Lifts the three
embedded Python heredocs out of the bash source into one importable module
while preserving every behavioural detail (exit codes, stderr messages,
state-file shape, deadlock payload, env cascade, priority lookup).

Co-existence model (strangler-fig): ``lib/coord_registry.sh`` stays
byte-identical. This port gives Python callers an in-process target and
gives ``tests/unit/test_coord_registry_py.py`` a stable surface to diff
against the LIVE bash subprocess (no mocks, no hardcoded outputs).

Exit-code contract (mirrors bash exactly):
    0  — success
    1  — conflict / unknown / malformed lease id
    2  — usage / invalid args (missing agent/path/mode, bad mode, bad ttl)
    3  — coord_renew called by non-holder (a leaked lease_id cannot
         indefinitely extend someone else's claim)
    4  — deadlock abort (requester is the lowest-priority participant in a
         wait-for cycle). Returns a dict payload:
         ``{"status":"abort","reason":"deadlock","agent":<requester>,
            "cycle":[<sorted participants including requester>]}``

Stdout/stderr contract (mirrors bash exactly):
    coord_acquire  → stdout: lease_id (hex) on success, OR JSON deadlock
                     payload on rc=4. stderr: usage / mode / ttl validation
                     on rc=2, conflict description on rc=1.
    coord_release  → stdout: empty. stderr: usage on rc=2, malformed id or
                     unknown lease id on rc=1.
    coord_renew    → stdout: empty. stderr: usage on rc=2, malformed id or
                     unknown/expired lease id on rc=1, not-the-current-holder
                     on rc=3.

State file env cascade (matches bash ``_coord_registry_state_file``):
    1. ``COORD_REGISTRY_STATE_FILE``  (explicit override)
    2. ``${MINI_ORK_RUN_DIR}/state/coord-registry/leases.json``
    3. ``${MINI_ORK_HOME}/state/coord-registry/leases.json``
    4. ``${HOME}/.mini-ork/state/coord-registry/leases.json``
    5. ``/tmp/coord-registry/leases.json``

Cross-process safety: ``fcntl.flock(LOCK_EX)`` on ``<state_file>.lock`` —
the same per-call pattern the bash heredocs use.

Public surface:
    coord_acquire(agent, path, mode, ttl=None, state_file=None)
        -> tuple[str | dict | None, int]
        Success:        (lease_id, 0)
        Conflict:       (None, 1)
        Validation:     (None, 2)
        Deadlock:       ({"status":"abort", ...}, 4)
    coord_release(lease_id, state_file=None) -> int
    coord_renew(agent, lease_id, ttl=None, state_file=None) -> int

Path normalization (matches bash heredoc exactly):
    "src/api"   → "/src/api"
    "/src/api/" → "/src/api"
    "//src//api//" → "/src/api"
    "/"         → "/" (root preserved)

Prefix overlap is component-aligned: ``src/api`` overlaps ``src/api/x.rs``
but NOT ``src/ap`` (appending "/" forces strict component alignment).

Agent priority resolution (for deadlock victim selection):
    1. ``COORD_REGISTRY_AGENT_PRIORITIES`` (comma list, "name=int")
    2. ``COORD_REGISTRY_AGENT_PRIORITY_<UPPER_SANITIZED>`` env var, where
       sanitization uppercases alnum and converts others to ``_``.
       Without this uppercase transform the lookup silently missed for
       mixed-case agent names (frc-b4/b5 critic fix).
    3. Default 0.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import sys
import time
from typing import Any

DEFAULT_TTL = 120
MAX_TTL = 3600

_HEX_RE = re.compile(r"^[A-Fa-f0-9]+$")
_INT_RE = re.compile(r"^[0-9]+$")


def _state_file_path(explicit: str | None = None) -> str:
    """Env cascade matching bash ``_coord_registry_state_file``."""
    if explicit:
        return explicit
    env_explicit = os.environ.get("COORD_REGISTRY_STATE_FILE")
    if env_explicit:
        return env_explicit
    base = ""
    if os.environ.get("MINI_ORK_RUN_DIR"):
        base = os.environ["MINI_ORK_RUN_DIR"]
    elif os.environ.get("MINI_ORK_HOME"):
        base = os.environ["MINI_ORK_HOME"]
    elif os.environ.get("HOME"):
        base = os.path.join(os.environ["HOME"], ".mini-ork")
    if base:
        return os.path.join(base, "state", "coord-registry", "leases.json")
    return "/tmp/coord-registry/leases.json"


def _lock_file_path(state_file: str) -> str:
    return state_file + ".lock"


def _ensure_state_dir(state_file: str) -> None:
    state_dir = os.path.dirname(state_file)
    if state_dir:
        os.makedirs(state_dir, exist_ok=True)
    lock_file = _lock_file_path(state_file)
    # Touch the lock file so the flock open() never races against creation.
    with open(lock_file, "a", encoding="utf-8"):
        pass


def _load_state(state_file: str) -> dict:
    if not os.path.exists(state_file):
        return {"leases": {}, "waits": {}}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"leases": {}, "waits": {}}
    if not isinstance(data, dict) or not isinstance(data.get("leases"), dict):
        return {"leases": {}, "waits": {}}
    if not isinstance(data.get("waits"), dict):
        data["waits"] = {}
    return data


def _save_state(state_file: str, data: dict) -> None:
    tmp = state_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True)
        f.write("\n")
    os.replace(tmp, state_file)


def _effective_ttl(requested: int | str | None) -> int:
    """Cap at MAX_TTL, fall back to DEFAULT_TTL on empty/non-positive/non-int."""
    default_ttl = int(os.environ.get("COORD_REGISTRY_DEFAULT_TTL", DEFAULT_TTL))
    max_ttl = int(os.environ.get("COORD_REGISTRY_MAX_TTL", MAX_TTL))
    if requested is None or requested == "":
        return default_ttl
    s = str(requested)
    if not _INT_RE.match(s) or int(s) <= 0:
        return default_ttl
    n = int(s)
    if n > max_ttl:
        return max_ttl
    return n


def _normalize_path(p: str) -> str:
    """Bash heredoc normalize byte-for-byte."""
    p = p.strip()
    while "//" in p:
        p = p.replace("//", "/")
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    if not p.startswith("/"):
        p = "/" + p
    return p


def _overlaps(a: str, b: str) -> bool:
    """Component-aligned prefix overlap (trailing-slash trick)."""
    if a == b:
        return True
    a_slash = a if a.endswith("/") else a + "/"
    b_slash = b if b.endswith("/") else b + "/"
    return a_slash.startswith(b_slash) or b_slash.startswith(a_slash)


def _mode_conflicts(existing_mode: str, new_mode: str) -> bool:
    """R+R allowed; R+W / W+R / W+W block."""
    if existing_mode == "read" and new_mode == "read":
        return False
    return True


def _priority_for(agent_name: str) -> int:
    """Read COORD_REGISTRY_AGENT_PRIORITIES list then uppercase-suffix env var."""
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


def _prune_expired(leases: dict, now: int) -> dict:
    return {
        lid: rec
        for lid, rec in leases.items()
        if int(rec.get("expires_at", 0)) > now
    }


def _prune_waits(waits: dict, active: dict) -> dict:
    active_agents = {str(rec.get("agent", "")) for rec in active.values()}
    pruned: dict[str, list[str]] = {}
    for waiter, blockers in waits.items():
        if waiter not in active_agents:
            continue
        live_blockers = sorted({b for b in blockers if b in active_agents and b != waiter})
        if live_blockers:
            pruned[waiter] = live_blockers
    return pruned


def _prune_waits_after_release(waits: dict, active: dict, released_agent: str) -> dict:
    """Release-path prune: drop the released agent from every blocker's list AND
    drop the released agent's own waiter entry (it just released, no longer
    waiting). Matches the bash heredoc in coord_release."""
    active_agents = {str(rec.get("agent", "")) for rec in active.values()}
    pruned: dict[str, list[str]] = {}
    for waiter, blockers in waits.items():
        if waiter == released_agent or waiter not in active_agents:
            continue
        live_blockers = sorted({
            b for b in blockers if b in active_agents and b != released_agent
        })
        if live_blockers:
            pruned[waiter] = live_blockers
    return pruned


def _find_requester_cycle(graph: dict, requester: str) -> list[str] | None:
    """DFS from requester; return the cycle stack (with requester repeated
    as the closing node) iff a back-edge to requester is reached."""
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


def _with_flock(lock_file: str, fn):
    """Acquire exclusive flock on ``lock_file`` and run ``fn``. Releases on exit."""
    fd = os.open(lock_file, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fn()
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def coord_acquire(
    agent: str,
    path: str,
    mode: str,
    ttl: int | str | None = None,
    state_file: str | None = None,
) -> tuple[str | dict | None, int]:
    """Acquire a scoped, time-bounded path-prefix lease for ``agent``.

    Returns ``(lease_id, 0)`` on success, ``(None, 1)`` on conflict,
    ``(None, 2)`` on invalid args, ``(abort_payload, 4)`` on deadlock.
    """
    if not agent or not path or not mode:
        sys.stderr.write(
            "coord_acquire: usage: coord_acquire <agent> <path> <mode> [ttl_seconds]\n"
        )
        return None, 2
    if mode != "read" and mode != "write":
        sys.stderr.write(
            f'coord_acquire: mode must be "read" or "write" (got {mode})\n'
        )
        return None, 2
    if ttl not in (None, "") and (
        not _INT_RE.match(str(ttl)) or int(str(ttl)) <= 0
    ):
        sys.stderr.write(
            f"coord_acquire: ttl must be a positive integer (got {ttl})\n"
        )
        return None, 2

    effective_ttl = _effective_ttl(ttl)
    sf = _state_file_path(state_file)
    lf = _lock_file_path(sf)
    _ensure_state_dir(sf)

    def _do() -> tuple[Any, int]:
        now = int(time.time())
        expires_at = now + effective_ttl
        npath = _normalize_path(path)
        data = _load_state(sf)
        leases = data["leases"]
        active = _prune_expired(leases, now)
        waits = _prune_waits(data.get("waits", {}), active)

        blockers: list[str] = []
        for rec in active.values():
            existing_path = rec.get("path", "")
            existing_mode = rec.get("mode", "write")
            if not _overlaps(existing_path, npath):
                continue
            if not _mode_conflicts(existing_mode, mode):
                continue
            blocker = str(rec.get("agent", ""))
            if blocker:
                blockers.append(blocker)

        if blockers:
            waits[agent] = sorted({b for b in blockers if b != agent})
            cycle = _find_requester_cycle(waits, agent)
            if cycle:
                participants = sorted(set(cycle))
                # Wound-wait: victim is the deterministic lowest-priority
                # participant (tie-broken by name). A process can only abort
                # ITSELF, so the requester aborts iff it is that victim; any
                # other (lower-priority) waiter self-wounds when it next
                # evaluates.
                victim = min(participants, key=lambda a: (_priority_for(a), a))
                if victim == agent:
                    waits.pop(agent, None)
                    _save_state(sf, {"leases": active, "waits": waits})
                    payload = {
                        "status": "abort",
                        "reason": "deadlock",
                        "agent": agent,
                        "cycle": participants,
                    }
                    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
                    return payload, 4
            _save_state(sf, {"leases": active, "waits": waits})
            sys.stderr.write(
                "coord_acquire: conflict with active lease "
                f"held by {', '.join(sorted(set(blockers)))} "
                f"on path {npath} (mode={mode})\n"
            )
            return None, 1

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
        _save_state(sf, {"leases": active, "waits": waits})
        sys.stdout.write(lease_id + "\n")
        return lease_id, 0

    return _with_flock(lf, _do)


def coord_release(lease_id: str, state_file: str | None = None) -> int:
    """Release a lease by id. Returns 0 on success, 1 on unknown/malformed, 2 on usage."""
    if not lease_id:
        sys.stderr.write("coord_release: usage: coord_release <lease_id>\n")
        return 2
    if not _HEX_RE.match(lease_id):
        sys.stderr.write("coord_release: malformed lease id\n")
        return 1
    sf = _state_file_path(state_file)
    lf = _lock_file_path(sf)
    _ensure_state_dir(sf)

    def _do() -> int:
        now = int(time.time())
        data = _load_state(sf)
        leases = data["leases"]
        active = _prune_expired(leases, now)
        if lease_id not in active:
            sys.stderr.write(f"coord_release: unknown lease id {lease_id}\n")
            return 1
        released_agent = str(active[lease_id].get("agent", ""))
        del active[lease_id]
        waits = _prune_waits_after_release(
            data.get("waits", {}), active, released_agent
        )
        _save_state(sf, {"leases": active, "waits": waits})
        return 0

    return _with_flock(lf, _do)


def coord_renew(
    agent: str,
    lease_id: str,
    ttl: int | str | None = None,
    state_file: str | None = None,
) -> int:
    """Renew an active lease. Returns 0/1/2/3 per the bash contract."""
    if not agent or not lease_id:
        sys.stderr.write(
            "coord_renew: usage: coord_renew <agent> <lease_id> [ttl_seconds]\n"
        )
        return 2
    if not _HEX_RE.match(lease_id):
        sys.stderr.write("coord_renew: malformed lease id\n")
        return 1
    if ttl not in (None, "") and (
        not _INT_RE.match(str(ttl)) or int(str(ttl)) <= 0
    ):
        sys.stderr.write(
            f"coord_renew: ttl must be a positive integer (got {ttl})\n"
        )
        return 2

    effective_ttl = _effective_ttl(ttl)
    sf = _state_file_path(state_file)
    lf = _lock_file_path(sf)
    _ensure_state_dir(sf)

    def _do() -> int:
        now = int(time.time())
        new_expires_at = now + effective_ttl
        data = _load_state(sf)
        leases = data["leases"]
        active = _prune_expired(leases, now)
        if lease_id not in active:
            sys.stderr.write(
                f"coord_renew: unknown or expired lease id {lease_id}\n"
            )
            return 1
        rec = active[lease_id]
        if rec.get("agent") != agent:
            sys.stderr.write(
                "coord_renew: caller is not the current holder "
                f"(held by {rec.get('agent')})\n"
            )
            return 3
        rec["expires_at"] = new_expires_at
        active_agents = {str(rec.get("agent", "")) for rec in active.values()}
        waits: dict[str, list[str]] = {}
        for waiter, blockers in data.get("waits", {}).items():
            if waiter not in active_agents:
                continue
            live_blockers = sorted({b for b in blockers if b in active_agents and b != waiter})
            if live_blockers:
                waits[waiter] = live_blockers
        _save_state(sf, {"leases": active, "waits": waits})
        return 0

    return _with_flock(lf, _do)