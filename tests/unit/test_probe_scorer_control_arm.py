"""Hermetic unit tests for the matched-attempt control arm in the probe scorer.

G06-T03 — ``MO_APPLY_PROBE_CONTROL_N`` (default 3, minimum 1) makes
``probe_score`` and ``probe_score_code`` run the unmutated baseline arm N
times per probe task instead of once, and takes the BEST of N retries as the
control outcome. The apply gate then rejects a candidate whose solved set is
not a strict superset gain over the control, so run-to-run noise in the
unmutated recipe cannot be credited to the candidate.

These tests follow the seam surface used by ``test_probe_scorer_code_arm.py``:
stub ``_launch_run`` / ``_run_outcome`` / ``_seed_arm_home`` /
``_bootstrap_arm_db`` and ``delenv`` ``MINI_ORK_RUN_DIR``. Nothing boots a real
lane, no network, no LLM.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from mini_ork.cli import apply as ap
from mini_ork.learning import probe_scorer as ps


@pytest.fixture(autouse=True)
def _probe_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default cap is 2; the framework_repo fixture below writes 3 probes so
    the new control-arm tests can exercise per-probe N-retries independently.
    Module-scoped autouse: does not leak into test_cli_apply_py.py, which still
    expects cap=2 (e.g. test_probe_scorer_two_arms_vectors_and_cleanup).
    """
    monkeypatch.setenv("MO_APPLY_PROBE_MAX_TASKS", "3")


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return proc.stdout


