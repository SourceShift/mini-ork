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
from mini_ork.workflow.artifacts import ArtifactContractError
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
harvest_evidence = _mod.harvest_evidence

# Same file-path load for the transforms module, to bind the ledger-free
# ``_run_apply`` core of goal_apply_deploy for direct unit tests. The
# @register_transform side effects mirror what register.py does; the
# _reset_loader_state fixture snapshots + restores _TRANSFORMS so this is
# benign.
_TRANSFORMS_PATH = RECIPE_DIR / "lib" / "transforms.py"
_tspec = importlib.util.spec_from_file_location("goal_loop_transforms", _TRANSFORMS_PATH)
if _tspec is None or _tspec.loader is None:
    raise ImportError(f"could not load transforms module from {_TRANSFORMS_PATH}")
_tmod = importlib.util.module_from_spec(_tspec)
sys.modules.setdefault(_tspec.name, _tmod)
_tspec.loader.exec_module(_tmod)
run_apply = _tmod._run_apply
harvest_selected_evidence = _tmod._harvest_selected_evidence
evidence_slug = _tmod._slug

# The book-goal-loop terminal-FAILURE binding (companion to chapter_predicate).
# Loaded by file path — it lives under kickoffs/, not a Python package. We test
# its pure guard rails (argv/env validation) + the stall helper hermetically,
# without a live psql/DB.
_BIND_DIR = REPO / "kickoffs" / "book-goal-loop" / "binding"
_TFAIL_PATH = _BIND_DIR / "chapter_terminal_fail.py"
_tfspec = importlib.util.spec_from_file_location("chapter_terminal_fail", _TFAIL_PATH)
if _tfspec is None or _tfspec.loader is None:
    raise ImportError(f"could not load terminal-fail binding from {_TFAIL_PATH}")
_tfmod = importlib.util.module_from_spec(_tfspec)
sys.modules.setdefault(_tfspec.name, _tfmod)
_tfspec.loader.exec_module(_tfmod)


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


def test_workflow_compiles_with_six_edge_chain():
    # register.py must load so the @register_transform decorators fire BEFORE
    # compile_workflow() looks up transform identifiers.
    assert load_recipe_register(RECIPE_DIR) is True

    compiled = compile_workflow(RECIPE_DIR / "workflow.yaml")

    expected_parents = {
        "goal_state": ("planner",),
        "sweep_dispatcher": ("goal_state",),
        "sweep": ("sweep_dispatcher",),
        "goal_apply": ("sweep",),
        "goal_check": ("goal_apply",),
        "publisher": ("goal_check",),
    }
    for node_id, parents in expected_parents.items():
        actual = tuple(compiled.control_parents.get(node_id, ()))
        assert sorted(actual) == sorted(parents), (node_id, actual, parents)

    order = compiled.topological_order
    assert order.index("planner") < order.index("goal_state")
    assert order.index("sweep_dispatcher") < order.index("sweep")
    assert order.index("sweep") < order.index("goal_apply")
    assert order.index("goal_apply") < order.index("goal_check")
    assert order.index("goal_check") < order.index("publisher")


def test_workflow_declares_sweep_implementer_node_between_dispatcher_and_check():
    wf_path = RECIPE_DIR / "workflow.yaml"
    wf = yaml.safe_load(wf_path.read_text(encoding="utf-8"))

    names = [n["name"] for n in wf["nodes"]]
    assert names.index("sweep_dispatcher") < names.index("sweep") < names.index("goal_check")

    sweep_node = next(n for n in wf["nodes"] if n["name"] == "sweep")
    assert sweep_node["type"] == "implementer"

    edges = {(e["from"], e["to"]) for e in wf["edges"]}
    assert ("sweep_dispatcher", "sweep") in edges
    assert ("sweep", "goal_apply") in edges
    assert ("goal_apply", "goal_check") in edges
    # sweep no longer wires straight into goal_check — the apply node is the
    # loop-closing seam between them.
    assert ("sweep", "goal_check") not in edges
    assert ("sweep_dispatcher", "goal_check") not in edges


# ── 2. load_recipe_register + goal_sweep submode registry ────────────────


