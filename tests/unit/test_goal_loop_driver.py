"""Unit tests for the U4b goal-loop cross-wave driver.

Six scenarios from kickoff §Goal ¶3 (U4b DoD):

1. goal_met on wave 2 → stop goal_met, state file has 2 waves
2. same failure signature two waves running → stop diverged
3. regressing failing count two waves running → stop diverged
4. budget: wave costs 5.0 + 5.0 with budget 12 → stop budget before wave 3
5. quarantine: unit failing with identical hash across 2 waves is excluded
   from wave 3 hunt set and listed in final verdict quarantined_units
6. MO_GOAL_SPAWN_DRY=1 sweep submode records planned spawns without
   calling spawn

Zero LLM, zero subprocess to bin/mini-ork. All fakes are deterministic.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

# File-path import pattern (recipes/ is not a Python package).
RECIPE_DIR = Path(__file__).resolve().parents[2] / "recipes" / "goal-loop"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


_DRIVE = _load("goal_loop_drive", RECIPE_DIR / "lib" / "drive.py")
_LOOP_STATE = _load("goal_loop_loop_state", RECIPE_DIR / "lib" / "loop_state.py")

drive = _DRIVE.drive
sweep_run = _DRIVE.sweep_run
record_wave = _LOOP_STATE.record_wave
load_state = _LOOP_STATE.load_state
save_state = _LOOP_STATE.save_state
should_quarantine = _LOOP_STATE.should_quarantine


# ── 1. goal_met on wave 2 ──────────────────────────────────────────────────


def test_goal_met_on_wave_two(tmp_path):
    state_dir = tmp_path / "state"
    waves_run: list[int] = []

    def run_wave(wave_no, quarantined):
        waves_run.append(wave_no)
        if wave_no == 1:
            return {"verdict": "fail", "failing_before": ["u1"], "failing_after": ["u1"],
                    "cost_usd": 1.0, "run_id": f"r{wave_no}"}
        return {"verdict": "pass", "failing_before": [], "failing_after": [],
                "cost_usd": 1.0, "run_id": f"r{wave_no}"}

    verdict = drive(
        goal_id="g1",
        target_cwd="/tmp",
        units_cmd="echo u1",
        predicate_cmd="echo ok",
        child_recipe="code-fix",
        max_waves=10,
        budget_total_usd=100.0,
        run_wave_fn=run_wave,
        cost_fn=lambda: 0.0,
        state_dir=state_dir,
    )

    assert verdict["stop"] == "goal_met"
    assert verdict["waves"] == 2
    assert waves_run == [1, 2]
    persisted = load_state(state_dir, "g1")
    assert len(persisted["waves"]) == 2
    final = json.loads((state_dir / "final-verdict.json").read_text())
    assert final["stop"] == "goal_met"


# ── 2. diverged: same failure signature two waves ────────────────────────


def test_diverged_same_signature_two_waves(tmp_path):
    state_dir = tmp_path / "state"

    def run_wave(wave_no, quarantined):
        # Same failing set every wave → identical signature → divergence.
        return {"verdict": "fail", "failing_before": ["u1"], "failing_after": ["u1"],
                "cost_usd": 1.0, "run_id": f"r{wave_no}"}

    verdict = drive(
        goal_id="g2", target_cwd="/tmp", units_cmd="echo u1",
        predicate_cmd="echo ok", child_recipe="code-fix",
        max_waves=10, budget_total_usd=100.0,
        run_wave_fn=run_wave, cost_fn=lambda: 0.0, state_dir=state_dir,
    )

    assert verdict["stop"] == "diverged"
    assert verdict["waves"] == 2
    assert verdict["signature"].startswith("no_progress:")
    assert (state_dir / "final-verdict.json").is_file()


# ── 3. diverged: regressing failing count two waves ──────────────────────


def test_diverged_regressing_two_waves(tmp_path):
    state_dir = tmp_path / "state"
    wave_seq = [{"failing_after": ["u1"]}, {"failing_after": ["u1", "u2"]},
                {"failing_after": ["u1", "u2", "u3"]}]

    def run_wave(wave_no, quarantined):
        idx = wave_no - 1
        return {"verdict": "fail", "failing_before": [],
                "failing_after": wave_seq[idx]["failing_after"],
                "cost_usd": 1.0, "run_id": f"r{wave_no}"}

    verdict = drive(
        goal_id="g3", target_cwd="/tmp", units_cmd="echo u",
        predicate_cmd="echo ok", child_recipe="code-fix",
        max_waves=10, budget_total_usd=100.0,
        run_wave_fn=run_wave, cost_fn=lambda: 0.0, state_dir=state_dir,
    )

    assert verdict["stop"] == "diverged"
    assert verdict["waves"] == 2
    assert verdict["signature"].startswith("regressing:")


# ── 4. budget: wave costs 5.0 + 5.0 with budget 12 → stop before wave 3 ──


def test_budget_stops_before_wave_three(tmp_path):
    state_dir = tmp_path / "state"
    waves_run: list[int] = []

    def run_wave(wave_no, quarantined):
        waves_run.append(wave_no)
        return {"verdict": "fail", "failing_before": ["u1"], "failing_after": ["u1"],
                "cost_usd": 5.0, "run_id": f"r{wave_no}"}

    # cost_fn returns cumulative spend; 10.0 after 2 waves + projected 5.0 = 15 > 12.
    verdict = drive(
        goal_id="g4", target_cwd="/tmp", units_cmd="echo u1",
        predicate_cmd="echo ok", child_recipe="code-fix",
        max_waves=10, budget_total_usd=12.0,
        run_wave_fn=run_wave, cost_fn=lambda: 10.0, state_dir=state_dir,
    )

    assert verdict["stop"] == "budget"
    # The pre-wave-budget check should fire after wave 2 when projected
    # (mean of 5+5 = 5) plus spent (10) = 15 > 12. So waves_run == [1, 2]
    # and the 3rd wave never runs.
    assert waves_run == [1, 2]
    assert verdict["waves"] == 2
    final = json.loads((state_dir / "final-verdict.json").read_text())
    assert final["stop"] == "budget"


# ── 5. quarantine: identical-hash unit excluded after 2 waves ─────────────


def test_quarantine_excludes_unit_after_two_identical_hashes(tmp_path):
    """Wave 3's hunt set MUST exclude a unit whose failure hash appeared
    twice in a row (kickoff §Goal ¶1).

    Sequence:
      wave 1 — both u1 and u2 fail
      wave 2 — u1 still fails (same hash → quarantine candidate), u2 passes
                (signature changes → no divergence)
      wave 3 — u1 is in ``quarantined``; u2 still passes → goal_met
    """
    state_dir = tmp_path / "state"
    quarantined_seen: list[set[str]] = []

    def run_wave(wave_no, quarantined):
        quarantined_seen.append(set(quarantined))
        if wave_no == 1:
            return {"verdict": "fail", "failing_after": ["u1", "u2"],
                    "cost_usd": 0.0, "run_id": "r1"}
        if wave_no == 2:
            return {"verdict": "fail", "failing_after": ["u1"],
                    "cost_usd": 0.0, "run_id": "r2"}
        # wave 3 — u1 quarantined; only u2 hunted (passes) → goal_met.
        return {"verdict": "pass", "failing_after": [],
                "cost_usd": 0.0, "run_id": "r3"}

    verdict = drive(
        goal_id="g5", target_cwd="/tmp", units_cmd="echo u",
        predicate_cmd="echo ok", child_recipe="code-fix",
        max_waves=10, budget_total_usd=100.0,
        run_wave_fn=run_wave, cost_fn=lambda: 0.0, state_dir=state_dir,
    )

    assert len(quarantined_seen) >= 3
    assert quarantined_seen[0] == set()
    assert quarantined_seen[1] == set()
    assert quarantined_seen[2] == {"u1"}, quarantined_seen
    assert verdict["stop"] == "goal_met"
    assert verdict["quarantined_units"] == ["u1"]


# ── 6. MO_GOAL_SPAWN_DRY=1 sweep submode records planned spawns ───────────


def test_sweep_dry_run_records_planned_spawns(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    plan = [
        {"unit_id": "u1", "child_recipe": "code-fix", "kickoff_hint": {"unit_id": "u1"}},
        {"unit_id": "u2", "child_recipe": "code-fix", "kickoff_hint": {"unit_id": "u2"}},
    ]
    (run_dir / "sweep-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_GOAL_SPAWN_DRY", "1")

    rc = sweep_run(spawn_fn=lambda e: {"status": "spawned", "unit_id": e["unit_id"]})

    assert rc == 0
    result = json.loads((run_dir / "sweep-result.json").read_text())
    assert result["status"] == "fanned_out"
    assert result["dry_run"] is True
    assert {u["unit_id"] for u in result["units"]} == {"u1", "u2"}


def test_sweep_dry_run_uses_default_spawn_fn_under_env(monkeypatch, tmp_path):
    """With MO_GOAL_SPAWN_DRY=1, the default spawn_fn records without invoking spawn."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    plan = [{"unit_id": "u1", "child_recipe": "code-fix"}]
    (run_dir / "sweep-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_GOAL_SPAWN_DRY", "1")

    # Default spawn_fn is injected as the _default_spawn_fn closure. Pass
    # it explicitly so we don't have to import private names from drive.py.
    default = _DRIVE._default_spawn_fn  # noqa: SLF001 — test seam
    rc = sweep_run(spawn_fn=default)
    assert rc == 0
    result = json.loads((run_dir / "sweep-result.json").read_text())
    assert result["dry_run"] is True
    assert result["units"][0]["status"] == "dry_run"


