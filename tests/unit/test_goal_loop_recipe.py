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
evidence_sha = _tmod._evidence_sha
render_wave_history_block = _tmod._render_wave_history_block
select_units = _tmod._select_units
unit_sort_key = _tmod._unit_sort_key
classify_failure = _tmod.classify_failure
operator_for = _tmod._operator_for

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


def _load_binding(name: str):
    """Load a binding script by file path (kickoffs/ is not a package)."""
    path = _BIND_DIR / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load binding from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


_predmod = _load_binding("chapter_predicate.py")
_qualmod = _load_binding("chapter_quality.py")


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


# ── 3b. _select_units: numeric-aware order + quarantine exclusion ─────────
# The wave-selection core. A single-child-per-wave book loop over ch1..ch10
# must (a) rotate ch1→ch2→ch3 in NATURAL order (not string 1,10,2,…) and
# (b) drop units the driver has quarantined so the freed slot reaches the
# other failing chapters instead of re-hammering the stuck one forever.


def _gs(*failing_and_passing):
    """Build a goal_state map: ('1', False), ('2', True) → {'1':{pass:False},…}."""
    return {uid: {"pass": ok} for uid, ok in failing_and_passing}


def test_unit_sort_key_orders_numeric_before_string_and_naturally():
    ids = ["10", "2", "1", "beta", "alpha", "9"]
    assert sorted(ids, key=unit_sort_key) == ["1", "2", "9", "10", "alpha", "beta"]


def test_select_units_picks_first_failing_natural_order():
    # ch1..ch10 all failing, one child slot → the ONE broken chapter, ch1.
    gs = _gs(*[(str(n), False) for n in range(1, 11)])
    assert select_units(gs, 1) == ["1"]
    # widen the slot and the next picks follow natural order (2 before 10).
    assert select_units(gs, 3) == ["1", "2", "3"]


def test_select_units_excludes_quarantined_and_rotates_slot():
    # ch1 quarantined (stuck) → the single slot rotates onto ch2, not ch1.
    gs = _gs(*[(str(n), False) for n in range(1, 11)])
    assert select_units(gs, 1, quarantined={"1"}) == ["2"]
    # quarantine ch1+ch2 → slot rotates to ch3.
    assert select_units(gs, 1, quarantined={"1", "2"}) == ["3"]


def test_select_units_starvation_guard_keeps_dispatching():
    # EVERY failing unit quarantined → exclusion dropped so the wave still
    # dispatches (driver's all_quarantined stop ends the loop, not a silent
    # empty plan).
    gs = _gs(("1", False), ("2", False))
    assert select_units(gs, 1, quarantined={"1", "2"}) == ["1"]


def test_select_units_skips_passing_units():
    # ch1 PASSED, ch2..ch4 failing → ch1 never selected; ch2 leads.
    gs = _gs(("1", True), ("2", False), ("3", False), ("4", False))
    assert select_units(gs, 1) == ["2"]


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
    # The instrument guard reads ambient env; an inherited value (e.g. when the
    # suite runs inside a goal-loop wave) would make these tests repo-dependent.
    monkeypatch.delenv("MO_GOAL_PROTECTED_PATHS", raising=False)
    monkeypatch.delenv("MO_GOAL_PROTECTED_MODE", raising=False)


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


# ── 5b. instrument guard: the fix child may not edit what scores it ────────
# The predicate's inputs are written by code inside the child's editable tree,
# so before anything ships, git is asked whether the child touched a path the
# operator declared part of the instrument. Refusal is fail-closed: an armed
# guard that cannot read the tree must not wave the deploy through either.


def _git_repo(path: Path, files: dict[str, str]) -> None:
    """A real repo with one commit, so ``git status`` has a HEAD to diff from."""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    for rel, body in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)


def _armed_repo(tmp_path, monkeypatch, instrument_glob: str) -> tuple[Path, Path]:
    """A repo with one instrument file + a separate generation file; returns
    (run_dir, deploy_marker) with the guard armed for ``instrument_glob``."""
    _git_repo(tmp_path, {
        "server/scoring.ts": "export const bar = 1;\n",
        "server/generation.ts": "export const draft = 1;\n",
    })
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sweep_result(run_dir, [{"unit_id": "1", "status": "spawned"}])
    _arm_apply_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MO_GOAL_PROTECTED_PATHS", instrument_glob)
    marker = tmp_path / "deployed"
    monkeypatch.setenv("MO_GOAL_APPLY_CMD", f"touch {marker}")
    monkeypatch.setenv("MO_GOAL_REDISPATCH_CMD", "true")
    monkeypatch.setenv("MO_GOAL_TERMINAL_CMD", "true")
    return run_dir, marker


def test_apply_guard_unset_is_inert(tmp_path, monkeypatch):
    # No MO_GOAL_PROTECTED_PATHS ⇒ historical behavior, whatever the tree says.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sweep_result(run_dir, [{"unit_id": "1", "status": "spawned"}])
    _arm_apply_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MO_GOAL_APPLY_CMD", "true")
    monkeypatch.setenv("MO_GOAL_REDISPATCH_CMD", "true")
    monkeypatch.setenv("MO_GOAL_TERMINAL_CMD", "true")
    payload = run_apply(str(run_dir))
    assert payload["status"] == "applied"
    assert payload["protected_violations"] == []


