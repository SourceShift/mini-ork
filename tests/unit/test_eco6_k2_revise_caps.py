"""ECOSYSTEM §6 K2: the revise loop drops CoBa and honors declared recursion caps.

Hermetic: every case runs against ``tmp_path`` with an injected deterministic
dispatch seam. No network, no provider, no credentials, no shared DB.

Part A pins the removal: ``mini_ork.learning.revise_value`` is gone and the
executor no longer mentions it. Part B pins the ``_round_cap`` contract and
drives the real executor over a two-node loop to prove the declared
``recursion.max_iterations`` and ``budget_cap_total_usd`` are live while a
``retries`` edge (and any recipe without a ``recursion:`` block) keeps the old
``min(edge.max_rounds, MO_REVISE_ROUNDS)`` rule.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
from pathlib import Path

import yaml

import mini_ork.cli.execute
from mini_ork.context import run_context_scope
from mini_ork.recovery.dag import CompiledRetryEdge

_REPO_ROOT = Path(mini_ork.cli.execute.__file__).resolve().parents[2]

_RECURSION_BLOCK = {
    "max_iterations": 4,
    "convergence_check": "objective_delta",
    "budget_cap_per_iter_usd": 0.50,
    "budget_cap_total_usd": 1.00,
    "divergence_kill": "none",
}


# ── Part A: the CoBa rule is gone ─────────────────────────────────────────────

def test_revise_value_module_and_its_test_are_deleted():
    assert importlib.util.find_spec("mini_ork.learning.revise_value") is None
    legacy_test = _REPO_ROOT / "tests" / "unit" / "test_revise_value.py"
    assert not legacy_test.exists(), f"{legacy_test} must be deleted with the module"


def test_execute_py_contains_neither_revise_value_nor_coba_stop():
    text = Path(mini_ork.cli.execute.__file__).read_text(encoding="utf-8")
    assert "revise_value" not in text
    assert "coba stop" not in text


def test_migration_0081_and_revise_rounds_table_stay():
    sql = _REPO_ROOT / "db" / "migrations" / "0081_revise_rounds.sql"
    assert sql.is_file()
    assert "revise_rounds" in sql.read_text(encoding="utf-8")


# ── Part B1: the `_round_cap` helper contract ────────────────────────────────

def _edge(max_rounds):
    return CompiledRetryEdge(target="implementer", max_rounds=max_rounds)


def _round_cap(source, edge, escalation_edges, revise_rounds_cap):
    from mini_ork.cli import execute

    return execute._round_cap(source, edge, escalation_edges, revise_rounds_cap)


def test_retries_edge_keeps_the_env_cap():
    assert _round_cap("reviewer", _edge(2), {}, 2) == 2
    assert _round_cap("reviewer", _edge(5), {}, 2) == 2


def test_recursion_edge_uses_declared_max_when_env_unset(monkeypatch):
    monkeypatch.delenv("MO_REVISE_ROUNDS", raising=False)
    edge = _edge(6)
    assert _round_cap("reviewer", edge, {"reviewer": edge}, 2) == 6


def test_recursion_edge_respects_explicit_env(monkeypatch):
    monkeypatch.setenv("MO_REVISE_ROUNDS", "3")
    edge = _edge(6)
    assert _round_cap("reviewer", edge, {"reviewer": edge}, 3) == 3


def test_empty_env_counts_as_unset(monkeypatch):
    monkeypatch.setenv("MO_REVISE_ROUNDS", "")
    edge = _edge(6)
    assert _round_cap("reviewer", edge, {"reviewer": edge}, 2) == 6


def test_non_positive_cap_disables_every_edge():
    edge = _edge(6)
    assert _round_cap("reviewer", edge, {"reviewer": edge}, 0) == 0
    assert _round_cap("reviewer", _edge(5), {}, 0) == 0
    assert _round_cap("reviewer", edge, {"reviewer": edge}, -1) == 0


# ── Part B2: the real executor honors the declared caps and budget ───────────

_WORKFLOW = {
    "nodes": [
        {"name": "implementer", "type": "implementer", "description": "do the thing"},
        {"name": "reviewer", "type": "reviewer", "description": "review the thing"},
    ],
    "edges": [
        {"from": "implementer", "to": "reviewer", "edge_type": "depends_on"},
    ],
}


def _rounds_on_disk(run_dir: Path) -> list[int]:
    revise = run_dir / "revise"
    if not revise.is_dir():
        return []
    return sorted(
        int(m.group(1))
        for entry in revise.iterdir()
        if (m := re.fullmatch(r"round-(\d+)\.md", entry.name))
    )


def _drive(tmp_path, monkeypatch, *, edge_type="escalates_to", max_rounds=4,
           with_recursion=True, env_overrides=None, run_cost_value=None):
    """Run the real executor once over a two-node loop; return its observable state."""
    root = tmp_path / "root"
    home = tmp_path / "home"
    run_dir = tmp_path / "run"
    target = tmp_path / "target"
    for directory in (root / "lib", home, run_dir, target):
        directory.mkdir(parents=True, exist_ok=True)
    # The reviewer verdict is parsed by the repo's tolerant extractor, which the
    # executor shells out to as <root>/lib/extract_verdict.py.
    shutil.copy(_REPO_ROOT / "lib" / "extract_verdict.py",
                root / "lib" / "extract_verdict.py")

    workflow = dict(_WORKFLOW)
    workflow["edges"] = list(_WORKFLOW["edges"]) + [{
        "from": "reviewer",
        "to": "implementer",
        "edge_type": edge_type,
        "max_rounds": max_rounds,
    }]
    if with_recursion:
        workflow["recursion"] = dict(_RECURSION_BLOCK)
    workflow_path = run_dir / "workflow.yaml"
    workflow_path.write_text(yaml.safe_dump(workflow), encoding="utf-8")
    plan = run_dir / "plan.json"
    plan.write_text(json.dumps({"objective": "k2", "decomposition": []}),
                    encoding="utf-8")

    calls: list[str] = []

    def fake_dispatch(task_class, lane, prompt):
        node_id = os.environ.get("MO_NODE_ID", "?")
        calls.append(node_id)
        if node_id == "reviewer":
            return 0, json.dumps({"verdict": "fail", "notes": ["keeps failing"]})
        return 0, "implemented; nothing to apply"

    env = {
        "MINI_ORK_ROOT": str(root),
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_RUN_DIR": str(run_dir),
        "MINI_ORK_WORKFLOW": str(workflow_path),
        "MINI_ORK_PLAN_PATH": str(plan),
        # Nonexistent DB + no run id: every DB writer no-ops. The ledger read is
        # patched below when the case wants a spend.
        "MINI_ORK_DB": str(tmp_path / "nonexistent-state.db"),
        "MO_TARGET_CWD": str(target),
        "MO_RUN_ORCHESTRATOR": "0",
        "MINI_ORK_RECIPE": "",
    }
    for stale in ("MINI_ORK_RUN_ID", "MO_REVISE_ROUNDS", "MINI_ORK_DRY_RUN",
                  "MINI_ORK_RECOVERY_CLOSURE", "MINI_ORK_RECOVERY_FROM"):
        env.pop(stale, None)
    env.update(env_overrides or {})

    if run_cost_value is not None:
        import mini_ork.cost_ledger as cost_ledger

        monkeypatch.setattr(cost_ledger, "run_cost",
                            lambda db, run_id: run_cost_value)

    saved_environ = dict(os.environ)
    err, out = io.StringIO(), io.StringIO()
    try:
        os.environ.clear()
        os.environ.update(env)
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            with run_context_scope({}):
                rc = mini_ork.cli.execute.main(
                    argv=[], root=str(root), dispatch_fn=fake_dispatch)
    finally:
        os.environ.clear()
        os.environ.update(saved_environ)

    return {
        "rc": rc,
        "calls": calls,
        "rounds": _rounds_on_disk(run_dir),
        "stderr": err.getvalue(),
    }


def test_budget_cap_total_usd_blocks_the_round(tmp_path, monkeypatch):
    r = _drive(tmp_path, monkeypatch, run_cost_value=1.50)
    assert r["rounds"] == []
    assert "[revise] round" not in r["stderr"]
    assert r["calls"] == ["implementer", "reviewer"]
    assert re.search(
        r"\[revise\] recursion budget cap reached for implementer: "
        r"spent \$1\.50 of \$1\.00",
        r["stderr"],
    )


def test_under_cap_spend_grants_round_one(tmp_path, monkeypatch):
    r = _drive(tmp_path, monkeypatch, run_cost_value=0.10)
    assert 1 in r["rounds"]
    assert re.search(r"\[revise\] round 1/4:", r["stderr"])


def test_unset_env_allows_the_declared_four_rounds(tmp_path, monkeypatch):
    r = _drive(tmp_path, monkeypatch, run_cost_value=0.10)
    assert r["rounds"] == [1, 2, 3, 4]
    assert re.search(r"\[revise\] round 3/4:", r["stderr"])
    assert not re.search(r"\[revise\] round 5/", r["stderr"])


def test_none_spend_fails_open(tmp_path, monkeypatch):
    r = _drive(tmp_path, monkeypatch, run_cost_value=None)
    assert 1 in r["rounds"]


def test_explicit_env_still_caps_a_recursion_edge(tmp_path, monkeypatch):
    r = _drive(tmp_path, monkeypatch,
               env_overrides={"MO_REVISE_ROUNDS": "3"}, run_cost_value=0.10)
    assert r["rounds"] == [1, 2, 3]
    assert not re.search(r"\[revise\] round 4/", r["stderr"])


def test_retries_edge_without_recursion_block_keeps_min_cap(tmp_path, monkeypatch):
    r = _drive(tmp_path, monkeypatch, edge_type="retries", max_rounds=5,
               with_recursion=False)
    assert r["rounds"] == [1, 2]
    assert re.search(r"\[revise\] round 2/5:", r["stderr"])


def test_zero_revise_rounds_disables_the_loop(tmp_path, monkeypatch):
    r = _drive(tmp_path, monkeypatch,
               env_overrides={"MO_REVISE_ROUNDS": "0"}, run_cost_value=0.10)
    assert r["rounds"] == []
    assert "[revise] round" not in r["stderr"]
