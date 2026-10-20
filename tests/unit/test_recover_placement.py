"""Recover restores the run's placement and refuses a run with no plan.

Two run-bugs are pinned here:

  * RB-06 — ``recover`` of a ``--placement remote`` run executed every node on
    the host: placement is published only by the run lifecycle, which a revival
    bypasses, so ``dispatch.providers`` saw no ``MO_PLACEMENT``. ``cli_main`` now
    restores the same env from ``run_profile.json`` before dispatching.
  * RB-08 — a run that failed before planning was revived with no ``plan.json``
    (and ``MINI_ORK_PLAN_PATH=""``), so the implementer ran against nothing.
    ``cli_main`` refuses that revival with rc 2.

Hermetic: everything lives under ``tmp_path``; ``execute_fn`` is stubbed so it
records the process env it was dispatched with. No live DB, no network. The
``isolated_env`` fixture scrubs ambient ``MINI_ORK_*``/``MO_*`` values (a leaked
run identity steers the planner, RB-04) and restores the process env afterwards
— ``cli_main`` publishes into ``os.environ`` for the rest of the process.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from mini_ork.recovery import planner  # noqa: E402
from test_recover_lease_wiring import (  # noqa: E402
    SCHEMA_SQL,
    _seed_success,
    _workflow,
)

RUN_ID = "run-1799999999-12345"
ENV_NAME = "placement-test-env"

WORKFLOW_YAML = """\
nodes:
  - name: implementer
    type: implementer
    model_lane: fast
  - name: test_verifier
    type: verifier
    model_lane: fast
edges:
  - from: implementer
    to: test_verifier
    edge_type: depends_on
"""

PLAN_JSON = {
    "objective": "placement fixture",
    "decomposition": [
        {"id": "implementer", "description": "impl",
         "node_type": "implementer", "depends_on": []},
        {"id": "test_verifier", "description": "verify",
         "node_type": "verifier", "depends_on": ["implementer"]},
    ],
    "dependencies": [{"from": "implementer", "to": "test_verifier"}],
}

PROFILE_REMOTE = {
    "schema_version": "1.0",
    "recipe": "",
    "task_class": "framework_edit",
    "placement": "remote",
    "environment": ENV_NAME,
    "profile_status": "ready",
}

PROFILE_HOST = {
    "schema_version": "1.0",
    "recipe": "",
    "task_class": "generic",
    "profile_status": "ready",
}

PROFILE_LOCAL = {
    "schema_version": "1.0",
    "recipe": "",
    "task_class": "generic",
    "placement": "local",
    "profile_status": "ready",
}

ENV_PROFILE_YAML = "image: python:3.11\nsetup: |\n  echo placement\n"

ENV_KEYS_TO_UNSET = (
    "MO_PLACEMENT",
    "MO_NODE_ENV",
    "MO_DEFAULT_NODE_ENV",
    "MO_REMOTE_NODE",
    "MO_REMOTE_SESSION_SCOPE",
    "MO_TARGET_CWD",
    "MINI_ORK_RECIPE",
    "MINI_ORK_TASK_CLASS",
    "MINI_ORK_PLAN_PATH",
    "MINI_ORK_RECOVERY_RUN_ID",
    "MINI_ORK_RECOVERY_SKU",
    "MINI_ORK_RECOVERY_CLOSURE",
    "MINI_ORK_RECOVERY_STRATEGY",
    "MINI_ORK_RECOVERY_FROM",
    "MINI_ORK_LEASE_TOKEN",
    "MINI_ORK_LEASE_RUN_ID",
    "MO_AUTO_REPAIR_ATTEMPT",
    "MO_AUTO_REPAIR_RUN_ID",
    "MO_AUTO_REPAIR_WAIT_PID",
)


@pytest.fixture(autouse=True)
def isolated_env():
    """Scrub the ambient run identity; restore the whole env afterwards."""
    saved = dict(os.environ)
    for key in [k for k in os.environ if k.startswith(("MINI_ORK_", "MO_"))]:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _run_recover(tmp_path, capsys, *, profile, plan="json", env_profile=True,
                 argv_extra=(), run_dir=None, db_path=None, env_extra=None):
    """Drive ``planner.cli_main`` over an isolated run dir.

    Returns ``(rc, seen, output)``; ``seen["called"]`` is False when the stub
    never ran.
    """
    home = tmp_path / "home"
    run_dir = Path(run_dir) if run_dir is not None else tmp_path / "run"
    (home / "config" / "environments").mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    if env_profile:
        (home / "config" / "environments" / f"{ENV_NAME}.yaml").write_text(
            ENV_PROFILE_YAML, encoding="utf-8")
    (run_dir / "run_profile.json").write_text(
        json.dumps(profile), encoding="utf-8")
    (run_dir / "workflow.yaml").write_text(WORKFLOW_YAML, encoding="utf-8")
    if plan == "json":
        (run_dir / "plan.json").write_text(
            json.dumps(PLAN_JSON), encoding="utf-8")
    elif plan == "corrupt":
        (run_dir / "plan.json").write_text(
            "{'objective': not-json", encoding="utf-8")

    for key in ENV_KEYS_TO_UNSET:
        os.environ.pop(key, None)
    os.environ["MINI_ORK_HOME"] = str(home)
    os.environ["MINI_ORK_ROOT"] = str(home)
    os.environ["MINI_ORK_RUN_DIR"] = str(run_dir)
    os.environ["MINI_ORK_RUN_ID"] = RUN_ID
    os.environ["MINI_ORK_DB"] = str(db_path or (tmp_path / "state.db"))
    os.environ["MINI_ORK_WORKFLOW"] = str(run_dir / "workflow.yaml")
    if env_extra:
        os.environ.update(env_extra)

    seen: dict = {"called": False}

    def stub_execute(argv):
        seen["called"] = True
        seen["argv"] = list(argv)
        seen["MO_PLACEMENT"] = os.environ.get("MO_PLACEMENT")
        seen["MO_NODE_ENV"] = os.environ.get("MO_NODE_ENV")
        seen["MO_REMOTE_SESSION_SCOPE"] = os.environ.get("MO_REMOTE_SESSION_SCOPE")
        return 0

    rc = planner.cli_main([RUN_ID, *argv_extra], execute_fn=stub_execute)
    captured = capsys.readouterr()
    return rc, seen, captured.out + captured.err


# ── placement restore ───────────────────────────────────────────────────────


def test_remote_profile_restores_the_lifecycles_placement_env(tmp_path, capsys):
    """A recorded remote placement is published exactly as the lifecycle does."""
    rc, seen, _ = _run_recover(tmp_path, capsys, profile=PROFILE_REMOTE)

    assert seen["called"], "a restorable remote run must dispatch"
    assert rc == 0
    assert seen["MO_PLACEMENT"] == "remote"
    assert seen["MO_NODE_ENV"] == ENV_NAME
    assert seen["MO_REMOTE_SESSION_SCOPE"] == "lifecycle"


def test_host_run_publishes_no_placement_vars(tmp_path, capsys):
    """A run with no recorded placement keeps today's host behaviour."""
    rc, seen, _ = _run_recover(tmp_path, capsys, profile=PROFILE_HOST)

    assert seen["called"]
    assert rc == 0
    assert seen["MO_PLACEMENT"] is None
    assert seen["MO_NODE_ENV"] is None
    assert seen["MO_REMOTE_SESSION_SCOPE"] is None


