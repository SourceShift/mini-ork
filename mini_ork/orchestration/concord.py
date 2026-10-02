"""``mini-ork concord`` — the client half of the Concord P0 wire contract.

Principals, a run wrapper, and principal-addressed messaging, talking to
ContextNest's ``/api/v1/coord/*`` endpoints (base ``CN_BASE_URL``, default
``http://127.0.0.1:28080``). The server half is built in parallel, so this
client is fully testable against a stub HTTP server and never assumes a live
ContextNest.

Exit codes: 0 ok, 2 usage, 3 ContextNest unavailable, 4 refused (e.g. the
``stop`` safety checks). ``run`` is the exception: it fails open — it warns
once on stderr and runs the command anyway when ContextNest is unreachable.

The wire contract (endpoint paths, JSON shapes, the principal-id regex and the
exit-code table) is pinned in ``docs/architecture/concord-protocol.md``.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Sequence

from mini_ork import cn_client

__all__ = ["main"]

_PRINCIPAL_ID_RE = re.compile(r"^(loop|run|session|human|agent):[A-Za-z0-9._@/-]{1,128}$")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-ork concord",
        description="Concord cross-agent coordination client (principals, run wrapper, mailbox).",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    run = sub.add_parser("run", help="run a command bound to a principal")
    run.add_argument("--name", "-n", default="", help="principal name (with --kind)")
    run.add_argument("--kind", default="loop", help="principal kind (default: loop)")
    run.add_argument("--heartbeat-secs", type=int, default=30,
                     help="heartbeat interval in seconds (default: 30)")
    run.add_argument("cmd", nargs=argparse.REMAINDER, help="command to run (precede with --)")

    ps = sub.add_parser("ps", help="list principals")
    ps.add_argument("--all", action="store_true", help="include ended principals")
    ps.add_argument("--json", action="store_true", help="emit raw JSON")

    stop = sub.add_parser("stop", help="kill a principal's process group")
    stop.add_argument("principal")
    stop.add_argument("--signal", choices=("TERM", "KILL"), default="TERM",
                      help="signal to send (default: TERM)")
    stop.add_argument("--yes", "-y", action="store_true", help="confirm without a tty")

    send = sub.add_parser("send", help="send a message to a principal")
    send.add_argument("principal")
    send.add_argument("message", nargs="+", help="message body (joined with spaces)")
    send.add_argument("--from", dest="from_", default="", help="sender principal id")

    inbox = sub.add_parser("inbox", help="read a principal's unacknowledged messages")
    inbox.add_argument("--principal", "-p", default="",
                       help="principal (default: $CONCORD_PRINCIPAL)")
    inbox.add_argument("--format", choices=("text", "json", "prompt"), default="text")
    inbox.add_argument("--ack", action="store_true", help="acknowledge what was shown")

    ack = sub.add_parser("ack", help="acknowledge a message")
    ack.add_argument("principal")
    ack.add_argument("msg_id")

    sub.add_parser("help", help="show this help")
    return parser


def _reject_id(principal_id: str) -> bool:
    """Print an error and return True when ``principal_id`` is not a valid id."""
    if _PRINCIPAL_ID_RE.match(principal_id or ""):
        return False
    print(f"error: invalid principal id {principal_id!r}", file=sys.stderr)
    return True


def _default_sender() -> str:
    return os.environ.get("CONCORD_PRINCIPAL") or f"human:{os.environ.get('USER', 'unknown')}"


def _hostname() -> str:
    return socket.gethostname()


def _call(fn, *args, **kwargs) -> tuple[int, dict | None]:
    """Run a cn_client coord_* helper, mapping failures to (exit_code, payload).

    Exit code 3 = ContextNest unreachable, 4 = refused/HTTP error. On success
    the exit code is 0 and the payload is the parsed JSON dict.
    """
    try:
        return 0, fn(*args, **kwargs)
    except cn_client.CoordUnavailable as exc:
        print(f"error: ContextNest unavailable: {exc}", file=sys.stderr)
        return 3, None
    except cn_client.CoordHTTPError as exc:
        print(f"error: ContextNest: {exc.status} {exc.body}", file=sys.stderr)
        return 4, None


def _git_worktree(cwd: str) -> str | None:
    """The git worktree containing ``cwd``, or None when it is not in one."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _group_pids(pgid: int, fallback: int) -> list[int]:
    """Live pids in process group ``pgid`` (``ps -o pid= -g``); ``[fallback]``
    if that fails or comes back empty."""
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=", "-g", str(pgid)],
            capture_output=True, text=True, timeout=5,
        )
        pids = [int(p) for p in out.stdout.split() if p.strip().isdigit()]
        return pids or [fallback]
    except (OSError, subprocess.TimeoutExpired):
        return [fallback]


