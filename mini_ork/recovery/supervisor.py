"""The self-driving supervisor: one sweep closes the parked-run gaps.

The self-driving loop parks an engine-fault run on a fix epic
(``escalation._record_waiting_on_fix`` writes ``runs/<id>/repair.json`` with
``state="waiting_on_fix"`` and ``fix_epic_id``), but nothing came back for it:
a human revived the run after the fix merged, the diagnosis ledger stayed
unlabelled, and a ``repair.json`` left in ``repairing`` by a dead process was
never cleared. Two more gaps sit beside those: a failed run whose work was
delivered elsewhere stayed in the failed bucket, and a ``done`` epic whose
closing run never published kept no record of what delivered it.
:func:`sweep` closes all of them under one pass:

    R1 — revive:     parked run + fix epic ``done`` + a landing signal on
                     ``origin/main`` → re-dispatch the failed node.
    R2 — label:      a revived run that reached a terminal status → label the
                     diagnosis ledger rows that carried its ``fix_epic_id``.
    R3 — orphan:     ``repairing`` with an old last attempt and a dead owner
                     process → ``gave_up``.
    R4 — supersede:  a failed run whose work was delivered some other way (a
                     later published run of the same kickoff, its diff landed
                     on ``origin/main``, or a ``done`` epic it attempted) →
                     terminal ``superseded`` (not unsuccessful).
    R5 — back-link:  a ``done`` epic whose closing run did not publish (and has
                     no ``delivered_by=`` note yet) → record the delivering run
                     or ``origin/main`` commit on the epic's notes.
    R6 — apply:      a failed run that no repair loop owns, with a still
                     ``proposed`` graph patch → apply the lowest one through
                     ``graph_patch.apply`` (budget-shared with R1), unless the
                     control arm shows its kind is no better than a blind
                     rerun. R2 also writes the ``NNN.outcome.json`` file R6's
                     credit gate and ``mini-ork graph-patch credit`` read.
    R7 — split:      a run ``waiting_on_children`` from a ``split`` graph
                     patch → supersede the parent once every child epic is
                     ``done``, or give up (and notify once) when one child
                     ``escalated``/``abandoned``.

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
from subprocess import DEVNULL, STDOUT
from typing import Any, Callable

from mini_ork.learning.code_findings import landed_patch_ids, run_patch_id
from mini_ork.recovery.escalation import _TERMINAL_UNSUCCESSFUL
from mini_ork.run_status import TERMINAL as _TERMINAL_RUN_STATUS

__all__ = ["sweep"]

SUPERVISOR_GATE = "self-driving-supervisor"
_FIX_EPIC_FEATURE = "fix epic failed: {run_id}"
_SPLIT_FAILED_FEATURE = "split child failed: {run_id}"
_LANDING_REF = "origin/main"
_ATTEMPT_FILE = "repair.json"
#: The child epic statuses that mean a split cannot deliver (R7).
_SPLIT_FAILED_CHILD_STATUSES = frozenset({"escalated", "abandoned"})
#: ``repair.json`` states that mean an active loop owns the run: R4 must never
#: supersede one out from under it.
_SUPERSEDE_OWNED_STATES = frozenset({"waiting_on_fix", "reviving", "repairing",
                                     "waiting_on_children"})
#: R4's budget: the most recent failed rows, newest first.
_SUPERSEDE_SCAN_LIMIT = 500
#: R6: ``repair.json`` states a run may be in when the supervisor may apply a
#: proposed patch. ``None`` (no ``repair.json`` at all) is eligible too.
_R6_ELIGIBLE_STATES = frozenset({"", "gave_up", "revive_failed"})
#: R6's hard ceiling on applied patches per run. The env var tightens it,
#: never raises it.
_DEFAULT_MAX_PATCHES_PER_RUN = 2


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


def _patch_key(repair: dict[str, Any]) -> str | None:
    """The ``NNN`` revive patch recorded by ``graph_patch.apply``, if any.

    ``graph_patch`` writes it zero-padded, but an older ``repair.json`` could
    carry a bare integer-looking string; normalising here keeps the outcome
    file name and the action's ``patch`` value canonical.
    """
    raw = repair.get("revive_patch")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return f"{int(text):03d}"
    except (TypeError, ValueError):
        return None


def _split_patch_key(repair: dict[str, Any]) -> str | None:
    """The ``NNN`` split patch recorded by ``graph_patch.apply``, if any."""
    raw = repair.get("split_patch")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return f"{int(text):03d}"
    except (TypeError, ValueError):
        return None


def _write_outcome_once(run_dir: Path, patch: str, payload: dict[str, Any]) -> bool:
    """Write ``graph-patches/NNN.outcome.json`` exactly once.

    ``O_CREAT|O_EXCL`` is the write-once guarantee: an existing file (written
    by an earlier sweep or an operator) is left byte-for-byte alone. Returns
    whether this call created the file.
    """
    directory = run_dir / "graph-patches"
    destination = directory / f"{patch}.outcome.json"
    if destination.is_file():
        return False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False  # another sweep won the race — its file is the outcome
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except OSError:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    return True


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


def _write(db: str, sql: str, args: tuple = ()) -> int:
    """Run one targeted write; return the rowcount (0 on any DB error).

    A missing DB file is never conjured (same cold-safe rule as :func:`_query`),
    and a failed statement leaves the sweep going — one row the schema cannot
    hold must not stop the other rules.
    """
    if not db or not os.path.isfile(str(db)):
        return 0
    try:
        con = _connect(str(db))
        try:
            cur = con.execute(sql, args)
            con.commit()
            return int(cur.rowcount or 0)
        finally:
            con.close()
    except sqlite3.Error:
        return 0


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


# Terminal statuses whose outcome cannot judge a repair's diagnosis (R2's
# repair-ledger path): the run's work was delivered by another run, or the run
# turned out to be unnecessary.
_UNJUDGEABLE_STATUS = frozenset({"superseded", "not_needed"})


def _unlabelled_ledger_ids(db: str, run_id: str) -> list[int]:
    """Unlabelled ledger row ids for ``run_id``; ``[]`` on any read error.

    The repair-ledger path writes rows from the repair loop itself, so it
    labels exactly the rows still waiting for a verdict — a row already
    labelled by another pass is left alone. ``_query`` already returns ``[]``
    for a missing DB or a schema that lacks the table.
    """
    try:
        rows = _query(db, "SELECT id FROM diagnosis_ledger"
                          " WHERE run_id=? AND label IS NULL", (run_id,))
        return [int(r[0]) for r in rows]
    except Exception:  # noqa: BLE001 — a label read never stops the sweep
        return []


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


def _notify_split_failed_once(db: str, run_id: str, child_id: str,
                              status: str) -> None:
    """Enqueue one split-failure notice, skipping when it already exists."""
    feature = _SPLIT_FAILED_FEATURE.format(run_id=run_id)
    if _inbox_row_exists(db, SUPERVISOR_GATE, feature):
        return
    from mini_ork.gates import oversight_inbox  # noqa: PLC0415

    oversight_inbox.enqueue(
        SUPERVISOR_GATE, feature,
        context={"run_id": run_id, "child_epic_id": child_id,
                 "child_status": status,
                 "reason": "a split child epic ended unsuccessfully"},
        db_path=db,
    )


# ── git landing signals ─────────────────────────────────────────────────────


def _git(engine_root: str, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(engine_root), *args],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None


def _landed_ids(engine_root: str, cache: dict[str, Any]) -> dict:
    """The sweep's one ``{patch_id: sha}`` map for ``origin/main``.

    ``code_findings.landed_patch_ids`` runs one whole-repo ``git log -p``, so
    R1 and R4 share the ``"patch_ids"`` cache key: the map is computed at most
    once per sweep no matter how many runs ask for it.
    """
    if "patch_ids" not in cache:
        cache["patch_ids"] = landed_patch_ids(str(engine_root), _LANDING_REF)
    return cache["patch_ids"]


def _fix_landed(engine_root: str, epic_id: str, last_run_id: str | None,
                home: Path, cache: dict[str, Any]) -> bool:
    """Did this epic's fix reach ``origin/main``?

    Signal (a): the patch id of the epic's last run diff is in
    ``code_findings.landed_patch_ids(engine_root, "origin/main")``. That helper
    runs one whole-repo ``git log -p``, so the map is computed lazily and
    cached for the sweep (shared with R4). Signal (b): a commit on
    ``origin/main`` whose message contains the epic id. Neither → not landed,
    which parks the run (the safe direction: a broken probe must never revive
    prematurely).
    """
    if last_run_id:
        patch_id = run_patch_id(str(home / "runs" / last_run_id))
        if patch_id and patch_id in _landed_ids(engine_root, cache):
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
          orphan_after_s: int = 3600, dispatch_epics: bool = False,
          dispatch_max_in_progress: int = 1,
          dispatch_only: list[str] | None = None, dispatch_fn=None,
          dispatch_target_root: str = "") -> list[dict]:
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
        dispatch_epics: enable R8 — start one ready epic through the scheduler.
            Off by default because a dispatch spends money.
        dispatch_max_in_progress: R8's cap on epics already ``in progress``.
        dispatch_only: optional allow-list of epic ids R8 may pick.
        dispatch_fn: injectable
            ``callable(engine_root, home, db, epic_id, target_root) -> dict``;
            the default spawns a detached
            ``mini-ork scheduler --once --epic <id>``.
        dispatch_target_root: the working tree a dispatched run edits. Required
            to enable R8: it must be an absolute, existing directory that is
            neither the engine checkout, inside it, nor a parent of it.

    Returns:
        One action dict per run or epic acted on (``run_id`` or ``epic_id`` /
        ``action`` / ``reason`` plus any detail the action carries). The
        per-``repair.json`` rules come first, then R4's supersede pass, R6's
        apply pass, and R5's back-link dicts. Something left alone yields no
        dict.
    """
    home_p = Path(home)
    now_f = float(now) if now is not None else time.time()
    spawn_fn: Callable[[str, str | None], int | None] = (
        spawn if spawn is not None
        else (lambda run_id, from_node: _default_spawn(
            home_p, run_id, from_node, engine_root)))

    budget = {"left": max(0, int(max_revives))}
    land_cache: dict[str, Any] = {}
    # ``graph_patch.credit`` walks every run's patches; R6 shares one report
    # across the whole sweep.
    credit_cache: dict[str, Any] = {}
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
            if state == "waiting_on_children":
                action = _rule_split(run_id, run_dir, repair, db, now_f,
                                     dry_run)
            elif state == "waiting_on_fix":
                action = _rule_revive(run_id, run_dir, repair, db, engine_root,
                                      home_p, now_f, dry_run, spawn_fn, budget,
                                      land_cache)
            else:
                # R2 keys on the revive marker, not on ``state``: the revived
                # run's own auto-repair rewrites the state it inherits
                # (``repaired`` on publish, ``repairing``/``gave_up`` on a
                # failure) while preserving the supervisor's keys, so a
                # state-keyed rule would never see most revived runs.
                if ((repair.get("revived_at")
                     or repair.get("ledger_recorded_at"))
                        and not repair.get("labelled_at")):
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

    # R4/R5 are DB-wide rules, not per-run-dir ones: their dicts are appended
    # after every per-repair.json dict so the existing action order is stable.
    try:
        actions.extend(_rule_supersede(home_p, db, engine_root, land_cache,
                                       dry_run))
    except Exception as exc:  # one bad DB must never stop the sweep
        actions.append({"run_id": "", "action": "error",
                        "reason": f"supersede rule: {type(exc).__name__}: {exc}"})

    # R6 scans run dirs that carry a ``graph-patches/`` directory, not every
    # ``repair.json``: a failed run may have no repair state at all. It runs
    # after R4 so a run whose work was delivered elsewhere is superseded, not
    # revived from a patch.
    try:
        actions.extend(_rule_apply_patch(home_p, db, engine_root, dry_run,
                                         spawn, budget, credit_cache))
    except Exception as exc:  # one bad run dir never stops the sweep
        actions.append({"run_id": "", "action": "error",
                        "reason": f"apply-patch rule: {type(exc).__name__}: {exc}"})

    try:
        actions.extend(_rule_backlink(db, engine_root, dry_run))
    except Exception as exc:  # one bad DB must never stop the sweep
        actions.append({"epic_id": "", "action": "error",
                        "reason": f"backlink rule: {type(exc).__name__}: {exc}"})

    if dispatch_epics:
        try:
            actions.extend(_rule_dispatch_epic(
                home, db, engine_root, dry_run,
                max_in_progress=dispatch_max_in_progress,
                only=dispatch_only, dispatch_fn=dispatch_fn,
                target_root=dispatch_target_root))
        except Exception as exc:  # a failed spawn must never stop the sweep
            actions.append({"epic_id": "", "action": "error",
                            "reason": f"dispatch rule: {type(exc).__name__}: {exc}"})

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