def test_local_placement_profile_publishes_no_vars(tmp_path, capsys):
    """``local`` is a host placement: nothing is exported."""
    rc, seen, _ = _run_recover(tmp_path, capsys, profile=PROFILE_LOCAL)

    assert seen["called"]
    assert rc == 0
    assert seen["MO_PLACEMENT"] is None
    assert seen["MO_NODE_ENV"] is None


def test_operator_placement_override_wins(tmp_path, capsys):
    """``MO_PLACEMENT=local`` in the shell is the manual host override."""
    rc, seen, _ = _run_recover(tmp_path, capsys, profile=PROFILE_REMOTE,
                               env_extra={"MO_PLACEMENT": "local"})

    assert seen["called"], "an explicit local override must not refuse"
    assert rc == 0
    assert seen["MO_PLACEMENT"] == "local", (
        "the operator's placement must win over the recorded remote one")
    assert seen["MO_NODE_ENV"] is None


# ── unrestorable remote placement ───────────────────────────────────────────


def test_remote_without_resolvable_env_is_refused_rc2(tmp_path, capsys):
    rc, seen, output = _run_recover(
        tmp_path, capsys, profile=PROFILE_REMOTE, env_profile=False)

    assert rc == 2
    assert not seen["called"], "a refused recover must never dispatch"
    assert (f"recover: {RUN_ID} ran with placement remote; "
            "restore it or pass --allow-local") in output


def test_allow_local_permits_the_revival(tmp_path, capsys):
    rc, seen, _ = _run_recover(
        tmp_path, capsys, profile=PROFILE_REMOTE, env_profile=False,
        argv_extra=("--allow-local",))

    assert seen["called"], "--allow-local must let the revival proceed"
    assert rc == 0
    assert seen["MO_PLACEMENT"] is None, "--allow-local revives on the host"


# ── no valid plan.json ──────────────────────────────────────────────────────


def test_missing_plan_is_refused_rc2(tmp_path, capsys):
    rc, seen, output = _run_recover(
        tmp_path, capsys, profile=PROFILE_HOST, plan="missing")

    assert rc == 2
    assert not seen["called"], "the stub must never run without a plan"
    assert (f"recover: {RUN_ID} failed before planning; "
            "start a new run instead") in output


def test_corrupt_plan_is_refused_rc2(tmp_path, capsys):
    rc, seen, output = _run_recover(
        tmp_path, capsys, profile=PROFILE_HOST, plan="corrupt")

    assert rc == 2
    assert not seen["called"], "an unparseable plan is not a valid plan"
    assert (f"recover: {RUN_ID} failed before planning; "
            "start a new run instead") in output


def test_checkpointed_revival_without_plan_still_dispatches(tmp_path, capsys):
    """A genuine mid-run revival has completed nodes and no plan.json — the
    pre-planning refusal must not swallow it (the gate is scoped to a run that
    never got past planning)."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_SQL)
    con.close()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(
        json.dumps({"recipe": "framework-edit"}), encoding="utf-8")
    workflow = tmp_path / "wf.yaml"
    _workflow(workflow)  # A -> B -> C -> D
    for node in ("A", "B", "C"):
        _seed_success(str(db), RUN_ID, node, "framework-edit",
                      "framework_edit", str(run_dir))

    os.environ["MINI_ORK_HOME"] = str(tmp_path)
    os.environ["MINI_ORK_ROOT"] = str(tmp_path)
    os.environ["MINI_ORK_RUN_DIR"] = str(run_dir)
    os.environ["MINI_ORK_RUN_ID"] = RUN_ID
    os.environ["MINI_ORK_DB"] = str(db)
    os.environ["MINI_ORK_WORKFLOW"] = str(workflow)

    seen: dict = {"called": False}

    def stub_execute(argv):
        seen["called"] = True
        return 0

    rc = planner.cli_main([RUN_ID], execute_fn=stub_execute)
    out_err = capsys.readouterr()

    assert rc == 0, (rc, out_err)
    assert seen["called"], "a checkpointed revival must still dispatch"
