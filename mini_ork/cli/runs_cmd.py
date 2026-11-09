"""``mini-ork runs`` — what has been delegated to mini-ork across every home.

The default listing is the current home's ``state.db``. ``--all-homes`` unions
the current home with every path in the homes registry written by
``mini_ork.orchestration.delegation.record_dispatch``, deduped by
``(home, run_id)`` and tagged with the absolute path of the ``state.db`` that
holds each row.

Two failure rules shape this command, because it is the one someone runs to
find out what is wrong:

* a pre-0091 database (no ``initiator`` column) lists with ``initiator`` shown
  as ``-`` — it must not raise;
* a missing or unreadable home never produces a non-zero exit. ``incomplete``
  in the ``--json`` payload (and one stderr line naming the home) is how a
  caller tells "no delegations" from "could not look".

Exit codes: 0 on success, 2 on a usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

from mini_ork.context import RunContext

_COLUMNS = ("run_id", "created_at", "status", "recipe", "initiator",
            "cost_usd", "artifact_path")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mini-ork runs",
                                     description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--all-homes", action="store_true",
                        help="union the current home with every registered home")
    parser.add_argument("--limit", type=int, default=None,
                        help="maximum rows per home")
    parser.add_argument("--status", default=None, help="exact status match")
    parser.add_argument("--initiator", default=None,
                        help="only rows whose initiator starts with this prefix")
    parser.add_argument("--since", default=None,
                        help="7d | 24h | YYYY-MM-DD")
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON object on stdout")
    return parser


def _parse_since(value: str, now: int) -> int | None:
    text = value.strip()
    if text.endswith("d") and text[:-1].isdigit():
        return now - int(text[:-1]) * 86400
    if text.endswith("h") and text[:-1].isdigit():
        return now - int(text[:-1]) * 3600
    try:
        return int(datetime.strptime(text, "%Y-%m-%d").timestamp())
    except ValueError:
        return None


def _read_home(db_path: str, *, status: str | None, initiator: str | None,
               since_epoch: int | None, limit: int | None) -> list[dict]:
    """Read one home. Raises ``sqlite3.Error``/``OSError`` for the caller to report."""
    con = sqlite3.connect(db_path, timeout=15.0)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(task_runs)")}
        if not columns:
            return []
        select = [
            "id AS run_id",
            "created_at",
            "status",
            ("recipe" if "recipe" in columns else "NULL") + " AS recipe",
            ("initiator" if "initiator" in columns else "NULL") + " AS initiator",
            ("cost_usd" if "cost_usd" in columns else "0") + " AS cost_usd",
            ("artifact_path" if "artifact_path" in columns else "NULL") + " AS artifact_path",
        ]
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if since_epoch is not None:
            clauses.append("created_at >= ?")
            params.append(since_epoch)
        sql = "SELECT " + ", ".join(select) + " FROM task_runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id ASC"
        rows = [dict(zip(_COLUMNS, row)) for row in con.execute(sql, params).fetchall()]
    finally:
        con.close()

    if initiator is not None:
        # Filter in Python: the column may be absent on a pre-0091 home, and a
        # pre-0091 home simply has no row that can match a prefix.
        if "initiator" not in columns:
            return []
        rows = [row for row in rows
                if isinstance(row["initiator"], str) and row["initiator"].startswith(initiator)]
    if limit is not None:
        rows = rows[:limit]
    return rows


def _registry_homes(env_map: dict) -> tuple[str, list[str], str | None]:
    """``(path, homes, error)``; a missing registry is empty-and-complete."""
    from mini_ork.orchestration.delegation import homes_registry_path

    path = homes_registry_path(env=env_map)
    if not os.path.exists(path):
        return path, [], None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return path, [], f"unreadable homes registry {path}: {exc}"
    homes = data.get("homes") if isinstance(data, dict) else None
    if not isinstance(homes, list):
        return path, [], f"malformed homes registry {path}"
    return path, [home for home in homes if isinstance(home, str) and home], None


def _cell(row: dict, column: str) -> str:
    value = row.get(column)
    if column == "initiator" and value is None:
        return "-"
    return "" if value is None else str(value)


def _print_table(rows: list[dict]) -> None:
    widths = [len(column) for column in _COLUMNS]
    for row in rows:
        for index, column in enumerate(_COLUMNS):
            widths[index] = max(widths[index], len(_cell(row, column)))
    sys.stdout.write(
        "  ".join(column.ljust(widths[index]) for index, column in enumerate(_COLUMNS)) + "\n")
    for row in rows:
        sys.stdout.write(
            "  ".join(_cell(row, column).ljust(widths[index])
                      for index, column in enumerate(_COLUMNS)).rstrip() + "\n")


def main(argv=None, root=None) -> int:
    del root
    parser = _parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse usage errors raise SystemExit(2)
        return int(exc.code) if exc.code is not None else 0

    now = int(time.time())
    since_epoch = None
    if args.since is not None:
        since_epoch = _parse_since(args.since, now)
        if since_epoch is None:
            sys.stderr.write(f"mini-ork runs: invalid --since value: {args.since}\n")
            return 2
    if args.limit is not None and args.limit < 0:
        sys.stderr.write("mini-ork runs: --limit must be >= 0\n")
        return 2

    env_map = dict(os.environ)
    current_db = os.path.abspath(RunContext.from_env(env_map).db_or_default())

    incomplete: list[str] = []
    if args.all_homes:
        _reg_path, registered, registry_error = _registry_homes(env_map)
        if registry_error:
            incomplete.append(_reg_path)
            sys.stderr.write(f"mini-ork runs: {registry_error}\n")
        requested = [current_db] + registered
    else:
        requested = [current_db]

    homes: list[str] = []
    seen: set[str] = set()
    for home in requested:
        identity = os.path.realpath(home)
        if identity in seen:
            continue
        seen.add(identity)
        homes.append(os.path.abspath(home))

    rows: list[dict] = []
    # A run's row lives in exactly ONE home, so a run id seen twice is not two
    # runs — it is two databases claiming the same run (a copied or moved
    # state.db). Dedupe on the id alone and REPORT the collision, so the count
    # stays honest without silently hiding a real divergence.
    seen_rows: dict[str, str] = {}
    collisions: list[dict] = []
    for home in homes:
        if not os.path.isfile(home):
            incomplete.append(home)
            sys.stderr.write(f"mini-ork runs: cannot read home (missing): {home}\n")
            continue
        try:
            fetched = _read_home(home, status=args.status, initiator=args.initiator,
                                 since_epoch=since_epoch, limit=args.limit)
        except (sqlite3.Error, OSError) as exc:
            incomplete.append(home)
            sys.stderr.write(f"mini-ork runs: cannot read home {home}: {exc}\n")
            continue
        for row in fetched:
            run_id = str(row["run_id"])
            first_home = seen_rows.get(run_id)
            if first_home is not None:
                collisions.append({"run_id": run_id, "homes": [first_home, os.path.abspath(home)]})
                sys.stderr.write(
                    f"mini-ork runs: run {run_id} appears in {first_home} and "
                    f"{os.path.abspath(home)}; keeping the first\n")
                continue
            seen_rows[run_id] = os.path.abspath(home)
            row["home"] = os.path.abspath(home)
            rows.append(row)

    if args.json:
        payload = {"rows": rows, "homes": homes, "incomplete": incomplete,
                   "collisions": collisions}
        sys.stdout.write(json.dumps(payload, default=str) + "\n")
    else:
        _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