def _rule_split(run_id: str, run_dir: Path, repair: dict[str, Any], db: str,
                now: float, dry_run: bool) -> dict | None:
    """R7 — close a run whose remaining work was split into child epics.

    Every child ``done`` → compare-and-set the parent ``failed → superseded``
    and record the outcome as passed. Any child ``escalated``/``abandoned`` →
    ``gave_up`` with one deduped oversight notice and a failed outcome. Anything
    else (still running, blocked, missing) → no action: the split is not done.
    """
    raw_ids = repair.get("child_epic_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return None
    child_ids = [str(child_id) for child_id in raw_ids if str(child_id)]
    if not child_ids:
        return None
    split_patch = _split_patch_key(repair)

    statuses: dict[str, str] = {}
    for child_id in child_ids:
        epic = _epic(db, child_id)
        statuses[child_id] = epic[0] if epic is not None else ""

    failed = [child_id for child_id in child_ids
              if statuses[child_id] in _SPLIT_FAILED_CHILD_STATUSES]
    if failed:
        child_id = failed[0]
        status = statuses[child_id]
        note = f"split child {child_id} ended {status}"
        if not dry_run:
            if split_patch:
                _write_outcome_once(run_dir, split_patch, {
                    "passed": False,
                    "status": "gave_up",
                    "child_epic_ids": child_ids,
                    "failed_child": child_id,
                    "failed_status": status,
                    "closed_at": int(now),
                })
            updated = dict(repair)
            updated["state"] = "gave_up"
            updated["note"] = note
            _write_repair(run_dir, updated)
            _notify_split_failed_once(db, run_id, child_id, status)
        action = {"run_id": run_id, "action": "split_failed", "reason": note,
                  "child_epic_ids": child_ids}
        if split_patch:
            action["patch"] = split_patch
        return action

    if not all(statuses[child_id] == "done" for child_id in child_ids):
        return None  # children still in flight (or not started): wait

    task = _task_run(db, run_id)
    if task is None or task[0] != "failed":
        return None  # only a failed parent is the split's to close

    note = (" superseded: delivered by split children "
            + ", ".join(child_ids))
    if not dry_run:
        if _write(
                db,
                "UPDATE task_runs SET status='superseded',"
                " notes=COALESCE(notes,'') || ?"
                " WHERE id=? AND status='failed'",
                (note, run_id)) != 1:
            return None  # the row moved on after it was read
        if split_patch:
            _write_outcome_once(run_dir, split_patch, {
                "passed": True,
                "status": "superseded",
                "child_epic_ids": child_ids,
                "closed_at": int(now),
            })
        updated = dict(repair)
        updated["state"] = "split_done"
        _write_repair(run_dir, updated)
    action = {"run_id": run_id, "action": "split_done",
              "reason": f"all split children done: {', '.join(child_ids)}",
              "child_epic_ids": child_ids}
    if split_patch:
        action["patch"] = split_patch
    return action


def _rule_label(run_id: str, run_dir: Path, repair: dict[str, Any], db: str,
                now: float, dry_run: bool) -> dict | None:
    """R2 — label the diagnosis once a revived or repaired run finishes.

    Two entry paths share this rule. A run the supervisor revived is judged by
    the ``revived_at`` marker exactly as before. A run whose repair loop
    recorded its diagnosis in the ledger (``ledger_recorded_at``, written by
    ``auto_repair.apply``) is judged by the run's terminal status: the repair
    was right iff the repaired run published.
    """
    task = _task_run(db, run_id)
    if task is None:
        return None
    status, updated_at = task
    if status not in _TERMINAL_RUN_STATUS:
        return None  # still running: no action

    if not repair.get("revived_at"):
        recorded_at = _as_epoch(repair.get("ledger_recorded_at"))
        updated_epoch = _as_epoch(updated_at)
        if (recorded_at is None or updated_epoch is None
                or updated_epoch <= recorded_at):
            return None  # not updated since the repair was recorded

        if status in _UNJUDGEABLE_STATUS:
            # The work landed elsewhere (superseded) or was never needed: the
            # outcome says nothing about the diagnosis, so it is not labelled —
            # a "wrong" label here would bias the calibrated bar. Stamp the
            # marker so the rule stops re-checking the run.
            reason = f"repaired run {status} — diagnosis not judged"
            if not dry_run:
                updated = dict(repair)
                updated["labelled_at"] = int(now)
                updated["label_skipped"] = status
                _write_repair(run_dir, updated)
            return {"run_id": run_id, "action": "label_skipped",
                    "reason": reason}

        correct = status == "published"
        reason = (f"repaired run {status} — diagnosis was "
                  f"{'right' if correct else 'wrong'}")
        if not dry_run:
            from mini_ork.stores import diagnosis_ledger  # noqa: PLC0415

            for ledger_id in _unlabelled_ledger_ids(db, run_id):
                diagnosis_ledger.set_label(db, ledger_id, correct, reason)
            updated = dict(repair)
            updated["labelled_at"] = int(now)
            updated["label_correct"] = bool(correct)
            # ``state`` is the repair loop's to own: labelling an outcome must
            # not rewrite it (unlike the revive path's reviving → revived).
            _write_repair(run_dir, updated)
        return {"run_id": run_id, "action": "label", "reason": reason,
                "correct": correct}

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
    patch = _patch_key(repair)
    if not dry_run:
        fix_epic_id = repair.get("fix_epic_id")
        from mini_ork.stores import diagnosis_ledger  # noqa: PLC0415

        for ledger_id in _ledger_ids(db, run_id,
                                     str(fix_epic_id) if fix_epic_id else None):
            diagnosis_ledger.set_label(db, ledger_id, correct, reason)
        if patch:
            # The outcome file is what ``graph_patch.credit`` joins back to the
            # patch kind (R6's control arm). Write it before ``labelled_at`` is
            # recorded so a failed write is retried by the next sweep instead
            # of being silently skipped forever.
            _write_outcome_once(run_dir, patch, {
                "passed": bool(correct),
                "status": status,
                "labelled_at": int(now),
            })
        updated = dict(repair)
        updated["labelled_at"] = int(now)
        updated["label_correct"] = bool(correct)
        # Only the supervisor's own marker state is replaced; a state the
        # revived run's auto-repair wrote (repaired / repairing / gave_up) is
        # that loop's to own.
        if repair.get("state") == "reviving":
            updated["state"] = new_state
        _write_repair(run_dir, updated)
    action = {"run_id": run_id, "action": "label", "reason": reason,
              "correct": correct}
    if patch:
        action["patch"] = patch
    return action


# ── R6: apply a proposed graph patch ───────────────────────────────────────


def _max_patches_per_run() -> int:
    """``MO_SUPERVISE_MAX_PATCHES_PER_RUN``, clamped to the hard default.

    Tighten-only (kickoff): a smaller value lowers the cap below 2, a larger
    one never raises it above 2. An unparseable value keeps the default, so a
    typo cannot switch the cap off.
    """
    raw = os.environ.get("MO_SUPERVISE_MAX_PATCHES_PER_RUN")
    if raw is None:
        return _DEFAULT_MAX_PATCHES_PER_RUN
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_MAX_PATCHES_PER_RUN
    return max(0, min(value, _DEFAULT_MAX_PATCHES_PER_RUN))


def _credit_report(cache: dict[str, Any], home: Path, graph_patch) -> dict:
    """``graph_patch.credit(home)`` computed at most once per sweep."""
    if "report" not in cache:
        cache["report"] = graph_patch.credit(home)
    return cache["report"]


def _argv_from_node(argv) -> str | None:
    """The ``--from-node`` value from the argv ``graph_patch.apply`` builds."""
    args = list(argv)
    try:
        index = args.index("--from-node")
    except ValueError:
        return None
    if index + 1 >= len(args):
        return None
    return str(args[index + 1])


def _rule_apply_patch(home: Path, db: str, engine_root: str, dry_run: bool,
                      spawn: Callable[[str, str | None], int | None] | None,
                      budget: dict, credit_cache: dict[str, Any]) -> list[dict]:
    """R6 — apply the lowest proposed graph patch on a failed, unparked run.

    Per run with a ``graph-patches/`` directory: only ``task_runs.status ==
    "failed"`` and a ``repair.json`` state that no loop owns (absent,
    ``gave_up``, ``revive_failed``) qualify. One applied patch without an
    outcome means a revive is in flight; ``MO_SUPERVISE_MAX_PATCHES_PER_RUN``
    bounds the run's total. When the picked kind is not ``rerun`` and the
    control arm has at least five samples on both sides, a kind that is not
    ``credited`` (strictly better than a blind rerun) is skipped.
    """
    from mini_ork.recovery import graph_patch  # noqa: PLC0415 — import-time flag

    cap = _max_patches_per_run()
    actions: list[dict] = []
    for patch_dir in sorted((home / "runs").glob(f"*/{graph_patch.PATCH_DIRNAME}")):
        if not patch_dir.is_dir():
            continue
        run_dir = patch_dir.parent
        run_id = run_dir.name
        try:
            action = _apply_one_patch(
                run_id, run_dir, patch_dir, home, db, engine_root, dry_run,
                spawn, budget, credit_cache, cap, graph_patch)
        except graph_patch.GraphPatchError as exc:
            actions.append({"run_id": run_id, "action": "error",
                            "reason": str(exc)})
            continue
        except Exception as exc:  # one bad run dir never stops the sweep
            actions.append({"run_id": run_id, "action": "error",
                            "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if action is not None:
            actions.append(action)
    return actions


def _apply_one_patch(run_id: str, run_dir: Path, patch_dir: Path, home: Path,
                     db: str, engine_root: str, dry_run: bool,
                     spawn: Callable[[str, str | None], int | None] | None,
                     budget: dict, credit_cache: dict[str, Any], cap: int,
                     graph_patch) -> dict | None:
    """One run's R6 decision: an action dict, or ``None`` when it is skipped."""
    task = _task_run(db, run_id)
    if task is None or task[0] != "failed":
        return None  # only a failed run that no other rule owns
    state = _repair_state(run_dir)
    if state is not None and state not in _R6_ELIGIBLE_STATES:
        return None  # a live repair loop owns the run (or it is unreadable)

    entries = graph_patch.list_patches(home, run_id)
    applied = [entry for entry in entries if entry.get("status") == "applied"]
    if any(entry.get("outcome") is None for entry in applied):
        return None  # one applied patch is still awaiting its R2 outcome
    if len(applied) >= cap:
        return None  # the per-run explore/exploit budget is spent

    # Read the replan flag at sweep time, never at import: SDE-6a flips it.
    replan_supported = bool(getattr(graph_patch, "REPLAN_SUPPORTED", False))
    chosen = next(
        (entry for entry in entries
         if entry.get("status") == "proposed"
         and (entry.get("kind") != "replan" or replan_supported)
         # A patch that already failed to apply is never retried: a replan
         # re-asks the model on every attempt, so a retry loop would spend on
         # every sweep for a patch that cannot materialize.
         and not (patch_dir / f"{int(entry['number']):03d}.apply-error.json").is_file()),
        None)
    if chosen is None:
        return None

    number = int(chosen["number"])
    kind = str(chosen.get("kind") or "")
    if kind != "rerun":
        report = _credit_report(credit_cache, home, graph_patch)
        mine = report.get(kind) or {"n": 0, "rate": 0.0, "credited": False}
        control = report.get("rerun") or {"n": 0, "rate": 0.0}
        if (int(mine.get("n") or 0) >= 5
                and int(control.get("n") or 0) >= 5
                and not mine.get("credited")):
            return {"run_id": run_id, "action": "skip",
                    "reason": (f"{kind} is not better than a blind rerun "
                               f"({float(mine.get('rate') or 0):.2f} vs "
                               f"{float(control.get('rate') or 0):.2f})")}

    if budget["left"] <= 0:
        return {"run_id": run_id, "action": "skip",
                "reason": "revive budget exhausted for this sweep"}
    budget["left"] -= 1

    action = {"run_id": run_id, "action": "apply_patch",
              "patch": f"{number:03d}", "kind": kind,
              "reason": f"applied {kind} patch {number:03d} on failed run"}
    if dry_run:
        # ``graph_patch.apply(dry_run=True)`` still materializes the workflow;
        # the sweep's dry run writes nothing at all, so preview the action
        # without calling it.
        return action

    adapter = None
    if spawn is not None:
        def adapter(argv, _run_id=run_id, _spawn=spawn):
            # The sweep's seam is ``spawn(run_id, from_node)``; the argv is how
            # ``graph_patch.apply`` names the node it would resume from.
            return _spawn(_run_id, _argv_from_node(argv))
    try:
        graph_patch.apply(home, run_id, number, engine_root=engine_root,
                          spawn=adapter)
    except graph_patch.GraphPatchError as exc:
        # Nothing was revived: give the budget back, and record the failure
        # once so later sweeps skip this patch instead of retrying it.
        budget["left"] += 1
        _write_apply_error_once(patch_dir, number, str(exc))
        return {"run_id": run_id, "action": "error", "reason": str(exc),
                "patch": f"{number:03d}"}
    return action


def _write_apply_error_once(patch_dir: Path, number: int, error: str) -> None:
    """``NNN.apply-error.json`` (write-once): R6 never picks this patch again."""
    destination = patch_dir / f"{int(number):03d}.apply-error.json"
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return
    except OSError:
        return  # best-effort: an unwritable marker just means another retry
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"error": error, "at": int(time.time())},
                                indent=2, sort_keys=True) + "\n")


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


