"""Run liveness and the reaper for runs whose dispatcher died.

A ``task_runs`` row reaches a terminal status (published / failed /
rolled_back) only when the process that owns the run writes it. A dispatcher
that dies without running its ``finally`` — SIGKILL, OOM, power loss — leaves
the row ``executing`` forever, and every reporter that buckets "not terminal"
as "working" shows it in flight (the IDE's Active dispatches table listed 17
such rows next to the one live run, 2026-10-07).

Owner contract: ``<run_dir>/.pid`` names the process that owns the run while
it is alive. ``mini-ork run`` claims it before classify and, at teardown,
writes a terminal status first and releases the file second (``cli/main.py``);
a standalone ``execute`` claims it for its own lifetime. So a ``.pid`` whose
processes are all gone proves the owner died without cleaning up — the only
case :func:`reap` acts on by default.

A row with no ``.pid`` is *unknown*: a queued child row, a legacy row, or an
older engine between phases. It is reaped only when the operator opts in with
``--stale-after`` and the row has been idle that long.

    mini-ork reap [--home H] [--dry-run] [--stale-after 6h] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mini_ork.dispatch.core import AGENT_PGIDS_FILE
from mini_ork.remote.providers.base import clear_stale_run_lock
from mini_ork.run_status import SQL_TERMINAL, TERMINAL, close_duration_ms

PID_FILE = ".pid"
# How long a stopped agent group gets to honour SIGTERM before SIGKILL.
_AGENT_TERM_GRACE_S = 5.0
# A process that started after its .pid was written is a different process
# wearing a recycled pid. The slack absorbs mtime / lstart second rounding.
_START_SLACK_S = 2


@dataclass(frozen=True)
class Probe:
    """``verdict`` is one of alive | dead | unknown | remote | paused | finished."""

    verdict: str
    pids: tuple[int, ...] = ()
    detail: str = ""


def _iso(ts: float) -> str:
    # UTC, always: notes are read by operators in other zones, and a naive
    # local timestamp (the old `time.localtime` shape) re-labels the same
    # moment differently on every host.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _verdict_passed(run_dir: Path) -> bool:
    """The run finished and passed: ``verdict.json`` says pass.

    An execute-only caller (e.g. libwit's verified-artifact) ends a passing run
    with no publish step, so its status stays ``executing``. That is finished
    work, not a death — never label it ``failed``.
    """
    try:
        data = json.loads((run_dir / "verdict.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and (data.get("verdict") == "pass" or data.get("pass") is True)


def _read_pids(path: Path) -> tuple[int, ...]:
    return tuple(int(p) for p in path.read_text(encoding="utf-8").split() if p.isdigit())


def _has_unanswered_ask(run_dir: Path) -> bool:
    """True when the run dir holds an ``asks/ask-*.json`` with ``answer`` unset.

    A headless run blocked on profile questions writes one ASK file per question
    (``mini_ork.cli.plan``) and exits 6 resumably. Until every ASK is answered
    the run is paused, not dead or failed — the reaper must leave it alone.
    """
    asks_dir = run_dir / "asks"
    if not asks_dir.is_dir():
        return False
    try:
        names = os.listdir(asks_dir)
    except OSError:
        return False
    for name in names:
        if not (name.startswith("ask-") and name.endswith(".json")):
            continue
        try:
            data = json.loads((asks_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("answer") in (None, ""):
            return True
    return False


# ── failure reason (derived from the run dir when the vocabulary has nothing) ─

# A derived note is one line of the artefact; it rides in a notes column read by
# humans and the IDE, so a whole repair log is never the answer.
_MAX_DERIVED_NOTE = 400
_FAILURE_WORD = re.compile(r"(?i)\b(?:fail|failed|fails|failure|error|crash|crashed)\b")
_REPAIR_FAILED_STEP = re.compile(r"^step\s+\d+:.*\bfailed:", re.IGNORECASE)


def _clip(line: str) -> str:
    """One bounded, single-line note."""
    line = line.strip()
    return line if len(line) <= _MAX_DERIVED_NOTE else line[: _MAX_DERIVED_NOTE - 1] + "…"


def _needs_change_note(run_dir: Path) -> str:
    """The first failure line of ``NEEDS-CHANGE.md`` — the gate's own prose.

    Headings and blank lines are skipped, and a line that reports a failure wins
    over the first line of prose; the first remaining line is the fallback (a
    file that names its failure without the word "failed" still says more than
    the caller's fixed vocabulary does)."""
    try:
        text = (run_dir / "NEEDS-CHANGE.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    for line in lines:
        if _FAILURE_WORD.search(line):
            return _clip(line)
    return _clip(lines[0]) if lines else ""


def _retry_hint_note(run_dir: Path) -> str:
    """Why the lane could not be retried, from ``retry-hint.json``.

    RB-30: every retry-hint shape the engine writes nests the reason under
    ``needs_change.detail`` (0 of 83 files carried a top-level ``detail``), so
    that is read first, then the hint's own ``notes[0]``, then the old
    top-level ``detail`` for pre-nesting files. A blank value is not a reason:
    it falls through to the next source.
    """
    try:
        data = json.loads((run_dir / "retry-hint.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    candidates = []
    needs_change = data.get("needs_change")
    if isinstance(needs_change, dict):
        candidates.append(needs_change.get("detail"))
    notes = data.get("notes")
    if isinstance(notes, list) and notes:
        candidates.append(notes[0])
    candidates.append(data.get("detail"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return _clip(candidate)
    return ""


def _repair_log_note(run_dir: Path) -> str:
    """The first ``step N: … failed:`` line across ``repair-*.log``."""
    for path in sorted(run_dir.glob("repair-*.log")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if _REPAIR_FAILED_STEP.match(line):
                return _clip(line)
    return ""


def _derive_failure_note(run_dir: Path) -> str:
    """A specific terminal-failure reason from the run dir's own artefacts.

    The fixed vocabulary says a run ended without a verdict; it cannot say *why*
    — and a run that died on a lane crash left the reason in its run dir all
    along (NEEDS-CHANGE.md, the retry hint, the repair log). Ordered by
    specificity: the gate's verdict prose, then the lane's retry detail, then the
    first failed repair step.

    Best-effort by contract: teardown must never raise because an artefact is
    missing, unreadable, or malformed — an unreadable source falls through to the
    next one, and no source at all returns ``""``.
    """
    return (_needs_change_note(run_dir) or _retry_hint_note(run_dir)
            or _repair_log_note(run_dir))


def _terminal_failure_notes(run_dir: Path, vocabulary: str, existing: str | None) -> str:
    """What to append to ``notes`` for a terminal failure.

    The vocabulary line plus the derived reason, ``"; "``-joined, each dropped
    when the row already carries it so a re-closed run never says the same thing
    twice. ``""`` means "leave the notes column as it is"."""
    already = existing or ""
    pieces = [vocabulary, _derive_failure_note(run_dir)]
    return "; ".join(p for p in pieces if p and p not in already)


# ── owner record (written by the run's own process) ─────────────────────────

def claim_pid_file(run_dir: Path) -> Path:
    """Name this process as the run's owner, replacing any stale owner."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / PID_FILE
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    return path


def release_pid_file(run_dir: Path) -> None:
    """Drop ``.pid`` only while it still names this process."""
    path = run_dir / PID_FILE
    try:
        if _read_pids(path) == (os.getpid(),):
            path.unlink()
    except FileNotFoundError:
        pass


def finalize_passed(db_path: str | Path, run_id: str, note: str, *, now: int | None = None) -> bool:
    """End a run that passed but has no publish step as ``published``.

    The ``task_runs`` CHECK allows three terminal statuses — published,
    rolled_back, failed — and only ``published`` records a pass. So a run that
    cleared every gate but delivers nothing beyond its run dir (no artifact
    contract, or a workflow without a publisher node) is ``published`` with
    ``note`` saying what was not delivered. Only a non-terminal row changes.
    """
    now = int(time.time()) if now is None else now
    con = sqlite3.connect(str(db_path), timeout=15.0)
    try:
        con.execute("PRAGMA busy_timeout = 15000")
        # Same active-time arithmetic as every other close: accumulated time
        # plus the segment since this attempt's marker (run_status helper), not
        # ``ended_at - created_at``.
        row = con.execute(
            "SELECT notes, duration_ms, ended_at, updated_at, created_at "
            "FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        prior_end = row[2] if row else None
        ended_at = now if prior_end is None else int(prior_end)
        duration = close_duration_ms(
            row[1] if row else 0, row[0] if row else None, ended_at=ended_at,
            updated_at=row[3] if row else None,
            created_at=row[4] if row else None,
            already_closed=prior_end is not None)
        cur = con.execute(
            "UPDATE task_runs SET status = 'published', notes = COALESCE(notes || '; ', '') || ?, "
            "updated_at = ?, ended_at = COALESCE(ended_at, ?), "
            "duration_ms = ? "
            "WHERE id = ? AND status NOT IN " + SQL_TERMINAL,
            (note, now, now, duration, run_id))
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()


_RUN_FINISHED_MARKER = ".run-finished-emitted"


def _emit_run_finished(run_id: str, status: str, verdict: str | None,
                       run_dir: Path | None = None) -> None:
    """Push a ``run_finished`` event through the simple-tier hook, once per run.

    A marker in the run dir makes it once-only: the publisher usually writes
    ``published`` mid-workflow, so the teardown finds the row already terminal
    and must still emit; a second teardown must not emit again.

    Lazy import + broad catch: teardown must never break because a hook is
    broken or missing.
    """
    try:
        if run_dir is not None:
            marker = Path(run_dir) / _RUN_FINISHED_MARKER
            if marker.exists():
                return
            try:
                marker.write_text(f"{status}\n", encoding="utf-8")
            except OSError:
                pass
        from mini_ork.observability.emit_hook import mo_emit_hook
        mo_emit_hook("run_finished", run_id,
                     json.dumps({"status": status, "verdict": verdict}))
    except Exception:
        pass


def _read_verdict(db_path: str | Path, run_id: str) -> str | None:
    """The run's ``task_runs.verdict`` on a read-only connection, or ``None``."""
    try:
        from mini_ork.sqlite_read import connect_readonly
        con = connect_readonly(db_path)
    except Exception:
        return None
    try:
        row = con.execute(
            "SELECT verdict FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        con.close()


def close_run_record(db_path: str | Path, run_id: str, run_dir: Path, *,
                     crashed: bool, rc: int | None = None, now: int | None = None) -> str | None:
    """Lifecycle teardown: a run that exits non-terminal gets a terminal status.

    Passed (``rc == 0``, no exception, ``verdict.json`` pass) → ``published``
    via :func:`finalize_passed`; anything else → ``failed``. Returns the status
    it replaced, or ``None`` when nothing changed. A cost-paused run keeps its
    status — it waits on ``mini-ork resume``. A human-review-paused run keeps
    its ``reviewing`` status too (kickoff hr-exec-park): the executor parked it
    for a person, ``.review-pause`` is the marker, and rewriting it to
    ``failed`` here would undo the gate's hand-off.

    The failure note is the fixed vocabulary line *plus* whatever the run dir
    says specifically (:func:`_derive_failure_note`) — §8.1 of the 48h triage:
    run-1791911811-5826 died on a lane crash and recorded no reason at all.

    Reaching a terminal status also clears the run's stale
    ``.node-provider.lock`` (:func:`clear_stale_run_lock`) — including when the
    row is ALREADY terminal on entry, because the publisher writes that status
    mid-workflow and this call is the only teardown a published run gets: the
    run is over either way, and the next reader must not find a provision lock
    nothing holds.
    """
    if not run_id or (run_dir / ".cost-pause").exists():
        return None
    if _has_unanswered_ask(run_dir):
        return None
    now = int(time.time()) if now is None else now
    con = sqlite3.connect(str(db_path), timeout=15.0)
    try:
        con.execute("PRAGMA busy_timeout = 15000")
        row = con.execute("SELECT status, notes, verdict FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        if row[0] in TERMINAL:
            # Terminal, but not by us: the publisher writes 'published' (or
            # 'failed' on abstain) mid-workflow, BEFORE the executor's ``finally``
            # reaches this call — and reap() only ever visits non-terminal rows,
            # so nothing later compensates. The run is provably over either way,
            # so its provisioning lock must not outlive it (AC3), exactly as on
            # the terminal transitions below. A missing row stays untouched:
            # there is no run whose lock this could be.
            clear_stale_run_lock(run_dir)
            # Same reason for the event: the usual published path ends here,
            # so run_finished still fires (once, by the run-dir marker).
            _emit_run_finished(run_id, row[0], row[2], run_dir)
            return None
        # Human-review-paused: the executor parked it and set ``reviewing``;
        # ``mini-ork approvals`` is the next move, so the status stays. The
        # sentinel alone is not enough: a run resumed past the gate (e.g. a
        # hand-run ``recover``) can still carry a stale ``.review-pause``,
        # and exempting it then would strand the run as ``executing``.
        if row[0] == "reviewing" and (run_dir / ".review-pause").exists():
            return None
        # A deadline exit returns 0 before verify runs: its verdict is unverified.
        if not crashed and rc == 0 and _verdict_passed(run_dir) and not (run_dir / ".deadline-hit").exists():
            con.close()
            done = finalize_passed(db_path, run_id, "lifecycle: passed; the workflow wrote no terminal status "
                                   "(no publish step ran)", now=now)
            # ``finalize_passed`` only ever moves a non-terminal row, so the run
            # is terminal now either way — no lock may outlive it.
            clear_stale_run_lock(run_dir)
            if done:
                # The connection is closed here; read the verdict read-only.
                _emit_run_finished(run_id, "published",
                                   _read_verdict(db_path, run_id), run_dir)
            return row[0] if done else None
        if _verdict_passed(run_dir) and not crashed and rc is None:
            return None  # outcome unknown: never fail finished work (bf2805dd)
        if _verdict_passed(run_dir) and rc not in (None, 0):
            note = f"verify or a later step failed after a passing execute (rc={rc})"
        elif crashed:
            note = "lifecycle exited on an exception before a verdict"
        elif (run_dir / ".deadline-hit").exists():
            note = "deadline hit before a verdict"
        else:
            note = "lifecycle exited without a verdict"
        note = _terminal_failure_notes(run_dir, note, row[1])
        changed = _mark_failed(con, run_id, row[0], None, "CRASH" if crashed else None,
                               note, ended_at=now, now=now)
        if changed:
            # Terminal transition this process performed: drop the run's stale
            # provision lock. Not on the ``not changed`` path — there the row
            # moved on to something we cannot claim is terminal.
            clear_stale_run_lock(run_dir)
            # ``row`` is still the row we read, and its verdict is untouched by
            # the failed transition. Emit once per terminal transition.
            _emit_run_finished(run_id, "failed", row[2], run_dir)
        return row[0] if changed else None
    finally:
        con.close()


# ── liveness probe ──────────────────────────────────────────────────────────

def _process(pid: int) -> tuple[str, float] | None:
    """``(stat, start epoch)`` from ``ps``; ``None`` when ``ps`` can't say."""
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2,
                             env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    parts = out.stdout.split()
    if out.returncode != 0 or len(parts) < 6:
        return None
    try:
        started = time.mktime(time.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None
    return parts[0], started


def _pid_alive(pid: int, written_at: float) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists, owned by another user
    info = _process(pid)
    if info is None:
        return True  # can't prove a zombie or a recycled pid — count it alive
    stat, started = info
    return not stat.startswith("Z") and started <= written_at + _START_SLACK_S


# ── agent process groups (written by dispatch/core.py) ─────────────────────

def _group_alive(pgid: int) -> bool:
    """True while any process (zombies included) still belongs to ``pgid``."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def _recorded_agent_groups(run_dir: Path) -> list[tuple[int, float]]:
    """``(pgid, recorded start ts)`` lines from ``.agent-pgids``, malformed skipped."""
    try:
        text = (run_dir / AGENT_PGIDS_FILE).read_text(encoding="utf-8")
    except OSError:
        return []
    groups: list[tuple[int, float]] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            groups.append((int(fields[0]), float(fields[1])))
        except ValueError:
            continue
    return groups


def _stop_agent_groups(run_dir: Path, *, grace_s: float = _AGENT_TERM_GRACE_S) -> int:
    """SIGTERM, then SIGKILL after ``grace_s``, every live agent group of a dead run.

    Agents are spawned detached (``start_new_session`` in
    ``dispatch/core.py``), so a dispatcher that dies without teardown leaves
    them running; the reaper fails the row and must also end the work it was
    driving (RBF-25). The recorded start ts reuses :func:`_pid_alive`'s
    start-time guard: a group whose leader started AFTER the record is a
    recycled pid and is never signalled — killing an unrelated process would be
    worse than the orphan.

    Best-effort: each group is independent, and an unreadable record or a
    lookup/permission failure is skipped rather than raised out of reap.
    Returns how many groups were signalled.
    """
    stopped = 0
    own_pgid = os.getpgrp()
    for pgid, recorded_at in _recorded_agent_groups(run_dir):
        if pgid <= 1 or pgid == own_pgid:
            continue  # never our own group, init, or an impossible pgid
        if not _pid_alive(pgid, recorded_at):
            continue
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            continue
        deadline = time.monotonic() + grace_s
        while _group_alive(pgid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _group_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        stopped += 1
    return stopped


def probe(run_dir: Path) -> Probe:
    """Is the process that owns this run still alive?

    A ``.pid`` naming processes decides alive/dead even when
    ``.remote-sync-state.json`` is present: a ``--placement remote`` run is
    still dispatched by a *local* process, and that process dying is exactly
    the case the reaper exists for. The marker only means "not local" for a
    run with no ``.pid`` at all (mirrored from a remote node).
    """
    if (run_dir / ".cost-pause").exists():
        return Probe("paused", detail="cost-paused; waits on `mini-ork resume`")
    if _has_unanswered_ask(run_dir):
        return Probe("paused", detail="blocked on profile answers; waits on `mini-ork resume --answer`")
    if _verdict_passed(run_dir):
        return Probe("finished", detail="verdict.json passed; finished without a publish step")
    pid_path = run_dir / PID_FILE
    try:
        pids = _read_pids(pid_path)
        written_at = pid_path.stat().st_mtime
    except FileNotFoundError:
        if (run_dir / ".remote-sync-state.json").is_file():
            return Probe("remote", detail="mirrored from a remote node; its pid is not local")
        return Probe("unknown", detail="no .pid")
    if not pids:
        return Probe("unknown", detail=".pid names no process")
    if any(_pid_alive(pid, written_at) for pid in pids):
        return Probe("alive", pids)
    shown = ", ".join(str(p) for p in pids)
    return Probe("dead", pids, f"pid {shown} gone (.pid written {_iso(written_at)})")


# ── reaper ──────────────────────────────────────────────────────────────────

def _last_activity(con: sqlite3.Connection, run_id: str, updated_at: int) -> int:
    """Newest sign of life: the row's ``updated_at``, its newest event, its newest heartbeat."""
    ev_at, hb_ms = con.execute(
        "SELECT MAX(created_at), MAX(last_heartbeat_at) FROM run_events WHERE run_id = ?",
        (run_id,)).fetchone()
    return max(int(updated_at or 0), int(ev_at or 0), int(hb_ms or 0) // 1000)


def _mark_failed(con: sqlite3.Connection, run_id: str, status: str, updated_at: int | None,
                 verdict: str | None, note: str, *, ended_at: int, now: int) -> bool:
    """``failed`` only if the row is still exactly as it was read (compare-and-set).

    ``note`` is appended (``"; "``-joined, append-only); an EMPTY note leaves the
    column untouched, which is how a caller says "the row already carries this
    reason" without writing a stray separator."""
    guard, params = "", ()
    if updated_at is not None:
        guard, params = " AND updated_at = ?", (updated_at,)
    # The crash close must add the revived attempt's active segment it is
    # reaping, not keep only the earlier attempts' accumulated time (the old
    # CASE kept ``duration_ms`` untouched). ``ended_at`` is the moment the run
    # provably stopped, so the segment ends there, not at ``now``.
    row = con.execute(
        "SELECT notes, duration_ms, ended_at, updated_at AS row_updated_at, "
        "created_at FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    prior_end = row[2] if row else None
    duration = close_duration_ms(
        row[1] if row else 0, row[0] if row else None, ended_at=ended_at,
        updated_at=row[3] if row else None,
        created_at=row[4] if row else None,
        already_closed=prior_end is not None)
    cur = con.execute(
        "UPDATE task_runs SET status = 'failed', verdict = COALESCE(verdict, ?), "
        "notes = CASE WHEN ? = '' THEN notes ELSE COALESCE(notes || '; ', '') || ? END, "
        "updated_at = ?, "
        "ended_at = COALESCE(ended_at, ?), "
        "duration_ms = ? "
        f"WHERE id = ? AND status = ?{guard}",
        (verdict, note, note, now, ended_at, duration, run_id, status, *params))
    con.commit()
    return cur.rowcount == 1


def _one_line(exc: BaseException) -> str:
    """An exception message collapsed to one line for a stderr note."""
    return " ".join(str(exc).split()) or exc.__class__.__name__


def _release_remote_nodes(run_dir: Path, run_id: str) -> int:
    """Free a provably-dead run's remote nodes; returns how many were freed.

    Best-effort by contract: each release is isolated, so a Daytona outage
    reports one stderr line and must never undo the reap that already marked
    the row ``failed``. The provider's TTL reaper is the net for a release
    that never lands. Called only after :func:`_mark_failed` wins its
    compare-and-set — above that guard, the row could still be live.

    The imports are deliberately local: monkeypatching the provider modules
    (the documented seam) must intercept the call.
    """
    from mini_ork.remote.providers import base as provider_base
    from mini_ork.remote.providers import run_nodes as provider_run_nodes

    released = 0
    if provider_base.node_record_path(run_dir).is_file():
        try:
            released += int(bool(provider_base.release_run_node(run_dir, os.environ)))
        except Exception as exc:  # noqa: BLE001 — one failure must not strand the other
            sys.stderr.write(
                f"mini-ork reap: {run_id}: release_run_node failed: {_one_line(exc)}\n")
    try:
        released += int(provider_run_nodes.release_all_nodes(run_dir, os.environ) or 0)
    except Exception as exc:  # noqa: BLE001 — never raise out of reap
        sys.stderr.write(
            f"mini-ork reap: {run_id}: release_all_nodes failed: {_one_line(exc)}\n")
    return released


def reap(home: Path, *, dry_run: bool = False, stale_after: int | None = None,
         now: int | None = None) -> list[dict[str, Any]]:
    """Fail every non-terminal run whose owner provably died.

    ``stale_after`` (seconds) also fails runs with no ``.pid`` that have been
    idle at least that long — an operator decision, never the default.
    Returns one entry per reaped (or, with ``dry_run``, reapable) run.
    """
    home = Path(home)
    db_path = home / "state.db"
    if not db_path.is_file():
        return []
    now = int(time.time()) if now is None else now
    reaped: list[dict[str, Any]] = []
    con = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        rows = con.execute(
            "SELECT id, status, updated_at FROM task_runs "
            "WHERE status NOT IN " + SQL_TERMINAL).fetchall()
        for run_id, status, updated_at in rows:
            run_dir = home / "runs" / run_id
            found = probe(run_dir)
            if found.verdict == "dead":
                verdict, note = "CRASH", f"reaped: dispatcher {found.detail}"
                last = _last_activity(con, run_id, updated_at)
            elif found.verdict == "unknown" and stale_after is not None:
                last = _last_activity(con, run_id, updated_at)
                if now - last < stale_after:
                    continue
                verdict, note = None, f"reaped: no live dispatcher ({found.detail}; idle since {_iso(last)})"
            else:
                continue
            entry = {"run_id": run_id, "previous_status": status, "verdict": verdict,
                     "reason": note, "pids": list(found.pids)}
            if not dry_run:
                if not _mark_failed(con, run_id, status, updated_at, verdict, note,
                                    ended_at=last, now=now):
                    continue  # the row moved on after it was read
                from mini_ork.web.control import _close_dangling_node_events
                from mini_ork.web.db import db_for

                # A dangling node_start reads as "running" in every DAG view.
                _close_dangling_node_events(db_for(home), run_id)
                if found.verdict == "dead":
                    (run_dir / PID_FILE).unlink(missing_ok=True)
                    # The owner died without teardown, so its agents still run
                    # in their detached process groups. Stop them BEFORE the
                    # remote release: they are work this run was driving and
                    # nothing else will ever end it (RBF-25).
                    stopped = _stop_agent_groups(run_dir)
                    # Its Daytona node and per-node sandboxes were never
                    # released either. The row is provably `failed` by now, so
                    # no provisioner can adopt them.
                    released = _release_remote_nodes(run_dir, run_id)
                    parts = []
                    if stopped:
                        parts.append(f"stopped {stopped} agent group(s)")
                    parts.append(f"released {released} node(s)")
                    suffix = "; ".join(parts)
                    con.execute(
                        "UPDATE task_runs SET notes = COALESCE(notes || '; ', '') || ? "
                        "WHERE id = ?", (suffix, run_id))
                    con.commit()
                    entry["reason"] = f"{note}; {suffix}"
                # The row is terminal now: no provision lock may outlive the run
                # whose owner died (§8.3's 0-byte locks sat in exactly these dirs).
                clear_stale_run_lock(run_dir)
            reaped.append(entry)
    finally:
        con.close()
    return reaped


# ── CLI ─────────────────────────────────────────────────────────────────────

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _duration(text: str) -> int:
    match = re.fullmatch(r"(\d+)([smhd])", text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"expected <n>s|m|h|d, got {text!r}")
    return int(match.group(1)) * _UNITS[match.group(2)]


def build_parser() -> argparse.ArgumentParser:
    """The argv contract, shared by ``main`` and the IDE's action checks."""
    parser = argparse.ArgumentParser(
        prog="mini-ork reap",
        description="Fail runs whose dispatcher died without writing a terminal status.")
    parser.add_argument("--home", default=None, help="the .mini-ork home (default: $MINI_ORK_HOME, else ./.mini-ork)")
    parser.add_argument("--dry-run", action="store_true", help="list what would be reaped; write nothing")
    parser.add_argument("--stale-after", type=_duration, default=None, metavar="DUR",
                        help="also reap runs with no .pid idle at least DUR (e.g. 6h)")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home = Path(args.home or os.environ.get("MINI_ORK_HOME") or Path.cwd() / ".mini-ork")
    home = home.expanduser().absolute()
    if not (home / "state.db").is_file():
        sys.stderr.write(f"mini-ork reap: no state.db under {home}\n")
        return 2
    reaped = reap(home, dry_run=args.dry_run, stale_after=args.stale_after)
    if args.json:
        sys.stdout.write(json.dumps({"ok": True, "dry_run": args.dry_run, "reaped": reaped}) + "\n")
        return 0
    verb = "would reap" if args.dry_run else "reaped"
    for entry in reaped:
        sys.stdout.write(f"{verb} {entry['run_id']} (was {entry['previous_status']}): {entry['reason']}\n")
    sys.stdout.write(f"{len(reaped)} run(s) {verb}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
