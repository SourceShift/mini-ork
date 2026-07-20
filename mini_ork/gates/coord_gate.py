"""coord_gate — Python port of lib/coord_gate.sh.

Faithful port of the PreToolUse-style coordination gate: the public bash API
(``coord_gate_check`` / ``coord_gate_metrics`` / ``coord_gate_metrics_field`` /
``coord_gate_audit`` / ``coord_gate_record_deadlock`` /
``coord_gate_record_ttl_expiration``) plus the internal bump / audit /
observe / probe helpers. Mirrors the embedded Python heredocs inside
``lib/coord_gate.sh`` exactly — same flock semantics, same default schema,
same snapshot-vs-delta counter split.

Co-existence model (strangler-fig): ``lib/coord_gate.sh`` stays
byte-identical. This port gives Python callers an in-process target and
gives ``tests/unit/test_coord_gate_py.py`` a stable surface to diff against
the LIVE bash subprocess (no mocks, no hardcoded outputs).

Env knobs (bash reads these at function entry; the Python port takes them as
explicit kwargs so callers/tests can pin them — the parity test passes the
same values it exports to the bash subprocess). Reading happens at function
entry, NOT at import time — matches bash semantics:

  COORD_GATE_MODE               → mode_override     (default "advisory")
  COORD_GATE_SCOPE              → scope_override    (default "/")
  COORD_GATE_METRICS_FILE       → explicit metrics path override
  COORD_GATE_AUDIT_FILE         → explicit audit path override
  COORD_GATE_AUDIT_MAX          → audit_max         (default 64)
  COORD_REGISTRY_STATE_FILE     → registry state file (skip fallback chain)
  MINI_ORK_RUN_DIR              → state base dir (run-scoped override)
  MINI_ORK_HOME                 → state base dir (user override)
  HOME                          → ~/.mini-ork fallback

Two exit-code paths preserved verbatim from bash:
  rc=0   advisory path (with nudge on conflict, or no conflict)
  rc=11  strict deny (path inside COORD_GATE_SCOPE)
  rc=2   usage / argument validation failure

Public surface:
    coord_gate_check(agent, path, mode, *,
                     mode_override=None, scope_override=None)
        -> tuple[str, str, int]          # (stdout, stderr, rc)
    coord_gate_metrics(*,
                       mode_override=None, scope_override=None) -> str
    coord_gate_metrics_field(name, default=0,
                             mode_override=None, scope_override=None) -> int
    coord_gate_audit(n=0,
                     mode_override=None, scope_override=None) -> str
    coord_gate_record_deadlock(
            mode_override=None, scope_override=None) -> None
    coord_gate_record_ttl_expiration(
            delta=1, mode_override=None, scope_override=None) -> None
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from typing import Any

__all__ = [
    "coord_gate_check",
    "coord_gate_metrics",
    "coord_gate_metrics_field",
    "coord_gate_audit",
    "coord_gate_record_deadlock",
    "coord_gate_record_ttl_expiration",
]

DEFAULT_AUDIT_MAX = 64

DEFAULT_SCHEMA: dict[str, int] = {
    "coord_leases_held": 0,
    "coord_queue_depth": 0,
    "coord_deadlocks_broken": 0,
    "coord_ttl_expirations": 0,
}


# ── path resolvers ──────────────────────────────────────────────────────────
# Fallback chain mirrors lib/coord_registry.sh::_coord_registry_state_file:
#   COORD_REGISTRY_STATE_FILE  (explicit)   <- metrics/audit only honour
#                                            their own COORD_GATE_*_FILE knob
#   MINI_ORK_RUN_DIR           (run-scoped)
#   MINI_ORK_HOME              (user)
#   HOME/.mini-ork             (default)
#   /tmp/coord-gate/*          (last resort)


def _state_base() -> str | None:
    if os.environ.get("MINI_ORK_RUN_DIR"):
        return os.environ["MINI_ORK_RUN_DIR"]
    if os.environ.get("MINI_ORK_HOME"):
        return os.environ["MINI_ORK_HOME"]
    home = os.environ.get("HOME")
    if home:
        return home + "/.mini-ork"
    return None


def _coord_gate_metrics_file() -> str:
    explicit = os.environ.get("COORD_GATE_METRICS_FILE")
    if explicit:
        return explicit
    base = _state_base()
    if base:
        return base + "/state/coord-gate/metrics.json"
    return "/tmp/coord-gate/metrics.json"


def _coord_gate_audit_file() -> str:
    explicit = os.environ.get("COORD_GATE_AUDIT_FILE")
    if explicit:
        return explicit
    base = _state_base()
    if base:
        return base + "/state/coord-gate/audit.json"
    return "/tmp/coord-gate/audit.json"


def _coord_gate_lock_file() -> str:
    return _coord_gate_metrics_file() + ".lock"


def _coord_gate_ensure_dirs() -> None:
    mfile = _coord_gate_metrics_file()
    afile = _coord_gate_audit_file()
    os.makedirs(os.path.dirname(mfile), exist_ok=True)
    os.makedirs(os.path.dirname(afile), exist_ok=True)
    # Match bash: truncate the lockfile to a fresh empty file.
    open(_coord_gate_lock_file(), "w").close()


# ── counter bump (atomic) ───────────────────────────────────────────────────


def _bump_counter(counter: str, delta: int = 1, *, mfile: str | None = None,
                  lfile: str | None = None) -> None:
    """Increment `counter` by `delta` under an exclusive flock.

    Mirrors the bash heredoc at lines 104-151 of lib/coord_gate.sh:
      - default schema merge preserves every counter at every increment
      - bad-int delta silently coerces to 1 (matches bash `int(...) else 1`)
      - tmp+os.replace makes the publish atomic
    """
    _coord_gate_ensure_dirs()
    if mfile is None:
        mfile = _coord_gate_metrics_file()
    if lfile is None:
        lfile = _coord_gate_lock_file()
    try:
        coerced = int(delta)
    except (TypeError, ValueError):
        coerced = 1

    os.makedirs(os.path.dirname(mfile), exist_ok=True)
    with open(lfile, "w", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            data: dict[str, Any] = {}
            if os.path.exists(mfile):
                try:
                    with open(mfile, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        data = loaded
                except (OSError, json.JSONDecodeError):
                    data = {}
            merged = dict(DEFAULT_SCHEMA)
            merged.update({k: int(v) for k, v in data.items() if isinstance(v, int)})
            merged[counter] = merged.get(counter, 0) + coerced
            tmp = mfile + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(merged, f, sort_keys=True)
                f.write("\n")
            os.replace(tmp, mfile)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


# ── audit append (bounded ring buffer) ──────────────────────────────────────


def _resolve_audit_max(env_value: str | None = None,
                       stored_max: int | None = None) -> int:
    """Resolution order matches bash lines 162-165 + 192-193 fallback chain:
    env value → stored file max → DEFAULT_AUDIT_MAX. Non-positive / non-int
    values collapse to the default."""
    raw = env_value if env_value is not None else os.environ.get(
        "COORD_GATE_AUDIT_MAX")
    if raw is None or raw == "":
        candidate: int | None = None
    else:
        try:
            candidate = int(raw)
        except (TypeError, ValueError):
            candidate = None
    if candidate is not None and candidate > 0:
        return candidate
    if stored_max is not None and stored_max > 0:
        return stored_max
    return DEFAULT_AUDIT_MAX


def _append_audit(record: Any, *, afile: str | None = None,
                  lfile: str | None = None,
                  max_records: int | None = None) -> None:
    """Append a record to the bounded ring-buffer audit file.

    Mirrors the bash heredoc at lines 156-210 of lib/coord_gate.sh:
      - 'max' is preserved inside the file so a changing env doesn't drift
        the cap mid-life (a stale-file load still honours its own max)
      - record parsing is forgiving: invalid JSON → wrapped as {"raw": ...}
      - tmp+os.replace atomic publish
    """
    _coord_gate_ensure_dirs()
    if afile is None:
        afile = _coord_gate_audit_file()
    if lfile is None:
        lfile = _coord_gate_lock_file()
    if max_records is None:
        max_records = _resolve_audit_max()

    if not isinstance(record, dict):
        record = {"raw": record}

    os.makedirs(os.path.dirname(afile), exist_ok=True)
    with open(lfile, "w", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            events: list[Any] = []
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
            if len(events) > max_seen:
                events = events[-max_seen:]
            tmp = afile + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"events": events, "max": max_seen}, f, sort_keys=True)
                f.write("\n")
            os.replace(tmp, afile)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


# ── registry snapshot observer ──────────────────────────────────────────────


def _registry_state_path() -> str | None:
    """Resolve the registry state file path.

    Mirrors lib/coord_gate.sh lines 221-227 fallback chain:
      COORD_REGISTRY_STATE_FILE → call _coord_registry_state_file → None
    """
    explicit = os.environ.get("COORD_REGISTRY_STATE_FILE")
    if explicit:
        return explicit
    try:
        from mini_ork.registries import coord_registry
    except Exception:
        return None
    resolver = getattr(coord_registry, "_coord_registry_state_file", None)
    if resolver is None:
        return None
    try:
        return resolver()
    except Exception:
        return None


def _observe_registry(*, mfile: str | None = None,
                      lfile: str | None = None,
                      state_file: str | None = None) -> None:
    """Snapshot the registry and overwrite leases_held / queue_depth.

    Mirrors the bash heredoc at lines 215-301 of lib/coord_gate.sh:
      - snapshot-derived counters overwrite (NOT merge) so the gate's view
        stays in sync with the registry's truth
      - coord_deadlocks_broken / coord_ttl_expirations are NOT touched
        here (clamped to >=0 in case observe fires after a manual reset)
      - live_waits = waiters with at least one active blocker (frc-b6 fix:
        pure waiters no longer collapse to 0)
    """
    if mfile is None:
        mfile = _coord_gate_metrics_file()
    if lfile is None:
        lfile = _coord_gate_lock_file()
    if state_file is None:
        state_file = _registry_state_path()

    now = int(time.time())
    leases: dict[str, Any] = {}
    waits: dict[str, Any] = {}
    if state_file and os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                if isinstance(data.get("leases"), dict):
                    leases = data["leases"]
                if isinstance(data.get("waits"), dict):
                    waits = data["waits"]
        except (OSError, json.JSONDecodeError):
            leases, waits = {}, {}

    active = {
        lid: rec for lid, rec in leases.items()
        if int(rec.get("expires_at", 0)) > now
    }
    active_agents = {str(r.get("agent", "")) for r in active.values()}
    # frc-b6 fix: pure waiters count when at least one blocker is active,
    # not only when the waiter itself is active.
    live_waits = {
        w: bs for w, bs in waits.items()
        if any(b in active_agents for b in (bs or []))
    }

    os.makedirs(os.path.dirname(mfile), exist_ok=True)
    with open(lfile, "w", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            merged: dict[str, Any] = dict(DEFAULT_SCHEMA)
            if os.path.exists(mfile):
                try:
                    with open(mfile, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        merged.update({k: int(v) for k, v in loaded.items() if isinstance(v, int)})
                except (OSError, json.JSONDecodeError):
                    pass
            # Snapshot overwrite for the two derived counters.
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


# ── conflict probe (read-only) ──────────────────────────────────────────────


def _normalize_path(p: str) -> str:
    """Component-aligned normalization for path-prefix overlap checks.

    Mirrors bash heredoc lines 324-332 of lib/coord_gate.sh:
      - strips trailing slashes (but keeps root "/")
      - collapses double slashes (including the root "//" → "/" hazard)
      - prefixes leading slash
    """
    p = p.strip()
    while "//" in p:
        p = p.replace("//", "/")
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    if not p.startswith("/"):
        p = "/" + p
    return p


def _overlaps(a: str, b: str) -> bool:
    """Component-aligned prefix-overlap. frc-b6 fix: don't double the root."""
    if a == b:
        return True
    a_slash = a if a.endswith("/") else a + "/"
    b_slash = b if b.endswith("/") else b + "/"
    return a_slash.startswith(b_slash) or b_slash.startswith(a_slash)


