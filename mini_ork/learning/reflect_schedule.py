"""A scheduled reflect pass — the cadence post-run reflect leaves behind.

Reflect runs after every run (post-run) and as an in-workflow node, but nothing
runs it on a schedule. A killed post-run pass, or traces skipped while the cost
circuit was open, are never revisited. This module fills that gap with the same
two-tier shape as :mod:`mini_ork.cli.supervise`:

* **Simple** — one ``run_once`` from cron/launchd against the local SQLite home.
* **Service** — ``run_forever`` as a long-running process on the shared DB.

Two hazards a schedule has to avoid, both handled here:

* **Overlap.** The whole pass holds the ``__reflect_schedule__`` lease, so two
  scheduled passes never overlap (the second prints a line and exits 0).
* **Spend.** It starts paid LLM calls, so ``enabled`` defaults to ``False``.
  A window's upper bound (``MO_REFLECTION_UNTIL_TS``) is ``now - settle_s``:
  a scheduled pass stays off traces a fresh run's post-run pass is still
  reflecting.

State (``<home>/state/reflect-schedule.json``) records the window actually
covered, so a failing pass is retried next tick instead of skipped.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time

from mini_ork.context import scoped_environ
from mini_ork.stores import lease

__all__ = [
    "DEFAULTS",
    "LEASE_KEY",
    "STATE_REL",
    "load_config",
    "run_once",
    "run_forever",
]

LEASE_KEY = "__reflect_schedule__"
STATE_REL = "state/reflect-schedule.json"
DEFAULTS = {
    "enabled": False,
    "interval_s": 3600,
    "deadline_s": 900,
    "settle_s": 1800,
    "max_window_s": 604800,
}
_INT_KEYS = ("interval_s", "deadline_s", "settle_s", "max_window_s")
_MIN_TTL_S = 900

LEASE_HELD_MESSAGE = "reflect schedule: another scheduled pass holds the lease"
DISABLED_MESSAGE = "reflect schedule: disabled"
NOTHING_SETTLED_MESSAGE = "reflect schedule: nothing settled yet"


class ConfigError(Exception):
    """A config path that cannot be read, parsed, or validated; exit 2."""


def _fail(path: str, detail: str) -> SystemExit:
    sys.stderr.write(f"reflect schedule: {path}: {detail}\n")
    return SystemExit(2)


def load_config(home: str, path: str | None) -> dict:
    """Read ``reflect_schedule:`` from ``path``, merged over :data:`DEFAULTS`.

    ``path`` None means ``<home>/config/reflect-schedule.yaml``; when that file
    is absent the defaults apply (so the schedule is off). A named path that is
    missing or unparseable — or a default file that exists but is broken —
    raises ``SystemExit(2)`` after a clear stderr message: fail closed, never a
    silent fallback to a paid schedule.
    """
    import yaml  # noqa: PLC0415 — PyYAML is a mandatory runtime dep

    named = path is not None
    if not named:
        path = os.path.join(home, "config", "reflect-schedule.yaml")
        if not os.path.isfile(path):
            return dict(DEFAULTS)

    assert path is not None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except OSError as exc:
        raise _fail(path, f"cannot read config ({exc})") from exc
    except yaml.YAMLError as exc:
        raise _fail(path, f"cannot parse config ({exc})") from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise _fail(path, "expected a mapping at the top level")
    block = data.get("reflect_schedule")
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise _fail(path, "reflect_schedule must be a mapping")

    out = dict(DEFAULTS)
    if block.get("enabled") is not None:
        enabled = block["enabled"]
        if not isinstance(enabled, bool):
            raise _fail(path, "reflect_schedule.enabled must be a bool")
        out["enabled"] = enabled
    for key in _INT_KEYS:
        if block.get(key) is None:
            continue
        value = block[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail(path, f"reflect_schedule.{key} must be an integer")
        out[key] = value

    # Fail closed on values a paid pass cannot honour.
    if out["interval_s"] < 1:
        raise _fail(path, "reflect_schedule.interval_s must be >= 1")
    if out["deadline_s"] < 1:
        raise _fail(path, "reflect_schedule.deadline_s must be >= 1")
    if out["settle_s"] < 0:
        raise _fail(path, "reflect_schedule.settle_s must be >= 0")
    if out["max_window_s"] < 0:
        raise _fail(path, "reflect_schedule.max_window_s must be >= 0")
    return out


# ── State under <home>/state/ (same pattern as review/gate.py) ──────────────

def _state_path(home: str) -> str:
    return os.path.join(home, STATE_REL)


def _read_state(home: str) -> dict:
    """Read the state file; missing, unreadable, or corrupt reads as absent.

    A corrupt window record must never crash the pass — the worst case is a
    pass that re-covers ``max_window_s``, which the watermark still guards.
    """
    try:
        with open(_state_path(home), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(home: str, state: dict) -> None:
    """Atomically replace the state file (tmp in the same dir + os.replace)."""
    path = _state_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _last_ok_until(state: dict) -> int | None:
    raw = state.get("last_ok_until")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


# ── One pass / the service loop ─────────────────────────────────────────────

def run_once(
    home: str,
    db: str,
    cfg: dict,
    *,
    now: int | None = None,
    reflect_fn=None,
) -> int:
    """Run at most one scheduled reflect pass and return its exit code.

    Disabled config is a no-op (no lease, no reflect). The lease is acquired
    before any paid work and released in ``finally``; a lease held elsewhere
    prints one line and returns 0, so a cron overlap is a no-op. The window is
    ``since = max(last_ok_until, now - max_window_s)`` to
    ``until = now - settle_s``; nothing settled means nothing to do. State
    advances only on exit 0, so a failed window is retried next tick.
    """
    cfg = {**DEFAULTS, **cfg}
    if not cfg.get("enabled"):
        print(DISABLED_MESSAGE)
        return 0

    now = int(now) if now is not None else int(time.time())
    deadline_s = int(cfg["deadline_s"])
    settle_s = int(cfg["settle_s"])
    max_window_s = int(cfg["max_window_s"])

    # The lease must outlive one pass: a pass that runs to its deadline must
    # not release only after the lease expired (a second tick could steal it
    # mid-pass). Wall clock only — ``now`` is the window's injected clock.
    ttl_s = max(_MIN_TTL_S, deadline_s + 60)
    # acquire_lease answers None on any sqlite error, which would read as
    # "another pass holds the lease" on an unmigrated DB and silently never
    # run. Fail closed with the real reason instead.
    if not lease.lease_tables_present(db):
        sys.stderr.write(
            f"reflect schedule: {db} has no run_leases table; "
            "run `mini-ork migrate` first\n")
        return 2
    token = lease.acquire_lease(db, LEASE_KEY, ttl_s=ttl_s)
    if not token:
        print(LEASE_HELD_MESSAGE)
        return 0

    try:
        state = _read_state(home)
        last_ok = _last_ok_until(state)
        floor = now - max_window_s
        since = max(last_ok, floor) if last_ok is not None else floor
        until = now - settle_s
        # Only a strictly inverted window has nothing to do. A zero-width
        # window (until == since) is still attempted: it selects at most the
        # traces exactly on the boundary, and treating it as "nothing settled"
        # would turn a same-second retry of a window into a silent skip.
        if until < since:
            print(NOTHING_SETTLED_MESSAGE)
            return 0

        if reflect_fn is None:
            from mini_ork.cli import reflect as reflect_cli  # noqa: PLC0415

            reflect_fn = reflect_cli.main

        overrides = {
            "MO_REFLECT_DEADLINE_S": str(deadline_s),
            "MO_REFLECTION_UNTIL_TS": str(until),
            # A scheduled pass is never run-scoped: an inherited run id would
            # route reflect through its run-scoped selector, which ignores the
            # until bound, and a post-run tag would skew timeout history.
            "MINI_ORK_REFLECT_RUN_ID": None,
            "MO_REFLECT_POST_RUN": None,
        }
        with scoped_environ(overrides):
            try:
                rc = reflect_fn(["--since", str(since)])
            except SystemExit as exc:
                # The SIGALRM deadline handler inside reflect.main raises
                # SystemExit(124) in-process. Map it to a return code so one
                # timed-out pass does not kill the service loop.
                rc = exc.code if isinstance(exc.code, int) else 1

        if rc == 0:
            _write_state(home, {"last_ok_until": until, "at": now})
        return rc
    finally:
        lease.release_lease(db, LEASE_KEY, token)


def run_forever(
    home: str,
    db: str,
    cfg: dict,
    *,
    sleep_fn=time.sleep,
    reflect_fn=None,
) -> int:
    """Loop ``run_once`` then sleep ``interval_s``; stop on Ctrl-C with 0."""
    cfg = {**DEFAULTS, **cfg}
    interval_s = max(1, int(cfg["interval_s"]))
    try:
        while True:
            run_once(home, db, cfg, reflect_fn=reflect_fn)
            sleep_fn(interval_s)
    except KeyboardInterrupt:
        return 0
