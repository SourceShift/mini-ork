"""``mini-ork intake`` — let a ticket start work, with no server and no webhook.

Intake tier A. The trigger is a timer in this process and the transport is
outbound HTTPS, so this works on a laptop with nothing running: no ``serve``,
no public URL, no TLS, no reverse proxy. The signed-webhook tier
(``mini_ork.web.routes.intake``) buys freshness with all four of those; this one
buys reach with none.

  intake once   [--source S] [--max-runs N] [--dry-run]   one pass now
  intake poll   [--interval SECS] [--passes N] ...        a pass every interval
  intake dispatch UID [--source S] [--recipe R] [--dry-run]  start one ticket by hand
  intake report [--dry-run]                               report finished runs back
  intake status                                          what is set up, and why not

The default source set is every registered source **except** ``local``.
``local`` reports mini-ork's own epics, whose state belongs to the scheduler —
starting a mini-ork run for each open epic would have the machine consume its
own backlog. It can still be asked for by name, because a rule the operator
wrote is a rule the operator wrote.

Nothing starts until a source declares a trigger (``trigger_label``,
``trigger_assignee``, ``trigger_states`` in ``config/pm.yaml``). With none set
every pass reports ``no trigger configured`` and exits 0 — the first run against
a real board is meant to be a dry run, and the safe default has to hold even
when someone forgets ``--dry-run``.

Exit codes: 0 ok, 1 failure (a sync that failed, an unknown source), 2 usage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mini_ork.intake import poll as intake_poll
from mini_ork.intake import report as intake_report
from mini_ork.pm.config import PmConfigError
from mini_ork.pm.provider import ProviderError
from mini_ork.pm.registry import UnknownProvider

_USAGE = """\
mini-ork intake — turn tickets into running agents.

Usage:
  mini-ork intake once  [--source S] [--max-runs N] [--dry-run] [--json]
  mini-ork intake poll  [--interval SECS] [--passes N] [--source S] \
[--max-runs N] [--dry-run] [--json]
  mini-ork intake report [--dry-run] [--json]
  mini-ork intake status [--json]

Verbs:
  once     sync the sources and dispatch what their triggers select, then exit
  poll     the same pass, every --interval seconds, until interrupted
  dispatch start one ticket now, whatever the triggers say
  report   report finished linked runs back to their tickets, once each
  status   every source this build knows, its trigger, and its last sync

Defaults:
  --source   every registered source except `local` (see `intake status`)
  --max-runs 1     tickets one pass may dispatch; a first pass is not a stampede
  --interval 300   seconds between passes

Options:
  --dry-run  report what would start and start nothing
  --home D   project home (default $MINI_ORK_HOME or ./.mini-ork)
  --json     machine-readable output