# ── R4 / R5: delivered-elsewhere runs and epics ─────────────────────────────


def _repair_state(run_dir: Path) -> str | None:
    """The ``repair.json`` state, or ``"unreadable"`` when it cannot be read.

    A run with no ``repair.json`` has no owning loop (``None``). An unreadable
    one is treated as owned: R4 must never supersede a run whose repair state
    it cannot see.
    """
    path = run_dir / _ATTEMPT_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unreadable"
    if not isinstance(data, dict):
        return "unreadable"
    return str(data.get("state") or "")


def _rule_supersede(home: Path, db: str, engine_root: str, land_cache: dict,
                    dry_run: bool) -> list[dict]:
    """R4 — mark a failed run whose work was delivered some other way.

    Signals, first match wins: (a) a later ``task_runs`` row with the same
    ``kickoff_path`` published; (b) the patch id of the run's own
    ``framework-edit.diff`` is in ``origin/main``; (c) the run is an attempt of
    a ``done`` epic. Only the 500 most recent failed rows are scanned, and a
    run whose ``repair.json`` says a loop owns it is skipped.

    The write is a compare-and-set on ``status='failed'``: a row that moved on
    (or was already superseded by a concurrent sweep) is left alone, which is
    also what makes a second sweep append no second note.
    """
    rows = _query(
        db,
        "SELECT id, kickoff_path, created_at FROM task_runs"
        " WHERE status='failed'"
        " ORDER BY created_at DESC, id DESC LIMIT ?",
        (_SUPERSEDE_SCAN_LIMIT,))
    actions: list[dict] = []
    for raw_id, kickoff_path, created_at in rows:
        run_id = str(raw_id)
        run_dir = home / "runs" / run_id
        if _repair_state(run_dir) in _SUPERSEDE_OWNED_STATES | {"unreadable"}:
            continue  # a repair loop owns it; or we cannot tell, so we wait

        reason: str | None = None
        signal_run: str | None = None
        if kickoff_path:
            later = _query(
                db,
                "SELECT id FROM task_runs WHERE kickoff_path=?"
                " AND status='published' AND created_at > ?"
                " ORDER BY created_at ASC, id ASC LIMIT 1",
                (str(kickoff_path), created_at))
            if later:
                signal_run = str(later[0][0])
                reason = f"later run {signal_run} published"
        if reason is None:
            patch_id = run_patch_id(str(run_dir))
            if patch_id and patch_id in _landed_ids(engine_root, land_cache):
                reason = "diff landed on origin/main"
        if reason is None:
            done_epic = _query(
                db,
                "SELECT a.epic_id FROM epic_attempts a"
                " JOIN epics e ON e.id = a.epic_id"
                " WHERE a.run_id=? AND e.status='done'"
                " ORDER BY a.epic_id LIMIT 1",
                (run_id,))
            if done_epic:
                reason = f"epic {done_epic[0][0]} done"
        if reason is None:
            continue

        if not dry_run and _write(
                db,
                "UPDATE task_runs SET status='superseded',"
                " notes=COALESCE(notes,'') || ?"
                " WHERE id=? AND status='failed'",
                (f" superseded: {reason}", run_id)) != 1:
            continue  # the row moved on after it was read
        action = {"run_id": run_id, "action": "supersede", "reason": reason}
        if signal_run:
            action["later_run_id"] = signal_run
        actions.append(action)
    return actions