def test_load_recipe_register_returns_true_and_registers_goal_sweep():
    assert load_recipe_register(RECIPE_DIR) is True

    # Recipe uses the hyphenated "goal-loop" recipe name (matches existing
    # submodes at mini_ork/cli/execute_handlers.py:357-362). The kickoff
    # refers to this as the "goal_sweep submode"; the registry key is the
    # (recipe, node_id) pair and U4b replaces the stub script with the
    # real driver (lib/drive.py) so the registered script path now ends in
    # drive.py — this assertion is the direct probe of register.py state.
    assert ("goal-loop", "sweep") in ex._IMPLEMENTER_SUBMODES
    assert ("goal-loop", "sweep_dispatcher") not in ex._IMPLEMENTER_SUBMODES
    results_artifact, script_path = ex._IMPLEMENTER_SUBMODES[("goal-loop", "sweep")]
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


# ── 3b. harvest_evidence: the deep-evidence counterpart of evaluate_units ─
# evaluate_units keeps only reason_lines[0]; harvest_evidence keeps the FULL
# per-unit output. These pin the three properties the evidence seam depends on:
# argv (not shell), whole-output capture, and rc-tolerance.


def _evidence_script(tmp_path, body: str):
    script = tmp_path / "evidence.sh"
    script.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


def test_harvest_evidence_captures_full_multiline_output(tmp_path):
    """Unlike the predicate (first line only), the harvester keeps every line."""
    script = _evidence_script(
        tmp_path,
        'echo "LINE1 for $1"\necho "LINE2 detail"\necho "LINE3 tail"\n',
    )
    out = harvest_evidence(str(tmp_path), str(script), ["ch1"])
    assert set(out) == {"ch1"}
    assert out["ch1"] == "LINE1 for ch1\nLINE2 detail\nLINE3 tail"


def test_harvest_evidence_passes_unit_as_argv_not_shell(tmp_path):
    """A unit id with shell metacharacters is a single argv slot, not injected."""
    script = _evidence_script(tmp_path, 'echo "arg=[$1]"\n')
    hostile = "ch1; touch PWNED"
    out = harvest_evidence(str(tmp_path), str(script), [hostile])
    assert out[hostile] == f"arg=[{hostile}]"
    assert not (tmp_path / "PWNED").exists()  # never shell-evaluated


def test_harvest_evidence_tolerates_nonzero_exit(tmp_path):
    """A non-zero exit still yields whatever partial evidence was printed."""
    script = _evidence_script(tmp_path, 'echo "partial evidence"\nexit 3\n')
    out = harvest_evidence(str(tmp_path), str(script), ["ch1"])
    assert out["ch1"] == "partial evidence"


def test_harvest_evidence_appends_stderr(tmp_path):
    """stderr is folded in under a marker so diagnostics on stderr aren't lost."""
    script = _evidence_script(
        tmp_path, 'echo "stdout body"\necho "stderr body" >&2\n',
    )
    out = harvest_evidence(str(tmp_path), str(script), ["ch1"])
    assert "stdout body" in out["ch1"]
    assert "[stderr]" in out["ch1"]
    assert "stderr body" in out["ch1"]


def test_harvest_evidence_clamps_to_max_chars(tmp_path):
    """Output is clamped so an oversized dump can't blow up the child kickoff."""
    script = _evidence_script(tmp_path, "printf 'x%.0s' {1..500}\n")
    out = harvest_evidence(str(tmp_path), str(script), ["ch1"], max_chars=100)
    assert len(out["ch1"]) == 100


def test_harvest_evidence_empty_units_is_empty_dict(tmp_path):
    script = _evidence_script(tmp_path, 'echo "unused"\n')
    assert harvest_evidence(str(tmp_path), str(script), []) == {}


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


# ── 5. goal_apply_deploy core (_run_apply): disabled / dry / live ────────
# The loop-closing node. Tests drive the ledger-free ``_run_apply`` core
# directly with fake commands + a tmp run dir; all wait windows collapse to
# zero so the block is instant.


def _write_sweep_result(run_dir: Path, units: list[dict]) -> None:
    (run_dir / "sweep-result.json").write_text(
        json.dumps({"status": "fanned_out", "units": units}), encoding="utf-8",
    )


