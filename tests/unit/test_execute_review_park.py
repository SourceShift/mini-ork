"""Executor parks a run for human node review (no failure, no rollback), and
``run`` stops cleanly — kickoff ``hr-exec-park.md`` revision round 2.

Revision-1 defects this addresses:
  - Deep chains (`impl → check → review → pub`) used to fall through the
    ``if not ready:`` fallback before the drain was moved above it (now
    covered by ``test_park_deep_chain_propagates``).
  - Retries-edge parks used to fire a revise round because the revise loop
    treated ``rc != 0`` as a failure (now covered by
    ``test_park_does_not_fire_revise_round``).
  - ``mini-ork run`` teardown rewrote ``reviewing`` to ``failed``
    (now covered by ``test_lifecycle_teardown_preserves_reviewing``).
  - Rollback was itself parked by the gate in failure-plus-pause runs
    (now covered by ``test_park_plus_failure_runs_rollback``).
  - DoD 4 test never produced a pause; rewritten as a real fail-then-park
    run that observes ``pause_count > 0`` and the parked sidecar
    (``test_failure_and_pause_in_same_run``).
  - DoD 2 rollback assertion was vacuous (rollback never went through
    ``dispatch_fn``); now checked via ``[ok] rollback`` on captured
    stderr, which the actual handler emits.

DoD cases:

1. **Policy off:** all nodes dispatch, the result is exactly as before.
2. **Park:** ``MO_REVIEW_AFTER_NODES=implementer`` AND ``_intervention_decision``
   monkeypatched to return ``park`` for ``check`` →
   ``impl`` runs, ``check`` + ``review`` NOT dispatched, ``rollback`` NOT
   dispatched, rc==9, ``task_runs.status='reviewing'`` and
   ``<run_dir>/.review-pause`` exists with ``waiting_node=='check'``.
3. **Block:** a ``block`` decision → the node is blocked, the run fails as
   today, and rollback runs.
4. **Failure + pause in same run:** the failure path wins (rc=1,
   status='failed') AND ``.review-pause`` is left for the operator.
6. **Existing executor tests pass unchanged.**

Plus revision-2 shapes:

A. **Deep chain:** ``impl → check → review → pub``, ``check`` parked, every
   descendant inherits ``paused``; rc==9, no rollback, no failed status.
B. **Retries edge:** ``impl → check`` with ``check → impl: retries``,
   ``check`` parked, NO revise round fires, the implementer's artifact is
   NOT moved into ``revise/round-1/``.
C. **Rollback exempt:** when a run has both a real failure and a parked
   node, rollback runs (the gate must not park the rollback).
D. **Lifecycle teardown:** a parked run whose ``mini-ork run`` teardown
   calls ``close_run_record`` keeps its ``reviewing`` status (not
   rewritten to ``failed``).
E. **Lifecycle helper short-circuits:** ``_handle_execute_park`` returns
   ``(9, False)`` for an execute rc of 9; non-park rc returns ``(rc, True)``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import execute as ex  # noqa: E402
from mini_ork.cli import main as cli_main  # noqa: E402
from mini_ork.gates import node_review  # noqa: E402


# ── env contract keys the gate / executor / DB / loader read ──
_POLICY_KEYS = (
    "MO_REVIEW_BEFORE_MERGE",
    "MO_REVIEW_AFTER_NODES",
    "MO_REVIEW_TIER",
    "MO_REVIEW_SIGNING_KEY",
)
_RUN_KEYS = (
    "MINI_ORK_RUN_ID",
    "MINI_ORK_RUN_DIR",
    "MO_TARGET_CWD",
)
_EXEC_HELPERS = (
    "MINI_ORK_DRY_RUN",
    "MINI_ORK_AUTO_REPAIR",
    "MO_GRADE_RUN_REWARD",
    "MO_LEARNING_WRITEBACK",
    "MO_LANE_ROUTER",
    "MO_REWARD_STAMP",
)


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    for key in _POLICY_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key in _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key in _EXEC_HELPERS:
        monkeypatch.delenv(key, raising=False)


# ── helpers ──


def _init_db(home: Path, db: Path) -> None:
    """Run the project's migration bootstrap with ``MINI_ORK_HOME`` pinned."""
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": str(db)},
        capture_output=True, text=True, check=True,
    )


