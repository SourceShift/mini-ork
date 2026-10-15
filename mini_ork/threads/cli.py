"""``mini-ork threads`` — drive the thread state machine with no server.

This is the simple tier's entry point: `launch_run` is a ``Popen``, not HTTP, so
a laptop with no uvicorn running can start a thread, follow up on it and read
its transcript. The secure tier reaches the same state machine through
``POST /api/conversations/{id}/events``; both end in
:func:`mini_ork.threads.service.deliver`, which is the point of the split.

``send`` and ``continue`` are deliberately not aliases. ``send`` hands the
decision to the machine — a live run is steered, a finished one is continued.
``continue`` asserts the operator wants a *new attempt* and refuses while a run
is live, because "inject into the running one" is a different intent that
steering already covers and that silently doing it would hide.

The home is ``--home`` → ``MINI_ORK_HOME`` → ``cwd/.mini-ork`` (the same chain
:func:`mini_ork.web.db.resolve_home` uses everywhere else), resolved here rather
than from the dispatch-time root, which is the engine checkout, not a project.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from mini_ork.threads import service, store
from mini_ork.threads.model import ThreadOrigin

_USAGE = """\
mini-ork threads — start, inspect and drive multi-turn work with no server.

Usage:
  mini-ork threads list [--json] [--limit N] [--origin O] [--status S]
  mini-ork threads show <id> [--json]
  mini-ork threads send <id> --text <t> [--title T] [--recipe R] ...
  mini-ork threads continue <id> --text <t> [--title T] [--recipe R] ...
  mini-ork threads attach <id> <path>... [--json]

Verbs:
  list       threads, most recently touched first
  show       one thread: its header, transcript and runs
  send       deliver a message — the machine decides. A live run is *steered*;
             a finished thread starts a new attempt that carries the transcript
  continue   start the next attempt, refusing while a run is live
  attach     copy local files onto the thread (one turn); the next send's run
             is told where they are. Starts nothing on its own

Common options:
  --text -   read the message from stdin
  --home D   project home (default $MINI_ORK_HOME or ./.mini-ork)
  --json     machine-readable output