def _killpg(pgid: int) -> None:
    """SIGTERM the group, wait briefly, then SIGKILL any survivors."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _age(iso: str) -> str:
    """Humanized age for the ``ps`` table (empty when it cannot be parsed)."""
    if not iso:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = int((datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds())
    if secs < 0:
        return "0s"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


# ── run ────────────────────────────────────────────────────────────────────


def _cmd_run(args: argparse.Namespace) -> int:
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("error: no command to run (use `-- CMD…`)", file=sys.stderr)
        return 2

    outer = os.environ.get("CONCORD_PRINCIPAL", "")
    if args.name:
        principal_id = f"{args.kind}:{args.name}"
        parent = outer or None
    elif outer:
        principal_id = outer
        parent = None
    else:
        print("error: --name is required when CONCORD_PRINCIPAL is not set", file=sys.stderr)
        return 2
    if _reject_id(principal_id):
        return 2

    cwd = os.getcwd()
    fields: dict = {
        "harness": "shell",
        "host": _hostname(),
        "cwd": cwd,
        "worktree": _git_worktree(cwd),
        "pgid": 0,  # filled after Popen
        "pids": [],
        "kill_recipe": [],
    }
    tmux_pane = os.environ.get("TMUX_PANE")
    if tmux_pane:
        fields["tmux_pane"] = tmux_pane
    if parent:
        fields["labels"] = {"parent": parent}

    env = dict(os.environ)
    env["CONCORD_PRINCIPAL"] = principal_id
    proc = subprocess.Popen(cmd, start_new_session=True, env=env)
    # start_new_session makes the child its own group leader → pgid == its pid.
    pgid = proc.pid
    fields["pgid"] = pgid
    fields["pids"] = _group_pids(pgid, fallback=pgid)
    fields["kill_recipe"] = [f"kill -TERM -{pgid}"]

    stop = threading.Event()

    def _heartbeat() -> None:
        while not stop.wait(args.heartbeat_secs):
            # Loops spawn a fresh worker per step, so the live pid set moves;
            # `concord ps` and `stop` must see the current group, not the first.
            fields["pids"] = _group_pids(pgid, fallback=pgid)
            try:
                cn_client.coord_upsert_principal(principal_id, fields)
            except (cn_client.CoordUnavailable, cn_client.CoordHTTPError):
                pass  # fail open: retry on the next interval, silently

    threading.Thread(target=_heartbeat, daemon=True).start()

    # Initial registration — fail open: warn once, run the child anyway.
    try:
        cn_client.coord_upsert_principal(principal_id, fields)
    except cn_client.CoordUnavailable as exc:
        print(f"warning: ContextNest unavailable: {exc}", file=sys.stderr)
    except cn_client.CoordHTTPError as exc:
        print(f"warning: ContextNest: {exc.status} {exc.body}", file=sys.stderr)

    def _forward(signum: int, _frame) -> None:
        try:
            os.killpg(pgid, signum)
        except ProcessLookupError:
            pass

    old_int = signal.signal(signal.SIGINT, _forward)
    old_term = signal.signal(signal.SIGTERM, _forward)
    try:
        rc = proc.wait()
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        stop.set()
        _killpg(pgid)
        try:
            cn_client.coord_end_principal(principal_id)
        except (cn_client.CoordUnavailable, cn_client.CoordHTTPError):
            pass  # fail open on teardown too
    return rc


# ── ps ─────────────────────────────────────────────────────────────────────


def _cmd_ps(args: argparse.Namespace) -> int:
    status = "all" if args.all else "active"
    rc, data = _call(cn_client.coord_list_principals, status=status)
    if rc:
        return rc
    if args.json:
        print(json.dumps(data))
        return 0
    principals = (data or {}).get("principals") or []
    print("PRINCIPAL\tSTATUS\tAGE\tLAST SEEN\tPGID\tPIDS\tPANE\tCWD\tUNACKED")
    for p in principals:
        pgid = p.get("pgid") or ""
        pids = ",".join(str(x) for x in (p.get("pids") or []))
        print(
            f"{p.get('principal_id', '')}\t{p.get('status', '')}\t{_age(p.get('started_at'))}"
            f"\t{p.get('last_seen', '')}\t{pgid}\t{pids}\t{p.get('tmux_pane') or ''}"
            f"\t{p.get('cwd') or ''}\t{p.get('unacked_messages', '')}"
        )
    return 0


# ── stop ───────────────────────────────────────────────────────────────────


def _cmd_stop(args: argparse.Namespace) -> int:
    pid = args.principal
    if _reject_id(pid):
        return 2
    rc, principal = _call(cn_client.coord_get_principal, pid)
    if rc:
        return rc
    principal = principal or {}
    raw_pgid = principal.get("pgid")
    try:
        pgid = int(raw_pgid) if raw_pgid is not None else None
    except (TypeError, ValueError):
        pgid = None
    if not pgid or pgid <= 1:
        print("refusing: no valid pgid recorded for the principal", file=sys.stderr)
        return 4
    if pgid == os.getpgid(0):
        print("refusing: the pgid is this process's own group", file=sys.stderr)
        return 4
    host = principal.get("host")
    if host and host != _hostname():
        print("refusing: the principal belongs to another host", file=sys.stderr)
        return 4
    if not args.yes:
        if not sys.stdin.isatty():
            print("refusing: not a tty; pass --yes to confirm", file=sys.stderr)
            return 4
        pids = ",".join(str(x) for x in (principal.get("pids") or [])) or "?"
        answer = input(f"kill -{args.signal} process group {pgid} (pids {pids}) of {pid}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("refusing: not confirmed", file=sys.stderr)
            return 4
    sig = signal.SIGKILL if args.signal == "KILL" else signal.SIGTERM
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        print("refusing: no permission to signal the group", file=sys.stderr)
        return 4
    try:
        cn_client.coord_end_principal(pid)
    except (cn_client.CoordUnavailable, cn_client.CoordHTTPError):
        pass
    return 0


# ── send / inbox / ack ─────────────────────────────────────────────────────


def _cmd_send(args: argparse.Namespace) -> int:
    pid = args.principal
    if _reject_id(pid):
        return 2
    body = " ".join(args.message)
    rc, _ = _call(cn_client.coord_send, pid, args.from_ or _default_sender(), body)
    return rc


def _cmd_inbox(args: argparse.Namespace) -> int:
    pid = args.principal or os.environ.get("CONCORD_PRINCIPAL", "")
    if not pid:
        print("error: --principal is required when CONCORD_PRINCIPAL is not set",
              file=sys.stderr)
        return 2
    if _reject_id(pid):
        return 2
    rc, data = _call(cn_client.coord_inbox, pid, unacked=True)
    if rc:
        return rc
    messages = (data or {}).get("messages") or []
    if args.format == "json":
        print(json.dumps(data))
    elif messages:
        # prompt (and text) render the compact block; empty renders nothing so
        # a loop can prepend unconditionally.
        lines = [f"[concord] {len(messages)} message(s) for {pid}"]
        for m in messages:
            lines.append(
                f"- {m.get('msg_id')} from {m.get('from')} "
                f"({m.get('created_at')}): {m.get('body')}"
            )
        print("\n".join(lines))
    if args.ack:
        for m in messages:
            _call(cn_client.coord_ack, pid, m.get("msg_id"), _default_sender())
    return 0


def _cmd_ack(args: argparse.Namespace) -> int:
    pid = args.principal
    if _reject_id(pid):
        return 2
    rc, _ = _call(cn_client.coord_ack, pid, args.msg_id, _default_sender())
    return rc


# ── main ───────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.action == "run":
        return _cmd_run(args)
    if args.action == "ps":
        return _cmd_ps(args)
    if args.action == "stop":
        return _cmd_stop(args)
    if args.action == "send":
        return _cmd_send(args)
    if args.action == "inbox":
        return _cmd_inbox(args)
    if args.action == "ack":
        return _cmd_ack(args)
    if args.action == "help":
        _build_parser().print_help()
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
