"""``mini-ork approvals`` — operate the merge-approval ledger by hand.

The merge paths will wire ``merge_approval.require(...)`` later; for now this
is the only place a human acts. The verbs mirror the policy/ledger split:

  ``list [--json]``        pending ``merge:`` and ``node:`` rows in the inbox
  ``show <run_id> [--json]``  ``status()`` plus the run's pending node reviews
  ``approve <run_id> [--head SHA | --node NODE_ID] [--note TEXT]``
                              resolve the pending row ``approved``
  ``reject <run_id> (--node NODE_ID)? --note TEXT``
                              resolve the pending row ``rejected`` (or the
                              ``request-changes`` alias verb)
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
import sys
from pathlib import Path

from mini_ork.gates import node_review
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
mini-ork approvals — operate the merge-approval and node-review ledger.

Usage:
  mini-ork approvals list [--json]
  mini-ork approvals show <run_id> [--json]
  mini-ork approvals approve <run_id> [--head SHA | --node NODE_ID]
                                    [--note TEXT] [--base SHA]
  mini-ork approvals reject <run_id> [--node NODE_ID] --note TEXT
  mini-ork approvals request-changes <run_id> [--node NODE_ID] --note TEXT
  mini-ork approvals policy [--json]

Verbs:
  list             list pending merge:* and node:* rows in mo_inbox_gates
  show             show the latest merge row, plus pending node reviews
  approve          resolve a pending row 'approved'
  reject           resolve a pending row 'rejected' (alias: request-changes)
  policy           print the resolved ReviewPolicy

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
    """Resolve ``(tree_sha, base_sha)`` for the run's open workspace.

    ``head_sha`` here is the **tree** sha (``git write-tree`` against a
    per-call scratch index) — the same value :func:`workspaces.merge`
    computes at write time. A CLI approval against this sha therefore
    matches the gate the merge will run, instead of an early ``HEAD`` that
    Step 1 of ``merge`` is about to overwrite with a fresh commit. Returns
    ``("", "")`` if the workspace cannot be loaded — the caller takes the
    explicit ``--head`` / ``--base`` path or surfaces the helpful message.
    """
    try:
        from mini_ork import workspaces  # local import: keeps the CLI usable
    except ImportError:
        return "", ""
    ws = workspaces.load(home, run_id)
    if ws is None:
        return "", ""
    tree = workspaces.content_sha(ws)
    if not tree:
        # ``content_sha`` returns ``""`` for any git error. An explicit
        # ``ApprovalError`` here keeps the old behaviour of failing with a
        # useful message instead of the silent "no workspace" fallback.
        raise ApprovalError(
            f"cannot compute content sha for {run_id!r} at {ws.path}"
        )
    return tree, (ws.base_sha or "")


def _resolve_db_path(db: str | None) -> tuple[str, Path]:
    """Mirror ``mini_ork.cli.intake_cmd._targets``' shape."""
    from mini_ork.web.db import resolve_db

    home = _home()
    home.mkdir(parents=True, exist_ok=True)
    actual = resolve_db(home, db)
    Path(actual).parent.mkdir(parents=True, exist_ok=True)
    return actual, home


def _cmd_list(args: argparse.Namespace) -> int:
    db, home_path = _resolve_db_path(args.db)
    rows = pending(db_path=db)
    merge_rows = [
        dict(r) for r in rows
        if (r.get("gate_id") or "").startswith("merge:")
        and r.get("feature") == "review.before_merge"
    ]
    node_rows = [
        dict(r) for r in rows
        if (r.get("gate_id") or "").startswith("node:")
        and r.get("feature") == "review.after_node"
    ]
    for r in merge_rows:
        r.setdefault("context", {}).setdefault("kind", "merge")
    for r in node_rows:
        r.setdefault("context", {}).setdefault("kind", "node")

    # Workspaces awaiting a human Merge decision. Read-only: no DB writes
    # beyond what ``is_approved`` / ``content_sha`` already do. Skipped
    # unless the policy's ``before_merge`` is ``required`` — this is the
    # run-loop-shaped gate ``workspaces.merge`` enforces at write time.
    awaiting_rows: list[dict] = []
    try:
        from mini_ork import workspaces  # local import keeps the CLI usable
        from mini_ork.gates import merge_approval
    except ImportError:
        workspaces = None  # type: ignore[assignment]
        merge_approval = None  # type: ignore[assignment]
    if workspaces is not None and merge_approval is not None:
        try:
            policy = load_policy(home=home_path)
        except (ValueError, OSError):
            policy = None
        if policy is not None and policy.before_merge == "required":
            pending_runs = {
                (row.get("gate_id") or "").partition(":")[2]
                for row in merge_rows
            }
            try:
                open_workspaces = workspaces.list_open(home_path)
            except OSError:
                open_workspaces = []
            for ws in open_workspaces:
                if ws.run_id in pending_runs:
                    continue  # already listed as a pending merge row
                tree = workspaces.content_sha(ws)
                if not tree:
                    continue
                if merge_approval.is_approved(
                    ws.run_id, tree,
                    base_sha=ws.base_sha,
                    db_path=db,
                    env=os.environ,
                ):
                    continue  # already approved against the current content
                if not _task_run_published(ws.run_id, db):
                    continue
                awaiting_rows.append({
                    "kind": "merge",
                    "state": "awaiting",
                    "run": ws.run_id,
                    "branch": ws.branch,
                    "tree": tree,
                    "base_sha": ws.base_sha,
                })

    if args.json:
        # Flat list preserves the existing output contract: callers iterate
        # ``rows`` and look at ``feature`` / ``context.kind`` to branch.
        combined = merge_rows + node_rows + awaiting_rows
        sys.stdout.write(json.dumps(combined, indent=2, sort_keys=True) + "\n")
        return 0
    if not merge_rows and not node_rows and not awaiting_rows:
        sys.stdout.write("no pending approvals\n")
        return 0
    for row in merge_rows:
        ctx = row.get("context") or {}
        run_id = ctx.get("run_id") or (row.get("gate_id") or "").partition(":")[2]
        sys.stdout.write(
            f"{row['inbox_id']:<6} merge  {run_id}  "
            f"head={ctx.get('head_sha', ''):<10} branch={ctx.get('branch', '')}\n"
        )
    for row in node_rows:
        ctx = row.get("context") or {}
        run_id = ctx.get("run_id") or (row.get("gate_id") or "").partition(":")[2]
        sys.stdout.write(
            f"{row['inbox_id']:<6} node   {run_id}  "
            f"node={ctx.get('node_id', ''):<20} "
            f"type={ctx.get('node_type', '')}\n"
        )
    for row in awaiting_rows:
        sys.stdout.write(
            f"{'—':<6} awaiting  {row['run']}  "
            f"branch={row['branch']}\n"
        )
    return 0