def test_apply_guard_refuses_when_instrument_file_edited(tmp_path, monkeypatch):
    run_dir, marker = _armed_repo(tmp_path, monkeypatch, "server/scoring.ts")
    (tmp_path / "server" / "scoring.ts").write_text("export const bar = 999;\n")

    payload = run_apply(str(run_dir))
    assert payload["status"] == "refused_instrument_edit"
    assert payload["units"] == []
    assert [v["glob"] for v in payload["violations"]] == ["server/scoring.ts"]
    assert not marker.exists()  # the deploy never ran


def test_apply_guard_refuses_on_untracked_instrument_file(tmp_path, monkeypatch):
    # -uall matters: a NEW file at a protected path is still an instrument edit.
    run_dir, marker = _armed_repo(tmp_path, monkeypatch, "server/scoring.ts")
    (tmp_path / "server" / "scoring.ts").unlink()
    (tmp_path / "server" / "scoring.ts").write_text("export const bar = 2;\n")

    payload = run_apply(str(run_dir))
    assert payload["status"] == "refused_instrument_edit"
    assert not marker.exists()


def test_apply_guard_allows_edits_outside_the_instrument(tmp_path, monkeypatch):
    # The whole point: generation stays fixable. This is the W15 class of edit.
    run_dir, marker = _armed_repo(tmp_path, monkeypatch, "server/scoring.ts")
    (tmp_path / "server" / "generation.ts").write_text("export const draft = 42;\n")

    payload = run_apply(str(run_dir))
    assert payload["status"] == "applied"
    assert payload["protected_violations"] == []
    assert marker.exists()


def test_apply_guard_warn_mode_ships_but_records(tmp_path, monkeypatch):
    run_dir, marker = _armed_repo(tmp_path, monkeypatch, "server/scoring.ts")
    monkeypatch.setenv("MO_GOAL_PROTECTED_MODE", "warn")
    (tmp_path / "server" / "scoring.ts").write_text("export const bar = 999;\n")

    payload = run_apply(str(run_dir))
    assert payload["status"] == "applied"
    assert [v["glob"] for v in payload["protected_violations"]] == ["server/scoring.ts"]
    assert marker.exists()


def test_apply_guard_dry_run_reports_without_refusing(tmp_path, monkeypatch):
    # A rehearsal must not change the plan, but should surface what the live run
    # would decide — otherwise the guard is discovered only at the go/no-go.
    run_dir, marker = _armed_repo(tmp_path, monkeypatch, "server/scoring.ts")
    monkeypatch.setenv("MO_GOAL_APPLY_DRY", "1")
    (tmp_path / "server" / "scoring.ts").write_text("export const bar = 999;\n")

    payload = run_apply(str(run_dir))
    assert payload["status"] == "dry_run"
    assert [v["glob"] for v in payload["protected_violations"]] == ["server/scoring.ts"]
    assert not marker.exists()


def test_apply_guard_fails_closed_when_git_cannot_read(tmp_path, monkeypatch):
    # Armed but the target is not a repo: the guard cannot prove the tree is
    # clean, so it refuses rather than shipping blind.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sweep_result(run_dir, [{"unit_id": "1", "status": "spawned"}])
    _arm_apply_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MO_GOAL_PROTECTED_PATHS", "server/scoring.ts")
    marker = tmp_path / "deployed"
    monkeypatch.setenv("MO_GOAL_APPLY_CMD", f"touch {marker}")
    monkeypatch.setenv("MO_GOAL_REDISPATCH_CMD", "true")
    monkeypatch.setenv("MO_GOAL_TERMINAL_CMD", "true")

    payload = run_apply(str(run_dir))
    assert payload["status"] == "refused_instrument_edit"
    assert payload["violations"][0]["error"] == "git-status-failed"
    assert not marker.exists()


# ── 5c. chapter_terminal_fail binding: guard rails + stall helper ──────────
# Hermetic — exercises only the paths BEFORE the psql call (argv/BOOK_UUID
# validation) plus the pure run-dir freshness helper. No DB required.


def test_terminal_fail_guards_reject_bad_invocation(monkeypatch):
    monkeypatch.delenv("BOOK_UUID", raising=False)
    # No chapter id at all.
    assert _tfmod.main([]) == 2
    # Non-numeric chapter id.
    monkeypatch.setenv("BOOK_UUID", "d0df3cdb-8164-450e-b841-2c9354ea0423")
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
    monkeypatch.setenv("BOOK_UUID", "d0df3cdb-8164-450e-b841-2c9354ea0423")
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
    monkeypatch.setenv("BOOK_UUID", "d0df3cdb-8164-450e-b841-2c9354ea0423")
    monkeypatch.setenv("MO_GOAL_RUNS_DIR", str(runs))
    monkeypatch.setenv("MO_GOAL_STALL_SECONDS", "1200")
    monkeypatch.delenv("MO_GOAL_FAIL_ON_STATUS_FAILED", raising=False)
    assert _tfmod.main(["1"]) == 1