def _rule_backlink(db: str, engine_root: str, dry_run: bool) -> list[dict]:
    """R5 — record what delivered a ``done`` epic whose closing run did not.

    For every done epic whose ``last_run_id`` run is not ``published`` and whose
    notes carry no ``delivered_by=`` marker, the evidence is the latest
    published run among its ``epic_attempts``, else the first ``origin/main``
    commit whose message names the epic. No evidence → no action. The write
    re-checks the marker so two sweeps can never stamp twice.
    """
    rows = _query(db, "SELECT id, last_run_id, notes FROM epics"
                      " WHERE status='done'")
    actions: list[dict] = []
    for raw_id, last_run_id, notes in rows:
        epic_id = str(raw_id)
        if "delivered_by=" in str(notes or ""):
            continue
        if not last_run_id:
            continue  # no closing run to say what happened
        last = _task_run(db, str(last_run_id))
        if last is not None and last[0] == "published":
            continue  # the epic's own last run delivered it

        marker: str | None = None
        reason = ""
        published = _query(
            db,
            "SELECT a.run_id FROM epic_attempts a"
            " JOIN task_runs t ON t.id = a.run_id"
            " WHERE a.epic_id=? AND t.status='published'"
            " ORDER BY a.attempt DESC LIMIT 1",
            (epic_id,))
        if published and published[0][0]:
            delivering_run = str(published[0][0])
            marker = f"delivered_by={delivering_run}"
            reason = f"delivered by run {delivering_run}"
        else:
            proc = _git(engine_root, "log", _LANDING_REF, "--format=%H",
                        "-F", f"--grep={epic_id}")
            sha = ""
            if proc is not None and proc.returncode == 0:
                lines = (proc.stdout or "").splitlines()
                sha = lines[0].strip() if lines else ""
            if not sha:
                continue
            marker = f"delivered_by=commit:{sha}"
            reason = f"delivered by commit {sha}"

        if not dry_run and _write(
                db,
                "UPDATE epics SET notes=COALESCE(notes,'') || ?"
                " WHERE id=? AND status='done'"
                " AND COALESCE(notes,'') NOT LIKE '%delivered_by=%'",
                (f" {marker}", epic_id)) != 1:
            continue
        actions.append({"epic_id": epic_id, "action": "backlink",
                        "reason": reason})
    return actions


