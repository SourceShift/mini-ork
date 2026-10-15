"""``mini-ork approvals`` — operate the merge-approval ledger by hand.

The merge paths will wire ``merge_approval.require(...)`` later; for now this
is the only place a human acts. The verbs mirror the policy/ledger split:

  ``list [--json]``        pending ``merge:`` rows in the inbox
  ``show <run_id> [--json]``  ``status()`` for the named run
  ``approve <run_id> [--head SHA] [--note TEXT]``  resolve the pending row ``approved``
  ``reject <run_id> --note TEXT``                  resolve the pending row ``rejected``
  ``policy [--json]``    the loaded ``ReviewPolicy``

Exit codes mirror the rest of the CLI surface: 0 ok, 1 a refused action (an
``ApprovalError`` from the ledger, a missing workspace, a failed ``git
rev-parse``), 2 usage (no verb, unknown verb, missing positional).

This module is dispatched as ``python -m mini_ork.cli.approvals_cmd`` —
``test_native_module_subs_are_runnable_via_python_m`` enforces that a
``__main__`` guard is present.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

from mini_ork.gates.merge_approval import (
    ApprovalError,
    approve,
    reject,
    status,
)
from mini_ork.gates.oversight_inbox import pending
from mini_ork.gates.review_policy import load_policy

__all__ = ["main"]


_USAGE = """\
mini-ork approvals — operate the merge-approval ledger.

Usage:
  mini-ork approvals list [--json]
  mini-ork approvals show <run_id> [--json]
  mini-ork approvals approve <run_id> [--head SHA] [--note TEXT] [--base SHA]
  mini-ork approvals reject <run_id> --note TEXT
  mini-ork approvals policy [--json]

Verbs:
  list       list pending merge:* rows in mo_inbox_gates
  show       show the latest row for a run
  approve    resolve a pending row 'approved'
  reject     resolve a pending row 'rejected'
  policy     print the resolved ReviewPolicy

All verbs read from $MINI_ORK_HOME (default ./.mini-ork) and $MINI_ORK_DB.
"""


def _approver() -> str:
    """Identify who is acting. ``getpass.getuser`` can fail in a minimal
    container; fall back to ``unknown`` rather than crash a CLI."""
    try:
        return f"cli:{getpass.getuser()}"
    except (OSError, KeyError):  # noqa: BLE001 — getpass can raise anything in weird envs
        return "cli:unknown"


def _home() -> Path:
    value = os.environ.get("MINI_ORK_HOME")
    return Path(value) if value else Path(".mini-ork")


def _resolve_head(home: Path, run_id: str) -> tuple[str, str]:
    """Resolve ``(head_sha, base_sha)`` from ``mini_ork.workspaces.load(home, run_id)``.

    Returns ``("", "")`` if the workspace cannot be loaded — the caller will
    then take the explicit ``--head`` / ``--base`` path or surface the
    helpful message.
    """
    try:
        from mini_ork import workspaces  # local import: keeps the CLI usable
    except ImportError:
        return "", ""
    ws = workspaces.load(home, run_id)
    if ws is None:
        return "", ""
    try:
        completed = subprocess.run(
            ["git", "-C", str(ws.path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApprovalError(
            f"cannot resolve HEAD for {run_id!r}: {exc}"
        ) from exc
    if completed.returncode != 0:
        raise ApprovalError(
            f"git rev-parse HEAD failed for {run_id!r} at {ws.path}: "
            f"{(completed.stderr or '').strip() or 'exit ' + str(completed.returncode)}"
        )
    return completed.stdout.strip(), (ws.base_sha or "")


def _resolve_db_path(db: str | None) -> tuple[str, Path]:
    """Mirror ``mini_ork.cli.intake_cmd._targets``' shape."""
    from mini_ork.web.db import resolve_db

    home = _home()
    home.mkdir(parents=True, exist_ok=True)
    actual = resolve_db(home, db)
    Path(actual).parent.mkdir(parents=True, exist_ok=True)
    return actual, home


def _cmd_list(args: argparse.Namespace) -> int:
    db, _home_path = _resolve_db_path(args.db)
    rows = pending(db_path=db)
    rows = [r for r in rows if (r.get("gate_id") or "").startswith("merge:") and r.get("feature") == "review.before_merge"]
    if args.json:
        sys.stdout.write(json.dumps(rows, indent=2, sort_keys=True) + "\n")
        return 0
    if not rows:
        sys.stdout.write("no pending merge approvals\n")
        return 0
    for row in rows:
        ctx = row.get("context") or {}
        run_id = (row.get("gate_id") or "").partition(":")[2]
        sys.stdout.write(
            f"{row['inbox_id']:<6} {run_id}  head={ctx.get('head_sha', ''):<10} "
            f"branch={ctx.get('branch', '')}\n"
        )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    db, _home_path = _resolve_db_path(args.db)
    info = status(args.run_id, db_path=db)
    if args.json:
        sys.stdout.write(json.dumps(info, indent=2, sort_keys=True) + "\n")
        return 0
    sys.stdout.write(
        f"run_id={args.run_id}  state={info['state']}  "
        f"head_sha={info.get('head_sha', '')}  approver={info.get('approver', '')}  "
        f"inbox_id={info.get('inbox_id')}\n"
    )
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    db, home = _resolve_db_path(args.db)
    head = args.head or ""
    base = args.base or ""
    if not head:
        head, ws_base = _resolve_head(home, args.run_id)
        if not head:
            sys.stderr.write(
                f"approvals approve: no workspace for {args.run_id!r} and no "
                "--head SHA given\n"
            )
            return 1
        if not base:
            base = ws_base

    # An approve without a prior request is fine: approve() enqueues first.
    try:
        inbox_id = approve(
            args.run_id,
            head,
            approver=_approver(),
            base_sha=base,
            note=args.note or "",
            db_path=db,
            env=os.environ,
        )
    except ApprovalError as exc:
        sys.stderr.write(f"approvals approve: {exc}\n")
        return 1
    sys.stdout.write(f"approved {args.run_id} (inbox_id={inbox_id})\n")
    return 0