Nothing starts until a source declares a trigger in config/pm.yaml
(`trigger_label`, `trigger_assignee`, `trigger_states`).
"""

#: The gap between passes when ``--interval`` is not given. Slow on purpose: the
#: cost of a long interval is latency, and the cost of a short one is a machine
#: that hammers an API and an operator who turns it off instead of tuning it.
DEFAULT_INTERVAL = 300.0


def _home(args: argparse.Namespace) -> Path:
    from mini_ork.web.db import resolve_home

    return resolve_home(getattr(args, "home", None))


def _targets(args: argparse.Namespace) -> tuple[Path, str]:
    """``(home, db_path)`` for one invocation. Same chain as ``threads``."""
    from mini_ork.web.db import resolve_db

    home = _home(args)
    db = resolve_db(home, args.db)
    home.mkdir(parents=True, exist_ok=True)
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    return home, db


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--json", action="store_true")


def _run_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, add_help=False, usage=argparse.SUPPRESS)
    parser.add_argument("--source", default=None)
    parser.add_argument("--max-runs", dest="max_runs", type=int, default=1)
    parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    _common(parser)
    return parser


def _report_parser(prog: str = "mini-ork intake report") -> argparse.ArgumentParser:
    """The secure tier's verb: a scheduler runs this against a webhook-driven
    deployment, where the poll loop is not ticking. Same ``report_pass``, same
    write-back gate, so the two tiers cannot drift."""
    parser = argparse.ArgumentParser(prog=prog, add_help=False, usage=argparse.SUPPRESS)
    parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    _common(parser)
    return parser


def _dispatch_parser(prog: str = "mini-ork intake dispatch") -> argparse.ArgumentParser:
    """One ticket, by hand. No ``--source`` needed when the uid carries one."""
    parser = argparse.ArgumentParser(prog=prog, add_help=False, usage=argparse.SUPPRESS)
    parser.add_argument("uid")
    parser.add_argument("--source", default=None)
    parser.add_argument("--recipe", default=None)
    parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    _common(parser)
    return parser


def _status_parser(prog: str = "mini-ork intake status") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, add_help=False, usage=argparse.SUPPRESS)
    _common(parser)
    return parser


def build_parser() -> argparse.ArgumentParser:
    """The whole ``mini-ork intake`` surface as one parser.

    ``main`` dispatches verb-first by hand, so an unknown verb prints the usage
    text rather than letting argparse exit — but the IDE's "did the button
    dispatch?" check (``tests/unit/test_ide_pages_actions.py``) needs one parser
    carrying every verb. Building it from the *same* ``_run_parser`` the command
    uses is what keeps that second view from drifting from the first.
    """
    parser = argparse.ArgumentParser(
        prog="mini-ork intake", add_help=False, usage=argparse.SUPPRESS
    )
    subs = parser.add_subparsers(dest="verb")
    subs.add_parser(
        "once", parents=[_run_parser("mini-ork intake once")], usage=argparse.SUPPRESS
    )
    poll = subs.add_parser(
        "poll", parents=[_run_parser("mini-ork intake poll")], usage=argparse.SUPPRESS
    )
    poll.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    poll.add_argument("--passes", type=int, default=None)
    subs.add_parser(
        "dispatch", parents=[_dispatch_parser()], usage=argparse.SUPPRESS
    )
    subs.add_parser(
        "report", parents=[_report_parser()], usage=argparse.SUPPRESS
    )
    subs.add_parser(
        "status", parents=[_status_parser()], usage=argparse.SUPPRESS
    )
    return parser


def _sources(args: argparse.Namespace) -> list[str]:
    """The sources a run should touch.

    ``local`` is excluded by default and can be asked for by name. See the
    module docstring for why.
    """
    if args.source:
        return [args.source]
    from mini_ork.pm.registry import provider_ids

    return [source for source in provider_ids() if source != "local"]


def main(argv=None, *, root=None) -> int:  # noqa: ARG001 - root is the dispatch contract
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help", "-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0 if argv else 2
    verb, rest = argv[0], argv[1:]
    try:
        if verb == "once":
            return _cmd_run(rest, loop=False)
        if verb == "poll":
            return _cmd_run(rest, loop=True)
        if verb == "dispatch":
            return _cmd_dispatch(rest)
        if verb == "report":
            return _cmd_report(rest)
        if verb == "status":
            return _cmd_status(rest)
    except (ProviderError, PmConfigError, UnknownProvider) as exc:
        sys.stderr.write(f"intake {verb}: {exc}\n")
        return 1
    sys.stderr.write(f"intake: unknown verb {verb!r}\n\n{_USAGE}")
    return 2


def _cmd_run(argv: list[str], *, loop: bool) -> int:
    prog = "mini-ork intake poll" if loop else "mini-ork intake once"
    parser = _run_parser(prog)
    if loop:
        parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
        parser.add_argument(
            "--passes",
            type=int,
            default=None,
            help="stop after this many passes (default: until interrupted)",
        )
    args = parser.parse_args(argv)

    home, db = _targets(args)
    sources = _sources(args)
    if not sources:
        sys.stderr.write(
            "intake: no source to poll — every registered source is `local`, or none "
            "is registered. See `mini-ork intake status`.\n"
        )
        return 1

    reports = []
    for source in sources:
        if loop:
            reports.extend(
                intake_poll.poll_loop(
                    home,
                    source,
                    interval=args.interval,
                    db=db,
                    dry_run=args.dry_run,
                    max_runs=max(1, args.max_runs),
                    passes=args.passes,
                    on_pass=lambda report: _announce(report, args.json),
                )
            )
        else:
            reports.append(
                intake_poll.poll_once(
                    home, source, db=db, dry_run=args.dry_run, max_runs=max(1, args.max_runs)
                )
            )
            _announce(reports[-1], args.json)

    if args.json:
        sys.stdout.write(json.dumps([report.to_dict() for report in reports], indent=2) + "\n")
    return 1 if any(report.error for report in reports) else 0


def _announce(report: intake_poll.PollReport, as_json: bool) -> None:
    """Report one pass. Silent when it did nothing and ``--json`` is coming."""
    if as_json:
        return
    if report.error is not None:
        sys.stderr.write(f"intake {report.source}: {report.error}\n")
        return
    if not report.changed:
        sys.stdout.write(
            f"intake {report.source}: {report.mirrored} mirrored, "
            f"{report.considered} open, nothing to start\n"
        )
        return
    for outcome in report.outcomes:
        if outcome.action == "skipped":
            continue
        where = f" → {outcome.thread_id}" if outcome.thread_id else ""
        run = f" [run {outcome.run_id}]" if outcome.run_id else ""
        sys.stdout.write(f"  {outcome.action:<12} {outcome.issue_uid}{where}{run}\n")
    for outcome in report.reported:
        if outcome.action == "skipped":
            continue
        sys.stdout.write(
            f"  {outcome.action:<12} {outcome.issue_uid} [run {outcome.run_id}]\n"
        )
    parts = [f"{report.mirrored} mirrored", f"{report.considered} open"]
    parts.append(f"{report.previewed} would start" if report.dry_run else f"{report.started} started")
    if report.deferred:
        parts.append(f"{report.deferred} over --max-runs")
    sys.stdout.write(f"intake {report.source}: " + ", ".join(parts) + "\n")


def _cmd_dispatch(argv: list[str]) -> int:
    """Start one ticket now, at a person's request rather than a rule's.

    The trigger is deliberately *not* consulted: a hand dispatch is its own
    authority, and the admission policies still run inside
    :func:`~mini_ork.intake.dispatch.dispatch_issue`, so this cannot start a
    ticket a policy would have refused. What it does not do is bypass the
    open-state check — a ticket the source reports as done is refused here
    rather than starting a run to close something already closed.
    """
    from mini_ork.intake.dispatch import dispatch_issue

    args = _dispatch_parser().parse_args(argv)
    source = args.source or (args.uid.split(":", 1)[0] if ":" in args.uid else "")
    if not source:
        sys.stderr.write(
            "intake dispatch: pass `--source S` or a `source:key` uid "
            "(the Board's ticket ids already carry it)\n"
        )
        return 2

    home, db = _targets(args)
    adapter = intake_poll._adapter(source, db)
    issue = intake_poll._read(adapter, args.uid)
    if isinstance(issue, str):
        sys.stderr.write(f"intake dispatch: {issue}\n")
        return 1
    if not issue.is_open:
        sys.stderr.write(
            f"intake dispatch: the source reports {issue.uid} as {issue.state}; "
            "open it there before starting a run\n"
        )
        return 1
    if args.dry_run:
        if args.json:
            sys.stdout.write(json.dumps({
                "issue_uid": issue.uid, "action": "would-start",
                "recipe": args.recipe, "dry_run": True,
            }, indent=2) + "\n")
        else:
            recipe = args.recipe or "the source's default recipe"
            sys.stdout.write(f"intake dispatch: would start {issue.uid} with {recipe}\n")
        return 0

    dispatch = dispatch_issue(
        home, adapter, issue, recipe=args.recipe, source="intake-cli", db=db
    )
    if args.json:
        sys.stdout.write(json.dumps(dispatch.to_dict(), indent=2) + "\n")
    else:
        where = f" → {dispatch.thread_id}" if dispatch.thread_id else ""
        run = f" [run {dispatch.run_id}]" if dispatch.run_id else ""
        detail = f" — {dispatch.detail}" if dispatch.detail else ""
        sys.stdout.write(f"  {dispatch.action:<12} {dispatch.issue_uid}{where}{run}{detail}\n")
    # A refusal is a real answer, and a caller that scripts this must be able to
    # tell it from a start: the admission policies said no, and nothing ran.
    return 1 if dispatch.action == "refused" else 0


def _cmd_report(argv: list[str]) -> int:
    """Report finished linked runs back to their tickets, once each.

    The secure tier's entry point: a webhook-driven deployment has no poll loop
    ticking, so its scheduler calls this. Whether anything is posted is decided
    by the write-back gate alone — the same gate the poll loop consults — so the
    two tiers fail closed identically. Exit 0 unless a source raised.
    """
    args = _report_parser().parse_args(argv)
    home, db = _targets(args)
    outcomes = intake_report.report_pass(home, db=db, dry_run=args.dry_run)

    if args.json:
        sys.stdout.write(json.dumps([o.to_dict() for o in outcomes], indent=2) + "\n")
    elif not outcomes:
        sys.stdout.write("intake report: nothing to report\n")
    else:
        for outcome in outcomes:
            where = f" → {outcome.thread_id}" if outcome.thread_id else ""
            sys.stdout.write(
                f"  {outcome.action:<13} {outcome.issue_uid} [run {outcome.run_id}]{where}\n"
            )
    return 1 if any(outcome.action == "error" for outcome in outcomes) else 0


def _cmd_status(argv: list[str]) -> int:
    args = _status_parser().parse_args(argv)

    _, db = _targets(args)
    statuses = intake_poll.source_statuses(db)

    if args.json:
        sys.stdout.write(json.dumps([s.to_dict() for s in statuses], indent=2) + "\n")
        return 0
    if not statuses:
        sys.stdout.write("no sources\n")
        return 0

    for status in statuses:
        if not status.registered:
            state = "skipped"
        elif status.enabled:
            state = "registered"
        else:
            state = "disabled"
        sys.stdout.write(f"{status.source:<12} {state:<11} {_trigger_line(status)}\n")
        if status.skipped_reason:
            sys.stdout.write(f"{'':<12} {'':<11} {status.skipped_reason}\n")
        if status.sync_error:
            sys.stdout.write(f"{'':<12} {'':<11} last sync failed: {status.sync_error}\n")

    if not any(s.trigger.configured for s in statuses if s.registered):
        sys.stdout.write(
            "\nnothing will start: no registered source has a trigger. Add "
            "`trigger_label`, `trigger_assignee` or `trigger_states` under a source "
            "in config/pm.yaml.\n"
        )
    return 0


def _trigger_line(status: intake_poll.SourceStatus) -> str:
    rules = status.trigger
    if not rules.configured:
        return "no trigger configured"
    parts = []
    if rules.label is not None:
        parts.append(f"label {rules.label!r}")
    if rules.assignee is not None:
        parts.append(f"assignee {rules.assignee!r}")
    if rules.states:
        states = ", ".join(str(state) for state in rules.states)
        parts.append(f"state in ({states})")
    line = " AND ".join(parts)
    if rules.recipe:
        line += f"  → recipe {rules.recipe!r}"
    if status.mirrored or status.synced_at:
        when = status.synced_at or "never"
        line += f"   [{status.mirrored} mirrored, synced {when}]"
    return line


if __name__ == "__main__":
    raise SystemExit(main())
