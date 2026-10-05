"""``mini-ork automations`` — manage per-project scheduled recipe runs.

Surface (kickoff §CLI):

  list [--json]                          table or JSON of every automation
  add --id --name --recipe --schedule    validate and store a new automation
        --kickoff-file <path> [--in-place]
  remove <id>                            drop an automation
  pause <id>                             enabled = false
  resume <id>                            enabled = true
  run <id>                               fire now, regardless of schedule
  tick                                   fire every due, enabled automation
  scheduler status|install|uninstall     install/inspect/remove the OS tick

Exit codes: 0 on success, 2 on usage errors, 1 on failures. Diagnostics go to
stderr (matches every other native subcommand — see :mod:`mini_ork.cli.zed_cmd`).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

from mini_ork import automations as _auto


_USAGE = (
    "Usage: mini-ork automations <list|add|remove|pause|resume|run|tick|"
    "scheduler> [--home <dir>] [--json]\n\n"
    "Manage scheduled recipe runs for a mini-ork project.\n"
    "Subcommands:\n"
    "  list                 show every automation (--json for JSON)\n"
    "  add                  register a new automation\n"
    "  remove <id>          drop an automation\n"
    "  pause <id>           disable an automation\n"
    "  resume <id>          re-enable an automation\n"
    "  run <id>             fire now regardless of schedule\n"
    "  tick                 fire every due, enabled automation\n"
    "  scheduler status     inspect the OS scheduler\n"
    "  scheduler install    install the OS scheduler\n"
    "  scheduler uninstall  remove the OS scheduler\n"
)


def _default_home() -> Path:
    """Same precedence as ``mini-ork init`` (default: ``.mini-ork`` under cwd)."""
    return Path(os.environ.get("MINI_ORK_HOME", "").strip() or
                (Path.cwd() / ".mini-ork"))


def _emit_table(rows: list[dict[str, str]]) -> None:
    if not rows:
        sys.stdout.write("(no automations)\n")
        return
    headers = list(rows[0].keys())
    widths = {h: max(len(h), *(len(str(r.get(h, ""))) for r in rows))
              for h in headers}
    fmt = "  ".join(f"{{:{widths[h]}}}" for h in headers)
    sys.stdout.write(fmt.format(*headers) + "\n")
    for r in rows:
        sys.stdout.write(fmt.format(*(str(r.get(h, "")) for h in headers)) + "\n")


def _format_next_fire(schedule: str) -> str:
    """Best-effort next-fire time, or ``—`` when we can't compute it."""
    if not schedule:
        return "—"
    try:
        spec = _auto.parse_cron(schedule)
    except ValueError:
        return "—"
    after = _dt.datetime.now()
    try:
        nxt = _auto.next_fire(spec, after)
    except ValueError:
        return "—"
    return nxt.replace(microsecond=0).isoformat()


def _cmd_list(args: argparse.Namespace) -> int:
    home = Path(args.home)
    items = _auto.load(home)
    if args.json:
        sys.stdout.write(json.dumps({"automations": items}, indent=2) + "\n")
        return 0
    rows = []
    for a in items:
        rows.append({
            "id": a.get("id", ""),
            "name": a.get("name", ""),
            "recipe": a.get("recipe", ""),
            "schedule": _auto.describe(str(a.get("schedule", ""))),
            "enabled": "yes" if a.get("enabled", True) else "no",
            "last_run": _auto.last_run_status(home, a),
            "next_fire": (_format_next_fire(str(a.get("schedule", "")))
                          if a.get("enabled", True) else "paused"),
        })
    _emit_table(rows)
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    home = Path(args.home)
    try:
        kickoff_text = Path(args.kickoff_file).read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"mini-ork automations add: could not read kickoff file: {exc}\n")
        return 1
    workspace = "in-place" if args.in_place else "worktree"
    result = _auto.add(
        home,
        id=args.id,
        name=args.name,
        recipe=args.recipe,
        kickoff=kickoff_text,
        schedule=args.schedule,
        workspace=workspace,
    )
    if not result.get("ok"):
        sys.stderr.write(f"mini-ork automations add: {result.get('error', 'unknown error')}\n")
        return 1
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    result = _auto.remove(args.home, args.automation_id)
    if not result.get("ok"):
        sys.stderr.write(
            f"mini-ork automations remove: {result.get('error', 'unknown error')}\n"
        )
        return 1
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


def _cmd_pause(args: argparse.Namespace) -> int:
    result = _auto.pause(args.home, args.automation_id)
    if not result.get("ok"):
        sys.stderr.write(
            f"mini-ork automations pause: {result.get('error', 'unknown error')}\n"
        )
        return 1
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    result = _auto.resume(args.home, args.automation_id)
    if not result.get("ok"):
        sys.stderr.write(
            f"mini-ork automations resume: {result.get('error', 'unknown error')}\n"
        )
        return 1
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    result = _auto.fire(args.home, args.automation_id)
    if not result.get("ok"):
        sys.stderr.write(
            f"mini-ork automations run: {result.get('error', 'unknown error')}\n"
        )
        return 1
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


def _cmd_tick(args: argparse.Namespace) -> int:
    firings = _auto.tick(args.home)
    sys.stdout.write(json.dumps({"fired": firings}, indent=2) + "\n")
    return 0