def test_sweep_run_deferred_on_value_error(tmp_path, monkeypatch):
    """A spawn_fn that raises ValueError marks the unit deferred."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    plan = [{"unit_id": "u1", "child_recipe": "code-fix"}]
    (run_dir / "sweep-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))

    def fail_spawn(entry):
        raise ValueError("cap hit: parallel limit")

    rc = sweep_run(spawn_fn=fail_spawn)
    assert rc == 0
    result = json.loads((run_dir / "sweep-result.json").read_text())
    assert result["units"][0]["status"] == "deferred"
    assert "cap hit" in result["units"][0]["reason"]


# ── 7-9. U4c per-unit kickoff templating ────────────────────────────────


def _capture_spawn(monkeypatch):
    """Patch ``mini_ork.cli.spawn.spawn`` and return the captured-kwargs dict.

    ``_default_spawn_fn`` reaches the spawn via a lazy import
    (``from mini_ork.cli.spawn import spawn`` inside the function body, so the
    ``spawn`` symbol is rebound on every call). Monkeypatching
    ``mini_ork.cli.spawn.spawn`` is what the lazy import will pick up next.
    """
    captured: dict = {}

    def fake_spawn(*args, **kwargs):
        captured.clear()
        captured.update(kwargs)
        return SimpleNamespace(exit_code=0, spawn_id="fake-id", child_run_id=None)

    import mini_ork.cli.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", fake_spawn)
    return captured


def test_default_spawn_fn_templating_substitutes_unit_and_reason(tmp_path, monkeypatch):
    """File template with {{unit_id}} and {{reason}} → materialized per-unit.

    The spawned kickoff path lives under the run dir, its filename has NO
    ``/`` from the unit id (sanitized), and its body carries both fields with
    no ``{{`` remaining.
    """
    template = tmp_path / "template.md"
    template.write_text(
        "# Fix {{unit_id}}\n\nReason: {{reason}}\n", encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_GOAL_CHILD_KICKOFF", str(template))
    monkeypatch.setenv("MINI_ORK_ALLOW_CHILD_SPAWN", "1")
    monkeypatch.setenv("MO_GOAL_NO_EXECUTE", "0")

    captured = _capture_spawn(monkeypatch)
    default = _DRIVE._default_spawn_fn  # noqa: SLF001 — test seam

    result = default({
        "unit_id": "docs/ch-01.md",
        "child_recipe": "code-fix",
        "kickoff_hint": {"unit_id": "docs/ch-01.md", "reason": "lens 04 failed"},
    })

    assert result["status"] == "spawned"
    kickoff_path = captured["kickoff"]
    assert kickoff_path.startswith(str(run_dir)), kickoff_path
    assert "/" not in Path(kickoff_path).name
    body = Path(kickoff_path).read_text(encoding="utf-8")
    assert "docs/ch-01.md" in body
    assert "lens 04 failed" in body
    assert "{{" not in body


def test_default_spawn_fn_passthrough_for_template_without_placeholders(tmp_path, monkeypatch):
    """Template file with NO placeholders → spawn receives the ORIGINAL path."""
    template = tmp_path / "static_template.md"
    template.write_text("# Static kickoff\nNo placeholders here.\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_GOAL_CHILD_KICKOFF", str(template))
    monkeypatch.setenv("MINI_ORK_ALLOW_CHILD_SPAWN", "1")

    captured = _capture_spawn(monkeypatch)
    default = _DRIVE._default_spawn_fn  # noqa: SLF001 — test seam

    result = default({
        "unit_id": "docs/ch-02.md",
        "child_recipe": "code-fix",
        "kickoff_hint": {"reason": "ignored"},
    })

    assert result["status"] == "spawned"
    # ORIGINAL template path passes through unchanged.
    assert captured["kickoff"] == str(template)
    # No materialized file is written under the run dir.
    assert list(run_dir.glob("_inline_kickoff_*")) == []


def test_default_spawn_fn_templating_for_inline_body(tmp_path, monkeypatch):
    """Inline (non-file) MO_GOAL_CHILD_KICKOFF body with placeholders → file written."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    # Inline body (NOT a file path) with placeholders.
    monkeypatch.setenv(
        "MO_GOAL_CHILD_KICKOFF",
        "# Fix {{unit_id}}\nreason: {{reason}}\n",
    )
    monkeypatch.setenv("MINI_ORK_ALLOW_CHILD_SPAWN", "1")

    captured = _capture_spawn(monkeypatch)
    default = _DRIVE._default_spawn_fn  # noqa: SLF001 — test seam

    result = default({
        "unit_id": "src/api.py",
        "child_recipe": "code-fix",
        "kickoff_hint": {"reason": "lens 07 timeout"},
    })

    assert result["status"] == "spawned"
    kickoff_path = captured["kickoff"]
    assert kickoff_path.startswith(str(run_dir)), kickoff_path
    assert "/" not in Path(kickoff_path).name
    body = Path(kickoff_path).read_text(encoding="utf-8")
    assert "src/api.py" in body
    assert "lens 07 timeout" in body
    assert "{{" not in body