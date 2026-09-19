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
import os
import sys

import pytest
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
divergence = _LOOP_STATE.divergence
wave_signature = _LOOP_STATE.wave_signature
_reason_fingerprint = _LOOP_STATE._reason_fingerprint  # noqa: SLF001 — test seam
_default_run_wave_fn = _DRIVE._default_run_wave_fn  # noqa: SLF001 — test seam


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


def test_default_run_wave_exports_quarantine_to_recipe(tmp_path, monkeypatch):
    """The driver must hand its GRAO quarantine set to the wave recipe via
    MO_GOAL_QUARANTINED_UNITS (newline-sorted) so goal_sweep_plan can rotate
    the freed child slot off the stuck unit. Without this the single-child
    loop re-selects the same failing unit every wave."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_GOAL_WAVE_KICKOFF", str(tmp_path / "wave.md"))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))

    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_DRIVE.subprocess, "run", fake_run)

    payload = _default_run_wave_fn(3, {"2", "1", "10"})

    env = captured["env"]
    assert isinstance(env, dict)
    # sorted, newline-delimited — the exact contract goal_sweep_plan parses.
    assert env["MO_GOAL_QUARANTINED_UNITS"] == "1\n10\n2"
    # and the wave payload still reports the same set for the driver's records.
    assert payload["quarantined"] == ["1", "10", "2"]


def test_default_run_wave_records_real_cost_delta(tmp_path, monkeypatch):
    """A wave's cost_usd must be the 24h-meter delta across the wave subprocess,
    NOT panel-verdict.json's panel-node cost (~$0). A $0 reading blinds the
    autoraise predictor: len(funded) never reaches patience, so it keeps raising
    the rail forever instead of stopping on funded-but-flat progress."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_GOAL_WAVE_KICKOFF", str(tmp_path / "wave.md"))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    # panel-verdict.json carries only the panel node's own (wrong) cost.
    (run_dir / "panel-verdict.json").write_text(
        '{"verdict": "fail", "failing_units": ["1"], "cost_usd": 0.0}',
        encoding="utf-8",
    )
    readings = iter([100.0, 108.0])  # before, after → a real $8 funded wave
    monkeypatch.setattr(_DRIVE, "_default_cost_fn", lambda: next(readings))
    monkeypatch.setattr(
        _DRIVE.subprocess, "run",
        lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    payload = _default_run_wave_fn(1, set())

    assert payload["cost_usd"] == 8.0  # the measured delta, not the panel's 0.0


def test_default_run_wave_cost_delta_clamps_nonpositive(tmp_path, monkeypatch):
    """A non-positive meter delta (starved wave, or the 24h window sliding faster
    than the wave spent) records as $0 — 'not funded' — never a negative that
    would corrupt the budget projection or falsely fund the predictor."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_GOAL_WAVE_KICKOFF", str(tmp_path / "wave.md"))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    readings = iter([50.0, 49.5])  # window slid; raw delta is negative
    monkeypatch.setattr(_DRIVE, "_default_cost_fn", lambda: next(readings))
    monkeypatch.setattr(
        _DRIVE.subprocess, "run",
        lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    payload = _default_run_wave_fn(1, set())

    assert payload["cost_usd"] == 0.0


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


def test_default_spawn_fn_templating_substitutes_evidence(tmp_path, monkeypatch):
    """{{evidence}} + {{evidence_path}} inline the deep signal from the sweep plan.

    This is the payload the evidence seam exists to deliver: the fix child sees
    the produced-vs-required delta instead of an 80-char opaque last_error.
    """
    template = tmp_path / "template.md"
    template.write_text(
        "# Fix {{unit_id}}\n\nreason: {{reason}}\n\n"
        "## Evidence\n{{evidence}}\n\nfull dump: {{evidence_path}}\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_GOAL_CHILD_KICKOFF", str(template))
    monkeypatch.setenv("MINI_ORK_ALLOW_CHILD_SPAWN", "1")

    captured = _capture_spawn(monkeypatch)
    default = _DRIVE._default_spawn_fn  # noqa: SLF001 — test seam

    deep = "produced ## Section scaffold vs required ## H2 outline / ## Per-section intent"
    result = default({
        "unit_id": "3",
        "child_recipe": "code-fix",
        "kickoff_hint": {
            "unit_id": "3",
            "reason": "ch3 FAIL status=failed err=...guard...",
            "evidence": deep,
            "evidence_path": "/tmp/run/evidence/3.md",
        },
    })

    assert result["status"] == "spawned"
    body = Path(captured["kickoff"]).read_text(encoding="utf-8")
    assert deep in body
    assert "/tmp/run/evidence/3.md" in body
    assert "{{" not in body


def test_default_spawn_fn_evidence_falls_back_to_reason(tmp_path, monkeypatch):
    """A kickoff that references {{evidence}} renders the reason when none harvested.

    Guarantees an unarmed loop (no MO_GOAL_EVIDENCE_CMD ⇒ empty hint.evidence)
    still produces an actionable kickoff rather than a hollow ``## Evidence``
    heading followed by nothing.
    """
    template = tmp_path / "template.md"
    template.write_text(
        "# Fix {{unit_id}}\n\n## Evidence\n{{evidence}}\n", encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_GOAL_CHILD_KICKOFF", str(template))
    monkeypatch.setenv("MINI_ORK_ALLOW_CHILD_SPAWN", "1")

    captured = _capture_spawn(monkeypatch)
    default = _DRIVE._default_spawn_fn  # noqa: SLF001 — test seam

    result = default({
        "unit_id": "4",
        "child_recipe": "code-fix",
        # No 'evidence' key at all — the unarmed path.
        "kickoff_hint": {"unit_id": "4", "reason": "ch4 FAIL status=degraded"},
    })

    assert result["status"] == "spawned"
    body = Path(captured["kickoff"]).read_text(encoding="utf-8")
    assert "ch4 FAIL status=degraded" in body  # reason filled the {{evidence}} slot
    assert "{{evidence}}" not in body


# ── 10-12. U4d wave-kickoff env resolution ────────────────────────────────


def _capture_run_wave(monkeypatch):
    """Patch ``subprocess.run`` at the drive module and return captured argv.

    ``_default_run_wave_fn`` does a top-level ``import subprocess`` (drive.py:30)
    and reads ``subprocess.run`` at call time, so monkeypatching the bound
    attribute on the loaded module redirects the call. Pytest restores the
    real ``subprocess.run`` on teardown.
    """
    captured: dict = {}

    def fake_run(argv, *args, **kwargs):
        captured.clear()
        captured["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_DRIVE.subprocess, "run", fake_run)
    return captured


def test_default_run_wave_fn_prefers_wave_kickoff(tmp_path, monkeypatch):
    """When BOTH env vars are set, the WAVE kickoff is the one shelled to ``bin/mini-ork``."""
    wave_kickoff = tmp_path / "wave.md"
    wave_kickoff.write_text("# WAVE\n", encoding="utf-8")
    child_kickoff = tmp_path / "child.md"
    child_kickoff.write_text("# CHILD\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_GOAL_WAVE_KICKOFF", str(wave_kickoff))
    monkeypatch.setenv("MO_GOAL_CHILD_KICKOFF", str(child_kickoff))

    captured = _capture_run_wave(monkeypatch)
    default = _DRIVE._default_run_wave_fn  # noqa: SLF001 — test seam

    payload = default(1, set())

    argv = captured["argv"]
    assert argv[1:3] == ["run", "goal-loop"]
    assert argv[3] == str(wave_kickoff)
    assert argv[3] != str(child_kickoff)
    # Panel-verdict.json absent → payload keeps verdict='fail' (kickoff ¶60).
    assert payload["verdict"] == "fail"
    assert payload["wave"] == 1
    assert payload["quarantined"] == []


def test_default_run_wave_fn_falls_back_to_child_kickoff(tmp_path, monkeypatch):
    """With only ``MO_GOAL_CHILD_KICKOFF`` set, the CHILD kickoff is shelled (backward compat)."""
    child_kickoff = tmp_path / "child.md"
    child_kickoff.write_text("# CHILD\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.delenv("MO_GOAL_WAVE_KICKOFF", raising=False)
    monkeypatch.setenv("MO_GOAL_CHILD_KICKOFF", str(child_kickoff))

    captured = _capture_run_wave(monkeypatch)
    default = _DRIVE._default_run_wave_fn  # noqa: SLF001 — test seam

    payload = default(1, set())

    argv = captured["argv"]
    assert argv[1:3] == ["run", "goal-loop"]
    assert argv[3] == str(child_kickoff)
    assert payload["verdict"] == "fail"


def test_default_run_wave_fn_raises_when_both_kickoffs_unset(tmp_path, monkeypatch):
    """Neither var set → ``RuntimeError`` naming BOTH env vars."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.delenv("MO_GOAL_WAVE_KICKOFF", raising=False)
    monkeypatch.delenv("MO_GOAL_CHILD_KICKOFF", raising=False)

    default = _DRIVE._default_run_wave_fn  # noqa: SLF001 — test seam

    with pytest.raises(RuntimeError) as exc_info:
        default(1, set())
    msg = str(exc_info.value)
    assert "MO_GOAL_WAVE_KICKOFF" in msg
    assert "MO_GOAL_CHILD_KICKOFF" in msg


# ── 7. the declared recursion block governs the driver ─────────────────────
#
# The executor publishes a recipe's `recursion:` block as MO_RECURSION_*
# (mini_ork/cli/execute.py). These tests pin the consumption side: caller beats
# declared beats literal, and a malformed value fails loud rather than quietly
# reverting to a default the recipe never declared.


def test_drive_reads_declared_budget_from_env(tmp_path, monkeypatch):
    """No kwarg + env set ⇒ the declared total budget is the one enforced.

    Mirrors scenario 4 (budget stop after wave 2) with the 12.0 arriving through
    the environment instead of the argument, which is the path a recipe takes.
    """
    state_dir = tmp_path / "state"

    def run_wave(wave_no, quarantined):
        return {"verdict": "fail", "failing_before": ["u1"], "failing_after": ["u1"],
                "cost_usd": 5.0, "run_id": f"r{wave_no}"}

    monkeypatch.setenv("MO_RECURSION_BUDGET_CAP_TOTAL_USD", "12.0")
    monkeypatch.delenv("MO_RECURSION_MAX_ITERATIONS", raising=False)

    verdict = drive(
        goal_id="g7", target_cwd="/tmp", units_cmd="echo u1",
        predicate_cmd="echo ok", child_recipe="code-fix",
        run_wave_fn=run_wave, cost_fn=lambda: 10.0, state_dir=state_dir,
    )

    assert verdict["stop"] == "budget"
    assert verdict["budget_total_usd"] == 12.0


def test_drive_reads_declared_max_iterations_from_env(tmp_path, monkeypatch):
    """max_waves cap comes from the declared max_iterations when no kwarg is given."""
    state_dir = tmp_path / "state"
    waves_run: list[int] = []

    def run_wave(wave_no, quarantined):
        waves_run.append(wave_no)
        # Never converges, never diverges (distinct failing sets each wave) —
        # so the only thing that can stop the loop is the wave cap.
        return {"verdict": "fail", "failing_before": [],
                "failing_after": [f"u{wave_no}"],
                "cost_usd": 0.0, "run_id": f"r{wave_no}"}

    monkeypatch.setenv("MO_RECURSION_MAX_ITERATIONS", "3")
    monkeypatch.delenv("MO_RECURSION_BUDGET_CAP_TOTAL_USD", raising=False)

    verdict = drive(
        goal_id="g8", target_cwd="/tmp", units_cmd="echo u",
        predicate_cmd="echo ok", child_recipe="code-fix",
        run_wave_fn=run_wave, cost_fn=lambda: 0.0, state_dir=state_dir,
    )

    assert verdict["stop"] == "max_waves_reached"
    assert verdict["waves"] == 3
    assert waves_run == [1, 2, 3]


def test_explicit_argument_beats_declared_env(tmp_path, monkeypatch):
    """An explicit caller value wins over the recipe's declaration."""
    state_dir = tmp_path / "state"
    waves_run: list[int] = []

    def run_wave(wave_no, quarantined):
        waves_run.append(wave_no)
        return {"verdict": "fail", "failing_before": [],
                "failing_after": [f"u{wave_no}"],
                "cost_usd": 0.0, "run_id": f"r{wave_no}"}

    monkeypatch.setenv("MO_RECURSION_MAX_ITERATIONS", "2")

    verdict = drive(
        goal_id="g9", target_cwd="/tmp", units_cmd="echo u",
        predicate_cmd="echo ok", child_recipe="code-fix",
        max_waves=4, budget_total_usd=100.0,
        run_wave_fn=run_wave, cost_fn=lambda: 0.0, state_dir=state_dir,
    )

    assert verdict["stop"] == "max_waves_reached"
    assert waves_run == [1, 2, 3, 4]


def test_absent_env_keeps_historical_defaults(monkeypatch):
    """Nothing declared, nothing passed ⇒ today's literals, not zero and not a crash.

    Asserted at the seam ``drive()`` itself uses, because observing 30 waves
    would mean running a 30-wave loop to prove a default.
    """
    monkeypatch.delenv("MO_RECURSION_MAX_ITERATIONS", raising=False)
    monkeypatch.delenv("MO_RECURSION_BUDGET_CAP_TOTAL_USD", raising=False)

    assert _DRIVE._env_int("MO_RECURSION_MAX_ITERATIONS", 30) == 30  # noqa: SLF001
    assert _DRIVE._env_float("MO_RECURSION_BUDGET_CAP_TOTAL_USD", 150.0) == 150.0  # noqa: SLF001


def test_empty_env_value_is_treated_as_absent(monkeypatch):
    """An exported-but-blank var is absence, not a parse error."""
    monkeypatch.setenv("MO_RECURSION_MAX_ITERATIONS", "")
    assert _DRIVE._env_int("MO_RECURSION_MAX_ITERATIONS", 30) == 30  # noqa: SLF001


def test_malformed_declared_value_fails_loud(monkeypatch):
    """Garbage in a published cap raises — a silent fallback would hide a broken
    upstream and run the loop at a bound the recipe never declared."""
    monkeypatch.setenv("MO_RECURSION_MAX_ITERATIONS", "thirty")
    with pytest.raises(ValueError, match="MO_RECURSION_MAX_ITERATIONS"):
        _DRIVE._env_int("MO_RECURSION_MAX_ITERATIONS", 30)  # noqa: SLF001

    monkeypatch.setenv("MO_RECURSION_BUDGET_CAP_TOTAL_USD", "lots")
    with pytest.raises(ValueError, match="MO_RECURSION_BUDGET_CAP_TOTAL_USD"):
        _DRIVE._env_float("MO_RECURSION_BUDGET_CAP_TOTAL_USD", 150.0)  # noqa: SLF001


def test_cli_max_waves_flag_reaches_the_driver():
    """``--max-waves`` is threaded to drive(), not swallowed by a default.

    A zero cap trips drive()'s own guard, so this proves the flag arrived
    without running a single wave or subprocess.
    """
    rc = _DRIVE.main([
        "--goal-id", "g10", "--target-cwd", "/tmp",
        "--units-cmd", "echo u", "--predicate-cmd", "echo ok",
        "--child-recipe", "code-fix", "--max-waves", "0",
    ])
    assert rc == 2


# ── 10. per-unit fingerprint progress detection (single-child book loop) ────
#
# A loop that attempts ONE unit per wave over N failing units cannot shrink the
# failing SET until a whole unit lands, so the historical set-only signature
# read "no progress" the instant the set stopped shrinking and killed the
# campaign at wave 2. These tests pin the fix: the signature folds each unit's
# stable failure fingerprint, and both give-up detectors take a patience window.

_W9 = ("ch{n} FAIL status=failed rubric=pending committed=f permfail=f "
       "degraded=f attempts={a} mdlen=0 err=chapterInternalDagDispatch: "
       "segment node 'W9_scaffold_sections' did not complete")
_W15 = ("ch{n} FAIL status=failed rubric=pending committed=f permfail=f "
        "degraded=f attempts={a} mdlen=0 err=chapterInternalDagDispatch: "
        "segment node 'W15_fragment_authoring' did not complete")
_PENDING = ("ch{n} FAIL status=pending rubric=pending committed=f permfail=f "
            "degraded=f attempts=0 mdlen={m}")


def test_reason_fingerprint_strips_volatile_counters():
    """attempts/mdlen churn is NOT progress; status + failing node IS."""
    a = _reason_fingerprint(_W9.format(n=1, a=2))
    b = _reason_fingerprint(_W9.format(n=1, a=6))  # attempts moved 2→6
    assert a == b == "failed|node:W9_scaffold_sections"
    # failing node moves → fingerprint changes (real progress).
    assert _reason_fingerprint(_W15.format(n=1, a=2)) != a
    # a pending unit fingerprints on status alone, mdlen is dropped.
    assert (_reason_fingerprint(_PENDING.format(n=2, m=16590))
            == _reason_fingerprint(_PENDING.format(n=2, m=16591))
            == "pending|")


def test_wave_signature_reasons_distinguish_moved_failure():
    """Same failing SET but a moved per-unit failure ⇒ different signature."""
    units = ["1"]
    at_w9 = {"1": _W9.format(n=1, a=2)}
    at_w15 = {"1": _W15.format(n=1, a=3)}
    assert wave_signature(units, at_w9) != wave_signature(units, at_w15)
    # …and a bare retry-counter tick does NOT change the signature.
    assert wave_signature(units, at_w9) == wave_signature(units, {"1": _W9.format(n=1, a=9)})
    # reasons=None reproduces the historical set-only signature byte-for-byte.
    assert wave_signature(units) == wave_signature(units, None)


def test_divergence_patience_window():
    """no_progress needs ``patience`` identical signatures, not just 2."""
    state = {"waves": [{"signature": "s", "failing_after": ["1"]} for _ in range(2)]}
    assert divergence(state, patience=3) is None      # 2 repeats, window is 3
    state["waves"].append({"signature": "s", "failing_after": ["1"]})
    assert divergence(state, patience=3) == "no_progress:s"  # 3rd repeat trips
    # A single change inside the window resets it.
    state["waves"][-1]["signature"] = "t"
    assert divergence(state, patience=3) is None


def test_record_wave_scopes_hash_to_attempted():
    """Only the units the wave ATTEMPTED accrue a fix-hash sighting."""
    state = {"goal_id": "g", "waves": [], "failed_fixes": {}}
    reasons = {"1": _W9.format(n=1, a=2), "2": _PENDING.format(n=2, m=16590)}
    for _ in range(2):
        record_wave(state, wave=1, run_id="r", failing_before=[],
                    failing_after=["1", "2"], cost_usd=0.0,
                    reasons=reasons, attempted=["1"])
    # ch1 attempted twice → 2 hashes; ch2 never attempted → no key at all.
    assert len(state["failed_fixes"]["1"]) == 2
    assert "2" not in state["failed_fixes"]


def _book_wave_fn(seq):
    """Build a run_wave_fn that replays a list of (failing_after, reasons, attempted)."""
    def run_wave(wave_no, quarantined):
        failing_after, reasons, attempted = seq[wave_no - 1]
        verdict = "pass" if not failing_after else "fail"
        return {"verdict": verdict, "failing_after": failing_after,
                "failing_before": [], "unit_reasons": reasons,
                "attempted": attempted, "cost_usd": 0.0, "run_id": f"r{wave_no}"}
    return run_wave


def test_book_loop_does_not_diverge_at_wave_two(tmp_path, monkeypatch):
    """The regression guard: ch1 stuck at W9 for 2 waves must NOT kill the loop
    when patience is 3 — it used to diverge at wave 2 on the set-only signature."""
    monkeypatch.setenv("MO_GOAL_DIVERGENCE_PATIENCE", "3")
    monkeypatch.setenv("MO_GOAL_QUARANTINE_PATIENCE", "3")
    # 4 waves: ch1 stuck at W9 (attempts churn), then wave 4 lands (pass).
    seq = [
        (["1"], {"1": _W9.format(n=1, a=2)}, ["1"]),
        (["1"], {"1": _W9.format(n=1, a=6)}, ["1"]),  # attempts moved, node same
        (["1"], {"1": _W15.format(n=1, a=2)}, ["1"]),  # W9→W15: real progress
        ([], {}, ["1"]),                               # committed → goal_met
    ]
    verdict = drive(
        goal_id="gbook", target_cwd="/tmp", units_cmd="echo u",
        predicate_cmd="echo ok", child_recipe="code-fix",
        max_waves=10, budget_total_usd=1000.0,
        run_wave_fn=_book_wave_fn(seq), cost_fn=lambda: 0.0, state_dir=tmp_path / "s",
    )
    assert verdict["stop"] == "goal_met", verdict
    assert verdict["waves"] == 4


def test_book_loop_diverges_when_truly_stuck(tmp_path, monkeypatch):
    """If the attempted unit NEVER moves off W9, the loop still gives up — after
    the patience window, not before."""
    monkeypatch.setenv("MO_GOAL_DIVERGENCE_PATIENCE", "3")
    monkeypatch.setenv("MO_GOAL_QUARANTINE_PATIENCE", "9")  # keep divergence the trigger
    seq = [(["1"], {"1": _W9.format(n=1, a=a)}, ["1"]) for a in (2, 6, 2, 6)]
    verdict = drive(
        goal_id="gstuck", target_cwd="/tmp", units_cmd="echo u",
        predicate_cmd="echo ok", child_recipe="code-fix",
        max_waves=10, budget_total_usd=1000.0,
        run_wave_fn=_book_wave_fn(seq), cost_fn=lambda: 0.0, state_dir=tmp_path / "s",
    )
    assert verdict["stop"] == "diverged", verdict
    assert verdict["waves"] == 3  # fires at the 3rd identical-fingerprint wave
    assert verdict["signature"].startswith("no_progress:")


def test_pending_units_dont_trip_all_quarantined(tmp_path, monkeypatch):
    """Chapters that are failing only because the single-child loop never reached
    them must NOT be quarantined, so ``all_quarantined`` cannot fire on them."""
    monkeypatch.setenv("MO_GOAL_DIVERGENCE_PATIENCE", "99")  # don't preempt
    monkeypatch.setenv("MO_GOAL_QUARANTINE_PATIENCE", "2")
    # ch1 attempted + stuck (→ quarantined), ch2/ch3 pending + never attempted.
    reasons = {"1": _W9.format(n=1, a=2),
               "2": _PENDING.format(n=2, m=16590),
               "3": _PENDING.format(n=3, m=12000)}
    seq = [(["1", "2", "3"], reasons, ["1"]) for _ in range(4)]
    verdict = drive(
        goal_id="gpend", target_cwd="/tmp", units_cmd="echo u",
        predicate_cmd="echo ok", child_recipe="code-fix",
        max_waves=4, budget_total_usd=1000.0,
        run_wave_fn=_book_wave_fn(seq), cost_fn=lambda: 0.0, state_dir=tmp_path / "s",
    )
    assert verdict["stop"] == "max_waves_reached", verdict
    assert verdict["quarantined_units"] == ["1"]  # ONLY the attempted, stuck unit


# ── 13-17. RSI prediction-gated daily-budget autoraise ─────────────────────
#
# The wave's code-fix child enforces the GLOBAL 24h cost circuit
# (MO_DAILY_BUDGET_USD), which is SEPARATE from the driver's cumulative
# budget_total_usd. When the circuit would starve the next wave, the opt-in
# autoraise (MO_GOAL_BUDGET_AUTORAISE) lifts the daily rail — but only if
# progress is predicted. A prediction that a raise WON'T help stops the loop
# instead of burning money. The predictor counts only FUNDED waves so that a
# run of circuit-starved ($0) flat waves reads as "not tried yet" (→ raise),
# never as "stuck" (→ stop). All fakes deterministic; zero LLM/DB/subprocess.


def test_autoraise_lifts_daily_budget_when_progress_predicted(tmp_path, monkeypatch):
    """Circuit would starve the next wave AND the fixer is making progress ⇒
    the daily rail is lifted above its starting value before the wave runs."""
    monkeypatch.setenv("MO_GOAL_BUDGET_AUTORAISE", "1")
    monkeypatch.setenv("MO_DAILY_BUDGET_USD", "10")
    monkeypatch.setenv("MO_GOAL_BUDGET_AUTORAISE_CAP", "100")
    monkeypatch.setenv("MO_GOAL_DIVERGENCE_PATIENCE", "2")
    monkeypatch.setenv("MO_GOAL_QUARANTINE_PATIENCE", "9")

    seen_budget: list[float] = []
    # A moved failure (u1→u2) is real progress; then it converges.
    seq = [(["u1"], "fail"), (["u2"], "fail"), ([], "pass")]

    def run_wave(wave_no, quarantined):
        seen_budget.append(float(os.environ["MO_DAILY_BUDGET_USD"]))
        failing, verdict = seq[wave_no - 1]
        return {"verdict": verdict, "failing_before": [], "failing_after": failing,
                "cost_usd": 6.0, "run_id": f"r{wave_no}"}

    verdict = drive(
        goal_id="graise", target_cwd="/tmp", units_cmd="echo u",
        predicate_cmd="echo ok", child_recipe="code-fix",
        max_waves=10, budget_total_usd=1000.0,
        run_wave_fn=run_wave, cost_fn=lambda: 12.0, state_dir=tmp_path / "s",
    )

    assert verdict["stop"] == "goal_met", verdict
    # spent ($12) already over the $10 rail ⇒ the FIRST wave lifted it.
    assert seen_budget[0] > 10.0, seen_budget
    # the live rail carries the raise; it is never dropped back to the start.
    assert float(os.environ["MO_DAILY_BUDGET_USD"]) > 10.0


def test_autoraise_stops_when_funded_waves_flat(tmp_path, monkeypatch):
    """The fixer HAD budget (funded waves) and never moved the failure across the
    autoraise window ⇒ a further raise is predicted NOT to help ⇒ STOP, don't
    burn money. Divergence is set loose so the stop is the autoraise gate itself,
    not the generic no-progress detector."""
    monkeypatch.setenv("MO_GOAL_BUDGET_AUTORAISE", "1")
    monkeypatch.setenv("MO_DAILY_BUDGET_USD", "10")
    monkeypatch.setenv("MO_GOAL_BUDGET_AUTORAISE_CAP", "100")
    monkeypatch.setenv("MO_GOAL_AUTORAISE_PATIENCE", "2")
    monkeypatch.setenv("MO_GOAL_DIVERGENCE_PATIENCE", "99")   # don't pre-empt
    monkeypatch.setenv("MO_GOAL_QUARANTINE_PATIENCE", "99")

    def run_wave(wave_no, quarantined):
        # SAME failure every wave, each FUNDED ($6 ≥ MO_GOAL_FUNDED_WAVE_MIN_USD).
        return {"verdict": "fail", "failing_before": [], "failing_after": ["u1"],
                "cost_usd": 6.0, "run_id": f"r{wave_no}"}

    verdict = drive(
        goal_id="gstop", target_cwd="/tmp", units_cmd="echo u",
        predicate_cmd="echo ok", child_recipe="code-fix",
        max_waves=10, budget_total_usd=1000.0,
        run_wave_fn=run_wave, cost_fn=lambda: 12.0, state_dir=tmp_path / "s",
    )

    assert verdict["stop"] == "budget_autoraise", verdict
    assert verdict["reason"] == "no_predicted_progress"
    assert verdict["failing_units"] == ["u1"]
    final = json.loads((tmp_path / "s" / "final-verdict.json").read_text())
    assert final["stop"] == "budget_autoraise"


def test_autoraise_starvation_aware_does_not_stop_on_unfunded_flat(tmp_path, monkeypatch):
    """The core subtlety: circuit-starved waves spend $0 and re-emit one flat
    signature, which a naive read calls "stuck". Because the predictor counts
    only FUNDED waves, a run of $0 flat waves is "not tried yet" — it keeps
    RAISING the rail (giving the fixer money), never stopping on
    no_predicted_progress. The 24h circuit keeps climbing past each raise so the
    hook re-engages every wave and we can watch the rail strictly increase."""
    monkeypatch.setenv("MO_GOAL_BUDGET_AUTORAISE", "1")
    monkeypatch.setenv("MO_DAILY_BUDGET_USD", "10")
    monkeypatch.setenv("MO_GOAL_BUDGET_AUTORAISE_CAP", "1000")
    monkeypatch.setenv("MO_GOAL_AUTORAISE_PATIENCE", "2")
    monkeypatch.setenv("MO_GOAL_DIVERGENCE_PATIENCE", "99")
    monkeypatch.setenv("MO_GOAL_QUARANTINE_PATIENCE", "99")
    monkeypatch.setenv("MO_GOAL_FUNDED_WAVE_MIN_USD", "1.0")

    seen: list[float] = []
    spend = {"v": 20.0}

    def cost_fn():
        return spend["v"]

    def run_wave(wave_no, quarantined):
        seen.append(float(os.environ["MO_DAILY_BUDGET_USD"]))
        spend["v"] += 20.0  # 24h circuit climbs past the last raise
        # STARVED: identical failure, $0 spent (the circuit halted the child).
        return {"verdict": "fail", "failing_before": [], "failing_after": ["u1"],
                "cost_usd": 0.0, "run_id": f"r{wave_no}"}

    verdict = drive(
        goal_id="gstarve", target_cwd="/tmp", units_cmd="echo u",
        predicate_cmd="echo ok", child_recipe="code-fix",
        max_waves=4, budget_total_usd=100000.0,
        run_wave_fn=run_wave, cost_fn=cost_fn, state_dir=tmp_path / "s",
    )

    # It must NOT stop on the prediction: starved-flat is not "stuck".
    assert verdict["stop"] == "max_waves_reached", verdict
    # every engaged wave lifted the rail, strictly increasing — proof the $0
    # flat waves were never counted as evidence-of-stuck.
    assert seen == sorted(seen) and len(set(seen)) == len(seen), seen
    assert all(b > 10.0 for b in seen), seen


def test_autoraise_flag_off_leaves_daily_budget_untouched(tmp_path, monkeypatch):
    """With the opt-in flag OFF, the hook is inert: the daily rail is never
    touched and the loop stops on its ordinary divergence detector."""
    monkeypatch.delenv("MO_GOAL_BUDGET_AUTORAISE", raising=False)
    monkeypatch.setenv("MO_DAILY_BUDGET_USD", "10")
    monkeypatch.setenv("MO_GOAL_DIVERGENCE_PATIENCE", "2")

    def run_wave(wave_no, quarantined):
        # Would-starve + flat: flag ON would raise/stop; OFF ⇒ neither.
        return {"verdict": "fail", "failing_before": [], "failing_after": ["u1"],
                "cost_usd": 6.0, "run_id": f"r{wave_no}"}

    verdict = drive(
        goal_id="goff", target_cwd="/tmp", units_cmd="echo u",
        predicate_cmd="echo ok", child_recipe="code-fix",
        max_waves=10, budget_total_usd=1000.0,
        run_wave_fn=run_wave, cost_fn=lambda: 12.0, state_dir=tmp_path / "s",
    )

    assert verdict["stop"] == "diverged", verdict
    assert os.environ["MO_DAILY_BUDGET_USD"] == "10"


def test_autoraise_stops_at_cap_even_when_progress_predicted(tmp_path, monkeypatch):
    """The cap is the ceiling: even with progress predicted, once a needed raise
    would exceed MO_GOAL_BUDGET_AUTORAISE_CAP the loop stops (cap_reached) rather
    than lifting the daily rail past the sanctioned bound."""
    monkeypatch.setenv("MO_GOAL_BUDGET_AUTORAISE", "1")
    monkeypatch.setenv("MO_DAILY_BUDGET_USD", "10")
    monkeypatch.setenv("MO_GOAL_BUDGET_AUTORAISE_CAP", "10")  # == current ⇒ no room
    monkeypatch.setenv("MO_GOAL_AUTORAISE_PATIENCE", "9")     # progress predicted
    monkeypatch.setenv("MO_GOAL_DIVERGENCE_PATIENCE", "99")

    def run_wave(wave_no, quarantined):
        return {"verdict": "fail", "failing_before": [], "failing_after": [f"u{wave_no}"],
                "cost_usd": 6.0, "run_id": f"r{wave_no}"}

    verdict = drive(
        goal_id="gcap", target_cwd="/tmp", units_cmd="echo u",
        predicate_cmd="echo ok", child_recipe="code-fix",
        max_waves=10, budget_total_usd=1000.0,
        run_wave_fn=run_wave, cost_fn=lambda: 12.0, state_dir=tmp_path / "s",
    )

    assert verdict["stop"] == "budget_autoraise", verdict
    assert verdict["reason"].startswith("cap_reached:"), verdict
    assert os.environ["MO_DAILY_BUDGET_USD"] == "10"  # never lifted past the cap