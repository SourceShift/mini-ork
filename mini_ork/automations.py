"""``mini_ork/automations.py`` — per-project scheduled recipe runs.

A user keeps a list of *automations* (``<home>/automations.json``) — each one
binds a recipe + kickoff + cron schedule. An OS-level tick (launchd / cron)
fires ``mini-ork automations tick --home <home>`` every minute; that tick
walks the list, and any automation whose schedule matches the current minute
(and has not already fired in it) is dispatched through
:func:`mini_ork.web.control.launch_run`, exactly the same way the
``start_run`` MCP tool does.

Why this lives next to ``launch_run`` instead of being a layer above it:
launch_run is the canonical spawn seam — it returns ``{ok, run_id, recipe,
pid, kickoff_path, log_path}`` synchronously and detaches the child via
``start_new_session=True``. Re-implementing that contract just for
automations would fork the audit trail and the inbox ingestion, so this
module threads through launch_run and only adds the schedule + worktree
plumbing that automations need.

Each fired run gets ``MO_AUTOMATION_ID=<id>`` in its env. It is audit-only:
never branch on it for trust.
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import platform
import plistlib
import re
import shlex
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# ── Constants ────────────────────────────────────────────────────────────────

#: Audit-only env var surfaced to fired runs (see module docstring).
ENV_AUTOMATION_ID = "MO_AUTOMATION_ID"

#: Filename for the JSON store under ``$MINI_ORK_HOME``.
_STORE_FILENAME = "automations.json"
#: Filename for the advisory flock that serialises tick + edit writes.
_LOCK_FILENAME = "automations.lock"
#: Filename for the per-firing JSON-line audit log (kickoff §fire).
_LOG_FILENAME = "automations.log"
#: Filename for the OS-scheduler tick stdout/stderr capture.
_TICK_LOG_FILENAME = "automations-tick.log"
#: Maximum entries kept in each automation's ``runs`` audit trail.
_MAX_RUNS = 20

#: Cron id regex — kickoff §Store.
_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,47}$")

#: Day-of-week bounds — kickoff §Store: 0 = Sunday, 7 also Sunday.
_DOW_MIN, _DOW_MAX = 0, 7

#: Number of seconds in a tick the OS scheduler installs.
_SCHEDULER_INTERVAL_SECONDS = 60


# ── Cron parsing ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CronSpec:
    """Parsed cron expression — 5 fields, local time.

    Each field is a sorted list of allowed integer values. Day-of-week is
    normalised so 7 collapses to 0 (kickoff §Store: "7 also Sunday").
    ``original`` carries the raw expression for ``describe()`` and audit
    messages.
    """

    minute: list[int]
    hour: list[int]
    dom: list[int]
    month: list[int]
    dow: list[int]
    original: str


_FIELD_BOUNDS = (
    (0, 59),    # minute
    (0, 23),    # hour
    (1, 31),    # day-of-month
    (1, 12),    # month
    (_DOW_MIN, _DOW_MAX),  # day-of-week (inclusive of 7 == Sunday)
)


def _parse_field(token: str, *, lo: int, hi: int) -> list[int]:
    """Parse one cron field into a sorted, de-duplicated list of ints.

    Supported: ``*``, ``n``, ``a-b``, ``a,b``, ``*/n``, ``a-b/n``. ``*/n`` is
    only legal when the start is ``*`` (no anchored step). Anchored step
    like ``0/5`` is rejected (not in the kickoff spec).
    """
    out: set[int] = set()
    for part in token.split(","):
        step: int | None = None
        anchored_step = False
        if "/" in part:
            base, step_str = part.split("/", 1)
            try:
                step = int(step_str)
            except ValueError as exc:
                raise ValueError(
                    f"invalid step {step_str!r} in {token!r}"
                ) from exc
            if step <= 0:
                raise ValueError(f"step must be positive in {token!r}")
            if base != "*":
                anchored_step = True
        else:
            base = part
        if anchored_step:
            # Reject ``0/5``, ``1-10/2`` is fine because it's a stepped range.
            if "-" not in base:
                raise ValueError(
                    f"invalid step {part!r} not allowed in {token!r}"
                )
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            s_str, e_str = base.split("-", 1)
            try:
                start, end = int(s_str), int(e_str)
            except ValueError as exc:
                raise ValueError(f"invalid range {base!r} in {token!r}") from exc
            if start > end:
                raise ValueError(f"range start > end in {token!r}")
        else:
            try:
                start = end = int(base)
            except ValueError as exc:
                raise ValueError(f"invalid value {base!r} in {token!r}") from exc
        for n in range(start, end + 1, step if step else 1):
            if lo <= n <= hi:
                out.add(n)
    if not out:
        raise ValueError(f"field {token!r} matches no values")
    return sorted(out)


def parse_cron(expr: str) -> CronSpec:
    """Parse a 5-field cron string into a :class:`CronSpec`.

    Raises ``ValueError`` with a plain message on bad input. Local time
    semantics (kickoff §Store).
    """
    if expr is None or not expr.strip():
        raise ValueError("cron expression is empty")
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(
            f"cron expression must have 5 fields, got {len(fields)}: {expr!r}"
        )
    parsed = [
        _parse_field(f, lo=lo, hi=hi) for f, (lo, hi) in zip(fields, _FIELD_BOUNDS)
    ]
    # Normalise DOW: cron 0 = Sunday, 7 also Sunday → Python weekday()
    # where Monday = 0 and Sunday = 6. So cron DOW X → (X + 6) % 7.
    # Reject 7 in the raw list (we already collapsed via d % 7 → 0; but
    # ensure the normalised set stays in 0..6).
    dow_raw = sorted(set(d % 7 for d in parsed[4]))
    dow = sorted({(d + 6) % 7 for d in dow_raw})
    return CronSpec(
        minute=parsed[0],
        hour=parsed[1],
        dom=parsed[2],
        month=parsed[3],
        dow=dow,
        original=expr.strip(),
    )


def next_fire(spec: CronSpec, after: _dt.datetime) -> _dt.datetime:
    """Smallest datetime ``> after`` that matches every field of ``spec``.

    DOW and DOM combine with standard cron OR semantics: when BOTH are
    restricted, fire when EITHER matches; if either is ``*``, only the
    other constrains. Iterates minute-by-minute; bounded to ~5 years to
    avoid runaway loops on pathological expressions.
    """
    # Start one minute past ``after`` at second=0, microsecond=0.
    candidate = (after + _dt.timedelta(minutes=1)).replace(
        second=0, microsecond=0
    )
    # Upper limit = +5 years. cron should always match within a year for any
    # sane expression; 5 years is a safety net that still terminates fast.
    deadline = candidate + _dt.timedelta(days=366 * 5)
    while candidate <= deadline:
        if _matches(spec, candidate):
            return candidate
        candidate += _dt.timedelta(minutes=1)
    raise ValueError(f"no fire time within 5 years for {spec.original!r}")


def describe(expr: str) -> str:
    """Human-friendly description: ``every 15 minutes``, ``every weekday at
    09:00``, ``every day at HH:MM``, else the raw expression.
    """
    try:
        spec = parse_cron(expr)
    except ValueError:
        return expr
    minutes = spec.minute
    hours = spec.hour
    doms = spec.dom
    months = spec.month
    dows = spec.dow
    full_hours = hours == list(range(0, 24))
    full_doms = doms == list(range(1, 32))
    full_months = months == list(range(1, 13))
    full_dows = dows == list(range(0, 7))
    # Evenly stepped minutes that divide the hour (*/15, 0,30, *).
    if (
        full_hours
        and full_doms
        and full_months
        and full_dows
        and len(minutes) > 1
        and minutes[0] == 0
        and 60 % (minutes[1] - minutes[0]) == 0
        and len(minutes) == 60 // (minutes[1] - minutes[0])
        and all(minutes[i] - minutes[i - 1] == minutes[1] - minutes[0]
                for i in range(1, len(minutes)))
    ):
        step = minutes[1] - minutes[0]
        return "every minute" if step == 1 else f"every {step} minutes"
    # Specific minute, every hour, every day
    if (
        full_hours
        and full_doms
        and full_months
        and full_dows
        and len(minutes) == 1
    ):
        return f"every hour at :{minutes[0]:02d}"
    # Specific HH:MM every day
    if (
        full_doms
        and full_months
        and full_dows
        and len(hours) == 1
        and len(minutes) == 1
    ):
        return f"every day at {hours[0]:02d}:{minutes[0]:02d}"
    # Weekday at a specific time
    if (
        full_doms
        and full_months
        and dows == [0, 1, 2, 3, 4]  # Mon..Fri in Python weekday()
        and len(hours) == 1
        and len(minutes) == 1
    ):
        return f"every weekday at {hours[0]:02d}:{minutes[0]:02d}"
    return expr


def due(automation: dict[str, Any], now: _dt.datetime) -> bool:
    """True when ``automation`` should fire at ``now`` (kickoff §due).

    An automation is ``due`` if it is enabled, the cron expression matches
    ``now``, and ``last_fired_at`` is strictly before the matching minute
    (so a re-tick in the same minute does not re-fire).
    """
    if not automation.get("enabled", True):
        return False
    spec = parse_cron(str(automation["schedule"]))
    minute_start = now.replace(second=0, microsecond=0)
    if not _matches(spec, now):
        return False
    last = automation.get("last_fired_at")
    if not last:
        return True
    try:
        last_dt = _dt.datetime.fromisoformat(str(last))
    except ValueError:
        return True
    return last_dt < minute_start


def _matches(spec: CronSpec, when: _dt.datetime) -> bool:
    """Single-instant cron match — same OR-on-DOW/DOM rule as next_fire()."""
    full_doms = spec.dom == list(range(_FIELD_BOUNDS[2][0], _FIELD_BOUNDS[2][1] + 1))
    full_dows = spec.dow == list(range(0, 7))
    dom_ok = when.day in spec.dom
    dow_ok = when.weekday() in spec.dow
    if full_doms or full_dows:
        day_ok = dom_ok and dow_ok
    else:
        # Both restricted: standard cron fires when EITHER matches.
        day_ok = dom_ok or dow_ok
    return (
        when.minute in spec.minute
        and when.hour in spec.hour
        and when.month in spec.month
        and day_ok
    )


# ── Store ────────────────────────────────────────────────────────────────────


def _store_path(home: Path) -> Path:
    return Path(home) / _STORE_FILENAME


def _lock_path(home: Path) -> Path:
    return Path(home) / _LOCK_FILENAME


def load(home: Path | str) -> list[dict[str, Any]]:
    """Read ``<home>/automations.json``; return its ``automations`` list.

    Missing file → ``[]``. Malformed JSON → ``[]`` with a best-effort empty
    list rather than crashing the tick (the lock file enforces write
    consistency, so a corrupted file means a process crashed mid-write;
    returning ``[]`` lets the next ``add`` rebuild it).
    """
    path = _store_path(Path(home))
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("automations") if isinstance(data, dict) else None
    return [a for a in items if isinstance(a, dict)] if isinstance(items, list) else []


def _load_for_write(home: Path) -> list[dict[str, Any]]:
    """Like :func:`load`, but a store that exists and does not parse raises.

    A malformed file means a hand edit went wrong (writes are atomic), so a
    write must not replace it with a near-empty list and lose every automation.
    """
    path = _store_path(home)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}) — fix or remove it") from exc
    items = data.get("automations") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"{path} has no \"automations\" list — fix or remove it")
    return [a for a in items if isinstance(a, dict)]


def _write_locked(home: Path, mutate: Callable[[list[dict[str, Any]]], Any]) -> dict:
    """Acquire the advisory flock, call ``mutate(items)``, atomic-rename.

    ``mutate`` may mutate the list in place (or replace it with a new
    list — both work). Returns whatever ``mutate`` returns so callers can
    surface errors without re-reading the store.
    """
    home.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(home)
    store_path = _store_path(home)
    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            try:
                items = _load_for_write(home)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            result = mutate(items)
            if isinstance(result, dict) and result.get("ok") is False:
                return result  # validation failed inside the lock: write nothing
            # Atomic write: temp + rename under the same flock.
            fd, tmp_name = tempfile.mkstemp(
                prefix=".automations.", suffix=".json.tmp", dir=str(home)
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump({"automations": items}, f, indent=2, sort_keys=True)
                    f.write("\n")
                os.replace(tmp_name, store_path)
            except OSError as exc:
                # Write/rename failed (disk full, permissions) — the original
                # store is intact and nothing was saved.
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                return {"ok": False, "error": f"could not save {store_path}: {exc}"}
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            return result
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _validate_workspace(workspace: str) -> str:
    if workspace not in {"worktree", "in-place"}:
        raise ValueError(f"workspace must be 'worktree' or 'in-place', got {workspace!r}")
    return workspace


def _validate_recipe_exists(recipe: str, home: Path) -> None:
    # Local import — recipes_catalog pulls yaml at module load; keep it
    # out of the import graph until add() actually validates.
    from mini_ork.recipes_catalog import find_recipe

    if find_recipe(recipe, home) is None:
        raise ValueError(f"recipe not found: {recipe!r}")


def add(
    home: Path | str,
    *,
    id: str,
    name: str,
    recipe: str,
    kickoff: str,
    schedule: str,
    workspace: str = "worktree",
) -> dict[str, Any]:
    """Add an automation; return ``{"ok": True, "automation": {...}}`` or
    ``{"ok": False, "error": "..."}`` (kickoff §add).
    """
    home = Path(home)
    try:
        if not isinstance(id, str) or not _ID_RE.match(id):
            raise ValueError(
                f"id must match {_ID_RE.pattern!r}, got {id!r}"
            )
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name is required")
        if not isinstance(kickoff, str) or not kickoff.strip():
            raise ValueError("kickoff is required")
        if not isinstance(recipe, str) or not recipe.strip():
            raise ValueError("recipe is required")
        _validate_workspace(workspace)
        parse_cron(schedule)  # raises ValueError with the parse message
        _validate_recipe_exists(recipe, home)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    def mutate(items: list[dict[str, Any]]) -> dict[str, Any]:
        if any(a.get("id") == id for a in items):
            return {"ok": False, "error": f"id already exists: {id}"}
        record = {
            "id": id,
            "name": name.strip(),
            "recipe": recipe.strip(),
            "kickoff": kickoff,
            "schedule": schedule.strip(),
            "workspace": workspace,
            "enabled": True,
            "created_at": _now_iso(),
            "last_fired_at": None,
            "last_run_id": None,
            "last_error": None,
            "runs": [],
        }
        items.append(record)
        return {"ok": True, "automation": record}

    return _write_locked(home, mutate)


def update(home: Path | str, automation_id: str, **fields: Any) -> dict[str, Any]:
    """Update mutable fields: enabled/schedule/kickoff/name/recipe/workspace.

    Validates each supplied field before writing; on any validation error
    returns ``{"ok": False, "error": "..."}`` and writes nothing.
    """
    home = Path(home)
    allowed = {"enabled", "schedule", "kickoff", "name", "recipe", "workspace"}
    unknown = set(fields) - allowed
    if unknown:
        return {"ok": False, "error": f"unknown fields: {sorted(unknown)}"}

    # Pre-validate to avoid a half-applied update.
    try:
        if "schedule" in fields:
            parse_cron(str(fields["schedule"]))
        if "workspace" in fields:
            _validate_workspace(str(fields["workspace"]))
        if "name" in fields and not str(fields["name"]).strip():
            raise ValueError("name cannot be empty")
        if "kickoff" in fields and not str(fields["kickoff"]).strip():
            raise ValueError("kickoff cannot be empty")
        if "recipe" in fields:
            recipe_v = str(fields["recipe"]).strip()
            if not recipe_v:
                raise ValueError("recipe cannot be empty")
            _validate_recipe_exists(recipe_v, home)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    def mutate(items: list[dict[str, Any]]) -> dict[str, Any]:
        for a in items:
            if a.get("id") == automation_id:
                for k, v in fields.items():
                    if k == "schedule":
                        a["schedule"] = str(v).strip()
                    elif k == "workspace":
                        a["workspace"] = str(v)
                    elif k == "recipe":
                        a["recipe"] = str(v).strip()
                    elif k in {"name", "kickoff"}:
                        a[k] = str(v)
                    else:
                        a[k] = v
                return {"ok": True, "automation": a}
        return {"ok": False, "error": f"unknown automation: {automation_id}"}

    return _write_locked(home, mutate)


def remove(home: Path | str, automation_id: str) -> dict[str, Any]:
    """Remove an automation by id; returns ``{"ok": True, "removed": id}``.

    Unknown id → ``{"ok": False, "error": "unknown automation: ..."}``.
    """
    home = Path(home)

    def mutate(items: list[dict[str, Any]]) -> dict[str, Any]:
        for i, a in enumerate(items):
            if a.get("id") == automation_id:
                items.pop(i)
                return {"ok": True, "removed": automation_id}
        return {"ok": False, "error": f"unknown automation: {automation_id}"}

    return _write_locked(home, mutate)


def pause(home: Path | str, automation_id: str) -> dict[str, Any]:
    """Convenience wrapper over :func:`update` with ``enabled=False``."""
    return update(home, automation_id, enabled=False)


def resume(home: Path | str, automation_id: str) -> dict[str, Any]:
    """Convenience wrapper over :func:`update` with ``enabled=True``."""
    return update(home, automation_id, enabled=True)


# ── Firing ───────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    """Local-time ISO-8601 with seconds; deterministic for tests via monkeypatching."""
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def _project_for(home: Path) -> Path:
    """The project is the directory that owns ``.mini-ork``.

    Not ``MINI_ORK_PROJECT_HOME``: the launcher sets that to the home itself
    (``<project>/.mini-ork``), so treating it as the project would point runs
    at the home directory.
    """
    return Path(home).absolute().parent


def _mint_run_id() -> str:
    """Same shape as ``control.launch_run``'s own ids (``run-<epoch>-<hex>``)."""
    return f"run-{int(time.time())}-{secrets.token_hex(3)}"