def _cmd_scheduler_status(args: argparse.Namespace) -> int:
    status = _auto.scheduler_status(args.home)
    if args.json:
        sys.stdout.write(json.dumps(status, indent=2) + "\n")
        return 0
    sys.stdout.write(f"platform: {status.get('platform', 'unknown')}\n")
    sys.stdout.write(f"installed: {'yes' if status.get('installed') else 'no'}\n")
    sys.stdout.write(f"command: {status.get('command', '')}\n")
    if status.get("log_path"):
        sys.stdout.write(f"log: {status['log_path']}\n")
    if status.get("last_tick"):
        sys.stdout.write(f"last_tick: {status['last_tick']}\n")
    return 0


def _cmd_scheduler_install(args: argparse.Namespace) -> int:
    result = _auto.install_scheduler(args.home)
    if not result.get("ok"):
        sys.stderr.write(
            f"mini-ork automations scheduler install: {result.get('error', 'unknown error')}\n"
        )
        return 1
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


def _cmd_scheduler_uninstall(args: argparse.Namespace) -> int:
    result = _auto.uninstall_scheduler(args.home)
    if not result.get("ok"):
        sys.stderr.write(
            f"mini-ork automations scheduler uninstall: {result.get('error', 'unknown error')}\n"
        )
        return 1
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-ork automations",
        description=_USAGE.splitlines()[0],
        add_help=False,
    )
    parser.add_argument("--home", default=None,
                        help="MINI_ORK_HOME (default: $MINI_ORK_HOME or ./.mini-ork)")
    # ``--home`` is accepted after the subcommand too (the OS scheduler runs
    # ``automations tick --home <home>``); SUPPRESS keeps an absent sub-level
    # flag from overwriting a top-level one.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="subcommand")

    list_p = sub.add_parser("list", parents=[common], add_help=False)
    list_p.add_argument("--json", action="store_true",
                         help="emit JSON instead of a table")

    add_p = sub.add_parser("add", parents=[common], add_help=False)
    add_p.add_argument("--id", required=True)
    add_p.add_argument("--name", required=True)
    add_p.add_argument("--recipe", required=True)
    add_p.add_argument("--schedule", required=True, help="5-field cron expression")
    add_p.add_argument("--kickoff-file", required=True)
    add_p.add_argument("--in-place", action="store_true",
                       help="run in-place instead of minting a worktree")

    remove_p = sub.add_parser("remove", parents=[common], add_help=False)
    remove_p.add_argument("automation_id")

    pause_p = sub.add_parser("pause", parents=[common], add_help=False)
    pause_p.add_argument("automation_id")

    resume_p = sub.add_parser("resume", parents=[common], add_help=False)
    resume_p.add_argument("automation_id")

    run_p = sub.add_parser("run", parents=[common], add_help=False)
    run_p.add_argument("automation_id")

    sub.add_parser("tick", parents=[common], add_help=False)

    scheduler_p = sub.add_parser("scheduler", parents=[common], add_help=False)
    scheduler_sub = scheduler_p.add_subparsers(dest="scheduler_subcommand")
    sched_status = scheduler_sub.add_parser("status", parents=[common], add_help=False)
    sched_status.add_argument("--json", action="store_true",
                                help="emit JSON instead of human-readable text")
    scheduler_sub.add_parser("install", parents=[common], add_help=False)
    scheduler_sub.add_parser("uninstall", parents=[common], add_help=False)

    return parser


def _resolve_home(args: argparse.Namespace) -> Path:
    # Absolute: ``scheduler install`` writes this path into a launchd/cron job.
    return (Path(args.home) if args.home else _default_home()).expanduser().absolute()


def main(rest: list[str], root: str) -> int:
    """Entry point invoked by ``mini-ork automations ...``."""
    del root  # not needed — automations live in <home>, not the engine checkout.
    if not rest:
        sys.stderr.write(_USAGE)
        return 2
    if rest[0] in ("--help", "-h"):
        sys.stderr.write(_USAGE)
        return 0

    known = {"list", "add", "remove", "pause", "resume", "run", "tick", "scheduler"}
    words = [w for i, w in enumerate(rest)
             if not w.startswith("-") and (i == 0 or rest[i - 1] != "--home")]
    if not words or words[0] not in known:
        shown = words[0] if words else rest[0]
        sys.stderr.write(f"mini-ork automations: unknown subcommand: {shown}\n")
        sys.stderr.write(_USAGE)
        return 2

    parser = _build_parser()
    try:
        args = parser.parse_args(rest)
    except SystemExit as exc:
        # argparse --help hits this; route through our usage.
        if exc.code == 0:
            sys.stderr.write(_USAGE)
            return 0
        sys.stderr.write(_USAGE)
        return 2

    if args.subcommand is None:
        sys.stderr.write(_USAGE)
        return 2
    args.home = _resolve_home(args)
    sub = args.subcommand

    if sub == "list":
        return _cmd_list(args)
    if sub == "add":
        return _cmd_add(args)
    if sub == "remove":
        return _cmd_remove(args)
    if sub == "pause":
        return _cmd_pause(args)
    if sub == "resume":
        return _cmd_resume(args)
    if sub == "run":
        return _cmd_run(args)
    if sub == "tick":
        return _cmd_tick(args)
    # scheduler
    if args.scheduler_subcommand == "status":
        return _cmd_scheduler_status(args)
    if args.scheduler_subcommand == "install":
        return _cmd_scheduler_install(args)
    if args.scheduler_subcommand == "uninstall":
        return _cmd_scheduler_uninstall(args)
    sys.stderr.write(_USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ.get("MINI_ORK_ROOT", "")))