def _cmd_reject(args: argparse.Namespace) -> int:
    if not args.note:
        sys.stderr.write("approvals reject: --note TEXT is required\n")
        return 2
    db, _home = _resolve_db_path(args.db)
    try:
        inbox_id = reject(
            args.run_id,
            approver=_approver(),
            note=args.note,
            db_path=db,
            env=os.environ,
        )
    except ApprovalError as exc:
        sys.stderr.write(f"approvals reject: {exc}\n")
        return 1
    sys.stdout.write(f"rejected {args.run_id} (inbox_id={inbox_id})\n")
    return 0


def _cmd_policy(args: argparse.Namespace) -> int:
    policy = load_policy()
    payload = {
        "before_merge": policy.before_merge,
        "after_nodes": list(policy.after_nodes),
        "tier": policy.tier,
    }
    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return 0
    sys.stdout.write(
        f"before_merge={policy.before_merge}  "
        f"after_nodes={','.join(policy.after_nodes) or '(none)'}  "
        f"tier={policy.tier}\n"
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-ork approvals",
        add_help=False,
        usage=argparse.SUPPRESS,
    )
    parser.add_argument("--db", default=None)
    parser.add_argument("--help", action="store_true")
    subs = parser.add_subparsers(dest="verb")

    p_list = subs.add_parser("list", add_help=False, usage=argparse.SUPPRESS)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(_verb_handler=_cmd_list)

    p_show = subs.add_parser("show", add_help=False, usage=argparse.SUPPRESS)
    p_show.add_argument("run_id")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(_verb_handler=_cmd_show)

    p_approve = subs.add_parser("approve", add_help=False, usage=argparse.SUPPRESS)
    p_approve.add_argument("run_id")
    p_approve.add_argument("--head", default="")
    p_approve.add_argument("--base", default="")
    p_approve.add_argument("--note", default="")
    p_approve.set_defaults(_verb_handler=_cmd_approve)

    p_reject = subs.add_parser("reject", add_help=False, usage=argparse.SUPPRESS)
    p_reject.add_argument("run_id")
    p_reject.add_argument("--note", default="")
    p_reject.set_defaults(_verb_handler=_cmd_reject)

    p_policy = subs.add_parser("policy", add_help=False, usage=argparse.SUPPRESS)
    p_policy.add_argument("--json", action="store_true")
    p_policy.set_defaults(_verb_handler=_cmd_policy)

    return parser


def main(argv: list[str] | None = None, *, root: str | None = None) -> int:
    """Dispatch verb-first by hand, never via argparse's auto-exit.

    Unknown verbs and missing positionals print the usage to stderr and
    return 2. ``ApprovalError`` / ``ValueError`` from the ledger print to
    stderr and return 1.
    """
    del root  # the registry lives in env (MINI_ORK_HOME / MINI_ORK_DB).
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("--help", "-h", "help"):
        sys.stdout.write(_USAGE)
        return 0 if argv else 2

    # Hoist a top-level ``--db`` so it is available even if the verb lives
    # on a subparser that did not declare it (argparse binds the option to
    # whichever parser first saw it; copying ``--db`` onto every subparser
    # at build time would shadow a value the user passed at the top level).
    db_value: str | None = None
    if "--db" in argv:
        idx = argv.index("--db")
        if idx + 1 < len(argv):
            db_value = argv[idx + 1]
            argv = argv[:idx] + argv[idx + 2:]

    parser = _build_parser()
    # Inject --db onto each subparser for ``_resolve_db_path``. argparse lets us
    # add parser-level args by re-parsing with the parent's options; the
    # simplest thing is to copy --db onto every subparser at build time.
    for action in parser._subparsers._actions:  # noqa: SLF001 — argparse internals
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001 — argparse internals
            for sub in action.choices.values():
                # ``default=None`` so a verb that never saw ``--db`` still
                # resolves through the env ladder. A top-level ``--db`` is
                # already hoisted into ``db_value`` and re-injected below.
                sub.add_argument("--db", default=None)

    if db_value is not None:
        if argv:
            argv = [argv[0], "--db", db_value] + argv[1:]
        else:
            # ``approvals --db X`` with no verb left argv empty after hoisting;
            # print usage instead of crashing on ``argv[0]``.
            sys.stdout.write(_USAGE)
            return 2

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse calls sys.exit(2) on usage errors. Lift to a return value.
        code = int(getattr(exc, "code", 2) or 2)
        if code != 0:
            sys.stderr.write(_USAGE)
        return code

    if getattr(args, "help", False):
        sys.stdout.write(_USAGE)
        return 0

    handler = getattr(args, "_verb_handler", None)
    if handler is None:
        sys.stderr.write(f"approvals: unknown verb {argv[0]!r}\n\n{_USAGE}")
        return 2

    try:
        return handler(args)
    except (ApprovalError, ValueError) as exc:
        sys.stderr.write(f"approvals: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