def _append_log_line(home: Path, payload: dict[str, Any]) -> None:
    path = home / _LOG_FILENAME
    line = json.dumps(payload, sort_keys=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fire(
    home: Path | str,
    automation_id: str,
    *,
    launcher: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fire ``automation_id`` immediately, regardless of schedule.

    Returns ``{"ok": True, "run_id": ..., ...}`` on launch success and
    ``{"ok": False, "error": "..."}`` on lookup / launch failures. On
    success, the automation's ``last_fired_at`` / ``last_run_id`` are
    updated under the store lock, the new run id is prepended to ``runs``
    (capped at :data:`_MAX_RUNS`), and a JSON line is appended to
    ``<home>/automations.log``.
    """
    home = Path(home)
    if launcher is None:
        from mini_ork.web.control import launch_run
        launcher = launch_run

    automations = load(home)
    target = next((a for a in automations if a.get("id") == automation_id), None)
    if target is None:
        return {"ok": False, "error": f"unknown automation: {automation_id}"}

    project = _project_for(home)
    workspace_mode = target.get("workspace", "worktree")
    rid = _mint_run_id()
    # Taken before the launch so a slow spawn cannot push the recorded time
    # into the next minute and make ``due`` skip that minute's firing.
    ts = _now_iso()
    ws = None
    if workspace_mode == "worktree" and (project / ".git").exists():
        # Lazy import: workspaces pulls git subprocess; keep it off the
        # import graph when not firing.
        from mini_ork import workspaces as _workspaces

        try:
            ws = _workspaces.create(project, home, rid)
        except RuntimeError as exc:
            # Never fall back to the user's checkout: an unattended run
            # editing it in place is exactly what worktree mode prevents.
            result = {"ok": False, "error": f"could not create a worktree: {exc}"}
        else:
            result = None
        target_cwd = ws.path if ws is not None else project
    else:
        result = None
        target_cwd = project

    if result is None:
        extra_env = {
            "MO_TARGET_CWD": str(target_cwd),
            ENV_AUTOMATION_ID: automation_id,
        }
        result = launcher(home, target["recipe"], target["kickoff"],
                          run_id=rid, extra_env=extra_env)
        if not result.get("ok") and ws is not None:
            from mini_ork import workspaces as _workspaces

            _workspaces.discard(ws)
    ok = bool(result.get("ok"))
    _append_log_line(home, {
        "ts": ts,
        "id": automation_id,
        "run_id": rid if ok else None,
        "ok": ok,
        "error": "" if ok else str(result.get("error", "")),
    })

    def mutate(items: list[dict[str, Any]]) -> dict[str, Any]:
        for a in items:
            if a.get("id") == automation_id:
                # Recorded on failure too: ``due`` fires at most once per
                # matching minute either way, and ``last_error`` shows why.
                a["last_fired_at"] = ts
                if ok:
                    a["last_run_id"] = rid
                    a["last_error"] = None
                    runs = list(a.get("runs") or [])
                    runs.insert(0, rid)
                    a["runs"] = runs[:_MAX_RUNS]
                else:
                    a["last_error"] = str(result.get("error", ""))
                break
        return {"ok": True}

    saved = _write_locked(home, mutate)
    if not ok:
        return result
    if not saved.get("ok"):
        return {"ok": False, "run_id": rid,
                "error": f"run {rid} started but was not recorded: {saved.get('error')}"}
    return {"ok": True, "run_id": rid, "target_cwd": str(target_cwd),
            "workspace": "worktree" if ws is not None else "in-place",
            "branch": ws.branch if ws is not None else None}


def last_run_status(home: Path | str, automation: dict[str, Any]) -> str:
    """``"<run id> <status>"`` for the latest firing, ``"not started: <why>"``
    when the latest firing failed to launch, ``"never"`` before the first."""
    rid = automation.get("last_run_id")
    if automation.get("last_error"):
        return f"not started: {automation['last_error']}"
    if not rid:
        return "never"
    from mini_ork.acp.history import list_runs

    try:
        rows, _ = list_runs(Path(home), limit=200)
    except Exception:  # noqa: BLE001 — a status column must not break `list`
        rows = []
    status = next((r.get("status") for r in rows if r.get("run_id") == rid), None)
    return f"{rid} {status or 'starting'}"


def tick(
    home: Path | str,
    now: _dt.datetime | None = None,
    *,
    launcher: Callable[..., dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Fire every due, enabled automation; return the firing results.

    A missed window (machine asleep) does NOT backfill — only the current
    minute counts (kickoff §tick).
    """
    home = Path(home)
    now = now or _dt.datetime.now()
    firings: list[dict[str, Any]] = []
    for a in load(home):
        if not due(a, now):
            continue
        result = fire(home, a["id"], launcher=launcher)
        firings.append({"id": a["id"], **result})
    return firings


# ── OS scheduler ─────────────────────────────────────────────────────────────


def _platform_key() -> str:
    """``"macos"`` | ``"linux"`` | ``"other"``."""
    name = platform.system().lower()
    if name == "darwin":
        return "macos"
    if name == "linux":
        return "linux"
    return "other"


def _platform_name() -> str:
    """Display name for the platform (``macos`` / ``linux`` / raw ``other``)."""
    key = _platform_key()
    if key == "macos":
        return "macos"
    if key == "linux":
        return "linux"
    return platform.system().lower() or "other"


def _home_hash(home: Path) -> str:
    """First 10 hex of sha256 of the *resolved* home path (kickoff §OS)."""
    import hashlib
    resolved = str(Path(home).resolve()).encode("utf-8")
    return hashlib.sha256(resolved).hexdigest()[:10]


def _launch_agents_dir() -> Path:
    """``~/Library/LaunchAgents`` — tests monkeypatch this seam."""
    return Path.home() / "Library" / "LaunchAgents"


def _os_run(argv: list[str], *, check: bool = True, stdin_input: str | None = None,
            **kwargs: Any) -> subprocess.CompletedProcess:
    """Run a single OS command (launchctl / crontab). Tests monkeypatch this.

    ``check=True`` (default) raises on non-zero rc; tests pass ``check=False``
    so they can inspect ``returncode`` directly. ``stdin_input`` is forwarded
    to ``subprocess.run`` as ``input=`` (for ``crontab -``); we rename to
    avoid shadowing the builtin.
    """
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        input=stdin_input,
        check=check,
        **kwargs,
    )


def _scheduler_env(home: Path) -> dict[str, str]:
    """Env for the scheduled tick.

    launchd and cron start jobs with a bare PATH (``/usr/bin:/bin``), so the
    agent CLIs a run dispatches to (claude, codex, …) would not be found.
    The PATH of the shell that installs the scheduler is captured instead.
    """
    from mini_ork.web.control import _mini_ork_root

    return {
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_ROOT": str(_mini_ork_root()),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }


def _tick_argv(home: Path) -> list[str]:
    from mini_ork.web.control import _mini_ork_root

    return [sys.executable, str(_mini_ork_root() / "bin" / "mini-ork"),
            "automations", "tick", "--home", str(home)]


def _scheduled_command(home: Path) -> str:
    """The per-tick command as one shell line (shown by ``status``)."""
    return shlex.join(_tick_argv(home))


def _crontab_line(home: Path) -> str:
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in _scheduler_env(home).items())
    log = shlex.quote(str(home / _TICK_LOG_FILENAME))
    return (f"* * * * * {env} {_scheduled_command(home)} >> {log} 2>&1 "
            f"# mini-ork-automations-marker:{_home_hash(home)}")


def _plist_path(home: Path) -> Path:
    return _launch_agents_dir() / f"ai.mini-ork.automations.{_home_hash(home)}.plist"


def _build_plist(home: Path) -> str:
    """Return the LaunchAgent XML body (``plistlib`` escapes every value)."""
    log_path = str(home / _TICK_LOG_FILENAME)
    return plistlib.dumps({
        "Label": _plist_label(home),
        "ProgramArguments": _tick_argv(home),
        "StartInterval": _SCHEDULER_INTERVAL_SECONDS,
        "RunAtLoad": False,
        "EnvironmentVariables": _scheduler_env(home),
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }, sort_keys=False).decode("utf-8")


def _plist_label(home: Path) -> str:
    return f"ai.mini-ork.automations.{_home_hash(home)}"


def install_scheduler(home: Path | str) -> dict[str, Any]:
    """Install the OS scheduler (kickoff §OS).

    macOS: LaunchAgent plist + ``launchctl bootstrap``. Linux: crontab line
    carrying the ``# mini-ork automations <hash>`` marker. Any other
    platform → ``{"ok": False, "error": "unsupported platform"}``.
    """
    home = Path(home)
    key = _platform_key()
    if key == "macos":
        agents_dir = _launch_agents_dir()
        agents_dir.mkdir(parents=True, exist_ok=True)
        plist = _plist_path(home)
        plist.write_text(_build_plist(home), encoding="utf-8")
        uid = os.getuid()
        # A re-install replaces a loaded job; bootstrap refuses a loaded label.
        _os_run(["launchctl", "bootout", f"gui/{uid}/{_plist_label(home)}"], check=False)
        bootstrap_argv = ["launchctl", "bootstrap", f"gui/{uid}", str(plist)]
        try:
            _os_run(bootstrap_argv)
        except subprocess.CalledProcessError:
            # Fall back to legacy ``launchctl load -w`` (kickoff §OS).
            try:
                _os_run(["launchctl", "load", "-w", str(plist)])
            except subprocess.CalledProcessError as exc:
                return {
                    "ok": False,
                    "error": f"launchctl bootstrap and load both failed: {exc}",
                }
        return {"ok": True, "platform": "macos", "plist": str(plist)}
    if key == "linux":
        home_hash = _home_hash(home)
        marker = f"# mini-ork-automations-marker:{home_hash}"
        new_line = _crontab_line(home) + "\n"
        try:
            existing = _os_run(["crontab", "-l"], check=False).stdout or ""
        except FileNotFoundError:
            return {"ok": False, "error": "crontab not available"}
        # Filter by the unique marker token (the home hash) so the
        # command's own ``bin/mini-ork automations tick`` doesn't match.
        lines = [
            ln for ln in existing.splitlines()
            if f"mini-ork-automations-marker:{home_hash}" not in ln
        ]
        lines.append(new_line.rstrip("\n"))
        new_payload = "\n".join(lines) + "\n"
        try:
            _os_run(["crontab", "-"], stdin_input=new_payload)
        except subprocess.CalledProcessError as exc:
            return {"ok": False, "error": f"crontab install failed: {exc}"}
        return {"ok": True, "platform": "linux", "marker": marker}
    return {"ok": False, "error": "unsupported platform"}


def uninstall_scheduler(home: Path | str) -> dict[str, Any]:
    """Remove the OS scheduler (kickoff §OS)."""
    home = Path(home)
    key = _platform_key()
    if key == "macos":
        plist = _plist_path(home)
        if not plist.is_file():
            return {"ok": True, "platform": "macos", "removed": False}
        label = _plist_label(home)
        uid = os.getuid()
        try:
            _os_run(["launchctl", "bootout", f"gui/{uid}/{label}"])
        except subprocess.CalledProcessError:
            try:
                _os_run(["launchctl", "unload", str(plist)])
            except subprocess.CalledProcessError as exc:
                return {"ok": False, "error": f"launchctl bootout/unload failed: {exc}"}
        try:
            plist.unlink()
        except OSError:
            pass
        return {"ok": True, "platform": "macos", "removed": True}
    if key == "linux":
        home_hash = _home_hash(home)
        marker = f"mini-ork-automations-marker:{home_hash}"
        try:
            existing = _os_run(["crontab", "-l"], check=False).stdout or ""
        except FileNotFoundError:
            return {"ok": False, "error": "crontab not available"}
        lines = [ln for ln in existing.splitlines() if marker not in ln]
        new_payload = "\n".join(lines) + ("\n" if lines else "")
        try:
            _os_run(["crontab", "-"], stdin_input=new_payload)
        except subprocess.CalledProcessError as exc:
            return {"ok": False, "error": f"crontab uninstall failed: {exc}"}
        return {"ok": True, "platform": "linux"}
    return {"ok": False, "error": "unsupported platform"}


def scheduler_status(home: Path | str) -> dict[str, Any]:
    """Report scheduler installation state, command, last tick time."""
    home = Path(home)
    key = _platform_key()
    name = _platform_name()
    command = _scheduled_command(home)
    log_path = home / _TICK_LOG_FILENAME
    last_tick = None
    if log_path.is_file():
        try:
            last_tick = _dt.datetime.fromtimestamp(
                log_path.stat().st_mtime
            ).replace(microsecond=0).isoformat()
        except OSError:
            last_tick = None
    if key == "macos":
        plist = _plist_path(home)
        return {
            "platform": name or "macos",
            "installed": plist.is_file(),
            "command": command,
            "plist": str(plist),
            "log_path": str(log_path),
            "last_tick": last_tick,
        }
    if key == "linux":
        home_hash = _home_hash(home)
        marker = f"mini-ork-automations-marker:{home_hash}"
        try:
            existing = _os_run(["crontab", "-l"], check=False).stdout or ""
        except FileNotFoundError:
            existing = ""
        present = marker in existing
        return {
            "platform": name or "linux",
            "installed": present,
            "command": command,
            "marker": marker,
            "log_path": str(log_path),
            "last_tick": last_tick,
        }
    return {"platform": name or "other", "installed": False, "command": command,
            "log_path": str(log_path), "last_tick": last_tick}