def test_terminal_fail_permfail_always_terminal(monkeypatch):
    # permanently_failed=t fires regardless of run-dir freshness or stall knobs.
    monkeypatch.setattr(_tfmod, "_q", _fake_q("failed", "f", "t", "gave up"))
    monkeypatch.setenv("BOOK_UUID", "d0df3cdb-8164-450e-b841-2c9354ea0423")
    monkeypatch.delenv("MO_GOAL_RUNS_DIR", raising=False)
    monkeypatch.delenv("MO_GOAL_STALL_SECONDS", raising=False)
    assert _tfmod.main(["1"]) == 0


# ── 5d. objective quality anchor (chapter_quality + the predicate hook) ────
# Both halves of the goal predicate are the researcher's own self-report. The
# anchor is the one signal authored OUTSIDE the judged system, so these tests
# pin its pure verdict logic (no DB) and the predicate's use of it.


def _sec(idx: int, slug: str, length: int, sha: str = "a" * 64, doc: int = 1):
    return (idx, slug, length, sha, doc)


def _clean_rows(n: int = 4, length: int = 5000) -> list:
    return [_sec(i + 1, f"section-{i + 1}", length) for i in range(n)]


def test_quality_floor_passes_a_real_chapter(monkeypatch):
    for var in ("MO_GOAL_QUALITY_MIN_SECTIONS", "MO_GOAL_QUALITY_MIN_SECTION_CHARS",
                "MO_GOAL_QUALITY_MIN_TOTAL_CHARS"):
        monkeypatch.delenv(var, raising=False)
    bad, facts = _qualmod._failures(_clean_rows(), "# S1\n" + "body " * 500)
    assert bad == []
    assert facts["sections"] == 4 and facts["doc_version"] == 1


def test_quality_floor_flags_each_vacuity_shape(monkeypatch):
    monkeypatch.delenv("MO_GOAL_QUALITY_MIN_SECTIONS", raising=False)
    # Too few sections AND too thin overall.
    bad, _ = _qualmod._failures([_sec(1, "only", 100)], "short")
    assert any(b.startswith("sections=") for b in bad)
    assert any(b.startswith("total=") for b in bad)
    assert any(b.startswith("short-section") for b in bad)
    # A section the commit path never hashed.
    bad, _ = _qualmod._failures([_sec(1, "s", 5000, sha="")], "x" * 5000)
    assert any(b.startswith("unhashed-section") for b in bad)
    # Parts of one commit disagreeing on doc_version is an assembly bug.
    bad, _ = _qualmod._failures(_clean_rows(3) + [_sec(4, "s4", 5000, doc=2)], "")
    assert any(b.startswith("mixed-doc-version") for b in bad)
    # The same H2 emitted twice.
    bad, _ = _qualmod._failures(
        [_sec(1, "dup", 5000), _sec(2, "dup", 5000), _sec(3, "x", 5000)], "",
    )
    assert any(b.startswith("dup-h2") for b in bad)


def test_quality_floor_flags_placeholders():
    # A chapter that is well-formed on every structural axis but still hollow.
    bad, _ = _qualmod._failures(_clean_rows(), "TODO: write this\n" + "body " * 500)
    assert any(b.startswith("placeholder:todo") for b in bad)
    bad, _ = _qualmod._failures(_clean_rows(), "{{ citation_needed }}\n" + "body " * 500)
    assert any(b.startswith("placeholder:unresolved-template") for b in bad)


def test_quality_floor_no_sections_is_a_failure():
    bad, facts = _qualmod._failures([], "")
    assert bad == ["no-sections"]
    assert facts["sections"] == 0


def test_quality_floor_does_not_false_trip_a_clean_chapter():
    # Calibrated against the one known-good committed chapter's shape: 4 sections,
    # 4,473-8,019 chars, single doc_version, all hashed, no placeholder markers.
    rows = [_sec(1, "a", 4942), _sec(2, "b", 4473), _sec(3, "c", 5656), _sec(4, "d", 8019)]
    bad, facts = _qualmod._failures(rows, "# a\n# b\n# c\n# d\n" + "prose " * 5000)
    assert bad == []
    assert facts["total_chars"] == 23090


# ── the floor the PLAN declared (bg_chapter_plan_spec.budgets.min_words) ──
# A uniform env floor cannot express "this chapter was planned to carry eight
# sections, so 3,000 chars is thin". These pin the declared floor's two jobs:
# raise the bar above the operator's when the plan commits to more, and never
# lower it when the plan commits to less (or to nothing at all).


def test_quality_floor_takes_the_plans_declared_bar(monkeypatch):
    monkeypatch.delenv("MO_GOAL_QUALITY_MIN_TOTAL_CHARS", raising=False)
    bad, facts = _qualmod._failures(
        _clean_rows(3, 1700), "body " * 500, planned_floor=6500,
    )
    assert bad == ["total=5100<6500:planned"]
    assert facts["floor"] == 6500 and facts["floor_source"] == "planned"