# ── R8: dispatch one ready epic ─────────────────────────────────────────────


def _default_dispatch_epic(engine_root: str, home: str, db: str,
                           epic_id: str, target_root: str) -> dict:
    """Spawn a detached ``mini-ork scheduler --once --epic <id>`` (R8).

    The child runs in ``target_root`` — the dedicated checkout the dispatched run
    edits, never the engine checkout itself. It gets the supervisor's
    home/root/DB pinned and every run-scoped key popped (the same list
    :func:`_default_spawn` drops) so an inherited run from the supervisor's own
    shell can never steer the dispatch. ``MO_TARGET_CWD`` names the target for
    the run, ``MO_ALLOW_FRAMEWORK_CWD=1`` lifts the run's "do not edit the
    engine" guard (correct only because R8 refused every unsafe target first),
    and ``MO_PLACEMENT`` defaults to ``local`` unless the operator set one.
    stdout and stderr go to ``<home>/state/supervisor-dispatch-<epic_id>.log``.
    On ``OSError`` it returns ``{"error": ...}`` and never raises — one failed
    spawn must not stop the sweep.
    """
    home_p = Path(home)
    state_dir = home_p / "state"
    log_path = state_dir / f"supervisor-dispatch-{epic_id}.log"

    env = dict(os.environ)
    env["MINI_ORK_ROOT"] = str(engine_root)
    env["MINI_ORK_HOME"] = str(home)
    env["MINI_ORK_DB"] = str(db)
    env["MO_TARGET_CWD"] = str(target_root)
    env["MO_ALLOW_FRAMEWORK_CWD"] = "1"
    if not env.get("MO_PLACEMENT"):
        env["MO_PLACEMENT"] = "local"
    for key in ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_PLAN_PATH"):
        env.pop(key, None)

    argv = [sys.executable,
            os.path.join(str(engine_root), "bin", "mini-ork"),
            "scheduler", "--once", "--epic", str(epic_id)]
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "ab")
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(target_root),
                env=env,
                stdout=log_fh,
                stderr=STDOUT,
                stdin=DEVNULL,
                start_new_session=True,
            )
        finally:
            log_fh.close()
    except OSError as exc:
        return {"error": str(exc)}
    return {"pid": getattr(proc, "pid", None), "log": str(log_path)}