@pytest.fixture
def framework_repo(tmp_path: Path) -> Path:
    """A throwaway git repo shaped like the framework tree.

    One committed file plus a recipe carrying a frozen probe set
    (``recipes/framework-edit/probes/*.md``). Three probes lets the control-
    arm tests exercise per-probe N-retries independently. The target prompt
    lives INSIDE the recipe so ``_materialize_arm`` (probe_scorer.py:363-402)
    can land the directive block in the temp recipe copy.
    """
    root = tmp_path / "fw"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    prompt = root / "recipes" / "framework-edit" / "prompts" / "implementer.md"
    prompt.mkdir(parents=True)
    (prompt / "prompt.md").write_text("base\n")
    probe_dir = root / "recipes" / "framework-edit" / "probes"
    probe_dir.mkdir(parents=True)
    (probe_dir / "p1.md").write_text("# probe one\n")
    (probe_dir / "p2.md").write_text("# probe two\n")
    (probe_dir / "p3.md").write_text("# probe three\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def scripted_arm(monkeypatch: pytest.MonkeyPatch):
    """Stub the expensive seams; per-(probe, retry) outcome + cost are scripted.

    The stub counts calls per probe name and assigns ``retry`` indices in order,
    so a test can write ``outcome_fn("p1.md", 0) -> 1.0`` to script the first
    retry on probe one. ``cost_fn(probe, retry) -> float`` is added to the
    running ``spent`` total and gates the budget check.
    """
    state: dict = {
        "calls": [],          # list[(probe_name, retry_idx, recipe, outcome, cost)]
        # Defaults: every launch solves, every launch is free.
        "outcome_fn": lambda _probe, _retry: 1.0,
        "cost_fn": lambda _probe, _retry: 0.0,
    }
    counter = {"n": 0}
    outcomes_by_run: dict[str, float] = {}

    def fake_launch(recipe, kickoff, target_cwd=None, root=None):
        probe_name = os.path.basename(kickoff)
        retry_idx = sum(1 for p, *_ in state["calls"] if p == probe_name)
        counter["n"] += 1
        rid = f"run-{counter['n']}"
        outcome = state["outcome_fn"](probe_name, retry_idx)
        cost = state["cost_fn"](probe_name, retry_idx)
        outcomes_by_run[rid] = outcome
        state["calls"].append((probe_name, retry_idx, recipe, outcome, cost))
        return (f'mini_ork_result={{"run_id": "{rid}"}}', rid, cost)

    def fake_outcome(run_id, db=None):
        return outcomes_by_run.get(run_id, 0.0)

    monkeypatch.setattr(ps, "_launch_run", fake_launch)
    monkeypatch.setattr(ps, "_run_outcome", fake_outcome)
    monkeypatch.setattr(ps, "_seed_arm_home", lambda tree: None)
    monkeypatch.setattr(
        ps, "_bootstrap_arm_db",
        lambda tree: os.path.join(tree, ".mini-ork", "state.db"))
    # Hermetic — _write_null_calibration writes into ${MINI_ORK_RUN_DIR}.
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    return state


def test_control_n_3_baseline_solves_on_1_of_3_retries_candidate_gets_no_credit(
        framework_repo: Path, monkeypatch, scripted_arm) -> None:
    """G06-T03 test case 1.

    N=3, baseline solves probe ``p1.md`` on 1 of 3 retries, candidate solves
    ``p1.md``. The control's per-probe solved set includes ``p1.md``; the
    candidate's solved set does NOT strictly extend it, so the gate must
    quarantine the candidate.
    """
    monkeypatch.setattr(ps, "_ROOT", str(framework_repo))
    # p1: control retries 0,1,2 = 1,0,0; candidate (retry=3) = 1.
    # p2: control retries 0,1,2 = 0,0,0; candidate (retry=3) = 0.
    # p3: control retries 0,1,2 = 0,0,0; candidate (retry=3) = 0.
    # Candidate retry index is control_n (= 3 by default): the candidate runs
    # AFTER the N control retries per probe, so its retry_idx equals control_n.
    outcomes = {
        ("p1.md", 0): 1.0, ("p1.md", 1): 0.0, ("p1.md", 2): 0.0, ("p1.md", 3): 1.0,
        ("p2.md", 0): 0.0, ("p2.md", 1): 0.0, ("p2.md", 2): 0.0, ("p2.md", 3): 0.0,
        ("p3.md", 0): 0.0, ("p3.md", 1): 0.0, ("p3.md", 2): 0.0, ("p3.md", 3): 0.0,
    }
    scripted_arm["outcome_fn"] = lambda probe, retry: outcomes.get(
        (probe, retry), 1.0)

    result = ps.probe_score(
        "framework-edit", "prompts/implementer.md/prompt.md",
        "do something", source_ref="gr-test1", context="obs")

    assert result is not None
    assert result["control_n"] == 3
    # Control's per-probe solved set: p1=1 (1 of 3 solved), p2=0, p3=0.
    assert result["control_solved"] == [1, 0, 0]
    # before = max per probe == control_solved (cast back to float for the sum).
    assert result["before"] == pytest.approx(1 / 3)
    # Candidate solves only p1 -> aggregate-up but no strict-superset.
    pt = json.loads(result["pertask_json"])
    assert pt["before"] == [1, 0, 0]
    assert pt["after"] == [1, 0, 0]

    gate = json.loads(ap.evaluate_gate(
        "cand-test", result["before"], result["after"],
        result["pertask_json"], control_n=result["control_n"]))
    assert gate["decision"] == "quarantined"
    assert "strict-superset" in gate["rationale"]
    assert gate["control_n"] == 3


def test_control_n_3_candidate_solves_a_task_no_retry_solved_is_credited(
        framework_repo: Path, monkeypatch, scripted_arm) -> None:
    """G06-T03 test case 2.

    N=3, control never solves ``p2.md`` across 3 retries, candidate solves
    ``p2.md``. The control's per-probe solved set is ``[1,0]``; the
    candidate's is ``[1,1]``. ``p2.md`` is a strict-superset gain over the
    control — the candidate is credited.
    """
    monkeypatch.setattr(ps, "_ROOT", str(framework_repo))
    # p1: control retries 0,1,2 = 1,1,1; candidate (retry=3) = 1.
    # p2: control retries 0,1,2 = 0,0,0; candidate (retry=3) = 1 (the strict-
    #     superset gain that the control arm exists to credit).
    # p3: control retries 0,1,2 = 0,0,0; candidate (retry=3) = 0.
    # Candidate retry index is control_n (= 3 by default): the candidate runs
    # AFTER the N control retries per probe, so its retry_idx equals control_n.
    outcomes = {
        ("p1.md", 0): 1.0, ("p1.md", 1): 1.0, ("p1.md", 2): 1.0, ("p1.md", 3): 1.0,
        ("p2.md", 0): 0.0, ("p2.md", 1): 0.0, ("p2.md", 2): 0.0, ("p2.md", 3): 1.0,
        ("p3.md", 0): 0.0, ("p3.md", 1): 0.0, ("p3.md", 2): 0.0, ("p3.md", 3): 0.0,
    }
    scripted_arm["outcome_fn"] = lambda probe, retry: outcomes.get(
        (probe, retry), 1.0)

    result = ps.probe_score(
        "framework-edit", "prompts/implementer.md/prompt.md",
        "do something", source_ref="gr-test2", context="obs")

    assert result is not None
    assert result["control_n"] == 3
    assert result["control_solved"] == [1, 0, 0]
    pt = json.loads(result["pertask_json"])
    # Candidate solved p1 and p2 -> p2 is the strict-superset gain over control.
    assert pt["before"] == [1, 0, 0]
    assert pt["after"] == [1, 1, 0]
    assert result["after"] == pytest.approx(2 / 3)

    gate = json.loads(ap.evaluate_gate(
        "cand-test", result["before"], result["after"],
        result["pertask_json"], control_n=result["control_n"]))
    assert gate["decision"] == "promoted"
    assert "strict-superset" in gate["rationale"]
    assert gate["control_n"] == 3


def test_control_n_1_yields_byte_identical_decisions_to_pre_change(
        framework_repo: Path, monkeypatch, scripted_arm) -> None:
    """G06-T03 test case 3.

    ``MO_APPLY_PROBE_CONTROL_N=1`` must reproduce the pre-change decisions
    byte-identically for the same stubbed outcomes — ``max([x]) == x``, and the
    strict-superset rule reduces to the existing ``delta > dt`` check on
    binary outcomes.
    """
    monkeypatch.setenv("MO_APPLY_PROBE_CONTROL_N", "1")
    monkeypatch.setattr(ps, "_ROOT", str(framework_repo))
    # Mixed scenario: p1 baseline=1 candidate=1, p2 baseline=0 candidate=1,
    # p3 baseline=0 candidate=0. Aggregate before=1/3, after=2/3, no
    # regression, candidate gained p2 → promote (matches the historical
    # "delta > dt with per-task vectors" rule).
    # Baseline retry_idx=0, candidate retry_idx=1 (== control_n).
    outcomes = {
        ("p1.md", 0): 1.0, ("p1.md", 1): 1.0,
        ("p2.md", 0): 0.0, ("p2.md", 1): 1.0,
        ("p3.md", 0): 0.0, ("p3.md", 1): 0.0,
    }
    scripted_arm["outcome_fn"] = lambda probe, retry: outcomes.get(
        (probe, retry), 1.0)

    result = ps.probe_score(
        "framework-edit", "prompts/implementer.md/prompt.md",
        "do something", source_ref="gr-test3", context="obs")

    assert result is not None
    assert result["control_n"] == 1
    # control_solved for N=1 is identical to before_v.
    assert result["control_solved"] == [1, 0, 0]
    assert result["before"] == pytest.approx(1 / 3)
    assert result["after"] == pytest.approx(2 / 3)
    pt = json.loads(result["pertask_json"])
    assert pt["before"] == [1, 0, 0]
    assert pt["after"] == [1, 1, 0]

    gate = json.loads(ap.evaluate_gate(
        "cand-test", result["before"], result["after"],
        result["pertask_json"], control_n=result["control_n"]))
    assert gate["decision"] == "promoted"
    # N=1 must NOT carry the explicit control-arm suffix — the rationale is
    # the same shape as the pre-change measurement verdict.
    assert "control_n" not in gate["rationale"]

    # Mirror scenario where the candidate matches the baseline exactly:
    # aggregate delta == 0, per-task vectors present → no measured improvement,
    # quarantined (matches the existing test_evaluate_gate_measured_path
    # fixture in tests/unit/test_cli_apply_py.py).
    flat_outcomes = {("p1.md", 0): 1.0, ("p2.md", 0): 1.0, ("p3.md", 0): 1.0}

    def flat_fn(probe, retry):
        return flat_outcomes.get((probe, retry), 1.0)

    scripted_arm["outcome_fn"] = flat_fn

    flat_result = ps.probe_score(
        "framework-edit", "prompts/implementer.md/prompt.md",
        "do something", source_ref="gr-test3b", context="obs")

    assert flat_result is not None
    assert flat_result["control_n"] == 1
    assert flat_result["control_solved"] == [1, 1, 1]
    flat_gate = json.loads(ap.evaluate_gate(
        "cand-test", flat_result["before"], flat_result["after"],
        flat_result["pertask_json"], control_n=flat_result["control_n"]))
    assert flat_gate["decision"] == "quarantined"
    assert flat_gate["rationale"].startswith("no measured improvement")


def test_budget_exhaustion_mid_control_returns_none(
        framework_repo: Path, monkeypatch, scripted_arm) -> None:
    """G06-T03 test case 4.

    Budget exhaustion MID-CONTROL must return ``None`` (unscored → quarantine),
    never a partial score. Setting ``MO_APPLY_PROBE_BUDGET_USD`` low enough
    that the second control retry exceeds it forces the mid-control exhaustion
    path.
    """
    monkeypatch.setattr(ps, "_ROOT", str(framework_repo))
    monkeypatch.setenv("MO_APPLY_PROBE_BUDGET_USD", "0.05")
    # Each control retry costs 0.04. retry 0 of p1 leaves spent=0.04 (<0.05),
    # retry 1 of p1 costs 0.04 → spent would become 0.08 → >=0.05 → break.
    scripted_arm["cost_fn"] = lambda probe, retry: 0.04
    scripted_arm["outcome_fn"] = lambda probe, retry: 0.0

    result = ps.probe_score(
        "framework-edit", "prompts/implementer.md/prompt.md",
        "do something", source_ref="gr-test4", context="obs")

    # Budget exhausted mid-control: the function returns None so the gate's
    # probe_unmeasured branch quarantines — the kickoff step 1 contract.
    assert result is None

    # The stubbed probe set has 3 probes, so the stub recorded only the first
    # control retry of p1 (retry 1 was refused by the budget check before the
    # launch). The candidate arm never runs.
    calls = scripted_arm["calls"]
    assert len(calls) == 1
    assert calls[0][0] == "p1.md"
    assert calls[0][1] == 0
    assert calls[0][2].endswith("_0")  # baseline arm's recipe copy


def test_control_n_env_minimum_is_clamped_to_one(
        framework_repo: Path, monkeypatch, scripted_arm) -> None:
    """``MO_APPLY_PROBE_CONTROL_N=0`` is clamped to 1 (kickoff minimum).

    The kickoff spec says ``N=1 reproduces today's behaviour exactly``;
    anything below 1 must be clamped to 1, never 0 (which would skip the
    baseline arm and fabricate a zero-cost measurement).
    """
    monkeypatch.setattr(ps, "_ROOT", str(framework_repo))
    monkeypatch.setenv("MO_APPLY_PROBE_CONTROL_N", "0")

    result = ps.probe_score(
        "framework-edit", "prompts/implementer.md/prompt.md",
        "do something", source_ref="gr-clamp", context="obs")

    assert result is not None
    assert result["control_n"] == 1
    # One baseline call + one candidate call per probe → 3 probes × 2 = 6.
    assert len(scripted_arm["calls"]) == 6


def test_control_solved_aggregates_best_of_n(
        framework_repo: Path, monkeypatch, scripted_arm) -> None:
    """The per-probe control is the BEST of N, never the average or the last.

    Concretely: 1 of 3 retries solving a probe → control_solved[i] == 1.
    Without the max-aggregation this would be 0 (mean) or 0 (last retry in
    this scenario) — both fail the strict-superset check in the wrong
    direction (too lenient on candidate).
    """
    monkeypatch.setattr(ps, "_ROOT", str(framework_repo))
    # p1: retry 0=1, retry 1=0, retry 2=0 → best=1.
    outcomes = {
        ("p1.md", 0): 1.0, ("p1.md", 1): 0.0, ("p1.md", 2): 0.0,
        ("p2.md", 0): 0.0, ("p2.md", 1): 0.0, ("p2.md", 2): 0.0,
        ("p3.md", 0): 0.0, ("p3.md", 1): 0.0, ("p3.md", 2): 0.0,
    }
    scripted_arm["outcome_fn"] = lambda probe, retry: outcomes.get(
        (probe, retry), 1.0)

    result = ps.probe_score(
        "framework-edit", "prompts/implementer.md/prompt.md",
        "do something", source_ref="gr-bestofn", context="obs")

    assert result is not None
    # control_solved[i] == 1 iff any retry solved probe i → only p1.
    assert result["control_solved"] == [1, 0, 0]
    # before == mean(max per probe) == mean([1, 0, 0]) == 1/3.
    assert result["before"] == pytest.approx(1 / 3)