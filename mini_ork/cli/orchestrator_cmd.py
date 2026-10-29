"""``mini-ork orchestrator watch`` — the per-run orchestrator's detached shell.

The policy lives in :mod:`mini_ork.orchestration.runner_orchestrator`
(``observe``); this module only holds the process: read the tick's inputs,
emit the decision, perform the one steering write, persist state, sleep.
It makes no model calls and owns no detection rule.

One process per run, enforced by a non-blocking ``flock`` on the run dir. The
kernel releases it when this process dies — including SIGKILL — so a second
invocation is a no-op and a crashed watcher never blocks its replacement.

Home tiers, same as the rest of the CLI:

* no ``--home`` — ``MINI_ORK_HOME`` else ``./.mini-ork``; starts even before
  ``state.db`` exists, and reads degrade to "not classified yet".
* explicit ``--home`` — must look initialised (``state.db`` or ``config/``),
  else exit 3. It never falls back to the cwd, because a mistyped home must
  not silently watch some other tree.

Exit codes: 0 watched (including "already watching"), 2 usage / no such run,
3 an explicit home that is not initialised.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from mini_ork.orchestration import runner_orchestrator as orch

_SAFE_TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
# How many run_events rows a tick reads. A recipe's event stream is small; the
# window only needs to contain every unmatched node_start.
_EVENT_WINDOW = 400


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mini-ork orchestrator",
        description="Observe a run between its node boundaries.",
    )
    sub = p.add_subparsers(dest="verb")
    watch = sub.add_parser("watch", help="watch one run until it ends")
    watch.add_argument("run_id", metavar="RUN_ID", help="the run to watch")
    # The IDE appends --home after the positional, so it belongs on the
    # subparser (the same contract as `kickoff new`).
    watch.add_argument("--home", default=None, help="the mini-ork home to read")
    watch.add_argument("--once", action="store_true",
                       help="run a single tick (operators and tests)")
    return p


def _is_safe_token(value: str) -> bool:
    return bool(value) and ".." not in value and all(c in _SAFE_TOKEN_CHARS for c in value)


def _default_home() -> Path:
    env = os.environ.get("MINI_ORK_HOME", "").strip()
    return Path(env).expanduser().absolute() if env else Path.cwd() / ".mini-ork"


def _resolve_home(explicit: str | None) -> Path | None:
    """The simple tier defaults; an explicit home must be initialised."""
    if not explicit:
        return _default_home()
    home = Path(explicit).expanduser().absolute()
    if (home / "state.db").is_file() or (home / "config").is_dir():
        return home
    return None


def _open_lock(run_dir: Path):
    """The exclusive watcher lock, or ``None`` when another process holds it."""
    handle = open(run_dir / orch.WATCH_LOCK_FILE, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _read_status(db_path: str, run_id: str) -> str | None:
    try:
        from mini_ork.sqlite_read import connect_readonly

        con = connect_readonly(db_path, timeout=2.0)
        try:
            row = con.execute("SELECT status FROM task_runs WHERE id = ?",
                              (run_id,)).fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — a missing/old db is "not classified yet"
        return None
    return str(row[0]) if row and row[0] is not None else None


def _read_events(db_path: str, run_id: str) -> tuple[dict[str, Any], ...]:
    try:
        from mini_ork.sqlite_read import connect_readonly

        con = connect_readonly(db_path, timeout=2.0)
        try:
            rows = con.execute(
                "SELECT event_type, payload_json, created_at FROM run_events "
                "WHERE run_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (run_id, _EVENT_WINDOW),
            ).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — no table yet is an empty history
        return ()
    return tuple(
        {"event_type": row[0], "payload_json": row[1], "created_at": row[2]}
        for row in reversed(rows)
    )


def _probe(run_dir: Path) -> tuple[str, str]:
    """``run_reaper.probe`` verdict, with a raising probe degraded to unknown."""
    try:
        from mini_ork.orchestration.run_reaper import probe

        verdict = probe(run_dir)
        return verdict.verdict, verdict.detail
    except Exception as exc:  # noqa: BLE001 — a permission error must not kill us
        return "unknown", f"probe failed: {exc}"


def _read_inputs(run_dir: Path, db_path: str, run_id: str) -> orch.WatchInputs:
    verdict, detail = _probe(run_dir)
    return orch.WatchInputs(
        status=_read_status(db_path, run_id),
        events=_read_events(db_path, run_id),
        probe=verdict,
        probe_detail=detail,
        pid_present=(run_dir / ".pid").is_file(),
        stop_requested=(run_dir / ".stop-requested").is_file(),
    )


def _emit(db_path: str, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
    try:
        from mini_ork.observability.node_events import mo_run_emit

        mo_run_emit(run_id, event_type, json.dumps(payload), db=db_path)
    except Exception as exc:  # noqa: BLE001 — observability never breaks the watcher
        print(f"[orchestrator] emit {event_type} failed: {exc}", file=sys.stderr)


def _apply_steer(db_path: str, run_id: str, request: orch.SteerRequest) -> bool:
    try:
        from mini_ork.web.control import steer_run
        from mini_ork.web.db import StateDB

        db = StateDB(Path(db_path))
    except Exception:  # noqa: BLE001 — no db means there is nothing to steer
        return False
    try:
        result = steer_run(
            db,
            run_id,
            request.message,
            role_target=request.role_target,
            severity="warn",
            source="orchestrator-watch",
            confidence=0.6,
            ttl_secs=request.ttl_secs,
        )
    except Exception as exc:  # noqa: BLE001 — a locked db is retried next tick
        print(f"[orchestrator] steer write failed: {exc}", file=sys.stderr)
        return False
    if not result.get("ok"):
        print(f"[orchestrator] steer rejected: {result.get('error')}", file=sys.stderr)
    return bool(result.get("ok"))


def _install_stop(stop: threading.Event) -> None:
    def _stop(_signum, _frame):
        stop.set()

    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, _stop)
        except (ValueError, OSError):  # not the main thread / unsupported
            continue


def _watch(run_id: str, home: Path, run_dir: Path, *, once: bool) -> int:
    lock = _open_lock(run_dir)
    if lock is None:
        print(f"already watching {run_id}")
        return 0

    from mini_ork.web.db import resolve_db

    db_path = resolve_db(home)
    config = orch.WatchConfig.from_env()
    state = orch.load_watch_state(run_dir)
    state.pid = os.getpid()
    stop = threading.Event()
    _install_stop(stop)

    print(f"orchestrator watch: {run_id} home={home} autonomy={config.autonomy} "
          f"interval={config.interval_s:g}s")

    try:
        while True:
            inputs = _read_inputs(run_dir, db_path, run_id)
            decision = orch.observe(
                now=time.time(),
                run_id=run_id,
                run_dir=run_dir,
                inputs=inputs,
                state=state,
                config=config,
                enabled_fn=lambda: orch.enabled() and orch.watch_enabled(),
            )
            for event_type, payload in decision.actions:
                _emit(db_path, run_id, event_type, payload)
                reason = str(payload.get("reason") or "")
                if event_type != "orchestrator.watching":
                    print(f"{event_type.removeprefix('orchestrator.')}: {reason}")

            if decision.steer is not None:
                request = decision.steer
                if _apply_steer(db_path, run_id, request):
                    state.steers += 1
                    state.steered[request.key] = time.time()
                    state.pending = None
                    _emit(db_path, run_id, "orchestrator.steered", {
                        "reason": f"steered {request.node_id}: {request.kind}",
                        "node_id": request.node_id,
                        "kind": request.kind,
                        "steers": state.steers,
                    })
                    print(f"steered: {request.node_id} ({request.kind})")

            orch.save_watch_state(run_dir, state)
            if decision.exit_reason:
                break
            if once:
                break
            if stop.wait(config.interval_s):
                state.ended_reason = "stopped"
                _emit(db_path, run_id, "orchestrator.ended", {
                    "reason": "stopped by signal",
                    "exit": "stopped",
                    "status": inputs.status or "",
                    "node_id": "",
                })
                orch.save_watch_state(run_dir, state)
                break
    finally:
        try:
            lock.close()
        except OSError:
            pass
    return 0


def main(rest: list[str] | None = None, root: str | None = None) -> int:
    del root
    argv = list(sys.argv[1:] if rest is None else rest)
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse already printed its usage line
        return int(exc.code or 0)
    if args.verb != "watch":
        sys.stderr.write("usage: mini-ork orchestrator watch <run_id> [--home H] [--once]\n")
        return 2
    run_id = str(args.run_id or "").strip()
    if not _is_safe_token(run_id):
        sys.stderr.write(f"mini-ork orchestrator: invalid run id {run_id!r}\n")
        return 2
    home = _resolve_home(args.home)
    if home is None:
        sys.stderr.write(
            f"mini-ork orchestrator: {args.home} is not an initialised mini-ork home "
            "(no state.db or config/)\n")
        return 3
    run_dir = home / "runs" / run_id
    if not run_dir.is_dir():
        sys.stderr.write(f"mini-ork orchestrator: no such run dir {run_dir}\n")
        return 2
    return _watch(run_id, home, run_dir, once=bool(args.once))


if __name__ == "__main__":
    raise SystemExit(main())
