"""``mini-ork supervise`` — the self-driving sweep, under one lease.

One sweep (``--once``) or a loop of sweeps closes the three parked-run gaps the
recovery loop leaves behind (see :mod:`mini_ork.recovery.supervisor`): revive a
run whose fix epic landed, label the diagnosis once the revived run's outcome is
known, and clear an orphaned ``repairing`` state. The whole run holds the
``__supervisor__`` lease, so two supervisors never sweep the same home at once.

Two tiers, one command:

* **Simple** — ``mini-ork supervise --once`` from cron/launchd against the local
  SQLite DB. No daemon, no server.
* **Service** — ``mini-ork supervise --interval 300`` as a long-running process
  against the shared DB. The operator is *notified* through the oversight inbox,
  never blocked on it.

Config is optional: ``--config PATH`` or ``MO_SUPERVISE_CONFIG`` names a YAML
mapping with ``interval_s`` / ``max_revives`` / ``orphan_after_s``. A named path
that is missing or unparseable is a hard error (exit 2) — never a silent
default. With no config named, the built-in defaults apply.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from mini_ork.recovery import supervisor
from mini_ork.stores import lease

LEASE_KEY = "__supervisor__"
LEASE_MESSAGE = "another supervisor holds the lease"
DEFAULTS = {
    "interval_s": 300,
    "max_revives": 2,
    "orphan_after_s": 3600,
    "dispatch_epics": {"enabled": False, "max_in_progress": 1, "only": []},
}
_SCALAR_KEYS = ("interval_s", "max_revives", "orphan_after_s")
_MIN_TTL_S = 900


class ConfigError(Exception):
    """A named config path that cannot be read or parsed; the caller exits 2."""


def _load_config(path: str) -> dict:
    """Read the YAML config, failing closed on a named-but-broken path."""
    import yaml  # noqa: PLC0415 — PyYAML is a mandatory runtime dep

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read supervise config {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"cannot parse supervise config {path}: {exc}") from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"cannot parse supervise config {path}: expected a mapping")
    out = dict(DEFAULTS)
    for key in _SCALAR_KEYS:
        if key not in data or data[key] is None:
            continue
        try:
            out[key] = int(data[key])
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"supervise config {path}: {key} must be an integer") from exc
    out["dispatch_epics"] = _parse_dispatch_block(
        path, data.get("dispatch_epics"))
    return out


def _parse_dispatch_block(path: str, raw) -> dict:
    """Parse and validate R8's ``dispatch_epics`` block, failing closed.

    Absent → ``enabled: false``. A wrong type on any sub-key raises
    :class:`ConfigError` naming the key, so the caller exits 2 rather than
    silently running a paid dispatch with a value the operator did not mean.
    """
    block = {"enabled": False, "max_in_progress": 1, "only": []}
    if raw is None:
        return block
    if not isinstance(raw, dict):
        raise ConfigError(
            f"supervise config {path}: dispatch_epics must be a mapping")

    if raw.get("enabled") is not None:
        enabled = raw["enabled"]
        if not isinstance(enabled, bool):
            raise ConfigError(
                f"supervise config {path}:"
                " dispatch_epics.enabled must be a bool")
        block["enabled"] = enabled

    if raw.get("max_in_progress") is not None:
        cap = raw["max_in_progress"]
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
            raise ConfigError(
                f"supervise config {path}:"
                " dispatch_epics.max_in_progress must be an integer >= 1")
        block["max_in_progress"] = cap

    if raw.get("only") is not None:
        only = raw["only"]
        if (not isinstance(only, list)
                or not all(isinstance(item, str) for item in only)):
            raise ConfigError(
                f"supervise config {path}:"
                " dispatch_epics.only must be a list of strings")
        block["only"] = list(only)

    return block


def _resolve_paths() -> tuple[str, str, str]:
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    db = os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
    engine_root = os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    return home, db, engine_root


def _emit(actions: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(actions))
        return
    for action in actions:
        run_id = action.get("run_id", "?")
        verb = action.get("action", "?")
        reason = action.get("reason", "")
        print(f"{run_id}: {verb} — {reason}".rstrip(" —"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mini-ork supervise",
        description="Sweep parked self-driving runs under one lease.")
    parser.add_argument("--once", action="store_true",
                        help="run one sweep and exit")
    parser.add_argument("--interval", type=int, default=None,
                        help="seconds between sweeps when looping (default 300)")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute actions without writing or spawning")
    parser.add_argument("--json", action="store_true",
                        help="print the action list as JSON")
    parser.add_argument("--config", default=None,
                        help="YAML config path (or MO_SUPERVISE_CONFIG)")
    args = parser.parse_args(argv)

    config_path = args.config or os.environ.get("MO_SUPERVISE_CONFIG") or ""
    try:
        config = _load_config(config_path) if config_path else dict(DEFAULTS)
    except ConfigError as exc:
        sys.stderr.write(f"supervise: {exc}\n")
        return 2

    interval = args.interval if args.interval is not None else int(
        config["interval_s"])
    interval = max(1, int(interval))
    ttl_s = max(_MIN_TTL_S, 3 * interval)

    home, db, engine_root = _resolve_paths()
    token = lease.acquire_lease(db, LEASE_KEY, ttl_s=ttl_s)
    if not token:
        print(LEASE_MESSAGE)
        return 0

    def run_sweep() -> list[dict]:
        dispatch = config["dispatch_epics"]
        return supervisor.sweep(
            home, db,
            engine_root=engine_root,
            dry_run=args.dry_run,
            max_revives=int(config["max_revives"]),
            orphan_after_s=int(config["orphan_after_s"]),
            dispatch_epics=bool(dispatch["enabled"]),
            dispatch_max_in_progress=int(dispatch["max_in_progress"]),
            dispatch_only=list(dispatch["only"]),
        )

    try:
        if args.once:
            actions = run_sweep()
            _emit(actions, args.json)
            return 1 if any(a.get("action") == "error" for a in actions) else 0

        while True:
            actions = run_sweep()
            _emit(actions, args.json)
            if not lease.refresh_lease(db, LEASE_KEY, token, ttl_s=ttl_s):
                # Lost the lease to a newer supervisor: stand down, don't fight.
                print(LEASE_MESSAGE)
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        lease.release_lease(db, LEASE_KEY, token)


if __name__ == "__main__":
    sys.exit(main())