def _same_dir_or_ancestor(path: str, other: str) -> bool:
    """True when ``path`` or any of its ancestors is the same directory as ``other``.

    ``samefile`` compares device and inode, so a case-variant path on a
    case-insensitive filesystem (macOS APFS) and a symlink both resolve to the
    same directory. An ancestor whose ``samefile`` raises ``OSError`` (a broken
    symlink, a vanished path) is skipped.
    """
    current = path
    while True:
        try:
            if os.path.samefile(current, other):
                return True
        except OSError:
            pass
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _dispatch_refusal(target_root: str, engine_root: str) -> dict | None:
    """Return R8's refusal action for an unsafe target, or ``None`` when safe.

    Ordering is the contract: the first matching row wins, so an empty target is
    reported as required, a relative one as non-absolute, and only an existing
    absolute directory is compared against the engine checkout.
    """
    if not target_root:
        return {
            "epic_id": "", "action": "dispatch_refused",
            "reason": ("dispatch_epics.target_root is required: a dispatched "
                       "run edits its working tree, which must never be the "
                       "engine checkout")}
    if not os.path.isabs(target_root):
        return {
            "epic_id": "", "action": "dispatch_refused",
            "reason": (f"dispatch_epics.target_root must be an absolute path: "
                       f"{target_root}")}
    if not os.path.isdir(target_root):
        return {
            "epic_id": "", "action": "dispatch_refused",
            "reason": f"dispatch_epics.target_root does not exist: {target_root}"}

    target = os.path.realpath(target_root)
    engine = os.path.realpath(engine_root)
    if _same_dir_or_ancestor(target, engine):
        return {
            "epic_id": "", "action": "dispatch_refused",
            "reason": "dispatch_epics.target_root is inside the engine checkout"}
    if _same_dir_or_ancestor(engine, target):
        return {
            "epic_id": "", "action": "dispatch_refused",
            "reason": ("dispatch_epics.target_root contains the engine "
                       "checkout")}
    return None


