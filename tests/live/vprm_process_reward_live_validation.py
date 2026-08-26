"""Model-free live smoke for the verifiable/process-reward stack (R1–R6).

Drives the REAL eval node (``_handle_eval``) end-to-end against a migrated DB,
real ``verifier_*.json`` files, and a stubbed judge dispatch (no model — codex/
LLM lanes are not required). Proves the four low-cost recommendations from
``internal-docs/research/2026-08-26-verifiable-process-reward-for-miniork.md``:

  R2  coherence — the missing Layer 2 — catches "shipped success the steps
      contradict" and, with MO_EVAL_COHERENCE_GATE=1, blocks it one-way.
  R1  the wired-but-heuristic process_reward is populated by deterministic
      per-stage VPRM checks.
  R3  decomposing the reward into independent components restores the GRPO
      group's advantage spread when the outcome term is near-binary (SEVA fix).
  R6  a failed run still earns partial-progress credit from verifiable subproblems.
  R4  a calibrated confidence γ shrinks the Layer-1 FP/FN priors.

Run: ``python3.11 tests/live/vprm_process_reward_live_validation.py``
(also pytest-collectable). Exits non-zero on any failed assertion.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _migrated_db(home: Path) -> str:
    from mini_ork.stores.migrate import init_db
    home.mkdir(parents=True, exist_ok=True)
    dbp = str(home / "state.db")
    rc, out, err = init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}\n{out}\n{err}"
    return dbp


def _ctx(run_dir: Path, db: str, dispatch_fn):
    from mini_ork.cli.execute import NodeDispatch
    return NodeDispatch(
        node_id="eval", node_type="eval", node_desc="grade the run",
        prompt_ref="", verifier_ref="", model_lane="reviewer",
        node_requires_capabilities="", root=str(REPO), run_dir=str(run_dir),
        plan_path="", task_class="code_fix", db=db, run_id="run-smoke-1",
        recipe="code-fix", workflow="", lane="reviewer",
        run_dir_eff=str(run_dir), recipe_dir="", prompt_file="",
        plan_content="the plan enumerates the edited files", learned="",
        dispatch_fn=dispatch_fn, trace=lambda *a, **k: None,
        charge=lambda *a, **k: None,
    )


def _write_verifier(run_dir: Path, name: str, obj: dict) -> None:
    (run_dir / f"verifier_{name}.json").write_text(json.dumps(obj))


def _run_eval(tmp: Path, verifiers: dict, judge: str, run_id: str = "run-smoke-1") -> dict:
    """Fresh home+db+run_dir, write verifiers, dispatch a canned judge, return eval.json."""
    db = _migrated_db(tmp / "home")
    run_dir = tmp / "run"
    run_dir.mkdir(exist_ok=True)
    for name, obj in verifiers.items():
        _write_verifier(run_dir, name, obj)
    from mini_ork.cli.execute import NodeDispatch  # noqa: F401
    from mini_ork.cli.execute import _handle_eval
    ctx = _ctx(run_dir, db, dispatch_fn=lambda tc, lane, prompt: (0, judge))
    ctx.run_id = run_id  # NodeDispatch is a dataclass; keep default unless overridden
    rc, fr = _handle_eval(ctx)
    assert (rc, fr) == (0, "done"), f"eval node did not complete: {(rc, fr)}"
    return json.loads((run_dir / "eval.json").read_text())


_PASS_JUDGE = '{"axes": {"correctness": 1.0, "safety": 1.0}, "verdict": "pass"}'


def _ok(label: str) -> None:
    print(f"  \033[32mPASS\033[0m {label}")


def scenario_backbone_unchanged() -> None:
    """R2 recorded, default OFF: coherent run keeps the execution-backbone score."""
    with tempfile.TemporaryDirectory() as d:
        saved = _run_eval(Path(d), {"test": {"pass": True}, "types": {"pass": True}},
                          _PASS_JUDGE)
    assert saved["reward_source"] == "eval-exec@v1", saved["reward_source"]
    assert saved["process"]["coherence"] == 1.0
    assert saved["process"]["process_reward"] is not None      # R1 populated
    assert saved["score"] == 1.0                               # backbone unchanged
    _ok("coherent run → coherence=1.0, execution backbone score=1.0 (R1 process_reward populated)")


def scenario_coherence_records_contradiction() -> None:
    """R2 detects 'claimed pass but a step failed' even with the gate OFF."""
    with tempfile.TemporaryDirectory() as d:
        saved = _run_eval(Path(d),
                          {"test": {"pass": True}, "metamorphic": {"pass": False}},
                          _PASS_JUDGE)
    assert saved["process"]["coherence"] == 0.0, saved["process"]
    # gate OFF → score is still the execution reward, NOT blocked
    assert saved["score"] > 0.0
    assert "gated_to" not in saved["process"]
    _ok("incoherent run recorded (coherence=0.0) but NOT gated when the gate is off")


def scenario_coherence_gate_blocks() -> None:
    """R2 gate ON: contradicted success is blocked one-way (score→0, needs_revision)."""
    os.environ["MO_EVAL_COHERENCE_GATE"] = "1"
    try:
        with tempfile.TemporaryDirectory() as d:
            saved = _run_eval(Path(d),
                              {"test": {"pass": True}, "metamorphic": {"pass": False}},
                              _PASS_JUDGE)
    finally:
        del os.environ["MO_EVAL_COHERENCE_GATE"]
    assert saved["process"]["coherence"] == 0.0
    assert saved["process"]["gated_to"] == 0.0
    assert saved["score"] == 0.0
    assert saved["verdict"] == "needs_revision"
    _ok("MO_EVAL_COHERENCE_GATE=1 → contradicted success blocked (score=0.0, needs_revision)")


def scenario_decomposed_reward_primary() -> None:
    """R3: with the flag, the scalar is the symmetric mean of independent components."""
    os.environ["MO_EVAL_DECOMPOSED_REWARD"] = "1"
    try:
        with tempfile.TemporaryDirectory() as d:
            saved = _run_eval(Path(d), {"test": {"pass": True}}, _PASS_JUDGE)
    finally:
        del os.environ["MO_EVAL_DECOMPOSED_REWARD"]
    comps = saved["process"]["components"]
    assert comps, "expected a non-empty component vector"
    assert abs(saved["score"] - saved["process"]["decomposed_score"]) < 1e-9
    _ok(f"MO_EVAL_DECOMPOSED_REWARD=1 → score is the component mean over {sorted(comps)}")


def scenario_grpo_variance_restored() -> None:
    """R3/SEVA: two rollouts that BOTH fail the outcome but differ on process must
    get DIFFERENT decomposed rewards — otherwise the group advantage is 0 and the
    gradient dies. This is the mechanism behind the flat-compounding-curve fix."""
    from mini_ork.learning import eval_judge as ej
    # both outcomes 0.0 (hard task, near-binary); differ on process + coherence
    a, _ = ej.combine_components({"execution": 0.0, "process": 0.8, "coherence": 1.0})
    b, _ = ej.combine_components({"execution": 0.0, "process": 0.2, "coherence": 0.0})
    assert a != b, "GRPO group would have zero advantage spread — gradient dies"
    assert a > b
    spread = abs(a - b)
    assert spread > 0.3, spread
    _ok(f"failed-outcome rollouts keep advantage spread Δ={spread:.2f} (a={a:.2f} > b={b:.2f})")


def scenario_subproblem_partial_progress() -> None:
    """R6: a failed suite with 3/5 sub-cases green still earns 0.6, not 0."""
    with tempfile.TemporaryDirectory() as d:
        saved = _run_eval(Path(d), {"suite": {"pass": False, "subtasks": [
            {"pass": True}, {"pass": True}, {"pass": True},
            {"pass": False}, {"pass": False}]}}, _PASS_JUDGE)
    assert saved["process"]["subproblem_reward"] == 0.6, saved["process"]
    _ok("failed run earns partial-progress subproblem_reward=0.6 (3/5 sub-cases)")


def scenario_calibrated_priors() -> None:
    """R4: a confident judge (γ=1.0) shrinks the FP/FN priors to 0, so noise_correct
    returns the raw execution reward (no de-biasing needed for a trusted verdict)."""
    os.environ["MO_EVAL_CALIBRATED_PRIORS"] = "1"
    try:
        with tempfile.TemporaryDirectory() as d:
            # 1 of 2 verifiers pass → r_exec 0.5; confidence 1.0 → priors → 0 → r_corr 0.5
            judge = ('{"axes": {"safety": 1.0}, "verdict": "needs_revision", '
                     '"confidence": 1.0}')
            saved = _run_eval(Path(d),
                              {"a": {"pass": True}, "b": {"pass": False}}, judge)
    finally:
        del os.environ["MO_EVAL_CALIBRATED_PRIORS"]
    ex = saved["execution"]
    assert ex["fp_rate"] == 0.0 and ex["fn_rate"] == 0.0, ex
    assert abs(ex["r_corrected"] - 0.5) < 1e-9, ex
    _ok("γ=1.0 → calibrated priors shrink to 0.0 → noise-correct returns raw exec reward")


SCENARIOS = [
    scenario_backbone_unchanged,
    scenario_coherence_records_contradiction,
    scenario_coherence_gate_blocks,
    scenario_decomposed_reward_primary,
    scenario_grpo_variance_restored,
    scenario_subproblem_partial_progress,
    scenario_calibrated_priors,
]


def test_vprm_process_reward_smoke():
    """pytest entrypoint — runs every scenario."""
    for fn in SCENARIOS:
        fn()


def main() -> int:
    print("VPRM / process-reward live smoke (model-free) — R1–R6\n")
    failed = 0
    for fn in SCENARIOS:
        try:
            fn()
        except AssertionError as exc:
            print(f"  \033[31mFAIL\033[0m {fn.__name__}: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  \033[31mERROR\033[0m {fn.__name__}: {exc!r}")
            failed += 1
    print()
    if failed:
        print(f"SMOKE FAIL — {failed}/{len(SCENARIOS)} scenarios failed")
        return 1
    print(f"SMOKE PASS — {len(SCENARIOS)}/{len(SCENARIOS)} scenarios green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