def _seed_task_run(db: Path, rid: str = "r-park", status: str = "planned") -> None:
    subprocess.run(
        [
            "sqlite3", str(db),
            "INSERT OR REPLACE INTO task_runs (id,task_class,workflow_version,"
            "kickoff_path,status,cost_usd,created_at,updated_at) VALUES ("
            f"'{rid}','code_fix','v1','k.md','{status}',0,"
            "strftime('%s','now','-60 seconds'),strftime('%s','now'));",
        ],
        check=True, capture_output=True, text=True,
    )


def _sql(db: Path, query: str) -> str:
    r = subprocess.run(["sqlite3", str(db), query],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _write_workflow(path: Path, *, edges=("impl,check", "check,review"),
                    nodes=("impl,researcher", "check,researcher",
                           "review,researcher", "rollback,rollback"),
                    retries=()) -> None:
    """Serial DAG with optional retries edge.

    ``edges`` is an iterable of ``"from,to"`` pairs (depends_on). ``nodes``
    is an iterable of ``"name,type"`` pairs. ``retries`` is an iterable of
    ``"from,to"`` pairs that emit a ``retries`` edge (no depends_on).
    """
    lines = ["dispatch_mode: serial", "nodes:"]
    for entry in nodes:
        name, ntype = entry.split(",", 1)
        lines.append(f"  - {{name: {name}, type: {ntype}, description: {ntype}-step}}")
    lines.append("edges:")
    for entry in edges:
        frm, to = entry.split(",", 1)
        lines.append(f"  - {{from: {frm}, to: {to}, edge_type: depends_on}}")
    for entry in retries:
        frm, to = entry.split(",", 1)
        lines.append(f"  - {{from: {frm}, to: {to}, edge_type: retries}}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_plan(run_dir: Path) -> Path:
    """A minimal plan.json for ``_resolve_plan_path``."""
    plan = run_dir / "plan.json"
    plan.write_text(json.dumps({
        "objective": "hr-exec-park test",
        "task_class": "code_fix",
        "decomposition": [],
    }))
    return plan


def _set_run_env(monkeypatch, *, home: Path, db: Path, workflow: Path,
                 plan: Path, run_dir: Path, run_id: str,
                 env_overrides: dict[str, str] | None = None) -> None:
    env = {
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_DB": str(db),
        "MINI_ORK_WORKFLOW": str(workflow),
        "MINI_ORK_PLAN_PATH": str(plan),
        "MINI_ORK_RUN_DIR": str(run_dir),
        "MINI_ORK_RUN_ID": run_id,
        "MINI_ORK_TASK_CLASS": "code_fix",
        "MINI_ORK_EXECUTE_GATE": "0",
    }
    if env_overrides:
        env.update(env_overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _dispatch_recording(dispatched: list[str], responses: dict[str, str] | None = None):
    """A ``dispatch_fn`` that records every node it ran and returns a
    pass-style response.

    ``responses`` maps node_id → the response text. Default: every node
    writes its name. Implements the (task_class, node_type, prompt) →
    (rc, text) shape every executor test seam uses.
    """
    responses = responses or {}

    def d(task_class: str, node_type: str, prompt: str):
        for nid in ("impl", "check", "review", "pub", "rollback"):
            if nid in prompt:
                dispatched.append(nid)
                return 0, responses.get(nid, f"ok: {nid}")
        return 0, "ok"

    return d


def _park_decision_for(node_id_to_park: str, awaiting=("impl",), inbox_ids=(42,)):
    """Build a fake ``_intervention_decision`` that parks the given node_id."""
    def fake_decision(_root, node_id, _node_type, _lane, _node_desc):
        if node_id == node_id_to_park:
            return ("park", {"awaiting": list(awaiting),
                             "inbox_ids": list(inbox_ids)})
        return ("proceed", {})
    return fake_decision


def _patch_park(monkeypatch, fake_decision) -> None:
    """Monkeypatch BOTH sides of the trampoline (execute.py AND
    execute_handlers.py) so the call site sees the override regardless of
    which module loaded the function first.
    """
    monkeypatch.setattr(ex, "_intervention_decision", fake_decision)
    from mini_ork.cli import execute_handlers as exh  # noqa: PLC0415
    monkeypatch.setattr(exh, "_intervention_decision", fake_decision)


# ── DoD 1: policy off → today's bytes ──


def test_policy_off_dispatches_every_node(tmp_path, monkeypatch):
    """No ``MO_REVIEW_AFTER_NODES``: the gate never fires, every node runs."""
    home = tmp_path / ".mini-ork"
    db = home / "state.db"
    run_dir = home / "runs" / "r-off"; run_dir.mkdir(parents=True)

    _init_db(home, db)
    _seed_task_run(db, rid="r-off", status="executing")
    workflow = tmp_path / "wf.yaml"
    _write_workflow(workflow)
    plan = _write_plan(run_dir)
    _set_run_env(monkeypatch, home=home, db=db, workflow=workflow,
                 plan=plan, run_dir=run_dir, run_id="r-off",
                 env_overrides={"MINI_ORK_AUTO_REPAIR": "0",
                                "MINI_ORK_EXECUTE_GATE": "0"})

    dispatched: list[str] = []
    rc = ex.main([], root=str(REPO),
                 dispatch_fn=_dispatch_recording(dispatched))

    assert rc == 0, f"rc={rc} stderr unchanged"
    # All three work-fields ran (rollback is intentionally skipped with no failures).
    assert set(dispatched) == {"impl", "check", "review"}, dispatched
    status = _sql(db, "SELECT status FROM task_runs WHERE id='r-off';")
    assert status in ("executing", "reviewing", "published"), status
    # No review-pause sidecar in the policy-off path.
    assert not (run_dir / ".review-pause").exists()


# ── DoD 2: park mid-run → run pauses, descendants + rollback skip ──


def test_park_mid_run(tmp_path, monkeypatch, capsys):
    """``_intervention_decision`` returning ``park`` for ``check`` parks the run."""
    home = tmp_path / ".mini-ork"
    db = home / "state.db"
    run_dir = home / "runs" / "r-park"; run_dir.mkdir(parents=True)

    _init_db(home, db)
    _seed_task_run(db, rid="r-park", status="executing")
    workflow = tmp_path / "wf.yaml"
    _write_workflow(workflow)
    plan = _write_plan(run_dir)
    _set_run_env(
        monkeypatch, home=home, db=db, workflow=workflow,
        plan=plan, run_dir=run_dir, run_id="r-park",
        env_overrides={
            "MO_REVIEW_AFTER_NODES": "implementer",
            "MINI_ORK_AUTO_REPAIR": "0",
            "MINI_ORK_EXECUTE_GATE": "0",
        },
    )

    dispatched: list[str] = []
    _patch_park(monkeypatch, _park_decision_for("check"))

    rc = ex.main([], root=str(REPO),
                 dispatch_fn=_dispatch_recording(dispatched))
    err = capsys.readouterr().err

    assert rc == node_review.REVIEW_PAUSE_RC, (
        f"rc={rc} (expected REVIEW_PAUSE_RC=9); dispatched={dispatched}"
    )
    # Only `impl` (the antecedent) ran. The parked node, its dependent
    # reviewer, AND the rollback all skipped — no rollback when paused.
    assert dispatched == ["impl"], dispatched
    # The rollback node is NOT in `dispatched` because it never goes through
    # the LLM dispatch seam; the executor's `if rollback_fields and fail_count > 0`
    # branch never fires when fail_count == 0. Assert on the captured stderr:
    # if rollback had run we'd see "[ok] rollback complete".
    assert "[ok] rollback" not in err, (
        "rollback must NOT run on a parked run; captured stderr was:\n" + err
    )
    status = _sql(db, "SELECT status FROM task_runs WHERE id='r-park';")
    assert status == "reviewing", status
    pause_path = run_dir / ".review-pause"
    assert pause_path.exists(), "expected .review-pause sidecar"
    payload = json.loads(pause_path.read_text())
    assert payload["waiting_node"] == "check"
    assert payload["awaiting"] == ["impl"]
    assert payload["inbox_ids"] == [42]


# ── DoD 3: block → today's failure path + rollback runs ──


def test_block_decision_keeps_todays_failure_path(tmp_path, monkeypatch, capsys):
    """A ``block`` decision is a real failure; the run fails and rollback runs."""
    home = tmp_path / ".mini-ork"
    db = home / "state.db"
    run_dir = home / "runs" / "r-block"; run_dir.mkdir(parents=True)

    _init_db(home, db)
    _seed_task_run(db, rid="r-block", status="executing")
    workflow = tmp_path / "wf.yaml"
    _write_workflow(workflow)
    plan = _write_plan(run_dir)
    _set_run_env(monkeypatch, home=home, db=db, workflow=workflow,
                 plan=plan, run_dir=run_dir, run_id="r-block",
                 env_overrides={"MINI_ORK_AUTO_REPAIR": "0",
                                "MINI_ORK_EXECUTE_GATE": "0"})

    dispatched: list[str] = []

    def fake_decision(_root, node_id, _node_type, _lane, _node_desc):
        if node_id == "check":
            return ("block", {"error": "synthetic reviewer rejection"})
        return ("proceed", {})

    _patch_park(monkeypatch, fake_decision)

    rc = ex.main([], root=str(REPO),
                 dispatch_fn=_dispatch_recording(dispatched))
    err = capsys.readouterr().err

    assert rc == 1, f"rc={rc}; block must mirror today's failure rc"
    # impl ran; check returned rc=1, "blocked" (its handler doesn't even
    # reach the LLM dispatch seam, so it never appends to `dispatched`).
    # review never ran because check failed.
    assert "impl" in dispatched, dispatched
    assert "review" not in dispatched
    # Rollback has its own handler (no LLM dispatch), so the `dispatched`
    # list never sees it. The handler prints one of several rollback
    # markers — "[ok] rollback complete" on the success path, "[ok] rollback:
    # nothing to revert ..." on the no-op path, "[ok] rollback: strategy
    # ..." on the partial-discard path. They all share the "[ok] rollback"
    # prefix; assert on that. (Revision-2 fix: a vacuous `dispatched`
    # check would never fail because rollback doesn't go through
    # ``dispatch_fn``.)
    assert "[ok] rollback" in err, (
        "rollback must run when fail_count > 0; captured stderr was:\n" + err
    )
    status = _sql(db, "SELECT status FROM task_runs WHERE id='r-block';")
    assert status == "failed", status
    assert not (run_dir / ".review-pause").exists()


# ── DoD 4: real failure + real pause in the same run ──


def test_failure_and_pause_in_same_run(tmp_path, monkeypatch, capsys):
    """One real failure + one parked node → the failure path wins AND
    ``.review-pause`` is left for the operator.

    Revision-2 rewrite: the prior version's ``failing_then_parking`` made
    the parked node ``review``, but ``review`` was never reached for
    dispatch because its parent ``check`` had already failed — so the
    pause never happened end-to-end and we never proved
    ``pause_count > 0``. Worse, the dep-graph dispatch loop drains ALL
    pending nodes once ``pause_count > 0`` (the wave that introduced the
    pause runs to completion, then subsequent waves are short-circuited).
    A real fail-then-park in the same run therefore requires the failing
    node to dispatch BEFORE the parking node in the work_fields order.
    """
    home = tmp_path / ".mini-ork"
    db = home / "state.db"
    run_dir = home / "runs" / "r-mix"; run_dir.mkdir(parents=True)

    _init_db(home, db)
    _seed_task_run(db, rid="r-mix", status="executing")
    workflow = tmp_path / "wf.yaml"
    # Two independent branches off `impl`: `lint` (real LLM failure) AND
    # `check` (gate-parked). `lint` MUST come before `check` in the nodes
    # list so it dispatches in the wave before `check` parks — the
    # dep-graph serial path drains everything once a pause lands, so the
    # failure has to land first to count.
    _write_workflow(
        workflow,
        edges=("impl,lint", "impl,check"),
        nodes=("impl,researcher", "lint,researcher",
               "check,researcher", "rollback,rollback"),
    )
    plan = _write_plan(run_dir)
    _set_run_env(monkeypatch, home=home, db=db, workflow=workflow,
                 plan=plan, run_dir=run_dir, run_id="r-mix",
                 env_overrides={"MINI_ORK_AUTO_REPAIR": "0",
                                "MINI_ORK_EXECUTE_GATE": "0"})

    def fail_lint(task_class: str, node_type: str, prompt: str):
        # `lint` is a real LLM-seam failure (rc != 0).
        if "lint" in prompt:
            return 1, "lint failed: trailing whitespace"
        return 0, "ok"

    _patch_park(monkeypatch, _park_decision_for("check",
                                                awaiting=("impl",),
                                                inbox_ids=(7,)))

    rc = ex.main([], root=str(REPO),
                 dispatch_fn=fail_lint)
    err = capsys.readouterr().err

    # Both branches actually ran: fail_count > 0 AND pause_count > 0.
    assert rc == 1, (
        f"rc={rc}; a real failure must outrank a pause (DoD 4). "
        "If rc=9 the failing node was drained by the pause before it "
        "reached the LLM seam — re-order the work_fields so the failure "
        "dispatches first."
    )
    status = _sql(db, "SELECT status FROM task_runs WHERE id='r-mix';")
    assert status == "failed", status
    # The .review-pause sidecar is left for the operator — the executor's
    # pre-flight teardown (run_reaper.close_run_record) honors it; the
    # review board and the dashboard see the same hand-off.
    pause_path = run_dir / ".review-pause"
    assert pause_path.exists(), (
        "a parked node must leave .review-pause behind even when the run "
        "ends rc=1"
    )
    payload = json.loads(pause_path.read_text())
    assert payload["waiting_node"] == "check"
    # Rollback runs because fail_count > 0.
    assert "[ok] rollback" in err, (
        "rollback must run on fail_count > 0; captured stderr was:\n" + err
    )


# ── A: deep chain park propagates ──


def test_park_deep_chain_propagates(tmp_path, monkeypatch, capsys):
    """``impl → check → review → pub``: parking ``check`` must propagate
    the pause to ``review`` AND ``pub``, not fall into the defensive
    ``unresolved workflow parents`` branch (revision-2 defect).
    """
    home = tmp_path / ".mini-ork"
    db = home / "state.db"
    run_dir = home / "runs" / "r-deep"; run_dir.mkdir(parents=True)

    _init_db(home, db)
    _seed_task_run(db, rid="r-deep", status="executing")
    workflow = tmp_path / "wf.yaml"
    _write_workflow(
        workflow,
        edges=("impl,check", "check,review", "review,pub"),
        nodes=("impl,researcher", "check,researcher", "review,researcher",
               "pub,researcher", "rollback,rollback"),
    )
    plan = _write_plan(run_dir)
    _set_run_env(
        monkeypatch, home=home, db=db, workflow=workflow,
        plan=plan, run_dir=run_dir, run_id="r-deep",
        env_overrides={
            "MO_REVIEW_AFTER_NODES": "implementer",
            "MINI_ORK_AUTO_REPAIR": "0",
            "MINI_ORK_EXECUTE_GATE": "0",
        },
    )

    dispatched: list[str] = []
    _patch_park(monkeypatch, _park_decision_for("check"))

    rc = ex.main([], root=str(REPO),
                 dispatch_fn=_dispatch_recording(dispatched))
    err = capsys.readouterr().err

    assert rc == node_review.REVIEW_PAUSE_RC, (
        f"rc={rc} (expected REVIEW_PAUSE_RC=9); dispatched={dispatched}"
    )
    # Only `impl` actually dispatches. The 2-level and 3-level descendants
    # are drained by the pause drain above the `if not ready:` fallback.
    assert dispatched == ["impl"], dispatched
    # No failure path: rollback must NOT run, status stays "reviewing".
    assert "[ok] rollback" not in err, (
        "rollback must NOT run when paused; captured stderr was:\n" + err
    )
    status = _sql(db, "SELECT status FROM task_runs WHERE id='r-deep';")
    assert status == "reviewing", (
        f"status={status}; a deep-chain park must not be rewritten to failed"
    )
    pause_path = run_dir / ".review-pause"
    assert pause_path.exists()


# ── B: park with retries edge does NOT fire a revise round ──


def test_park_does_not_fire_revise_round(tmp_path, monkeypatch, capsys):
    """A parked node with a ``retries`` edge back to the implementer must
    NOT trigger a revise round (revision-2 defect: revise loop treated
    ``rc != 0`` as a failure and started ``revise/round-1/``).
    """
    home = tmp_path / ".mini-ork"
    db = home / "state.db"
    run_dir = home / "runs" / "r-revise"; run_dir.mkdir(parents=True)

    _init_db(home, db)
    _seed_task_run(db, rid="r-revise", status="executing")
    workflow = tmp_path / "wf.yaml"
    # framework-edit / code-fix shape: ``check`` runs after ``impl``, and
    # ``check`` retries back to ``impl`` when it finds problems. Parking
    # ``check`` must not kick off the revise loop.
    _write_workflow(
        workflow,
        edges=("impl,check",),
        retries=("check,impl",),
        nodes=("impl,researcher", "check,researcher", "rollback,rollback"),
    )
    plan = _write_plan(run_dir)
    _set_run_env(
        monkeypatch, home=home, db=db, workflow=workflow,
        plan=plan, run_dir=run_dir, run_id="r-revise",
        env_overrides={
            "MO_REVIEW_AFTER_NODES": "implementer",
            "MINI_ORK_AUTO_REPAIR": "0",
            "MINI_ORK_EXECUTE_GATE": "0",
        },
    )

    dispatched: list[str] = []
    _patch_park(monkeypatch, _park_decision_for("check"))

    rc = ex.main([], root=str(REPO),
                 dispatch_fn=_dispatch_recording(dispatched))
    err = capsys.readouterr().err

    assert rc == node_review.REVIEW_PAUSE_RC, (
        f"rc={rc} (expected REVIEW_PAUSE_RC=9); dispatched={dispatched}"
    )
    # Only `impl` ran; `check` was parked before dispatch.
    assert dispatched == ["impl"], dispatched
    # The revise loop must NOT have fired: the implementer's edit must NOT
    # have been moved into ``revise/round-1/``.
    revise_dir = run_dir / "revise"
    assert not revise_dir.exists(), (
        f"park must NOT fire a revise round; found {revise_dir}"
    )
    # And no "re-running impl with feedback" line on stderr.
    assert "re-running impl with feedback" not in err, (
        "park must NOT trigger a revise round; captured stderr was:\n" + err
    )
    # .review-pause is present, status=reviewing.
    assert (run_dir / ".review-pause").exists()
    status = _sql(db, "SELECT status FROM task_runs WHERE id='r-revise';")
    assert status == "reviewing", status


# ── C: rollback is exempt from the intervention gate ──


def test_park_plus_failure_runs_rollback(tmp_path, monkeypatch, capsys):
    """When the run has both a parked node AND a real failure, rollback
    must still run (revision-2 defect: the gate parked the rollback
    itself, skipping the executor's rollback branch).

    Same order constraint as ``test_failure_and_pause_in_same_run``: the
    failing branch must dispatch BEFORE the parking one, otherwise the
    dep-graph pause drain short-circuits the rest of the run and the
    failure never counts.
    """
    home = tmp_path / ".mini-ork"
    db = home / "state.db"
    run_dir = home / "runs" / "r-pf"; run_dir.mkdir(parents=True)

    _init_db(home, db)
    _seed_task_run(db, rid="r-pf", status="executing")
    workflow = tmp_path / "wf.yaml"
    # `lint` (LLM failure) first, then `check` (parked). The rollback
    # must run regardless of which one came first — we test the
    # rollback exemption, not the order invariant (DoD 4 covers that).
    _write_workflow(
        workflow,
        edges=("impl,lint", "impl,check"),
        nodes=("impl,researcher", "lint,researcher",
               "check,researcher", "rollback,rollback"),
    )
    plan = _write_plan(run_dir)
    _set_run_env(
        monkeypatch, home=home, db=db, workflow=workflow,
        plan=plan, run_dir=run_dir, run_id="r-pf",
        env_overrides={
            "MO_REVIEW_AFTER_NODES": "implementer",
            "MINI_ORK_AUTO_REPAIR": "0",
            "MINI_ORK_EXECUTE_GATE": "0",
        },
    )

    def fail_lint(task_class: str, node_type: str, prompt: str):
        if "lint" in prompt:
            return 1, "lint failed"
        return 0, "ok"

    _patch_park(monkeypatch, _park_decision_for("check"))

    rc = ex.main([], root=str(REPO),
                 dispatch_fn=fail_lint)
    err = capsys.readouterr().err

    # Failure wins (DoD 4 invariant).
    assert rc == 1, f"rc={rc}; a failure must outrank a pause"
    # Rollback still runs even though there is ALSO a parked node — the
    # gate must not park the rollback.
    assert "[ok] rollback" in err, (
        "rollback must run on fail_count > 0 even when a parked node "
        "is present; captured stderr was:\n" + err
    )


# ── D: lifecycle teardown preserves ``reviewing`` ──


def test_lifecycle_teardown_preserves_reviewing(tmp_path, monkeypatch):
    """``close_run_record`` must NOT rewrite a parked run's status to
    ``failed`` (revision-2 defect: the executor parked the run, but the
    lifecycle teardown overwrote ``reviewing`` with ``failed``).

    The simplest, faithful reproduction of the lifecycle contract:
    seed a row in ``reviewing``, drop a ``.review-pause`` sidecar, and
    call ``run_reaper.close_run_record`` directly — the status must NOT
    change.
    """
    home = tmp_path / ".mini-ork"
    db = home / "state.db"
    run_dir = home / "runs" / "r-teardown"; run_dir.mkdir(parents=True)
    # Write the sidecar so the teardown sees the parked signal.
    (run_dir / ".review-pause").write_text(json.dumps({
        "waiting_node": "check",
        "awaiting": ["impl"],
        "inbox_ids": [42],
        "ts": 0,
    }))

    _init_db(home, db)
    _seed_task_run(db, rid="r-teardown", status="reviewing")

    from mini_ork.orchestration import run_reaper

    replaced = run_reaper.close_run_record(
        db, "r-teardown", run_dir,
        crashed=False, rc=node_review.REVIEW_PAUSE_RC,
    )
    assert replaced is None, (
        f"close_run_record returned {replaced!r}; the parked run must NOT "
        "be rewritten"
    )
    status = _sql(db, "SELECT status FROM task_runs WHERE id='r-teardown';")
    assert status == "reviewing", (
        f"status={status}; the lifecycle teardown must NOT overwrite "
        "a parked run's reviewing status"
    )
    # And the .pid file would still be released (we don't have one here,
    # but release_pid_file is no-op on a missing file).


# ── E: lifecycle helper short-circuits when execute returns 9 ──


def test_handle_execute_park_short_circuits():
    """``_handle_execute_park`` returns (9, False) on a parked rc, otherwise
    passes the rc through with ``continue_lifecycle=True``.

    DoD E: the lifecycle branch must skip auto-repair, rubric, verify,
    reflect and ``retry_notify`` when the execute rc is 9. The helper is
    the single decision point; verify by simulating both rc paths.

    Revision-2: the helper used to re-write execute_out / execute_err
    (the lifecycle already wrote them right after the execute subprocess
    returned). It no longer does — we just print the parked signal.
    """
    # Parked → short-circuits
    rc, cont = cli_main._handle_execute_park(
        run_rc=node_review.REVIEW_PAUSE_RC,
        execute_out="execute: ...\n",
        execute_err="[review] parked before impl: ...\n",
        run_id="r-x", run_dir="/tmp/non-existent",
    )
    assert rc == 9
    assert cont is False, "parked run must NOT continue with verify/reflect/retry_notify"

    # Green / fail-through → passes through, continues the lifecycle
    for non_park_rc in (0, 1, 2, 124):
        rc, cont = cli_main._handle_execute_park(
            run_rc=non_park_rc,
            execute_out="execute: ok\n",
            execute_err="",
            run_id="r-y", run_dir=None,
        )
        assert rc == non_park_rc, f"non-park rc={non_park_rc} must pass through"
        assert cont is True, (
            f"non-park rc={non_park_rc} must keep the lifecycle going"
        )