def _task_run_published(run_id: str, db_path: str) -> bool:
    """``True`` iff ``task_runs.status='published'`` for ``run_id``.

    The ``task_runs`` table lives behind migration 0013; pre-migration DBs
    never see this row and we simply skip the workspace (the legacy
    approval flow still gates a merge via ``merge:`` inbox rows).
    """
    import sqlite3

    try:
        con = sqlite3.connect(db_path)
    except sqlite3.OperationalError:
        return False
    try:
        try:
            row = con.execute(
                "SELECT status FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            return False
    finally:
        con.close()
    if row is None:
        return False
    return row[0] == "published"


def _cmd_show(args: argparse.Namespace) -> int:
    db, _home_path = _resolve_db_path(args.db)
    info = status(args.run_id, db_path=db)
    node_rows = node_review.pending_for_run(args.run_id, db_path=db)
    if args.json:
        payload = dict(info)
        payload["nodes"] = node_rows
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return 0
    sys.stdout.write(
        f"run_id={args.run_id}  state={info['state']}  "
        f"head_sha={info.get('head_sha', '')}  approver={info.get('approver', '')}  "
        f"inbox_id={info.get('inbox_id')}\n"
    )
    if node_rows:
        sys.stdout.write("pending node reviews:\n")
        for row in node_rows:
            ctx = row.get("context") or {}
            sys.stdout.write(
                f"  inbox_id={row['inbox_id']:<6} node={ctx.get('node_id', ''):<20} "
                f"type={ctx.get('node_type', '')}\n"
            )
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    db, home = _resolve_db_path(args.db)
    # Node review branch: --node resolves a pending node row, no head/workspace
    # required. Branch before the head-resolution block so a tmp run dir with
    # no workspace still works (DoD 9).
    if args.node:
        try:
            inbox_id = node_review.approve_node(
                args.run_id,
                args.node,
                approver=_approver(),
                note=args.note or "",
                db_path=db,
                env=os.environ,
            )
        except ApprovalError as exc:
            sys.stderr.write(f"approvals approve: {exc}\n")
            return 1
        sys.stdout.write(
            f"approved {args.run_id} node={args.node} (inbox_id={inbox_id})\n"
            f"resume with: mini-ork recover {args.run_id} --strategy resume\n"
        )
        return 0

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
    if args.node:
        try:
            inbox_id = node_review.request_changes(
                args.run_id,
                args.node,
                approver=_approver(),
                note=args.note,
                db_path=db,
                env=os.environ,
            )
        except ApprovalError as exc:
            sys.stderr.write(f"approvals reject: {exc}\n")
            return 1
        sys.stdout.write(
            f"rejected {args.run_id} node={args.node} (inbox_id={inbox_id})\n"
        )
        return 0
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
    p_approve.add_argument("--node", default="")
    p_approve.add_argument("--note", default="")
    p_approve.set_defaults(_verb_handler=_cmd_approve)

    # ``request-changes`` is registered as its own subparser so the
    # ``--db`` injection loop in ``main`` does not see the same parser
    # twice (aliases would otherwise add the option string twice and
    # trigger an ``ArgumentError`` before any verb runs).
    p_reject = subs.add_parser(
        "reject", add_help=False, usage=argparse.SUPPRESS
    )
    p_reject.add_argument("run_id")
    p_reject.add_argument("--note", default="")
    p_reject.add_argument("--node", default="")
    p_reject.set_defaults(_verb_handler=_cmd_reject)

    p_request = subs.add_parser(
        "request-changes", add_help=False, usage=argparse.SUPPRESS
    )
    p_request.add_argument("run_id")
    p_request.add_argument("--note", default="")
    p_request.add_argument("--node", default="")
    p_request.set_defaults(_verb_handler=_cmd_reject)

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