def test_quality_floor_never_lowers_below_the_operator_bar(monkeypatch):
    # A plan declaring LESS than the operator floor must not lower it: the env
    # number is a floor, not a default to override. Same verdict as no plan.
    monkeypatch.setenv("MO_GOAL_QUALITY_MIN_TOTAL_CHARS", "4000")
    bad, facts = _qualmod._failures(_clean_rows(3, 1000), "prose " * 600, planned_floor=600)
    assert bad == ["total=3000<4000"]
    assert facts["floor_source"] == "env"


def test_quality_floor_absent_plan_is_the_status_quo(monkeypatch):
    # None == no spec row / null min_words / unreadable row. Not a failure, and
    # visibly not the plan's number.
    monkeypatch.setenv("MO_GOAL_QUALITY_MIN_TOTAL_CHARS", "4000")
    bad, facts = _qualmod._failures(_clean_rows(3, 1000), "prose " * 600, planned_floor=None)
    assert bad == ["total=3000<4000"]
    assert facts["floor"] == 4000 and facts["floor_source"] == "env"


def test_quality_floor_a_plan_bar_cleared_is_not_a_failure(monkeypatch):
    monkeypatch.delenv("MO_GOAL_QUALITY_MIN_TOTAL_CHARS", raising=False)
    bad, facts = _qualmod._failures(
        _clean_rows(3, 2500), "body " * 500, planned_floor=6500,
    )
    assert bad == []
    assert facts["floor_source"] == "planned" and facts["total_chars"] == 7500


def test_planned_floor_sql_targets_the_spec_budget():
    # Contract guard: the probe must read the adoption gate's field off the spec
    # row, resolved through books (the spec keys on book_id, the loop only has
    # document_uuid) and ordered by the table's own W6 read axis — a re-planned
    # book must resolve to its newest spec, never a stale one.
    assert "bg_chapter_plan_spec" in _qualmod._PLANNED_FLOOR
    assert "budgets->>'min_words'" in _qualmod._PLANNED_FLOOR
    assert "b.document_uuid=" in _qualmod._PLANNED_FLOOR
    assert "ORDER BY s.created_at DESC" in _qualmod._PLANNED_FLOOR


def _floor_proc(stdout: str, rc: int = 0):
    import subprocess as _sp

    return lambda _sql: _sp.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


def test_planned_floor_reads_words_and_converts_to_chars(monkeypatch):
    monkeypatch.delenv("MO_GOAL_QUALITY_CHARS_PER_WORD", raising=False)
    monkeypatch.setattr(_qualmod, "_q", _floor_proc("1417\n"))
    assert _qualmod._planned_floor("d0df3cdb-8164-450e-b841-2c9354ea0423", "3") == 1417 * 6


def test_planned_floor_unit_matches_the_gate_it_reads(monkeypatch):
    # One divisor for both directions: the gate derived min_words with this
    # number, so this check must convert back with the same one.
    monkeypatch.setenv("MO_GOAL_QUALITY_CHARS_PER_WORD", "5")
    monkeypatch.setattr(_qualmod, "_q", _floor_proc("1417\n"))
    assert _qualmod._planned_floor("b", "1") == 1417 * 5


def test_planned_floor_absent_or_unreadable_is_none(monkeypatch):
    monkeypatch.delenv("MO_GOAL_QUALITY_CHARS_PER_WORD", raising=False)
    # No spec row for the book: psql succeeds with an empty result.
    monkeypatch.setattr(_qualmod, "_q", _floor_proc("\n"))
    assert _qualmod._planned_floor("b", "1") is None
    # A probe error is the same answer, deliberately: this term is additive
    # strictness, so an unreachable row leaves the operator floor in place.
    monkeypatch.setattr(_qualmod, "_q", _floor_proc("", rc=2))
    assert _qualmod._planned_floor("b", "1") is None
    # A non-numeric payload never raises into the caller.
    monkeypatch.setattr(_qualmod, "_q", _floor_proc("null\n"))
    assert _qualmod._planned_floor("b", "1") is None


def test_quality_probe_absent_cmd_is_not_consulted(monkeypatch):
    monkeypatch.delenv("MO_GOAL_QUALITY_CMD", raising=False)
    assert _predmod._quality_cmd() == ""


