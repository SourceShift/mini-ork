"""``mini-ork memory`` — operate the fallback side of the memory backend seam.

S1 ships one verb: ``memory sync``. It replays the ``semantic_memory`` rows
marked ``pending_cn=1`` (written while ContextNest was unreachable) to CN and
clears the flag only after CN accepts each write. A row that fails stays
pending, so a partial sync never pretends to have finished
(docs/architecture/memory-backend.md §5).

Exit codes:
  0  nothing pending, or every pending row replayed
  1  CN unreachable, or at least one row could not be replayed (rows stay
     pending — the flag is never cleared on a failed write)
  2  usage error
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mini-ork memory",
        description="Operate mini-ork's memory backends (S1: pending_cn sync).",
    )
    sub = p.add_subparsers(dest="command")

    sync = sub.add_parser(
        "sync",
        help="replay semantic_memory rows written while ContextNest was down",
        description=(
            "Replay pending_cn=1 semantic_memory rows to ContextNest and clear "
            "the flag only on a successful write. Idempotent: content-hash "
            "dedupe makes a replay of an already-synced fact a no-op."
        ),
    )
    sync.add_argument(
        "--limit", type=int, default=0, metavar="N",
        help="replay at most N rows (default: all pending rows)",
    )
    sync.add_argument(
        "--dry-run", action="store_true",
        help="report how many rows are pending without writing anything",
    )
    sync.add_argument(
        "--json", action="store_true", dest="as_json",
        help="emit a machine-readable summary",
    )
    return p


def _resolve_db() -> str:
    from mini_ork.memory.semantic import _resolve_db_path

    return _resolve_db_path(None)


def _emit(args: argparse.Namespace, payload: dict) -> None:
    if args.as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        for line in payload.get("lines", []):
            print(line)


def main(rest=None, root=None) -> int:
    del root
    argv = list(sys.argv[1:] if rest is None else rest)
    args = build_parser().parse_args(argv)
    if args.command != "sync":
        build_parser().print_help()
        return 2
    return _sync(args)


def _sync(args: argparse.Namespace) -> int:
    from mini_ork import cn_client
    from mini_ork.memory import semantic
    from mini_ork.memory.backends import CnMemoryBackend

    db = _resolve_db()
    if not db or not os.path.isfile(db):
        _emit(args, {
            "status": "no_database",
            "database": db,
            "pending": 0,
            "lines": [f"memory sync: no database at {db!r} — nothing to replay"],
        })
        return 0

    # Bootstrap the schema so an older DB gains pending_cn before the query.
    conn = semantic._connect(db)
    conn.close()

    rows = semantic.pending_records(db_path=db, limit=(args.limit or None))
    if not rows:
        _emit(args, {
            "status": "clean",
            "database": db,
            "pending": 0,
            "synced": 0,
            "lines": ["memory sync: no pending rows"],
        })
        return 0

    if args.dry_run:
        _emit(args, {
            "status": "dry_run",
            "database": db,
            "pending": len(rows),
            "synced": 0,
            "lines": [
                f"memory sync: {len(rows)} row(s) pending (dry run; nothing written)"
            ],
        })
        return 0

    if not cn_client.available():
        reason = "disabled" if cn_client._disabled() else "unreachable"
        _emit(args, {
            "status": "pending",
            "database": db,
            "pending": len(rows),
            "synced": 0,
            "reason": f"cn_{reason}",
            "lines": [
                f"memory sync: ContextNest {reason}; {len(rows)} row(s) left "
                "pending (nothing is dropped, nothing pretends to have synced)"
            ],
        })
        return 1

    backend = CnMemoryBackend(db_path=db)
    synced = 0
    failures: list[dict] = []
    for row in rows:
        try:
            backend.upsert(row["text"], scope=row["scope"], key=row.get("key"))
        except Exception as exc:  # noqa: BLE001 — one bad row must not stop the replay
            failures.append({"id": row["id"], "error": f"{type(exc).__name__}: {exc}"})
            continue
        # Only a verified write clears the flag; a crash between here and the
        # UPDATE leaves the row pending, which is the safe direction.
        semantic.clear_pending(row["id"], db_path=db)
        synced += 1

    remaining = len(rows) - synced
    status = "ok" if not failures else "partial"
    _emit(args, {
        "status": status,
        "database": db,
        "pending": remaining,
        "synced": synced,
        "failures": failures,
        "lines": [
            f"memory sync: replayed {synced} row(s); {remaining} still pending"
        ] + [f"  failed id={f['id']}: {f['error']}" for f in failures],
    })
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
