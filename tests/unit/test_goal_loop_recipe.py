"""Contracts for the goal-loop recipe skeleton (kickoff U4a).

Covers the four DoD groups from
``kickoffs/book-goal-loop/u4a-goal-loop-recipe-skeleton.md``:

1. workflow.yaml parses, validates the recursion block against the locked
   schema, and compiles via ``mini_ork.workflow.compiler.compile_workflow``
   with the 4-edge chain visible in ``control_parents``.
2. ``load_recipe_register(Path('recipes/goal-loop'))`` returns True and the
   ``goal_sweep`` submode appears in the submode registry.
3. ``recipes.goal_loop.lib.goal_state.evaluate_units`` with a fake
   predicate script classifies pass/fail units correctly.
4. ``verifiers/goal_check.py`` writes ``panel-verdict.json`` with verdict
   fail + failing_units list when 1 of 2 units fail, and verdict pass when
   zero units fail.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

from mini_ork.cli import execute_handlers as ex
from mini_ork.cli import recipe_register as rr
from mini_ork.cli.recipe_register import load_recipe_register
from mini_ork.workflow.compiler import compile_workflow

REPO = Path(__file__).resolve().parents[2]
RECIPE_DIR = REPO / "recipes" / "goal-loop"

# ``recipes/`` is not a Python package, so we load the helpers by file path
# the same way the recipe_register loader does (see
# ``mini_ork/cli/recipe_register.py:67``). The loaded module then binds
# ``evaluate_units`` / ``list_units`` at module scope for direct calls.
_HELPER_PATH = RECIPE_DIR / "lib" / "goal_state.py"
_spec = importlib.util.spec_from_file_location("goal_loop_goal_state", _HELPER_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError(f"could not load goal_state helper from {_HELPER_PATH}")
_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault(_spec.name, _mod)
_spec.loader.exec_module(_mod)
evaluate_units = _mod.evaluate_units
list_units = _mod.list_units


# ── Module-state fixtures ──────────────────────────────────────────────────
# register.py mutates both _IMPLEMENTER_SUBMODES (execute_handlers) and
# _TRANSFORMS (workflow/transforms). The fixture pattern from
# tests/unit/test_recipe_register.py:18-32 covers _LOADED; we extend it
# so cross-test contamination can't poison later suites.


@pytest.fixture(autouse=True)
def _reset_loader_state():
    saved_loaded = set(rr._LOADED)
    saved_submodes = dict(ex._IMPLEMENTER_SUBMODES)
    from mini_ork.workflow import transforms as tr
    saved_transforms = dict(tr._TRANSFORMS)
    rr._LOADED.clear()
    try:
        yield
    finally:
        rr._LOADED.clear()
        rr._LOADED.update(saved_loaded)
        ex._IMPLEMENTER_SUBMODES.clear()
        ex._IMPLEMENTER_SUBMODES.update(saved_submodes)
        tr._TRANSFORMS.clear()
        tr._TRANSFORMS.update(saved_transforms)


# ── 1. workflow.yaml: parse + schema-validate recursion + compile ─────────


def test_workflow_yaml_parses_and_validates_recursion():
    wf_path = RECIPE_DIR / "workflow.yaml"
    wf = yaml.safe_load(wf_path.read_text(encoding="utf-8"))

    schema = json.loads((REPO / "schemas" / "workflow.schema.json").read_text())
    jsonschema.validate(instance=wf["recursion"], schema=schema["properties"]["recursion"])

    expected = {
        "max_iterations",
        "convergence_check",
        "budget_cap_per_iter_usd",
        "budget_cap_total_usd",
        "divergence_kill",
    }
    assert set(wf["recursion"].keys()) == expected


def test_workflow_compiles_with_four_edge_chain():
    # register.py must load so the @register_transform decorators fire BEFORE
    # compile_workflow() looks up transform identifiers.
    assert load_recipe_register(RECIPE_DIR) is True

    compiled = compile_workflow(RECIPE_DIR / "workflow.yaml")

    expected_parents = {
        "goal_state": ("planner",),
        "sweep_dispatcher": ("goal_state",),
        "goal_check": ("sweep_dispatcher",),
        "publisher": ("goal_check",),
    }
    for node_id, parents in expected_parents.items():
        actual = tuple(compiled.control_parents.get(node_id, ()))
        assert sorted(actual) == sorted(parents), (node_id, actual, parents)

    assert compiled.topological_order.index("planner") < compiled.topological_order.index("goal_state")
    assert compiled.topological_order.index("goal_check") < compiled.topological_order.index("publisher")


# ── 2. load_recipe_register + goal_sweep submode registry ────────────────


def test_load_recipe_register_returns_true_and_registers_goal_sweep():
    assert load_recipe_register(RECIPE_DIR) is True

    # Recipe uses the hyphenated "goal-loop" recipe name (matches existing
    # submodes at mini_ork/cli/execute_handlers.py:357-362). The kickoff
    # refers to this as the "goal_sweep submode"; the registry key is the
    # (recipe, node_id) pair and U4b replaces the stub script with the
    # real driver (lib/drive.py) so the registered script path now ends in
    # drive.py — this assertion is the direct probe of register.py state.
    assert ("goal-loop", "sweep_dispatcher") in ex._IMPLEMENTER_SUBMODES
    results_artifact, script_path = ex._IMPLEMENTER_SUBMODES[("goal-loop", "sweep_dispatcher")]
    assert results_artifact == "sweep-result.json"
    assert Path(script_path).name == "drive.py"
    # register.py stores the script path relative to the recipes root
    # (matches sibling recipes — see register.py:_DRIVER_SCRIPT). Resolve
    # against RECIPE_DIR before asserting the file is on disk.
    resolved_script = (REPO / "recipes" / script_path).resolve()
    assert resolved_script.is_file(), resolved_script


def test_load_recipe_register_is_idempotent():
    first = load_recipe_register(RECIPE_DIR)
    second = load_recipe_register(RECIPE_DIR)
    assert first is True
    assert second is True


# ── 3. evaluate_units: pass/fail classification ─────────────────────────


def test_evaluate_units_classifies_pass_and_fail(tmp_path, monkeypatch):
    predicate = tmp_path / "predicate.sh"
    predicate.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$1\" in\n"
        "  good) echo 'ok'; exit 0 ;;\n"
        "  bad)  echo 'reason: missing'; exit 1 ;;\n"
        "  ugly) echo 'flaky'; exit 2 ;;\n"
        "  *)    echo 'unknown'; exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    predicate.chmod(0o755)

    monkeypatch.setenv("MO_GOAL_TARGET_CWD", str(tmp_path))
    monkeypatch.setenv("MO_GOAL_PREDICATE_CMD", str(predicate))

    states = evaluate_units(str(tmp_path), str(predicate), ["good", "bad", "ugly"])

    assert states["good"]["pass"] is True
    assert states["bad"]["pass"] is False
    assert states["bad"]["reason"] == "reason: missing"
    assert states["ugly"]["pass"] is False
    assert states["ugly"]["reason"] == "flaky"


# ── 4. verifiers/goal_check.py: panel-verdict.json shape ─────────────────


def _run_goal_check(
    tmp_path, monkeypatch,
    units_script_name="units.sh",
    predicate_lines=None,
    units_lines=("echo unit-A", "echo unit-B"),
):
    units_script = tmp_path / units_script_name
    units_body = "#!/usr/bin/env bash\n" + "\n".join(units_lines) + "\n"
    units_script.write_text(units_body, encoding="utf-8")
    units_script.chmod(0o755)
    if predicate_lines is None:
        predicate_lines = (
            "#!/usr/bin/env bash\n"
            "case \"$1\" in\n"
            "  unit-A) echo 'ok'; exit 0 ;;\n"
            "  unit-B) echo 'ok'; exit 0 ;;\n"
            "esac\n"
        )
    predicate = tmp_path / "predicate.sh"
    predicate.write_text(predicate_lines, encoding="utf-8")
    predicate.chmod(0o755)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    evidence = run_dir / "verifier-goal-check.log"
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_VERIFIER_EVIDENCE", str(evidence))
    monkeypatch.setenv("MO_GOAL_TARGET_CWD", str(tmp_path))
    monkeypatch.setenv("MO_GOAL_UNITS_CMD", str(units_script))
    monkeypatch.setenv("MO_GOAL_PREDICATE_CMD", str(predicate))

    verifier = RECIPE_DIR / "verifiers" / "goal_check.py"
    proc = subprocess.run(
        [sys.executable, str(verifier)],
        env=os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
    )
    return proc, run_dir / "panel-verdict.json"


def test_goal_check_writes_fail_with_failing_units(tmp_path, monkeypatch):
    predicate = (
        "#!/usr/bin/env bash\n"
        "case \"$1\" in\n"
        "  unit-A) echo 'ok'; exit 0 ;;\n"
        "  unit-B) echo 'reason: stale'; exit 1 ;;\n"
        "esac\n"
    )
    proc, panel = _run_goal_check(tmp_path, monkeypatch, "units.sh", predicate)
    assert proc.returncode == 0
    payload = json.loads(panel.read_text(encoding="utf-8"))
    assert payload["verdict"] == "fail"
    assert payload["failing_units"] == ["unit-B"]
    assert payload["total_units"] == 2


def test_goal_check_writes_pass_when_zero_failing(tmp_path, monkeypatch):
    predicate = (
        "#!/usr/bin/env bash\n"
        "case \"$1\" in\n"
        "  unit-A) echo 'ok'; exit 0 ;;\n"
        "  unit-B) echo 'ok'; exit 0 ;;\n"
        "esac\n"
    )
    proc, panel = _run_goal_check(tmp_path, monkeypatch, "units.sh", predicate)
    assert proc.returncode == 0
    payload = json.loads(panel.read_text(encoding="utf-8"))
    assert payload["verdict"] == "pass"
    assert payload["failing_units"] == []
    assert payload["total_units"] == 2


def test_goal_check_handles_zero_units(tmp_path, monkeypatch):
    proc, panel = _run_goal_check(
        tmp_path, monkeypatch,
        units_script_name="empty_units.sh",
        predicate_lines="#!/usr/bin/env bash\nexit 0\n",
        units_lines=(),
    )
    assert proc.returncode == 0
    payload = json.loads(panel.read_text(encoding="utf-8"))
    assert payload["verdict"] == "fail"
    assert payload["reason"] == "no_units"
    assert payload["total_units"] == 0
    assert payload["failing_units"] == []