def test_quality_probe_reports_ok_and_failure(tmp_path, monkeypatch):
    marker = tmp_path / "args.txt"
    script = tmp_path / "probe.sh"
    script.write_text(
        f'#!/usr/bin/env bash\necho "$1" > {marker}\n'
        'case "$1" in 1) echo PASS-LINE; exit 0;; *) echo FAIL-LINE; exit 1;; esac\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("MO_GOAL_QUALITY_CMD", str(script))
    monkeypatch.setenv("MO_GOAL_TARGET_CWD", str(tmp_path))

    ok, detail = _predmod._quality_probe("1")
    assert (ok, detail) == (True, "PASS-LINE")
    assert marker.read_text().strip() == "1"  # unit id, never shell-interpolated
    ok, detail = _predmod._quality_probe("2")
    assert (ok, detail) == (False, "FAIL-LINE")


def test_quality_probe_missing_binary_is_a_failure(monkeypatch):
    # An armed anchor that cannot run must not silently abstain: abstaining is
    # exactly the self-report hole it exists to close.
    monkeypatch.setenv("MO_GOAL_QUALITY_CMD", "definitely-not-a-real-binary-xyz")
    ok, detail = _predmod._quality_probe("1")
    assert ok is False
    assert detail.startswith("probe-error")


def test_quality_mode_defaults_to_enforce_and_warn_opt_in(monkeypatch):
    monkeypatch.delenv("MO_GOAL_QUALITY_MODE", raising=False)
    assert _predmod._quality_enforcing() is True
    monkeypatch.setenv("MO_GOAL_QUALITY_MODE", "warn")
    assert _predmod._quality_enforcing() is False


# ── 6. _harvest_selected_evidence: sweep-plan enrichment + on-disk file ───
# The seam goal_sweep_plan calls to fill kickoff_hint.evidence / evidence_path.
# It must be OFF by default (no MO_GOAL_EVIDENCE_CMD → empty, so unarmed loops
# are unchanged), persist the full text under <run_dir>/evidence/<slug>.md, and
# degrade to the predicate reason when the harvest yields nothing.


def _arm_evidence(monkeypatch, tmp_path, body: str) -> None:
    script = _evidence_script(tmp_path, body)
    monkeypatch.setenv("MO_GOAL_EVIDENCE_CMD", str(script))
    monkeypatch.setenv("MO_GOAL_TARGET_CWD", str(tmp_path))
    # The driver leaks MO_GOAL_WAVE_HISTORY into the process env on every wave;
    # clear the meta^n channel so an armed harvest test reads a clean env.
    monkeypatch.delenv("MO_GOAL_WAVE_HISTORY", raising=False)
    monkeypatch.delenv("MO_GOAL_EVIDENCE_UNINFORMATIVE", raising=False)


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


# ── 7. diagnostic policy: evidence_sha + prior-waves history block ────────
# goal_sweep_plan delegates to two ledger-free seams pinned directly here:
# _evidence_sha (the per-entry sweep-plan fingerprint) and the MO_GOAL_WAVE_HISTORY
# block _harvest_selected_evidence appends to the evidence text BEFORE it is
# fingerprinted. The @register_transform wrapper itself is exercised end-to-end
# by test_workflow_compiles_with_six_edge_chain above.


def test_evidence_sha_is_stable_and_empty_for_blank():
    assert evidence_sha("") == ""
    digest = evidence_sha("deep evidence for ch1")
    assert len(digest) == 64  # sha256 hex
    assert digest == evidence_sha("deep evidence for ch1")  # deterministic
    assert digest != evidence_sha("deep evidence for ch2")


def test_render_wave_history_block_empty_on_absent_or_garbage(monkeypatch):
    monkeypatch.delenv("MO_GOAL_WAVE_HISTORY", raising=False)
    assert render_wave_history_block("") == ""
    assert render_wave_history_block("not json") == ""
    assert render_wave_history_block("[]") == ""      # empty list → no block
    assert render_wave_history_block('{"a": 1}') == ""  # not a list → no block


def test_harvest_selected_evidence_appends_history_block_when_set(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    _arm_evidence(monkeypatch, tmp_path, 'echo "deep evidence for $1"\n')
    monkeypatch.setenv("MO_GOAL_WAVE_HISTORY", json.dumps([
        {"wave": 1, "attempted": ["1"], "child_verdict": ["pass"],
         "review_diff_bytes": [8313], "headroom_closed": 0, "predicate_moved": False},
    ]))

    goal_state = {"1": {"pass": False, "reason": "ch1 FAIL status=failed"}}
    out = harvest_selected_evidence(["1"], goal_state)

    assert "### Prior waves" in out["1"]["text"]
    assert "do NOT repeat these" in out["1"]["text"]
    # the appended block changes the fingerprint goal_sweep_plan will emit.
    assert evidence_sha(out["1"]["text"]) != evidence_sha("deep evidence for 1")
    ev_file = run_dir / "evidence" / "1.md"
    assert ev_file.read_text(encoding="utf-8").strip() == out["1"]["text"].strip()


def test_harvest_selected_evidence_unset_history_is_byte_identical(tmp_path, monkeypatch):
    """MO_GOAL_WAVE_HISTORY unset ⇒ the evidence file is byte-identical to today."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    _arm_evidence(monkeypatch, tmp_path, 'echo "deep evidence for $1"\n')
    monkeypatch.delenv("MO_GOAL_WAVE_HISTORY", raising=False)
    monkeypatch.delenv("MO_GOAL_EVIDENCE_UNINFORMATIVE", raising=False)

    goal_state = {"1": {"pass": False, "reason": "ch1 FAIL status=failed"}}
    out = harvest_selected_evidence(["1"], goal_state)

    assert out["1"]["text"] == "deep evidence for 1"
    assert "### Prior waves" not in out["1"]["text"]
    assert evidence_sha(out["1"]["text"]) == evidence_sha("deep evidence for 1")

# ── 8. operator typing (S5, SHADOW) ────────────────────────────────────────
#
# The loop's action set has size one: every wave spawns MO_GOAL_CHILD_RECIPE
# (code-fix) for the selected unit. That is a constant, not a policy. The
# classifier names the class of action a failure actually calls for, so the
# loop can RECORD what a typed action set would have chosen. Dispatch is
# untouched: `child_recipe` is still what spawns.

# Live reasons, copied verbatim from goal-state.json on the running book loop.
_CH4_NEVER_RAN = (
    "ch4 FAIL status=pending rubric=pending committed=f permfail=f "
    "degraded=f attempts=0 mdlen=43368"
)
_CH1_HARNESS_CONFLICT = (
    "ch1 FAIL status=generating rubric=pass committed=f attempts=2 mdlen=0 "
    "err=chapterGevalRepair: 0 of 10 edits anchored"
)


def test_classify_failure_defaults_to_code_fix():
    operator, rationale = classify_failure(
        "ch2 FAIL status=failed rubric=fail committed=t attempts=1 mdlen=9000")
    assert operator == "code-fix"
    assert "historical default" in rationale


def test_classify_failure_names_dispatch_repair_for_a_unit_that_never_ran():
    """status=pending + attempts=0 ⇒ the dispatcher never started it.

    This is the class the live ch4 sits in: no code-fix child can reach it,
    which is why the loop re-paid an identical patch every wave."""
    operator, rationale = classify_failure(_CH4_NEVER_RAN)
    assert operator == "dispatch-repair"
    assert "never ran" in rationale


def test_classify_failure_names_framework_edit_for_a_harness_stage_conflict():
    """A repair/gate stage rejecting the citation form an earlier stage emitted."""
    operator, rationale = classify_failure(_CH1_HARNESS_CONFLICT)
    assert operator == "framework-edit"
    assert "stage-order conflict" in rationale


def test_classify_failure_does_not_escalate_on_a_bare_stage_mention():
    """The stage name alone is not a signature — both halves must match."""
    operator, _ = classify_failure(
        "ch9 FAIL status=failed err=chapterGevalRepair: retry budget exhausted")
    assert operator == "code-fix"


def test_classify_failure_treats_a_running_unit_as_code_fix():
    """attempts>0 means it DID run, so a child can reach the failure."""
    operator, _ = classify_failure(
        "ch5 FAIL status=generating rubric=pending committed=f attempts=3 mdlen=12000")
    assert operator == "code-fix"


def test_classify_failure_never_raises_and_defaults_on_empty():
    for reason in ("", None, "garbage", "status=pending"):
        operator, rationale = classify_failure(reason)  # type: ignore[arg-type]
        assert operator == "code-fix"
        assert rationale


def test_operator_for_honors_the_disable_switch(monkeypatch):
    goal_state = {"4": {"reason": _CH4_NEVER_RAN}}
    monkeypatch.setenv("MO_GOAL_OPERATOR_TYPING", "0")
    operator, rationale = operator_for("4", goal_state)
    assert operator == "code-fix"
    assert rationale == "operator typing disabled"

    monkeypatch.setenv("MO_GOAL_OPERATOR_TYPING", "1")
    assert operator_for("4", goal_state)[0] == "dispatch-repair"


def test_operator_for_reads_each_units_own_reason(monkeypatch):
    monkeypatch.delenv("MO_GOAL_OPERATOR_TYPING", raising=False)
    goal_state = {
        "1": {"reason": _CH1_HARNESS_CONFLICT},
        "4": {"reason": _CH4_NEVER_RAN},
        "7": {"reason": "ch7 PASS"},
    }
    assert operator_for("1", goal_state)[0] == "framework-edit"
    assert operator_for("4", goal_state)[0] == "dispatch-repair"
    assert operator_for("7", goal_state)[0] == "code-fix"

# ── goal-level diagnostics (pure) ──────────────────────────────────────────
#
# ``loop_state`` is loaded here by file path too; the driver tests exercise it
# through ``drive()``, these pin the grammar and the ordering directly.

_LOOP_STATE_PATH = RECIPE_DIR / "lib" / "loop_state.py"
_ls_spec = importlib.util.spec_from_file_location(
    "goal_loop_loop_state_recipe_tests", _LOOP_STATE_PATH,
)
if _ls_spec is None or _ls_spec.loader is None:
    raise ImportError(f"could not load loop_state helper from {_LOOP_STATE_PATH}")
_ls = importlib.util.module_from_spec(_ls_spec)
sys.modules.setdefault(_ls_spec.name, _ls)
_ls_spec.loader.exec_module(_ls)

_reason_axes = _ls._reason_axes  # noqa: SLF001 — test seam
goal_vacuity = _ls.goal_vacuity
parse_obligations = _ls.parse_obligations
obligation_gap = _ls.obligation_gap
read_obligations = _mod.read_obligations


def test_reason_axes_parses_the_pass_grammar():
    axes = _reason_axes("ch7 PASS status=committed rubric=pass mdlen=1200 quality=unset")
    assert axes == {
        "status": "committed", "rubric": "pass",
        "mdlen": "1200", "quality": "unset",
    }


def test_reason_axes_parses_the_fail_grammar_and_drops_the_terminal_err():
    """``err=`` is free text and may hold spaces — it must not become an axis."""
    reason = (
        "ch4 FAIL status=failed rubric=none committed=f permfail=false "
        "degraded=0 attempts=7 mdlen=0 err=contract build failed: H2 too long"
    )
    axes = _reason_axes(reason)
    assert axes["permfail"] == "false"
    assert axes["degraded"] == "0"
    assert axes["attempts"] == "7"
    assert "err" not in axes
    # The trailing sentence is swallowed with the err field, not read as axes.
    assert "too" not in axes


def test_reason_axes_skips_hyphenated_suffix_flags():
    """``rubric-healed=true`` is a flag, not an ``axis=value`` pair."""
    axes = _reason_axes("ch7 PASS status=committed quality=pass rubric-healed=true")
    assert "rubric-healed" not in axes
    assert "healed" not in axes


def test_reason_axes_ignores_nested_annotations():
    """The ``[...]`` suffix is a sub-annotation, not part of the axis grammar.

    Live PASS reasons carry a quality probe detail that ends in
    ``[figure-loss attached=8 live=0 cascade=8 attempts=0]``. Those keys belong
    to one chapter's figure history; reading them as axes made a uniform
    ``live=0`` masquerade as the axis the green was vacuous with respect to.
    """
    reason = (
        "ch1 PASS status=completed rubric=pass mdlen=24101 "
        "quality=ch1 QUALITY-OK sections=4 total=23090 headings=4 "
        "[figure-loss attached=8 live=0 cascade=8 attempts=0]"
    )
    axes = _reason_axes(reason)
    assert axes == {"status": "completed", "rubric": "pass", "mdlen": "24101",
                    "quality": "ch1"}


def test_reason_axes_on_garbage_is_empty():
    assert _reason_axes("") == {}
    assert _reason_axes("no axes at all here") == {}


def test_goal_vacuity_names_the_dead_axis():
    state = {"waves": [{"failing_after": []}]}
    reasons = {"7": "ch7 PASS status=committed rubric=pass mdlen=0 quality=unset"}
    assert goal_vacuity(state, reasons) == "vacuous_goal_met:mdlen+quality"


def test_goal_vacuity_is_silent_when_every_axis_moved():
    state = {"waves": [{"failing_after": []}]}
    reasons = {"7": "ch7 PASS status=committed rubric=pass mdlen=1200 quality=pass"}
    assert goal_vacuity(state, reasons) is None


def test_goal_vacuity_ignores_a_still_failing_wave():
    """Vacuity is a statement about a PASS; a failing wave is not one."""
    state = {"waves": [{"failing_after": ["7"]}]}
    reasons = {"7": "ch7 PASS status=committed quality=unset"}
    assert goal_vacuity(state, reasons) is None


def test_goal_vacuity_without_reasons_cannot_diagnose():
    assert goal_vacuity({"waves": [{"failing_after": []}]}, None) is None
    assert goal_vacuity({"waves": [{"failing_after": []}]}, {}) is None


def test_goal_vacuity_refuses_to_guess_from_an_unparseable_reason():
    """No axes parsed ⇒ cannot diagnose, which is not the same as 'no problem'."""
    state = {"waves": [{"failing_after": []}]}
    assert goal_vacuity(state, {"7": "something went sideways"}) is None


def test_parse_obligations_reads_rows_and_drops_malformed_ones():
    text = (
        "# a comment\n"
        "\n"
        "figure_requirement|10|0|chapters with a live viz_image forest\n"
        "rubric_axis_coverage|16|0|axes the predicate never reads\n"
        "garbage-with-no-pipes\n"
        "no_counts|many|few|not integers\n"
    )
    rows = parse_obligations(text)
    assert [r["name"] for r in rows] == ["figure_requirement", "rubric_axis_coverage"]
    assert rows[0]["declared"] == 10 and rows[0]["satisfied"] == 0
    assert rows[0]["detail"].startswith("chapters with a live")


def test_obligation_gap_follows_declaration_order_not_gap_size():
    """Counts are incommensurable; the operator's ordering is the priority."""
    rows = parse_obligations(
        "figure_requirement|10|0|first\n"
        "rubric_axis_coverage|16|0|second and numerically wider\n"
    )
    assert obligation_gap(rows) == "obligation_gap:figure_requirement:0/10"


def test_obligation_gap_skips_satisfied_rows():
    rows = parse_obligations(
        "figure_requirement|10|10|met\n"
        "rubric_axis_coverage|16|2|owed\n"
    )
    assert obligation_gap(rows) == "obligation_gap:rubric_axis_coverage:2/16"


def test_obligation_gap_is_none_when_nothing_is_owed():
    assert obligation_gap(None) is None
    assert obligation_gap(parse_obligations("figure_requirement|10|10|met")) is None


def test_read_obligations_returns_stdout_on_success(tmp_path):
    out, err = read_obligations(str(tmp_path), "echo 'figure_requirement|10|0|d'")
    assert err == ""
    assert "figure_requirement|10|0|d" in out


def test_read_obligations_reports_a_failing_sensor_as_an_error(tmp_path):
    """A configured sensor that breaks must never read as 'no obligations'."""
    out, err = read_obligations(str(tmp_path), "exit 3")
    assert out == ""
    assert "obligation sensor failed" in err and "rc=3" in err


# ── the unmeasured wave (pure) ─────────────────────────────────────────────
#
# A wave whose verdict never arrived records ``verdict_known: False``: it timed
# out, crashed, or its verifier emitted an ``error`` panel. Its failing set is
# UNOBSERVED, which is not the same as empty — and only this module can tell the
# two apart, because by the time the driver folds a wave both look like ``[]``.

_measured_tail = _ls.measured_tail  # noqa: SLF001 — test seam
_record_wave = _ls.record_wave
_divergence = _ls.divergence
_should_quarantine = _ls.should_quarantine


def test_measured_tail_breaks_at_an_unobserved_wave():
    """[known, unknown, known] — only the last measured wave survives. The cut
    is a BREAK, not a filter: divergence is ``patience`` CONSECUTIVE waves, so
    an unobserved wave between two measured ones means the pattern was never
    sustained across the window."""
    waves = [
        {"wave": 1, "verdict_known": True, "failing_after": ["a"]},
        {"wave": 2, "verdict_known": False, "failing_after": []},
        {"wave": 3, "verdict_known": True, "failing_after": ["a"]},
    ]
    assert [w["wave"] for w in _measured_tail(waves)] == [3]


def test_measured_tail_keeps_a_trailing_run_of_measured_waves():
    waves = [
        {"wave": 1, "verdict_known": False, "failing_after": []},
        {"wave": 2, "verdict_known": True, "failing_after": ["a"]},
        {"wave": 3, "verdict_known": True, "failing_after": ["a"]},
    ]
    assert [w["wave"] for w in _measured_tail(waves)] == [2, 3]


def test_measured_tail_defaults_an_absent_flag_to_known():
    """Every wave written before this change has no flag. Absent must read as
    measured, or a resumed state file would silently stop diverging."""
    waves = [{"wave": 1, "failing_after": ["a"]}, {"wave": 2, "failing_after": ["a"]}]
    assert len(_measured_tail(waves)) == 2


def test_divergence_is_silent_across_an_unobserved_wave():
    """Two identical measured waves separated by a dead one must NOT report
    ``no_progress`` — the loop cannot say the stall is real."""
    known = {"wave": 1, "verdict_known": True, "failing_after": ["a"], "signature": "s"}
    dead = {"wave": 2, "verdict_known": False, "failing_after": [], "signature": None}
    later = {"wave": 3, "verdict_known": True, "failing_after": ["a"], "signature": "s"}
    assert _divergence({"waves": [known, dead, later]}, patience=2) is None
    # Nothing between them, and the same two waves really do diverge.
    assert _divergence({"waves": [known, later]}, patience=2) == "no_progress:s"


def test_record_wave_of_an_unmeasured_wave_records_no_signature():
    """``sha256([])`` is exactly what a measured all-green wave hashes to. A
    wave that observed nothing must not wear that signature — nor report
    headroom it never saw move."""
    state = {"goal_id": "g", "waves": [], "failed_fixes": {}}
    _record_wave(state, wave=1, run_id="r", failing_before=["u"], failing_after=[],
                 cost_usd=0.0, attempted=["u"],
                 diagnostics={"evidence": {"u": "deadbeef"}}, verdict_known=False)
    w = state["waves"][0]
    assert w["signature"] is None
    assert w["verdict_known"] is False
    assert w["headroom_closed"] is None
    assert w["predicate_moved"] is None


def test_record_wave_of_an_unmeasured_wave_accrues_no_fix_hash():
    """A timeout is not 'attempted twice, same failure'. Quarantining a unit the
    loop never scored would retire it on a wave that produced no evidence."""
    state = {"goal_id": "g", "waves": [], "failed_fixes": {}}
    for wave in (1, 2):
        _record_wave(state, wave=wave, run_id="r", failing_before=["u"],
                     failing_after=["u"], cost_usd=0.0, attempted=["u"],
                     verdict_known=False)
    assert state["failed_fixes"] == {}
    assert _should_quarantine("u", _ls.fix_hash("u", None), state) is False


def test_an_unmeasured_wave_is_not_a_green_for_vacuity():
    """The vacuity diagnostic looks for a green whose axes never moved. A dead
    wave is not a green, so it must not be read as one."""
    state = {"waves": [{"wave": 1, "verdict_known": False,
                        "failing_after": [], "signature": None}]}
    assert goal_vacuity(state) is None