def _mode_conflicts(existing: str, new: str) -> bool:
    """read+read is the only non-conflicting combination (matches bash)."""
    return not (existing == "read" and new == "read")


def _probe_conflict(agent: str, path: str, mode: str, *,
                    state_file: str | None = None) -> tuple[int, str]:
    """Return (rc, holder) where rc=1 means conflict and holder is the
    blocking agent; rc=0 means no conflict. Registry I/O failures fall
    back to "no conflict" (matches bash fail-open semantics)."""
    if state_file is None:
        state_file = os.environ.get("COORD_REGISTRY_STATE_FILE") or ""
        if not state_file:
            try:
                from mini_ork.registries import coord_registry
                resolver = getattr(coord_registry, "_coord_registry_state_file", None)
                if resolver is not None:
                    state_file = resolver()
            except Exception:
                state_file = ""
    if not state_file or not os.path.exists(state_file):
        return 0, ""
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return 0, ""

    now = int(time.time())
    leases = data.get("leases", {}) if isinstance(data.get("leases"), dict) else {}
    npath = _normalize_path(path)
    for rec in leases.values():
        if int(rec.get("expires_at", 0)) <= now:
            continue
        if not _overlaps(str(rec.get("path", "")), npath):
            continue
        if not _mode_conflicts(str(rec.get("mode", "write")), mode):
            continue
        holder = str(rec.get("agent", ""))
        return 1, holder
    return 0, ""


