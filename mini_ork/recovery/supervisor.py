"""The self-driving supervisor: one sweep closes the three parked-run gaps.

The self-driving loop parks an engine-fault run on a fix epic
(``escalation._record_waiting_on_fix`` writes ``runs/<id>/repair.json`` with
``state="waiting_on_fix"`` and ``fix_epic_id``), but nothing came back for it:
a human revived the run after the fix merged, the diagnosis ledger stayed
unlabelled, and a ``repair.json`` left in ``repairing`` by a dead process was
never cleared. :func:`sweep` closes all three under one pass:

    R1 — revive:     parked run + fix epic ``done`` + a landing signal on
                     ``origin/main`` → re-dispatch the failed node.
    R2 — label:      a revived run that reached a terminal status → label the
                     diagnosis ledger rows that carried its ``fix_epic_id``.
    R3 — orphan:     ``repairing`` with an old last attempt and a dead owner
                     process → ``gave_up``.

No rule asks a human for approval: the operator is *notified* through the
oversight inbox (``mo_inbox_gates``), never blocked on it. Every
``repair.json`` rewrite is atomic and keeps every key it does not change, and
one unreadable run dir never stops the sweep.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from mini_ork.recovery.escalation import _TERMINAL_UNSUCCESSFUL
from mini_ork.run_status import TERMINAL as _TERMINAL_RUN_STATUS

__all__ = ["sweep"]

SUPERVISOR_GATE = "self-driving-supervisor"
_FIX_EPIC_FEATURE = "fix epic failed: {run_id}"
_LANDING_REF = "origin/main"
_ATTEMPT_FILE = "repair.json"


# ── small io / time helpers ─────────────────────────────────────────────────


def _as_epoch(value: Any) -> float | None:
    """A timestamp from an int/float epoch or an ISO-8601 string; None if unreadable.

    ``repair.json`` attempts written by ``auto_repair`` carry an ISO string
    (``_now_iso``) while the hermetic fixtures use epoch ints; ``task_runs``
    and ``revived_at`` are epochs. Parsing both keeps the age comparisons from
    raising on the value they cannot read — an unreadable timestamp fails
    closed (the caller leaves the run alone) instead of clearing state.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        from datetime import datetime  # noqa: PLC0415 — only on the ISO path

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _read_repair(run_dir: Path) -> dict[str, Any]:
    """Strict read: a corrupt file raises so the sweep records an ``error``."""
    data = json.loads((run_dir / _ATTEMPT_FILE).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("repair.json is not a JSON object")
    return data


def _write_repair(run_dir: Path, data: dict[str, Any]) -> None:
    """Atomic write: a temp file in the same directory, then ``os.replace``.

    Mirrors ``escalation._write_repair`` (kept local, like that module keeps its
    own copy, because the two consumers sit on opposite sides of an import
    cycle). ``os.replace`` is atomic on the same filesystem, so a reader never
    observes a half-written ``repair.json``.
    """
    target = run_dir / _ATTEMPT_FILE
    tmp = run_dir / f".{_ATTEMPT_FILE}.tmp.{os.getpid()}.{time.time_ns()}"
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ── sqlite reads (the sweep never writes the DB directly) ───────────────────


def _connect(db: str):
    con = sqlite3.connect(str(db), timeout=5.0)
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _query(db: str, sql: str, args: tuple = ()) -> list[tuple]:
    """Run a read; a missing DB or table reads as ``[]`` (never raises).

    The read-only shape matters for ``dry_run``: opening a DB that does not
    exist would create it, so a missing file short-circuits to no rows.
    """
    if not db or not os.path.isfile(str(db)):
        return []
    try:
        con = _connect(db)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []


def _epic(db: str, epic_id: str) -> tuple[str, str | None] | None:
    rows = _query(db, "SELECT status, last_run_id FROM epics WHERE id=?",
                  (epic_id,))
    if not rows:
        return None
    status, last_run_id = rows[0]
    return str(status or ""), (str(last_run_id) if last_run_id else None)


def _task_run(db: str, run_id: str) -> tuple[str, Any] | None:
    rows = _query(db, "SELECT status, updated_at FROM task_runs WHERE id=?",
                  (run_id,))
    if not rows:
        return None
    return str(rows[0][0] or ""), rows[0][1]


def _checkpoint_status(db: str, run_id: str, node_id: str) -> str | None:
    rows = _query(db, "SELECT status FROM node_checkpoints"
                      " WHERE run_id=? AND node_id=?", (run_id, node_id))
    return str(rows[0][0] or "") if rows else None


def _ledger_ids(db: str, run_id: str, fix_epic_id: str | None) -> list[int]:
    if fix_epic_id:
        rows = _query(db, "SELECT id FROM diagnosis_ledger"
                          " WHERE run_id=? AND fix_epic_id=?",
                      (run_id, fix_epic_id))
    else:
        rows = _query(db, "SELECT id FROM diagnosis_ledger WHERE run_id=?",
                      (run_id,))
    return [int(r[0]) for r in rows]


def _inbox_row_exists(db: str, gate_id: str, feature: str) -> bool:
    rows = _query(db, "SELECT 1 FROM mo_inbox_gates"
                      " WHERE gate_id=? AND feature=? LIMIT 1",
                  (gate_id, feature))
    return bool(rows)


def _notify_once(db: str, run_id: str) -> None:
    """Enqueue one oversight notice, skipping when the row exists in any status."""
    feature = _FIX_EPIC_FEATURE.format(run_id=run_id)
    if _inbox_row_exists(db, SUPERVISOR_GATE, feature):
        return
    from mini_ork.gates import oversight_inbox  # noqa: PLC0415

    oversight_inbox.enqueue(
        SUPERVISOR_GATE, feature,
        context={"run_id": run_id,
                 "reason": "the fix epic for this run ended unsuccessfully"},
        db_path=db,
    )


# ── git landing signals ─────────────────────────────────────────────────────


def _git(engine_root: str, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(engine_root), *args],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None


def _fix_landed(engine_root: str, epic_id: str, last_run_id: str | None,
                home: Path, cache: dict[str, Any]) -> bool:
    """Did this epic's fix reach ``origin/main``?

    Signal (a): the patch id of the epic's last run diff is in
    ``code_findings.landed_patch_ids(engine_root, "origin/main")``. That helper
    runs one whole-repo ``git log -p``, so the map is computed lazily and
    cached for the sweep. Signal (b): a commit on ``origin/main`` whose message
    contains the epic id. Neither → not landed, which parks the run (the safe
    direction: a broken probe must never revive prematurely).
    """
    if last_run_id:
        from mini_ork.learning import code_findings  # noqa: PLC0415

        patch_id = code_findings.run_patch_id(str(home / "runs" / last_run_id))
        if patch_id:
            if "patch_ids" not in cache:
                cache["patch_ids"] = code_findings.landed_patch_ids(
                    str(engine_root), _LANDING_REF)
            if patch_id in cache["patch_ids"]:
                return True
    if not epic_id:
        return False
    proc = _git(engine_root, "log", _LANDING_REF, "--format=%H",
                "-F", f"--grep={epic_id}")
    return proc is not None and proc.returncode == 0 and bool(proc.stdout.strip())


def _rev_parse_head(engine_root: str) -> str | None:
    proc = _git(engine_root, "rev-parse", "HEAD")
    if proc is None or proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


# ── the default revive spawn ────────────────────────────────────────────────


def _default_spawn(home: Path, run_id: str, from_node: str | None,
                   engine_root: str) -> int | None:
    """Spawn a detached ``mini_ork.recovery.planner`` for the run.

    Modelled on ``auto_repair._spawn_recover``: same argv shape (``--from-node``
    + ``--force``), the same home/root pinning, detached session, and stdout and
    stderr to ``runs/<id>/supervisor-revive.log``. ``MO_AUTO_REPAIR=1`` makes
    the child the failure's owner so ``execute`` does not triage it a second
    time; ``MINI_ORK_PLAN_PATH`` is dropped because the recover builds its own
    plan (an inherited path breaks its positional-argv parse).
    """
    run_dir = Path(home) / "runs" / run_id
    argv = [sys.executable, "-m", "mini_ork.recovery.planner", str(run_id)]
    if from_node:
        argv += ["--from-node", str(from_node)]
    argv.append("--force")

    env = dict(os.environ)
    env["MINI_ORK_HOME"] = str(home)
    env["MINI_ORK_ROOT"] = str(engine_root)
    env["MO_AUTO_REPAIR"] = "1"
    # The recover derives the run id and run dir from its positional argument
    # (``recovery.planner`` handoff); an inherited run-scoped value from the
    # supervisor's own shell must never steer it, and an inherited plan path
    # breaks its positional-argv parse.
    for key in ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_PLAN_PATH"):
        env.pop(key, None)

    log_path = run_dir / "supervisor-revive.log"
    log_fh = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(engine_root),
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log_fh.close()
    return getattr(proc, "pid", None)


# ── per-rule helpers ────────────────────────────────────────────────────────


def _pick_from_node(repair: dict[str, Any], run_dir: Path) -> str | None:
    """``attempts[-1].from_node`` → ``attempts[-1].diagnosis.node`` → the
    run's own ``diagnosis.json`` ``node``."""
    attempts = repair.get("attempts")
    if isinstance(attempts, list) and attempts:
        last = attempts[-1]
        if isinstance(last, dict):
            node = last.get("from_node")
            if node:
                return str(node)
            diagnosis = last.get("diagnosis")
            if isinstance(diagnosis, dict) and diagnosis.get("node"):
                return str(diagnosis["node"])
    try:
        data = json.loads((run_dir / "diagnosis.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("node"):
        return str(data["node"])
    return None


def _owner_alive(run_dir: Path) -> bool:
    """Reuse the reaper's pid probe. A missing ``.pid`` is *unknown*, not dead."""
    from mini_ork.orchestration import run_reaper  # noqa: PLC0415

    try:
        return run_reaper.probe(run_dir).verdict == "alive"
    except Exception:  # pragma: no cover — the probe is best-effort by contract
        return True


# ── the sweep ───────────────────────────────────────────────────────────────


def sweep(home: str, db: str, *, engine_root: str, now: float | None = None,
          dry_run: bool = False, spawn=None, max_revives: int = 2,
          orphan_after_s: int = 3600) -> list[dict]:
    """One supervisor pass over ``home/runs/*/repair.json``.

    Args:
        home: mini-ork home (``<home>/runs/<id>`` holds the attempt files).
        db: ``state.db`` path — read for epics/task runs/checkpoints, and the
            target of R2's ledger labels and R1's oversight notices.
        engine_root: the checkout whose ``origin/main`` is the landing ref and
            whose ``HEAD`` is recorded on a revive.
        now: injectable clock (epoch seconds) for deterministic age checks.
        dry_run: compute the identical action list, write no file, touch no DB
            row, and never spawn.
        spawn: injectable ``callable(run_id, from_node) -> pid | None``; the
            default spawns a detached recover subprocess.
        max_revives: at most this many revives per sweep.
        orphan_after_s: a ``repairing`` attempt older than this with a dead
            owner is orphaned.

    Returns:
        One action dict per run acted on (``run_id`` / ``action`` / ``reason``
        plus any detail the action carries). A run left alone yields no dict.
    """
    home_p = Path(home)
    now_f = float(now) if now is not None else time.time()
    spawn_fn: Callable[[str, str | None], int | None] = (
        spawn if spawn is not None
        else (lambda run_id, from_node: _default_spawn(
            home_p, run_id, from_node, engine_root)))

    budget = {"left": max(0, int(max_revives))}
    land_cache: dict[str, Any] = {}
    actions: list[dict] = []

    for repair_path in sorted((home_p / "runs").glob(f"*/{_ATTEMPT_FILE}")):
        run_dir = repair_path.parent
        run_id = run_dir.name
        try:
            repair = _read_repair(run_dir)
        except (OSError, ValueError) as exc:
            actions.append({"run_id": run_id, "action": "error",
                            "reason": f"unreadable repair.json: {exc}"})
            continue

        state = repair.get("state")
        try:
            action = None
            if state == "waiting_on_fix":
                action = _rule_revive(run_id, run_dir, repair, db, engine_root,
                                      home_p, now_f, dry_run, spawn_fn, budget,
                                      land_cache)
            else:
                # R2 keys on the revive marker, not on ``state``: the revived
                # run's own auto-repair rewrites the state it inherits
                # (``repaired`` on publish, ``repairing``/``gave_up`` on a
                # failure) while preserving the supervisor's keys, so a
                # state-keyed rule would never see most revived runs.
                if repair.get("revived_at") and not repair.get("labelled_at"):
                    action = _rule_label(run_id, run_dir, repair, db, now_f,
                                         dry_run)
                if action is None and state == "repairing":
                    action = _rule_orphan(run_id, run_dir, repair, now_f,
                                          dry_run, orphan_after_s)
        except Exception as exc:  # one bad run dir never stops the sweep
            actions.append({"run_id": run_id, "action": "error",
                            "reason": f"{type(exc).__name__}: {exc}"})
            continue

        if action is not None:
            actions.append(action)

    return actions


def _rule_revive(run_id: str, run_dir: Path, repair: dict[str, Any], db: str,
                 engine_root: str, home: Path, now: float, dry_run: bool,
                 spawn: Callable[[str, str | None], int | None], budget: dict,
                 land_cache: dict) -> dict | None:
    """R1 — revive a parked run once its fix epic's change is on origin/main."""
    epic_id = repair.get("fix_epic_id")
    if not epic_id:
        return None
    epic_id = str(epic_id)
    epic = _epic(db, epic_id)
    if epic is None:
        return {"run_id": run_id, "action": "wait",
                "reason": f"fix epic {epic_id} not found"}
    status, last_run_id = epic

    if status in _TERMINAL_UNSUCCESSFUL:
        note = f"fix epic {epic_id} ended {status}"
        updated = dict(repair)
        updated["state"] = "gave_up"
        updated["note"] = note
        if not dry_run:
            _write_repair(run_dir, updated)
            _notify_once(db, run_id)
        return {"run_id": run_id, "action": "gave_up", "reason": note,
                "epic_id": epic_id}

    if status != "done":
        return {"run_id": run_id, "action": "wait",
                "reason": f"fix epic {epic_id} is {status}"}

    if not _fix_landed(engine_root, epic_id, last_run_id, home, land_cache):
        return {"run_id": run_id, "action": "wait",
                "reason": "fix epic done but not on origin/main"}

    from_node = _pick_from_node(repair, run_dir)
    if not from_node:
        return {"run_id": run_id, "action": "skip",
                "reason": "no failed node recorded"}

    if budget["left"] <= 0:
        return {"run_id": run_id, "action": "wait",
                "reason": "revive budget exhausted for this sweep"}

    budget["left"] -= 1
    revived = dict(repair)
    revived["state"] = "reviving"
    revived["revived_at"] = int(now)
    revived["revive_from_node"] = from_node
    if dry_run:
        return {"run_id": run_id, "action": "revive",
                "reason": f"fix epic {epic_id} landed on origin/main",
                "from_node": from_node, "epic_id": epic_id}

    revived["revive_engine_sha"] = _rev_parse_head(engine_root)
    revived["revive_pid"] = spawn(run_id, from_node)
    _write_repair(run_dir, revived)
    return {"run_id": run_id, "action": "revive",
            "reason": f"fix epic {epic_id} landed on origin/main",
            "from_node": from_node, "epic_id": epic_id}


def _rule_label(run_id: str, run_dir: Path, repair: dict[str, Any], db: str,
                now: float, dry_run: bool) -> dict | None:
    """R2 — label the diagnosis once a revived run reaches a terminal status."""
    task = _task_run(db, run_id)
    if task is None:
        return None
    status, updated_at = task
    if status not in _TERMINAL_RUN_STATUS:
        return None  # still running: no action

    revived_at = _as_epoch(repair.get("revived_at"))
    updated_epoch = _as_epoch(updated_at)
    if revived_at is None or updated_epoch is None or updated_epoch <= revived_at:
        return None

    from_node = repair.get("revive_from_node")
    correct = status == "published"
    if not correct and from_node:
        correct = _checkpoint_status(db, run_id, str(from_node)) == "success"

    new_state = "revived" if correct else "revive_failed"
    reason = (f"revived run {status} — diagnosis was "
              f"{'right' if correct else 'wrong'}")
    if not dry_run:
        fix_epic_id = repair.get("fix_epic_id")
        from mini_ork.stores import diagnosis_ledger  # noqa: PLC0415

        for ledger_id in _ledger_ids(db, run_id,
                                     str(fix_epic_id) if fix_epic_id else None):
            diagnosis_ledger.set_label(db, ledger_id, correct, reason)
        updated = dict(repair)
        updated["labelled_at"] = int(now)
        updated["label_correct"] = bool(correct)
        # Only the supervisor's own marker state is replaced; a state the
        # revived run's auto-repair wrote (repaired / repairing / gave_up) is
        # that loop's to own.
        if repair.get("state") == "reviving":
            updated["state"] = new_state
        _write_repair(run_dir, updated)
    return {"run_id": run_id, "action": "label", "reason": reason,
            "correct": correct}


def _rule_orphan(run_id: str, run_dir: Path, repair: dict[str, Any], now: float,
                 dry_run: bool, orphan_after_s: int) -> dict | None:
    """R3 — clear a ``repairing`` state whose owner process is gone."""
    attempts = repair.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return None
    last = attempts[-1]
    ts = _as_epoch(last.get("ts")) if isinstance(last, dict) else None
    if ts is None:
        return None  # fail closed on a timestamp we cannot read
    if (now - ts) <= float(orphan_after_s):
        return None
    if _owner_alive(run_dir):
        return None

    note = "orphaned repairing state cleared by supervisor"
    if not dry_run:
        updated = dict(repair)
        updated["state"] = "gave_up"
        updated["note"] = note
        _write_repair(run_dir, updated)
    return {"run_id": run_id, "action": "gave_up", "reason": note}
