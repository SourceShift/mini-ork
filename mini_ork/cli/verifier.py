"""``mini-ork verifier`` — inspect and label verifier outcomes.

The operator half of the verifier-calibration loop. Every verifier node writes
a ``verifier_results`` row (``execute_handlers._record_verifier_result``); an
operator labels the wrong ones here, and ``gates.abstain_gate`` reads the
labelled history to calibrate its abstention threshold. The runner comments in
``gates/verifier_rubric.py`` and migration ``0025_verifier_rubrics.sql`` name
``mini-ork verifier annotate`` as the way to set the ground-truth
``is_false_positive`` / ``is_false_negative`` columns — but the writer
``verifier_result_annotate`` had no caller, so the labels the gate calibrates
against could never be set. This module is that missing caller.

Subcommands:
  list     [--run <id>] [--unannotated] [--limit N] [--json]
  annotate --result-id <id> --kind false_positive|false_negative
           [--annotator <who>] [--notes <text>]

Dispatched as ``python -m mini_ork.cli.verifier`` by the subcommand registry
(see ``mini_ork/cli/main.py``), so it exposes both a ``main(rest, root)`` entry
(the registry contract) and a ``__main__`` guard (the subprocess it spawns).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys


def _db_path(root: str) -> str:
    """``state.db`` path: ``MINI_ORK_DB``, else ``$MINI_ORK_HOME/state.db``.

    Mirrors ``cli/garden.py``: ``MINI_ORK_HOME`` defaults to
    ``<root>/.mini-ork`` so an operator running from a project checkout reads
    the same DB the runner wrote to.
    """
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(root or os.getcwd(), ".mini-ork")
    return os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")


def _cmd_list(args: argparse.Namespace, db: str) -> int:
    if not os.path.isfile(db):
        print(f"verifier: no state.db at {db}", file=sys.stderr)
        return 1
    where: list[str] = []
    params: list[object] = []
    if args.run:
        where.append("run_id = ?")
        params.append(args.run)
    if args.unannotated:
        where.append("is_false_positive = 0 AND is_false_negative = 0")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT result_id, run_id, verifier_name, verdict, confidence, "
            "       is_false_positive, is_false_negative, "
            "       datetime(created_at,'unixepoch','localtime') AS created_at "
            f"FROM verifier_results{clause} "
            "ORDER BY created_at DESC, result_id LIMIT ?",
            (*params, args.limit),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        print(f"verifier: {exc}", file=sys.stderr)
        return 1
    finally:
        con.close()
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return 0
    for r in rows:
        flag = "fp" if r["is_false_positive"] else ("fn" if r["is_false_negative"] else "-")
        print(f"{r['result_id']}  {r['verdict']:<13} {r['verifier_name']:<24} "
              f"{r['run_id']}  [{flag}]")
    return 0


def _result_exists(db: str, result_id: str) -> bool:
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT 1 FROM verifier_results WHERE result_id=?", (result_id,)
        ).fetchone() is not None
    finally:
        con.close()


def _cmd_annotate(args: argparse.Namespace, db: str) -> int:
    from mini_ork.gates.verifier_rubric import verifier_result_annotate  # noqa: PLC0415

    if not os.path.isfile(db):
        print(f"verifier: no state.db at {db}", file=sys.stderr)
        return 1
    # The writer's UPDATE is a silent no-op on an unknown id (it mirrors a bash
    # heredoc whose UPDATE also matches 0 rows), so an id typo would otherwise
    # print success. Fail loudly instead of recording a label that never landed.
    if not _result_exists(db, args.result_id):
        print(f"verifier: no result {args.result_id}", file=sys.stderr)
        return 1
    try:
        verifier_result_annotate(db, args.result_id, args.kind,
                                 args.annotator, args.notes or None)
    except ValueError as exc:
        # Unknown kind (the writer's own guard) — usage error.
        print(f"verifier: {exc}", file=sys.stderr)
        return 2
    except sqlite3.IntegrityError as exc:
        # Cross-flag CHECK: the row already carries the opposite label.
        print(f"verifier: {args.result_id}: {exc}", file=sys.stderr)
        return 1
    print(f"annotated {args.result_id} as {args.kind}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mini-ork verifier",
        description="Inspect and label verifier results (calibration ground truth).",
    )
    sub = p.add_subparsers(dest="cmd")
    ls = sub.add_parser("list", help="list recent verifier results")
    ls.add_argument("--run", default="", help="only this run_id")
    ls.add_argument("--unannotated", action="store_true",
                    help="only rows with no false-positive/negative label yet")
    ls.add_argument("--limit", type=int, default=50, help="max rows (default 50)")
    ls.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    an = sub.add_parser("annotate", help="label a result as a false positive or negative")
    an.add_argument("--result-id", required=True, help="the vr-… id from `list`")
    an.add_argument("--kind", required=True,
                    choices=["false_positive", "false_negative"])
    an.add_argument("--annotator", default=os.environ.get("USER", "operator"))
    an.add_argument("--notes", default="")
    return p


def main(rest: list[str], root: str) -> int:
    parser = _build_parser()
    args = parser.parse_args(rest)
    db = _db_path(root)
    if args.cmd == "list":
        return _cmd_list(args, db)
    if args.cmd == "annotate":
        return _cmd_annotate(args, db)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ.get("MINI_ORK_ROOT", os.getcwd())))