# ── public API ──────────────────────────────────────────────────────────────


def _read_gate_mode(mode_override: str | None = None) -> str:
    return mode_override if mode_override is not None else os.environ.get(
        "COORD_GATE_MODE", "advisory")


def _read_gate_scope(scope_override: str | None = None) -> str:
    return scope_override if scope_override is not None else os.environ.get(
        "COORD_GATE_SCOPE", "/")


def _path_in_scope(path: str, scope: str) -> bool:
    """Match bash lines 444-449: root "/" covers everything; otherwise exact
    or component-aligned prefix match."""
    if scope == "/":
        return True
    trimmed = scope.rstrip("/")
    return path == trimmed or path.startswith(trimmed + "/")


def coord_gate_check(agent: str, path: str, mode: str, *,
                     mode_override: str | None = None,
                     scope_override: str | None = None
                     ) -> tuple[str, str, int]:
    """PreToolUse-style gate. Returns (stdout, stderr, rc).

    Exit codes mirror bash exactly:
      0   advisory (with nudge on conflict) or no conflict
      11  strict deny when path is inside COORD_GATE_SCOPE
      2   usage / argument validation failure
    """
    if not agent or not path or not mode:
        return ("", "coord_gate_check: usage: "
                "coord_gate_check <agent> <path> <mode>\n", 2)
    if mode != "read" and mode != "write":
        return ("", f'coord_gate_check: mode must be "read" or "write" '
                f"(got {mode})\n", 2)

    gate_mode = _read_gate_mode(mode_override)
    gate_scope = _read_gate_scope(scope_override)
    if gate_mode != "advisory" and gate_mode != "strict":
        return ("", "coord_gate_check: COORD_GATE_MODE must be advisory or "
                f"strict (got {gate_mode})\n", 2)

    _observe_registry()
    probe_rc, probe_holder = _probe_conflict(agent, path, mode)
    if probe_rc != 1:
        return ("", "", 0)

    audit_record = {
        "event": "conflict",
        "mode": gate_mode,
        "path": path,
        "requested_mode": mode,
        "holder": probe_holder,
        "scope": gate_scope,
        "ts": int(time.time()),
    }
    _append_audit(audit_record)

    if gate_mode == "advisory":
        nudge = (f"WAIT before editing {path} — coordination lease held by "
                 f"{probe_holder} (mode={mode}, scope={gate_scope})\n")
        return ("", nudge, 0)

    if _path_in_scope(path, gate_scope):
        deny = (f"coord_gate_check: strict deny for {path} "
                f"(mode={mode}, holder={probe_holder}, scope={gate_scope})\n")
        return ("", deny, 11)

    fallback = (f"WAIT before editing {path} — coordination lease held by "
                f"{probe_holder} (mode={mode}, out of strict scope={gate_scope})\n")
    return ("", fallback, 0)