Origins: ide, chat, intake, cli.
"""


def _home(args: argparse.Namespace):
    from mini_ork.web.db import resolve_home

    return resolve_home(getattr(args, "home", None))


def _text(args: argparse.Namespace) -> str | None:
    """The message. ``-`` reads stdin, so a ticket can be piped in. ``None``
    means the caller gave nothing usable, which the caller reports."""
    raw = args.text
    if raw == "-":
        raw = sys.stdin.read()
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--json", action="store_true")


def _delivery_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, add_help=False, usage=argparse.SUPPRESS)
    parser.add_argument("id")
    parser.add_argument("--text", default=None)
    parser.add_argument("--title", default="")
    parser.add_argument("--recipe", default=None)
    parser.add_argument("--origin", default=ThreadOrigin.CLI.value,
                        choices=[o.value for o in ThreadOrigin])
    parser.add_argument("--origin-ref", dest="origin_ref", default=None)
    parser.add_argument("--external-id", dest="external_id", default=None)
    parser.add_argument("--issue-uid", dest="issue_uid", default=None)
    parser.add_argument("--source", default="threads")
    _common(parser)
    return parser


def main(argv=None, *, root=None) -> int:  # noqa: ARG001 - root is the dispatch contract
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help", "-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0 if argv else 2
    verb, rest = argv[0], argv[1:]
    if verb == "list":
        return _cmd_list(rest)
    if verb == "show":
        return _cmd_show(rest)
    if verb in ("send", "continue"):
        return _cmd_deliver(verb, rest)
    if verb == "attach":
        return _cmd_attach(rest)
    sys.stderr.write(f"threads: unknown verb {verb!r}\n\n{_USAGE}")
    return 2


def _cmd_list(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mini-ork threads list", add_help=False,
                                     usage=argparse.SUPPRESS)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--origin", default=None)
    parser.add_argument("--status", default=None)
    _common(parser)
    args = parser.parse_args(argv)

    _, db = _targets(args)
    threads = store.list_threads(limit=args.limit, db=db)
    if args.origin:
        threads = [t for t in threads if str(t.origin) == args.origin]
    if args.status:
        threads = [t for t in threads if str(t.status) == args.status]

    if args.json:
        sys.stdout.write(json.dumps([asdict(t) for t in threads], indent=2) + "\n")
        return 0
    if not threads:
        sys.stdout.write("no threads\n")
        return 0

    width = max(len(t.id) for t in threads)
    sys.stdout.write(f"{'ID':<{width}}  {'STATUS':<8}  {'ORIGIN':<7}  TURN  UPDATED  TITLE\n")
    for t in threads:
        sys.stdout.write(
            f"{t.id:<{width}}  {str(t.status):<8}  {str(t.origin):<7}  "
            f"{t.last_turn:<4}  {t.updated_at}  {t.title}\n"
        )
    return 0


def _cmd_show(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mini-ork threads show", add_help=False,
                                     usage=argparse.SUPPRESS)
    parser.add_argument("id")
    _common(parser)
    args = parser.parse_args(argv)

    _, db = _targets(args)
    thread = store.get_thread(args.id, db=db)
    if thread is None:
        sys.stderr.write(f"threads: no thread {args.id!r}\n")
        return 1
    turns = store.messages(args.id, db=db)
    runs = store.runs_for(args.id, db=db)

    if args.json:
        payload = {
            "thread": asdict(thread),
            "messages": [asdict(m) for m in turns],
            "runs": [asdict(r) for r in runs],
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 0

    sys.stdout.write(f"# {thread.title or thread.id}\n")
    sys.stdout.write(f"id       {thread.id}\n")
    sys.stdout.write(f"status   {thread.status}\n")
    sys.stdout.write(f"origin   {thread.origin}"
                     + (f" ({thread.origin_ref})" if thread.origin_ref else "") + "\n")
    sys.stdout.write(f"recipe   {thread.recipe or service.DEFAULT_RECIPE}\n")
    sys.stdout.write(f"turns    {thread.last_turn}\n")

    sys.stdout.write("\n## Transcript\n")
    if not turns:
        sys.stdout.write("(empty)\n")
    for m in turns:
        sys.stdout.write(f"\n[{m.turn}] {str(m.role).upper()}\n{m.text}\n")

    sys.stdout.write("\n## Runs\n")
    if not runs:
        sys.stdout.write("(none)\n")
    for r in runs:
        sys.stdout.write(
            f"\nattempt {r.attempt}  {r.status}  {r.run_id}\n"
            f"  recipe       {r.recipe or '-'}\n"
            f"  kickoff      {r.kickoff_path or '-'}\n"
        )
    return 0


def _cmd_deliver(verb: str, argv: list[str]) -> int:
    args = _delivery_parser(f"mini-ork threads {verb}").parse_args(argv)
    text = _text(args)
    if text is None:
        sys.stderr.write(f"threads {verb}: --text is required (use '-' to read stdin)\n")
        return 2

    home, db = _targets(args)

    if verb == "continue":
        live = store.live_run(args.id, db=db)
        if live is not None:
            sys.stderr.write(
                f"threads continue: run {live.run_id} is live — "
                f"`threads send {args.id} --text ...` steers it instead\n"
            )
            return 1

    out = service.deliver(
        home, args.id, text,
        title=args.title,
        origin=args.origin,
        origin_ref=args.origin_ref,
        recipe=args.recipe,
        external_id=args.external_id,
        issue_uid=args.issue_uid,
        source=args.source,
        db=db,
    )

    if args.json:
        sys.stdout.write(json.dumps(out.to_dict(), indent=2) + "\n")
    elif out.ok:
        where = f"run {out.run_id}" if out.run_id else "no run"
        sys.stdout.write(f"{out.action}: thread {out.thread_id} turn {out.turn} — {where}\n")
        if out.kickoff_path:
            sys.stdout.write(f"kickoff: {out.kickoff_path}\n")
    else:
        sys.stderr.write(f"threads {verb}: {out.detail}\n")
    return 0 if out.ok else 1


def _cmd_attach(argv: list[str]) -> int:
    """Copy files onto an existing thread as ``FILE`` rows sharing one new turn.

    Nothing is launched or steered: attaching is not a request. The files reach
    a run through the next ``send`` — its kickoff carries every earlier
    attachment (``threads.service._history``). The IDE's "Attach file…" button
    runs this with the paths the native picker returned.
    """
    from mini_ork.threads import attachments
    from mini_ork.threads.model import MessageRole

    parser = argparse.ArgumentParser(prog="mini-ork threads attach", add_help=False,
                                     usage=argparse.SUPPRESS)
    parser.add_argument("id")
    parser.add_argument("paths", nargs="*")
    _common(parser)
    args = parser.parse_args(argv)
    if not args.paths:
        sys.stderr.write("threads attach: name at least one file\n")
        return 2
    home, db = _targets(args)
    if store.get_thread(args.id, db=db) is None:
        sys.stderr.write(f"threads attach: no thread {args.id!r}\n")
        return 1

    stored: list[str] = []
    for raw in args.paths:
        try:
            stored.append(str(attachments.store_file(home, args.id, raw).resolve()))
        except (attachments.AttachmentError, OSError) as exc:
            sys.stderr.write(f"threads attach: {exc}\n")
            return 1
    turn = None
    for item in stored:
        row = store.append_message(args.id, role=MessageRole.FILE, text="", turn=turn,
                                   attachments=(item,), db=db)
        turn = row.turn
    if args.json:
        sys.stdout.write(json.dumps({"thread_id": args.id, "turn": turn,
                                     "attachments": stored}, indent=2) + "\n")
    else:
        for item in stored:
            sys.stdout.write(f"attached: {item}\n")
    return 0


def _targets(args: argparse.Namespace) -> tuple[Path, str]:
    """``(home, db_path)`` for one invocation, with both created if missing.

    The chain itself lives in :func:`mini_ork.web.db.resolve_db`, so this and
    the intake CLI cannot drift into reading one file and writing another. It
    cannot be left to the store's own env lookup: that one knows nothing of
    ``--home``, so ``threads send --home X`` would write X while ``threads list
    --home X`` read ``$MINI_ORK_HOME`` — or worse, ``cwd/.mini-ork``.

    The directories are created here because ``sqlite3.connect`` refuses to
    create a file inside a directory that does not exist, so a first run against
    a fresh home would otherwise die with a traceback instead of `no threads`.
    Creating the schema on a read is already the store's behaviour
    (``ensure_schema``); creating the directory it lives in is the same act.
    """
    from mini_ork.web.db import resolve_db

    home = _home(args)
    db = resolve_db(home, args.db)
    home.mkdir(parents=True, exist_ok=True)
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    return home, db


if __name__ == "__main__":
    raise SystemExit(main())