def _arm_apply_env(monkeypatch, target_cwd: Path) -> None:
    """Common armed-but-fast env: no deploy-await command + zero waits."""
    monkeypatch.setenv("MO_GOAL_APPLY", "1")
    monkeypatch.setenv("MO_GOAL_TARGET_CWD", str(target_cwd))
    monkeypatch.delenv("MO_GOAL_APPLY_DRY", raising=False)
    monkeypatch.delenv("MO_GOAL_AWAIT_DEPLOY_CMD", raising=False)
    monkeypatch.setenv("MO_GOAL_DEPLOY_SETTLE_SECONDS", "0")
    monkeypatch.setenv("MO_GOAL_APPLY_POLL_SECONDS", "0")
    monkeypatch.setenv("MO_GOAL_APPLY_AWAIT_SECONDS", "0")


def test_apply_disabled_is_noop_passthrough(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_GOAL_APPLY", raising=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sweep_result(run_dir, [{"unit_id": "1", "status": "spawned"}])
    payload = run_apply(str(run_dir))
    assert payload["status"] == "disabled"
    assert payload["units"] == []


def test_apply_armed_requires_deploy_and_redispatch_cmds(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sweep_result(run_dir, [{"unit_id": "1", "status": "spawned"}])
    monkeypatch.setenv("MO_GOAL_APPLY", "1")
    monkeypatch.setenv("MO_GOAL_TARGET_CWD", str(tmp_path))
    monkeypatch.delenv("MO_GOAL_APPLY_CMD", raising=False)
    monkeypatch.delenv("MO_GOAL_REDISPATCH_CMD", raising=False)
    with pytest.raises(ArtifactContractError):
        run_apply(str(run_dir))


def test_apply_dry_records_plan_without_executing(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # unit 2 is 'deferred' — never touched the tree, so it must be excluded.
    _write_sweep_result(run_dir, [
        {"unit_id": "1", "status": "spawned"},
        {"unit_id": "2", "status": "deferred"},
    ])
    sentinel = tmp_path / "SHOULD_NOT_EXIST"
    monkeypatch.setenv("MO_GOAL_APPLY", "1")
    monkeypatch.setenv("MO_GOAL_APPLY_DRY", "1")
    monkeypatch.setenv("MO_GOAL_TARGET_CWD", str(tmp_path))
    monkeypatch.setenv("MO_GOAL_APPLY_CMD", f"touch {sentinel}")
    monkeypatch.setenv("MO_GOAL_REDISPATCH_CMD", f"touch {sentinel}")
    payload = run_apply(str(run_dir))
    assert payload["status"] == "dry_run"
    assert [u["unit_id"] for u in payload["units"]] == ["1"]
    assert not sentinel.exists()  # dry never runs the deploy/redispatch


def test_apply_live_deploys_redispatches_with_argv_and_awaits(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sweep_result(run_dir, [{"unit_id": "1", "status": "spawned"}])

    deploy_marker = tmp_path / "deployed"
    redispatch_log = tmp_path / "redispatch.log"
    redispatch = tmp_path / "redispatch.sh"
    redispatch.write_text(
        f"#!/usr/bin/env bash\necho \"redispatch $1\" >> {redispatch_log}\nexit 0\n",
        encoding="utf-8",
    )
    redispatch.chmod(0o755)
    terminal = tmp_path / "terminal.sh"  # settles immediately
    terminal.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    terminal.chmod(0o755)

    _arm_apply_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MO_GOAL_APPLY_CMD", f"touch {deploy_marker}")
    monkeypatch.setenv("MO_GOAL_REDISPATCH_CMD", str(redispatch))
    monkeypatch.setenv("MO_GOAL_TERMINAL_CMD", str(terminal))

    payload = run_apply(str(run_dir))
    assert payload["status"] == "applied"
    assert deploy_marker.exists()  # deploy command ran once
    assert payload["deploy"]["rc"] == 0
    # unit id handed to redispatch as a discrete argv slot (never shell-split).
    assert redispatch_log.read_text().strip() == "redispatch 1"
    assert payload["units"][0]["await_regen"]["settled"] is True


def test_apply_live_failed_status_still_deploys_and_await_gives_up(tmp_path, monkeypatch):
    # A 'failed' child commonly leaves a real patch behind; it must still be
    # deployed. And a unit that never settles must make the await BLOCK give up
    # (settled False) rather than hang forever.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sweep_result(run_dir, [{"unit_id": "1", "status": "failed"}])
    terminal = tmp_path / "terminal.sh"  # never settles
    terminal.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    terminal.chmod(0o755)

    _arm_apply_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MO_GOAL_APPLY_CMD", "true")
    monkeypatch.setenv("MO_GOAL_REDISPATCH_CMD", "true")
    monkeypatch.setenv("MO_GOAL_TERMINAL_CMD", str(terminal))

    payload = run_apply(str(run_dir))
    assert payload["status"] == "applied"
    assert payload["units"][0]["unit_id"] == "1"
    assert payload["units"][0]["await_regen"]["settled"] is False


# ── 5b. Fail-fast terminal detection (MO_GOAL_TERMINAL_FAIL_CMD) ───────────
# The await used to only stop on SUCCESS, burning the whole
# MO_GOAL_APPLY_AWAIT_SECONDS window (90 min live) even on a deploy proven
# dead. The optional fail-cmd lets the loop give up within one poll. These
# lock the three branches of _poll_until_settled as reached THROUGH run_apply.


def _mk_probe(path: Path, rc: int) -> Path:
    path.write_text(f"#!/usr/bin/env bash\nexit {rc}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_apply_live_terminal_fail_cmd_gives_up_fast(tmp_path, monkeypatch):
    # Pass probe never fires (exit 1); the fail probe fires (exit 0). The await
    # must settle terminal_failure immediately — NOT wait for the pass window.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sweep_result(run_dir, [{"unit_id": "1", "status": "spawned"}])
    never_pass = _mk_probe(tmp_path / "pass.sh", 1)
    fail_now = _mk_probe(tmp_path / "fail.sh", 0)

    _arm_apply_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MO_GOAL_APPLY_CMD", "true")
    monkeypatch.setenv("MO_GOAL_REDISPATCH_CMD", "true")
    monkeypatch.setenv("MO_GOAL_TERMINAL_CMD", str(never_pass))
    monkeypatch.setenv("MO_GOAL_TERMINAL_FAIL_CMD", str(fail_now))

    regen = run_apply(str(run_dir))["units"][0]["await_regen"]
    assert regen["settled"] is False
    assert regen["terminal"] is True
    assert regen["reason"] == "terminal_failure"


def test_apply_live_terminal_fail_cmd_unset_is_backward_compatible(tmp_path, monkeypatch):
    # No fail-cmd ⇒ a never-passing unit must exhaust the window and report a
    # plain timeout (terminal False) — the historical wait-for-pass behavior,
    # distinct from a terminal_failure. Proves the seam is strictly opt-in.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sweep_result(run_dir, [{"unit_id": "1", "status": "spawned"}])
    never_pass = _mk_probe(tmp_path / "pass.sh", 1)

    _arm_apply_env(monkeypatch, tmp_path)
    monkeypatch.delenv("MO_GOAL_TERMINAL_FAIL_CMD", raising=False)
    monkeypatch.setenv("MO_GOAL_APPLY_CMD", "true")
    monkeypatch.setenv("MO_GOAL_REDISPATCH_CMD", "true")
    monkeypatch.setenv("MO_GOAL_TERMINAL_CMD", str(never_pass))

    regen = run_apply(str(run_dir))["units"][0]["await_regen"]
    assert regen["settled"] is False
    assert regen["terminal"] is False
    assert regen["reason"] == "timeout"


def test_apply_live_terminal_pass_beats_fail(tmp_path, monkeypatch):
    # Success is probed FIRST each cycle: a unit that both passed and (racily)
    # trips the fail probe must be reported as PASSED, never abandoned.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sweep_result(run_dir, [{"unit_id": "1", "status": "spawned"}])
    pass_now = _mk_probe(tmp_path / "pass.sh", 0)
    fail_now = _mk_probe(tmp_path / "fail.sh", 0)

    _arm_apply_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MO_GOAL_APPLY_CMD", "true")
    monkeypatch.setenv("MO_GOAL_REDISPATCH_CMD", "true")
    monkeypatch.setenv("MO_GOAL_TERMINAL_CMD", str(pass_now))
    monkeypatch.setenv("MO_GOAL_TERMINAL_FAIL_CMD", str(fail_now))

    regen = run_apply(str(run_dir))["units"][0]["await_regen"]
    assert regen["settled"] is True
    assert regen["reason"] == "goal_met"


# ── 5c. chapter_terminal_fail binding: guard rails + stall helper ──────────
# Hermetic — exercises only the paths BEFORE the psql call (argv/BOOK_UUID
# validation) plus the pure run-dir freshness helper. No DB required.


def test_terminal_fail_guards_reject_bad_invocation(monkeypatch):
    monkeypatch.delenv("BOOK_UUID", raising=False)
    # No chapter id at all.
    assert _tfmod.main([]) == 2
    # Non-numeric chapter id.
    monkeypatch.setenv("BOOK_UUID", "00000000-0000-0000-0000-000000000000")
    assert _tfmod.main(["not-a-number"]) == 2
    # Valid chapter but missing/short BOOK_UUID never reaches psql.
    monkeypatch.setenv("BOOK_UUID", "too-short")
    assert _tfmod.main(["1"]) == 2


def test_terminal_fail_newest_run_age(tmp_path):
    import time as _t

    # Empty / missing dir ⇒ None (no run has ever churned).
    assert _tfmod._newest_run_age_seconds(str(tmp_path / "nope")) is None
    runs = tmp_path / "runs"
    runs.mkdir()
    assert _tfmod._newest_run_age_seconds(str(runs)) is None
    # A fresh run-* dir ⇒ a small, non-negative age (churning, not stalled).
    (runs / "run-123").mkdir()
    age = _tfmod._newest_run_age_seconds(str(runs))
    assert age is not None and 0.0 <= age < 60.0
    # An OLD run-* dir ⇒ a large age (the stall detector's trip condition).
    old = runs / "run-000"
    old.mkdir()
    stale = _t.time() - 4000
    os.utime(old, (stale, stale))
    # Newest wins: run-123 is fresh, so the reported age stays small.
    assert _tfmod._newest_run_age_seconds(str(runs)) < 60.0


def _fake_q(status: str, committed: str, permfail: str, lasterr: str):
    """Build a psql-wrapper stand-in returning one row in the binding's 4-col
    ``status|committed_complete|permanently_failed|left(last_error,80)`` shape."""
    import subprocess as _sp

    row = f"{status}|{committed}|{permfail}|{lasterr}\n"
    return lambda _sql: _sp.CompletedProcess(args=[], returncode=0, stdout=row, stderr="")


def _mk_run(runs: Path, name: str, age_s: float) -> None:
    import time as _t

    d = runs / name
    d.mkdir(parents=True, exist_ok=True)
    when = _t.time() - age_s
    os.utime(d, (when, when))


def test_terminal_fail_worker_exhausted_idle_is_terminal(tmp_path, monkeypatch):
    # The dominant observed mode: worker burned its retry budget, left ch1
    # 'failed' (NOT permanently_failed), and went idle. status='failed' alone is
    # NOT terminal — but 'failed' + an IDLE run-dir (worker stopped) is, because
    # only a redispatch can advance it and the await must not sit out the window.
    runs = tmp_path / "runs"
    _mk_run(runs, "run-old", age_s=4000)  # worker idle 4000s
    monkeypatch.setattr(_tfmod, "_q", _fake_q("failed", "f", "f", "W9 boom"))
    monkeypatch.setenv("BOOK_UUID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("MO_GOAL_RUNS_DIR", str(runs))
    monkeypatch.setenv("MO_GOAL_STALL_SECONDS", "1200")
    monkeypatch.delenv("MO_GOAL_FAIL_ON_STATUS_FAILED", raising=False)
    assert _tfmod.main(["1"]) == 0


def test_terminal_fail_failed_but_still_churning_is_not_terminal(tmp_path, monkeypatch):
    # The safety case that makes broadening to 'failed' non-abandoning: a chapter
    # racily 'failed' BETWEEN retries still has a churning worker (fresh run-*),
    # so it must NOT be declared terminal — the redispatch would waste a wave.
    runs = tmp_path / "runs"
    _mk_run(runs, "run-fresh", age_s=5)  # worker churned 5s ago
    monkeypatch.setattr(_tfmod, "_q", _fake_q("failed", "f", "f", "W9 boom"))
    monkeypatch.setenv("BOOK_UUID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("MO_GOAL_RUNS_DIR", str(runs))
    monkeypatch.setenv("MO_GOAL_STALL_SECONDS", "1200")
    monkeypatch.delenv("MO_GOAL_FAIL_ON_STATUS_FAILED", raising=False)
    assert _tfmod.main(["1"]) == 1


def test_terminal_fail_permfail_always_terminal(monkeypatch):
    # permanently_failed=t fires regardless of run-dir freshness or stall knobs.
    monkeypatch.setattr(_tfmod, "_q", _fake_q("failed", "f", "t", "gave up"))
    monkeypatch.setenv("BOOK_UUID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.delenv("MO_GOAL_RUNS_DIR", raising=False)
    monkeypatch.delenv("MO_GOAL_STALL_SECONDS", raising=False)
    assert _tfmod.main(["1"]) == 0


# ── 6. _harvest_selected_evidence: sweep-plan enrichment + on-disk file ───
# The seam goal_sweep_plan calls to fill kickoff_hint.evidence / evidence_path.
# It must be OFF by default (no MO_GOAL_EVIDENCE_CMD → empty, so unarmed loops
# are unchanged), persist the full text under <run_dir>/evidence/<slug>.md, and
# degrade to the predicate reason when the harvest yields nothing.


def _arm_evidence(monkeypatch, tmp_path, body: str) -> None:
    script = _evidence_script(tmp_path, body)
    monkeypatch.setenv("MO_GOAL_EVIDENCE_CMD", str(script))
    monkeypatch.setenv("MO_GOAL_TARGET_CWD", str(tmp_path))


def test_harvest_selected_evidence_unset_cmd_is_empty(monkeypatch):
    """No MO_GOAL_EVIDENCE_CMD ⇒ {} ⇒ unarmed loops behave exactly as before."""
    monkeypatch.delenv("MO_GOAL_EVIDENCE_CMD", raising=False)
    goal_state = {"1": {"pass": False, "reason": "ch1 FAIL"}}
    assert harvest_selected_evidence(["1"], goal_state) == {}


def test_harvest_selected_evidence_writes_file_and_returns_text(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    _arm_evidence(monkeypatch, tmp_path, 'echo "deep evidence for $1"\n')

    goal_state = {"1": {"pass": False, "reason": "ch1 FAIL status=failed"}}
    out = harvest_selected_evidence(["1"], goal_state)

    assert out["1"]["text"] == "deep evidence for 1"
    ev_file = run_dir / "evidence" / "1.md"
    assert out["1"]["path"] == str(ev_file)
    assert ev_file.read_text(encoding="utf-8").strip() == "deep evidence for 1"


def test_harvest_selected_evidence_falls_back_to_reason_when_blank(tmp_path, monkeypatch):
    """A harvester that prints nothing ⇒ the child still gets the predicate reason."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    _arm_evidence(monkeypatch, tmp_path, "exit 0\n")  # no stdout

    goal_state = {"7": {"pass": False, "reason": "ch7 FAIL status=degraded"}}
    out = harvest_selected_evidence(["7"], goal_state)
    assert out["7"]["text"] == "ch7 FAIL status=degraded"


def test_harvest_selected_evidence_slug_sanitizes_pathlike_unit(tmp_path, monkeypatch):
    """A unit id with a slash yields a flat, slash-free evidence filename."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    _arm_evidence(monkeypatch, tmp_path, 'echo "ev"\n')

    goal_state = {"docs/ch-01.md": {"pass": False, "reason": "r"}}
    out = harvest_selected_evidence(["docs/ch-01.md"], goal_state)
    ev_path = Path(out["docs/ch-01.md"]["path"])
    assert "/" not in ev_path.name
    assert ev_path.name == f"{evidence_slug('docs/ch-01.md')}.md"
    assert ev_path.is_file()


def test_harvest_selected_evidence_no_run_dir_has_text_empty_path(tmp_path, monkeypatch):
    """Without MINI_ORK_RUN_DIR the text is still returned; only the path is blank."""
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    _arm_evidence(monkeypatch, tmp_path, 'echo "inline only for $1"\n')

    goal_state = {"2": {"pass": False, "reason": "ch2 FAIL"}}
    out = harvest_selected_evidence(["2"], goal_state)
    assert out["2"]["text"] == "inline only for 2"
    assert out["2"]["path"] == ""