def coord_gate_metrics(*, mode_override: str | None = None,
                       scope_override: str | None = None) -> str:
    """Print current metrics JSON to stdout. Always includes all four
    counters (defaulting to 0) so downstream tooling sees a stable
    schema even on a fresh install."""
    _coord_gate_ensure_dirs()
    _observe_registry()
    mfile = _coord_gate_metrics_file()
    if os.path.exists(mfile):
        with open(mfile, "r", encoding="utf-8") as f:
            return f.read()
    return (json.dumps({"coord_leases_held": 0, "coord_queue_depth": 0,
                        "coord_deadlocks_broken": 0,
                        "coord_ttl_expirations": 0},
                       sort_keys=True) + "\n")


def coord_gate_metrics_field(name: str, default: int = 0, *,
                             mode_override: str | None = None,
                             scope_override: str | None = None) -> int:
    """Read a single metrics field by name. Returns the integer value or
    the supplied default when the file/field is missing."""
    _coord_gate_ensure_dirs()
    mfile = _coord_gate_metrics_file()
    if not os.path.exists(mfile):
        return default
    try:
        with open(mfile, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return default
    if isinstance(data, dict) and name in data:
        try:
            return int(data[name])
        except (TypeError, ValueError):
            return default
    return default


def coord_gate_audit(n: int = 0, *,
                     mode_override: str | None = None,
                     scope_override: str | None = None) -> str:
    """Return the most recent N audit records (default all), most-recent
    first. Returns a JSON object — bounded `events` buffer plus a
    `count` field for sanity-check probes."""
    _coord_gate_ensure_dirs()
    afile = _coord_gate_audit_file()
    if not os.path.exists(afile):
        return json.dumps({"events": [], "count": 0,
                           "max": _resolve_audit_max()}, sort_keys=True) + "\n"
    try:
        with open(afile, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return json.dumps({"events": [], "count": 0, "max": 0},
                          sort_keys=True) + "\n"
    if isinstance(data, dict):
        events = data.get("events", [])
        max_seen = data.get("max", 0)
        if not isinstance(events, list):
            events = []
    elif isinstance(data, list):
        events = data
        max_seen = 0
    else:
        events = []
        max_seen = 0
    recent = list(reversed(events))
    if n and n > 0:
        recent = recent[:n]
    return json.dumps({"events": recent, "count": len(events), "max": max_seen},
                      sort_keys=True) + "\n"


def coord_gate_record_deadlock(*, mode_override: str | None = None,
                               scope_override: str | None = None) -> None:
    """Bump coord_deadlocks_broken."""
    _bump_counter("coord_deadlocks_broken", 1)


def coord_gate_record_ttl_expiration(delta: int = 1, *,
                                     mode_override: str | None = None,
                                     scope_override: str | None = None) -> None:
    """Bump coord_ttl_expirations by delta (default 1)."""
    _bump_counter("coord_ttl_expirations", delta)