def _rule_dispatch_epic(home: str, db: str, engine_root: str, dry_run: bool,
                        *, max_in_progress: int, only: list[str] | None,
                        dispatch_fn, target_root: str = "") -> list[dict]:
    """R8 — start at most one ready epic through the scheduler.

    First refuses an unsafe ``target_root`` — a dispatched run edits its working
    tree, so it must never be the engine checkout, inside it, or a parent of it
    (that is what makes setting ``MO_ALLOW_FRAMEWORK_CWD=1`` on the child safe).
    Then reclaims stale ``in progress`` epics with the scheduler's own rules, so
    an epic wedged by a crashed scheduler cannot hold the cap forever. Only then
    is the cap counted: it must see the post-reclaim state, or the fix is a
    no-op. Bounded by the number of epics still ``in progress`` (the scheduler
    enforces the per-epic budget and ``MO_DAILY_BUDGET_USD``; R8 adds no budget
    logic of its own). ``only`` intersects the ready-set with an allow-list.
    Nothing ready or the cap reached → the reclaim action, if any. ``dry_run``
    reports the pick and spawns nothing.
    """
    from mini_ork import scheduler  # noqa: PLC0415 — avoids an import cycle

    refusal = _dispatch_refusal(target_root, engine_root)
    if refusal is not None:
        return [refusal]

    actions: list[dict] = []
    if not dry_run:
        try:
            reclaimed = scheduler.reclaim_stale(db)
        except Exception as exc:  # reclaim is best-effort; dispatch continues
            actions.append({"epic_id": "", "action": "error",
                            "reason": f"reclaim: {type(exc).__name__}: {exc}"})
        else:
            if reclaimed:
                actions.append({"epic_id": "", "action": "reclaim_stale",
                                "epic_ids": list(reclaimed)})

    rows = _query(db, "SELECT COUNT(*) FROM epics"
                      " WHERE status='in progress' AND archived_at IS NULL")
    in_progress = int(rows[0][0]) if rows else 0
    if in_progress >= int(max_in_progress):
        return actions

    ready = scheduler.pick_ready(db, only=set(only) if only else None)
    if not ready:
        return actions
    epic_id = str(ready[0])

    if dry_run:
        actions.append({"epic_id": epic_id, "action": "dispatch_epic",
                        "reason": "ready; dry-run", "dry_run": True})
        return actions

    result = (dispatch_fn or _default_dispatch_epic)(
        engine_root, home, db, epic_id, target_root)
    actions.append({"epic_id": epic_id, "action": "dispatch_epic",
                    "reason": "ready", **result})
    return actions
