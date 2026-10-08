from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RECIPE = REPO / "recipes" / "registry-audit"
DRIVER = RECIPE / "lib" / "fanout_driver.py"
VERIFIER = RECIPE / "verifiers" / "audit_coverage.py"

# A registry with two different table shapes so the chain exercises the parser's
# header-name lookup end to end. Hermetic: no external file, no network.
REGISTRY = r"""
### Cluster A — onboarding

| ID | Feature | Src | Home | Status | Owner | Evidence / spec |
|---|---|---|---|---|---|---|
| A-1 | First thing | s1 | home | partial | own | — |
| A-2 | Second thing (`x\|y`) | s2 | home | shipped | own | — |

### Cluster B — deletions

| ID | Action | Src | Status | Evidence |
|---|---|---|---|---|
| DEL-1 | Remove a thing | s6 | done-deletion | `abc` |
"""


def _bootstrap() -> None:
    """Register the recipe's transforms exactly as the runtime does."""
    from mini_ork.cli.recipe_register import load_recipe_register

    load_recipe_register(RECIPE)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    registry = tmp_path / "registry.md"
    registry.write_text(REGISTRY, encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-test")
    monkeypatch.setenv("MO_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("MO_REGISTRY_DRY", "1")
    monkeypatch.delenv("MO_REGISTRY_MAX_ITEMS", raising=False)
    return {
        **os.environ,
        "PYTHONPATH": str(REPO),
        "MINI_ORK_RUN_DIR": str(run_dir),
        "MINI_ORK_RUN_ID": "run-test",
        "MO_REGISTRY_PATH": str(registry),
        "MO_REGISTRY_DRY": "1",
    }


def _parse_and_plan(run_dir: Path) -> dict:
    """Drive the two transforms in-process, then return the plan."""
    _bootstrap()
    from mini_ork.workflow.artifacts import ArtifactLedger
    from mini_ork.workflow.compiler import compile_workflow
    from mini_ork.workflow.transforms import execute_transform

    compiled = compile_workflow(RECIPE / "workflow.yaml")
    ledger = ArtifactLedger(run_dir, "run-test")
    execute_transform(compiled, ledger, "registry_parse")
    ledger.publish_node_outputs(compiled, "registry_parse")
    ledger.prepare_inputs(compiled, "audit_plan")
    plan_path = execute_transform(compiled, ledger, "audit_plan")
    ledger.publish_node_outputs(compiled, "audit_plan")
    return json.loads(plan_path.read_text(encoding="utf-8"))


def _run_driver(env: dict[str, str]) -> dict:
    proc = subprocess.run([sys.executable, str(DRIVER)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip(), "driver must print a non-empty verdict (empty == vacuous)"
    return json.loads((Path(env["MINI_ORK_RUN_DIR"]) / "audit-result.json").read_text(encoding="utf-8"))


def _run_verifier(env: dict[str, str]) -> tuple[int, dict | None]:
    proc = subprocess.run([sys.executable, str(VERIFIER)], capture_output=True, text=True, env=env)
    verdict_path = Path(env["MINI_ORK_RUN_DIR"]) / "audit-verdict.json"
    payload = json.loads(verdict_path.read_text(encoding="utf-8")) if verdict_path.is_file() else None
    return proc.returncode, payload


# --- compile -----------------------------------------------------------------


def test_recipe_workflow_compiles():
    _bootstrap()
    from mini_ork.workflow.compiler import compile_workflow

    compiled = compile_workflow(RECIPE / "workflow.yaml")
    assert set(compiled.nodes) == {
        "planner", "registry_parse", "audit_plan", "audit_items", "audit_check", "publisher",
    }


# --- parse + plan ------------------------------------------------------------


def test_parse_yields_every_item_with_a_result_path(env):
    run_dir = Path(env["MINI_ORK_RUN_DIR"])
    plan = _parse_and_plan(run_dir)
    assert plan["total_items"] == 3
    assert plan["planned"] == 3
    assert [p["id"] for p in plan["pending"]] == ["A-1", "A-2", "DEL-1"]
    # Each planned item carries the exact checkpoint the verifier will look for.
    assert plan["pending"][0]["result_path"].endswith("/results/A-1.json")


def test_plan_is_empty_when_every_item_already_has_a_result(env):
    run_dir = Path(env["MINI_ORK_RUN_DIR"])
    _parse_and_plan(run_dir)
    _run_driver(env)  # writes results/*.json
    plan = _parse_and_plan(run_dir)  # re-plan: all results present now
    assert plan["planned"] == 0


def test_max_items_bounds_one_wave(env, monkeypatch):
    monkeypatch.setenv("MO_REGISTRY_MAX_ITEMS", "2")
    env = {**env, "MO_REGISTRY_MAX_ITEMS": "2"}
    plan = _parse_and_plan(Path(env["MINI_ORK_RUN_DIR"]))
    assert plan["planned"] == 2


def test_quarantined_items_are_skipped(env, monkeypatch):
    monkeypatch.setenv("MO_REGISTRY_QUARANTINED", "A-2\n")
    env = {**env, "MO_REGISTRY_QUARANTINED": "A-2\n"}
    plan = _parse_and_plan(Path(env["MINI_ORK_RUN_DIR"]))
    assert [p["id"] for p in plan["pending"]] == ["A-1", "DEL-1"]


def test_parse_refuses_an_empty_registry(env, tmp_path, monkeypatch):
    empty = tmp_path / "empty.md"
    empty.write_text("# nothing here\n\nno tables at all\n", encoding="utf-8")
    monkeypatch.setenv("MO_REGISTRY_PATH", str(empty))
    _bootstrap()
    from mini_ork.workflow.artifacts import ArtifactContractError, ArtifactLedger
    from mini_ork.workflow.compiler import compile_workflow
    from mini_ork.workflow.transforms import execute_transform

    compiled = compile_workflow(RECIPE / "workflow.yaml")
    ledger = ArtifactLedger(Path(env["MINI_ORK_RUN_DIR"]), "run-test")
    with pytest.raises(ArtifactContractError, match="0 items"):
        execute_transform(compiled, ledger, "registry_parse")


# --- the whole chain ---------------------------------------------------------


def test_full_dry_chain_passes(env):
    run_dir = Path(env["MINI_ORK_RUN_DIR"])
    _parse_and_plan(run_dir)
    manifest = _run_driver(env)
    assert manifest["verdict"] == "pass"
    assert manifest["ok"] == 3
    rc, verdict = _run_verifier(env)
    assert rc == 0
    assert verdict is not None
    assert verdict["verdict"] == "pass"
    assert verdict["reason"] == "complete"


def test_a_deleted_result_makes_the_verifier_fail_with_missing(env):
    run_dir = Path(env["MINI_ORK_RUN_DIR"])
    _parse_and_plan(run_dir)
    _run_driver(env)
    (run_dir / "results" / "A-2.json").unlink()
    rc, verdict = _run_verifier(env)
    assert rc == 0  # a fail verdict is a valid wave result, not a process error
    assert verdict["verdict"] == "fail"
    assert verdict["missing"] == ["A-2"]


def test_zero_item_registry_cannot_pass(env):
    run_dir = Path(env["MINI_ORK_RUN_DIR"])
    # A plan over nothing, with an empty manifest: must not read as a pass.
    (run_dir / "audit-plan.json").write_text(
        json.dumps({"registry_path": "x", "total_items": 0, "planned": 0, "pending": []}),
        encoding="utf-8",
    )
    (run_dir / "audit-result.json").write_text(
        json.dumps({"verdict": "pass", "items": []}), encoding="utf-8"
    )
    rc, verdict = _run_verifier(env)
    assert rc == 0
    assert verdict is not None
    assert verdict["verdict"] == "fail"
    assert verdict["reason"] == "no_items"


def test_verifier_exits_two_when_inputs_are_unreadable(env):
    # Missing plan/result -> undecidable, distinct from a clean fail.
    rc, verdict = _run_verifier(env)
    assert rc == 2
    assert verdict is None


def test_all_items_audited_reads_as_nothing_pending(env):
    run_dir = Path(env["MINI_ORK_RUN_DIR"])
    _parse_and_plan(run_dir)
    _run_driver(env)
    _parse_and_plan(run_dir)  # re-plan -> empty pending
    _run_driver(env)
    rc, verdict = _run_verifier(env)
    assert rc == 0
    assert verdict is not None
    assert verdict["verdict"] == "pass"
    assert verdict["reason"] == "nothing_pending"
