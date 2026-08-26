"""Unit tests for the Step-3 eval judge (mini_ork/learning/eval_judge.py).

Covers the pure logic — envelope parsing, deterministic axis aggregation,
reward-payload shape, fail-open fallback, trajectory digest, prompt assembly —
plus the wiring assertions (node registered, schema enum) that prove the eval
node is reachable. No LLM/DB required; the DB write path is trace_store's."""

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.learning import eval_judge as ej

REPO = Path(__file__).resolve().parents[2]


# ── clamp01 ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value,expected", [
    (0.5, 0.5), (1.5, 1.0), (-0.2, 0.0), (1, 1.0), (0, 0.0),
    ("0.3", 0.3), ("nope", 0.0), (None, 0.0), (float("nan"), 0.0),
])
def test_clamp01(value, expected):
    assert ej.clamp01(value) == expected


# ── parse_eval_envelope ──────────────────────────────────────────────────────
def test_parse_fenced_json():
    text = ('```json\n{"axes": {"correctness": 0.9, "completeness": 0.8, '
            '"groundedness": 0.7, "safety": 1.0}, "verdict": "pass", '
            '"rationale": "ok", "trajectory_findings": ["a"]}\n```')
    env = ej.parse_eval_envelope(text)
    assert env is not None
    assert env["axes"] == {"correctness": 0.9, "completeness": 0.8,
                           "groundedness": 0.7, "safety": 1.0}
    assert env["verdict"] == "pass"
    assert env["trajectory_findings"] == ["a"]


def test_parse_bare_json_with_surrounding_prose():
    text = 'Here is my grade: {"axes": {"safety": 1.0}, "verdict": "PASS"} — done.'
    env = ej.parse_eval_envelope(text)
    assert env is not None
    assert env["axes"] == {"safety": 1.0}
    assert env["verdict"] == "pass"  # normalized lower


def test_parse_handles_braces_inside_string_values():
    text = '{"rationale": "uses a {curly} token", "axes": {"correctness": 1.0}}'
    env = ej.parse_eval_envelope(text)
    assert env is not None
    assert env["axes"] == {"correctness": 1.0}
    assert env["rationale"] == "uses a {curly} token"


def test_parse_overall_score_only_is_usable():
    env = ej.parse_eval_envelope('{"score": 0.82, "verdict": "pass"}')
    assert env is not None
    assert env["score"] == 0.82
    assert env["axes"] == {}


def test_parse_rejects_unusable_and_malformed():
    assert ej.parse_eval_envelope("") is None
    assert ej.parse_eval_envelope("no json at all") is None
    # a JSON object with neither axes nor an overall score is unusable → fallback
    assert ej.parse_eval_envelope('{"verdict": "pass"}') is None
    assert ej.parse_eval_envelope("{not valid json}") is None


def test_parse_clamps_out_of_range_axes():
    env = ej.parse_eval_envelope('{"axes": {"correctness": 1.7, "safety": -3}}')
    assert env["axes"] == {"correctness": 1.0, "safety": 0.0}


# ── aggregate_axes (the tunable reward policy) ───────────────────────────────
def test_aggregate_all_axes_perfect():
    axes = {"correctness": 1.0, "completeness": 1.0, "groundedness": 1.0, "safety": 1.0}
    assert ej.aggregate_axes(axes) == 1.0


def test_aggregate_correctness_weighted():
    # base = .5*.8 + .25*.6 + .25*.4 = 0.65 ; safety 1.0 → 0.65
    axes = {"correctness": 0.8, "completeness": 0.6, "groundedness": 0.4, "safety": 1.0}
    assert ej.aggregate_axes(axes) == pytest.approx(0.65)


def test_aggregate_safety_is_a_one_way_veto():
    axes = {"correctness": 0.9, "completeness": 0.9, "groundedness": 0.9, "safety": 0.0}
    assert ej.aggregate_axes(axes) == 0.0  # unsafe run tanks regardless of the rest
    axes["safety"] = 0.5
    assert ej.aggregate_axes(axes) == pytest.approx(0.45)  # 0.9 * 0.5


def test_aggregate_missing_axes_renormalize():
    # only correctness present, no safety → base 1.0, safety absent = 1.0
    assert ej.aggregate_axes({"correctness": 1.0}) == 1.0
    # correctness+completeness, no safety: (.5*.5+.25*.5)/.75 = 0.5
    assert ej.aggregate_axes({"correctness": 0.5, "completeness": 0.5}) == pytest.approx(0.5)


def test_aggregate_empty_axes_uses_overall_then_neutral():
    assert ej.aggregate_axes({}, overall=0.7) == 0.7
    assert ej.aggregate_axes({}) == 0.5


# ── eval_reward_payload ──────────────────────────────────────────────────────
def test_reward_payload_shape():
    axes = {"correctness": 1.0, "safety": 1.0}
    p = ej.eval_reward_payload("code_fix", "run-1", 1.0, axes, "pass",
                               artifact_ref="out.md")
    assert p["reward_source"] == "eval@v1"
    assert p["reward_value"] == 1.0
    assert p["reward_anchor"] == 0.5
    assert p["reward_g"] == pytest.approx(1.0)  # (1.0-0.5)/0.5
    assert p["reward_primary_metric"] == "eval_score"
    assert p["reward_vector"] == axes
    assert p["status"] == "success"
    assert p["final_artifact_ref"] == "out.md"
    assert p["run_id"] == "run-1"


def test_reward_payload_reward_g_at_anchor_is_zero():
    p = ej.eval_reward_payload("code_fix", "run-1", 0.5, {}, "needs_revision")
    assert p["reward_g"] == pytest.approx(0.0)
    assert "reward_vector" not in p  # no axes → no vector


def test_reward_payload_clamps_score():
    p = ej.eval_reward_payload("t", "r", 2.0, None, "pass")
    assert p["reward_value"] == 1.0
    assert p["reward_g"] == pytest.approx(1.0)


# ── fallback_score (fail-open) ───────────────────────────────────────────────
def test_fallback_prefers_rubric(tmp_path):
    (tmp_path / "rubric.json").write_text(json.dumps({"score": 6}))
    score, source = ej.fallback_score(str(tmp_path))
    assert score == pytest.approx(0.75)  # 6/8
    assert source == "eval@v1-fallback-rubric"


def test_fallback_neutral_when_no_rubric(tmp_path):
    score, source = ej.fallback_score(str(tmp_path))
    assert score == 0.5
    assert source == "eval@v1-fallback-neutral"


def test_fallback_neutral_on_garbled_rubric(tmp_path):
    (tmp_path / "rubric.json").write_text("{ broken")
    score, source = ej.fallback_score(str(tmp_path))
    assert score == 0.5
    assert source == "eval@v1-fallback-neutral"


# ── trajectory_digest + prompt ───────────────────────────────────────────────
def test_trajectory_digest_formats_rows():
    traces = [{"status": "success", "reviewer_verdict": "pass",
               "reward_source": "verifier@v1", "reward_value": 1.0,
               "final_artifact_ref": "a.py"}]
    out = ej.trajectory_digest(traces, {"test": "3 passed"})
    assert "status=success" in out
    assert "verdict=pass" in out
    assert "verifier[test]: 3 passed" in out


def test_trajectory_digest_empty():
    assert ej.trajectory_digest([], {}) == ""


def test_build_eval_prompt_contains_rubric_and_recipe_context():
    prompt = ej.build_eval_prompt(
        node_desc="fix the bug", plan_content="PLAN", artifact_text="DIFF",
        trajectory_summary="TRAJ", recipe_prompt="RECIPE-CONTEXT")
    for axis in ej.EVAL_AXES:
        assert axis in prompt
    assert "RECIPE-CONTEXT" in prompt
    assert "fix the bug" in prompt
    assert '"axes"' in prompt  # instructs the strict JSON envelope


def test_truncate_bounds_long_text():
    long = "x" * 20000
    out = ej.truncate(long, limit=8000)
    assert len(out) < 20000
    assert "elided" in out
    assert ej.truncate("short", 8000) == "short"


# ── wiring: node registered + schema enum ────────────────────────────────────
def test_eval_node_is_registered():
    from mini_ork.cli.execute import NODE_HANDLER_REGISTRY
    assert "eval" in NODE_HANDLER_REGISTRY
    assert callable(NODE_HANDLER_REGISTRY["eval"])


def test_eval_in_workflow_schema_enum():
    schema = json.loads((REPO / "schemas" / "workflow.schema.json").read_text())
    node_type_enum = schema["$defs"]["WorkflowNode"]["properties"]["type"]["enum"]
    assert "eval" in node_type_enum


# ── integration: the eval node writes a real eval@v1 reward (Phase-0 DoD) ─────
def _migrated_db(home: Path) -> str:
    from mini_ork.stores.migrate import init_db
    home.mkdir(parents=True, exist_ok=True)
    dbp = str(home / "state.db")
    rc, out, err = init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}\n{out}\n{err}"
    return dbp


def _make_ctx(run_dir: Path, db: str, dispatch_fn):
    from mini_ork.cli.execute import NodeDispatch
    return NodeDispatch(
        node_id="eval", node_type="eval", node_desc="grade the run",
        prompt_ref="", verifier_ref="", model_lane="reviewer",
        node_requires_capabilities="", root=str(REPO), run_dir=str(run_dir),
        plan_path="", task_class="code_fix", db=db, run_id="run-eval-1",
        recipe="code-fix", workflow="", lane="reviewer",
        run_dir_eff=str(run_dir), recipe_dir="", prompt_file="",
        plan_content="the plan", learned="",
        dispatch_fn=dispatch_fn, trace=lambda *a, **k: None,
        charge=lambda *a, **k: None,
    )


def _eval_rows(db: str, run_id: str) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT reward_source, reward_value, reward_g, reward_primary_metric, "
        "reward_vector_json FROM execution_traces WHERE run_id=?", (run_id,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _write_verifier(run_dir, name, obj):
    (run_dir / f"verifier_{name}.json").write_text(json.dumps(obj))


_ALL_PASS_JUDGE = ('{"axes": {"correctness": 1.0, "completeness": 1.0, '
                   '"groundedness": 1.0, "safety": 1.0}, "verdict": "pass", '
                   '"rationale": "clean", "trajectory_findings": []}')


def test_eval_node_execution_is_the_backbone(tmp_path):
    """With verifiers present, the reward comes from EXECUTION (eval-exec@v1),
    and the judge can only veto — here safety=0.5 halves a perfect exec reward."""
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_verifier(run_dir, "test", {"pass": True})
    _write_verifier(run_dir, "typecheck", {"pass": True})
    judge = ('{"axes": {"correctness": 1.0, "completeness": 1.0, '
             '"groundedness": 1.0, "safety": 0.5}, "verdict": "pass"}')
    from mini_ork.cli.execute import _handle_eval
    ctx = _make_ctx(run_dir, db, dispatch_fn=lambda tc, lane, prompt: (0, judge))
    rc, fr = _handle_eval(ctx)

    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["reward_source"] == "eval-exec@v1"
    assert saved["execution"]["r_exec"] == 1.0
    assert saved["score"] == pytest.approx(0.5)  # exec 1.0 vetoed by safety 0.5

    rows = _eval_rows(db, "run-eval-1")
    assert rows[0]["reward_source"] == "eval-exec@v1"
    assert rows[0]["reward_value"] == pytest.approx(0.5)
    assert json.loads(rows[0]["reward_vector_json"])["r_exec"] == 1.0


def test_eval_node_execution_failure_scores_low(tmp_path):
    """A failing verifier drives the reward down via execution, not the judge."""
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_verifier(run_dir, "test", {"pass": False})
    _write_verifier(run_dir, "typecheck", {"pass": True})
    from mini_ork.cli.execute import _handle_eval
    ctx = _make_ctx(run_dir, db, dispatch_fn=lambda tc, lane, prompt: (0, _ALL_PASS_JUDGE))
    rc, fr = _handle_eval(ctx)

    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["reward_source"] == "eval-exec@v1"
    assert saved["execution"]["r_exec"] == 0.5  # 1 of 2 verifiers passed
    # noise_correct(0.5, .05, .10) = (0.5-0.05)/0.85 ≈ 0.529, judge safety=1 → no veto
    assert saved["score"] == pytest.approx(0.45 / 0.85)


def test_eval_node_judge_only_when_no_execution_signal(tmp_path):
    """No verifiers → judge-only reward, tagged eval-judge@v1 (lower trust)."""
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    from mini_ork.cli.execute import _handle_eval
    ctx = _make_ctx(run_dir, db, dispatch_fn=lambda tc, lane, prompt: (0, _ALL_PASS_JUDGE))
    rc, fr = _handle_eval(ctx)

    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["reward_source"] == "eval-judge@v1"
    assert saved["score"] == 1.0
    assert saved["execution"]["r_exec"] is None
    rows = _eval_rows(db, "run-eval-1")
    assert rows[0]["reward_source"] == "eval-judge@v1"


def test_eval_node_fails_open_when_judge_errors_and_no_execution(tmp_path):
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    from mini_ork.cli.execute import _handle_eval
    # judge fails AND no verifiers → neutral heuristic, run not sunk
    ctx = _make_ctx(run_dir, db, dispatch_fn=lambda tc, lane, prompt: (1, "boom"))
    rc, fr = _handle_eval(ctx)

    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["reward_source"] == "eval@v1-fallback-neutral"
    assert saved["score"] == 0.5


# ── Layer 0/1/3 pure functions ───────────────────────────────────────────────
def test_execution_reward_pass_fraction():
    assert ej.execution_reward({"a": {"pass": True}, "b": {"pass": True}})[0] == 1.0
    assert ej.execution_reward({"a": {"pass": False}, "b": {"pass": True}})[0] == 0.5
    assert ej.execution_reward({"a": {"verdict": "pass"}, "b": {"verdict": "fail"}})[0] == 0.5


def test_execution_reward_none_when_no_signal():
    assert ej.execution_reward({})[0] is None
    assert ej.execution_reward({"a": {"verdict": "vacuous"}})[0] is None  # not a real pass
    assert ej.execution_reward({"a": {"raw": "junk"}})[0] is None


def test_noise_correct_backward_formula():
    # (R - ρ_FP) / (1 - ρ_FP - ρ_FN)
    assert ej.noise_correct(0.5, 0.1, 0.2) == pytest.approx((0.5 - 0.1) / 0.7)
    assert ej.noise_correct(0.5, 0.0, 0.0) == 0.5
    assert ej.noise_correct(1.0, 0.05, 0.10) == 1.0  # (0.95/0.85) clamps to 1.0


def test_noise_correct_guards_overestimated_rates():
    # 1 - 0.6 - 0.6 < 0 → skip correction, return raw (paper's failure edge)
    assert ej.noise_correct(0.5, 0.6, 0.6) == 0.5


def test_judge_veto_is_one_way():
    assert ej.judge_veto(1.0, {"safety": 0.5}) == pytest.approx(0.5)
    assert ej.judge_veto(1.0, {"safety": 0.8, "groundedness": 0.4}) == pytest.approx(0.4)
    assert ej.judge_veto(0.8, {}) == 0.8  # no veto axes → unchanged
    assert ej.judge_veto(1.0, {"correctness": 0.2}) == 1.0  # correctness is not a veto axis


# ── Layer 3: jury (decorrelated panel) ───────────────────────────────────────
def test_panel_consensus_takes_median():
    envs = [{"axes": {"safety": 0.2}}, {"axes": {"safety": 0.9}}, {"axes": {"safety": 0.5}}]
    assert ej.panel_consensus(envs)["safety"] == 0.5  # median, robust to outliers


def test_panel_agreement_high_and_low():
    agree = [{"axes": {"safety": 0.8, "groundedness": 0.9}},
             {"axes": {"safety": 0.8, "groundedness": 0.9}}]
    assert ej.panel_agreement(agree) == 1.0
    diverge = [{"axes": {"safety": 0.0}}, {"axes": {"safety": 1.0}}]
    assert ej.panel_agreement(diverge) == 0.0  # max disagreement → 0 agreement
    assert ej.panel_agreement([{"axes": {"safety": 0.5}}]) == 1.0  # <2 judges → 1.0


def test_jury_veto_applies_consensus_when_agreed():
    envs = [{"axes": {"safety": 0.5, "groundedness": 1.0}},
            {"axes": {"safety": 0.5, "groundedness": 1.0}}]
    score, meta = ej.jury_veto(1.0, envs)
    assert score == pytest.approx(0.5)  # min(median safety .5, ground 1.0)
    assert meta["jury"] == "applied"
    assert meta["n"] == 2


def test_jury_veto_abstains_on_disagreement():
    envs = [{"axes": {"safety": 0.0}}, {"axes": {"safety": 1.0}}]
    score, meta = ej.jury_veto(0.9, envs)  # agreement 0 < alpha_min → abstain
    assert score == 0.9  # execution reward stands; untrusted veto NOT applied
    assert meta["jury"] == "abstain_low_agreement"


def test_jury_veto_single_and_empty_degenerate():
    s1, m1 = ej.jury_veto(1.0, [{"axes": {"safety": 0.5}}])
    assert s1 == pytest.approx(0.5)
    assert m1["jury"] == "single"
    s0, m0 = ej.jury_veto(0.7, [])
    assert s0 == 0.7  # empty panel → no veto (judge-unavailable fail-open)
    assert m0["jury"] == "empty"


def test_eval_node_jury_applies_consensus_veto(tmp_path, monkeypatch):
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_verifier(run_dir, "test", {"pass": True})  # execution reward 1.0
    monkeypatch.setenv("MO_EVAL_JURY_LANES", "opus,kimi")

    def dispatch(tc, lane, prompt):
        return 0, '{"axes": {"safety": 0.5, "groundedness": 1.0}, "verdict": "pass"}'

    from mini_ork.cli.execute import _handle_eval
    ctx = _make_ctx(run_dir, db, dispatch_fn=dispatch)
    rc, fr = _handle_eval(ctx)

    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["reward_source"] == "eval-exec@v1"
    assert saved["execution"]["jury"]["jury"] == "applied"
    assert saved["execution"]["jury"]["n"] == 2
    assert saved["score"] == pytest.approx(0.5)  # 1.0 vetoed by consensus safety 0.5


def test_eval_node_jury_abstains_when_panel_disagrees(tmp_path, monkeypatch):
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_verifier(run_dir, "test", {"pass": True})  # execution reward 1.0
    monkeypatch.setenv("MO_EVAL_JURY_LANES", "opus,kimi")

    def dispatch(tc, lane, prompt):
        if lane == "opus":
            return 0, '{"axes": {"safety": 0.0, "groundedness": 0.0}, "verdict": "fail"}'
        return 0, '{"axes": {"safety": 1.0, "groundedness": 1.0}, "verdict": "pass"}'

    from mini_ork.cli.execute import _handle_eval
    ctx = _make_ctx(run_dir, db, dispatch_fn=dispatch)
    rc, fr = _handle_eval(ctx)

    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["execution"]["jury"]["jury"] == "abstain_low_agreement"
    # panel can't agree → no veto applied → execution reward stands
    assert saved["score"] == 1.0


def test_eval_node_jury_escalates_hung_panel_to_tiebreaker(tmp_path, monkeypatch):
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_verifier(run_dir, "test", {"pass": True})  # execution reward 1.0
    monkeypatch.setenv("MO_EVAL_JURY_LANES", "opus,kimi")
    monkeypatch.setenv("MO_EVAL_JURY_ESCALATE_LANE", "strong")

    def dispatch(tc, lane, prompt):
        if lane == "opus":
            return 0, '{"axes": {"safety": 0.0}, "verdict": "fail"}'
        if lane == "kimi":
            return 0, '{"axes": {"safety": 1.0}, "verdict": "pass"}'
        # the strong tiebreaker breaks the hung jury
        return 0, '{"axes": {"safety": 0.3, "groundedness": 1.0}, "verdict": "needs_revision"}'

    from mini_ork.cli.execute import _handle_eval
    ctx = _make_ctx(run_dir, db, dispatch_fn=dispatch)
    rc, fr = _handle_eval(ctx)

    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    jury = saved["execution"]["jury"]
    assert jury["jury"] == "escalated"           # hung jury → tiebreaker decided
    assert jury["tiebreaker_lane"] == "strong"
    assert saved["score"] == pytest.approx(0.3)  # tiebreaker's safety veto applied


# ── R2: coherence — the missing Layer 2 ──────────────────────────────────────
def test_decide_from_steps():
    assert ej.decide_from_steps([True, True]) == "pass"
    assert ej.decide_from_steps([True, False]) == "fail"
    assert ej.decide_from_steps([None, None]) is None        # no concrete signal
    assert ej.decide_from_steps([True, None]) == "pass"      # vacuous steps drop out
    assert ej.decide_from_steps([]) is None


def test_coherence_catches_shipped_success_the_steps_contradict():
    # test_green_wrong: run claims pass but a step concretely failed → incoherent.
    assert ej.coherence("pass", [True, False]) == 0.0
    assert ej.coherence("pass", [True, True]) == 1.0         # steps support the claim
    assert ej.coherence("fail", [True, False]) == 1.0        # honest failure is coherent
    assert ej.coherence("pass", [None, None]) == 1.0         # no signal → fail-open
    assert ej.coherence("weird-verdict", [True, False]) == 1.0  # unknown verdict → open


def test_coherence_gate_is_one_way():
    assert ej.coherence_gate(0.9, 1.0) == pytest.approx(0.9)  # coherent → unchanged
    assert ej.coherence_gate(0.9, 0.0) == 0.0                 # incoherent, default block
    assert ej.coherence_gate(0.9, 0.0, penalty=0.5) == pytest.approx(0.45)  # soft


# ── R1: VPRM per-stage process reward ────────────────────────────────────────
def test_process_reward_sums_stage_checks():
    score, detail = ej.process_reward(
        {"plan": True, "execute": True, "verify": False, "coverage": 0.5})
    assert score == pytest.approx(2.5 / 4)                    # (1+1+0+0.5)/4
    assert detail == {"plan": 1.0, "execute": 1.0, "verify": 0.0, "coverage": 0.5}


def test_process_reward_none_when_no_stage_signal():
    score, detail = ej.process_reward({"plan": None, "execute": None})
    assert score is None
    assert detail == {"plan": None, "execute": None}


def test_process_reward_missing_stages_renormalize():
    # only the two present stages count; weights renormalize over them
    score, _ = ej.process_reward({"plan": True, "execute": False})
    assert score == pytest.approx(0.5)


# ── R3: reward decomposition (SEVA advantage-collapse fix) ────────────────────
def test_combine_components_symmetric_mean():
    scalar, vec = ej.combine_components(
        {"execution": 0.0, "coherence": 1.0, "process": 0.5})
    assert scalar == pytest.approx(0.5)                      # (0+1+0.5)/3
    assert vec == {"execution": 0.0, "coherence": 1.0, "process": 0.5}


def test_combine_components_drops_none_and_renormalizes():
    scalar, vec = ej.combine_components(
        {"execution": 1.0, "coherence": None, "process": 0.0})
    assert scalar == pytest.approx(0.5)                      # None drops → (1+0)/2
    assert "coherence" not in vec


def test_combine_components_restores_group_variance():
    """SEVA Prop 1/2: a near-binary outcome collapses a GRPO group's advantage
    spread; independent components keep it alive. Two rollouts that BOTH fail the
    outcome (0.0) but differ on process/coherence must get DIFFERENT decomposed
    rewards — otherwise the group-relative advantage is zero and the gradient dies."""
    a, _ = ej.combine_components({"execution": 0.0, "coherence": 1.0, "process": 0.8})
    b, _ = ej.combine_components({"execution": 0.0, "coherence": 0.0, "process": 0.2})
    assert a != b                                            # variance survives
    assert a > b                                             # the more-coherent rollout scores higher


def test_combine_components_empty_is_zero():
    assert ej.combine_components({}) == (0.0, {})
    assert ej.combine_components({"x": None}) == (0.0, {})


# ── R4: calibrated confidence ────────────────────────────────────────────────
def test_calibrated_priors_shrink_with_confidence():
    assert ej.calibrated_priors(0.0, 0.05, 0.10) == (0.05, 0.10)   # γ=0 → full prior
    assert ej.calibrated_priors(1.0, 0.05, 0.10) == (0.0, 0.0)     # γ=1 → trust fully
    fp, fn = ej.calibrated_priors(0.5, 0.04, 0.10)
    assert (fp, fn) == pytest.approx((0.02, 0.05))


def test_calibration_reward_rewards_confident_right_punishes_confident_wrong():
    assert ej.calibration_reward(0.8, agreed=True) == pytest.approx(0.12)   # +γ·0.15
    assert ej.calibration_reward(0.8, agreed=False) == pytest.approx(-0.08)  # −γ·0.10
    assert ej.calibration_reward(0.0, agreed=False) == 0.0                   # hedged → ~0


# ── R6: partial-progress from verifiable subproblems ─────────────────────────
def test_subproblem_reward_partial_progress():
    assert ej.subproblem_reward([True, True, True, False, False]) == pytest.approx(0.6)
    assert ej.subproblem_reward([False, False]) == 0.0
    assert ej.subproblem_reward([True, None, True]) == 1.0   # None drops out
    assert ej.subproblem_reward([None, None]) is None        # no signal
    assert ej.subproblem_reward([]) is None


# ── R5: agentic forward/backward verifier (deterministic core) ───────────────
def test_forward_backward_verify_requires_both_directions():
    v, g, _ = ej.forward_backward_verify([("a", True, True), ("b", True, True)])
    assert (v, g) == ("pass", 1.0)
    # forward passes but backward fails → a false positive is caught
    v, g, det = ej.forward_backward_verify([("a", True, True), ("b", True, False)])
    assert v == "fail"
    assert g == pytest.approx(0.75)                          # 3 of 4 directional checks agreed
    assert det["checks"]["b"] == {"forward": True, "backward": False}
    assert ej.forward_backward_verify([])[0] == "pass"       # no sub-checks → fail-open


# ── wiring: gated behaviors through _handle_eval ──────────────────────────────
def test_eval_failing_verifier_is_not_incoherence(tmp_path, monkeypatch):
    """The rework's anti-redundancy guarantee: a FAILING verifier is NOT process
    incoherence. Coherence reads the independent execute/verify stages, not the
    verifier pass/fraction the execution backbone already down-weights — so a
    partial failure keeps coherence=1.0 and the graded execution score stands (no
    double penalty), even with the gate on by default."""
    monkeypatch.delenv("MO_EVAL_COHERENCE_GATE", raising=False)  # default ON
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_verifier(run_dir, "test", {"pass": True})
    _write_verifier(run_dir, "metamorphic", {"pass": False})  # concrete → verify stage OK
    from mini_ork.cli.execute import _handle_eval
    judge = '{"axes": {"safety": 1.0}, "verdict": "pass"}'
    ctx = _make_ctx(run_dir, db, dispatch_fn=lambda tc, lane, prompt: (0, judge))
    rc, fr = _handle_eval(ctx)
    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["process"]["coherence"] == 1.0              # NOT redundant with backbone
    assert "gated_to" not in saved["process"]                # gate did not fire
    assert saved["score"] == pytest.approx((0.5 - 0.05) / 0.85)  # graded exec reward stands


def test_eval_coherence_gate_blocks_test_theater(tmp_path, monkeypatch):
    """Genuine process incoherence (default-ON gate): the judge rubber-stamps a
    pass but the run verified nothing real — a vacuous verifier (no pass/verdict)
    → verify stage FALSE → coherence 0. The over-claimed success is blocked one-way
    (score 0.0, needs_revision). This is the test-theater case the execution
    backbone cannot see (r_exec is None → it would otherwise trust the judge)."""
    monkeypatch.delenv("MO_EVAL_COHERENCE_GATE", raising=False)  # default ON
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_verifier(run_dir, "smoke", {"ran": True})  # vacuous: no pass/verdict signal
    from mini_ork.cli.execute import _handle_eval
    judge = '{"axes": {"safety": 1.0}, "verdict": "pass"}'
    ctx = _make_ctx(run_dir, db, dispatch_fn=lambda tc, lane, prompt: (0, judge))
    rc, fr = _handle_eval(ctx)
    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["process"]["coherence"] == 0.0
    assert saved["process"]["overclaimed_success"] is True
    assert saved["process"]["gated_to"] == 0.0
    assert saved["score"] == 0.0                             # blocked
    assert saved["verdict"] == "needs_revision"


def test_eval_records_incoherence_without_gate(tmp_path, monkeypatch):
    """Recording ≠ enforcement: with the gate explicitly OFF, a process-incoherent
    run (vacuous verifier + claimed pass) still RECORDS coherence=0 but the score
    is NOT downgraded — the judge-only reward stands."""
    monkeypatch.setenv("MO_EVAL_COHERENCE_GATE", "0")
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_verifier(run_dir, "smoke", {"ran": True})  # vacuous
    from mini_ork.cli.execute import _handle_eval
    judge = '{"axes": {"safety": 1.0}, "verdict": "pass"}'
    ctx = _make_ctx(run_dir, db, dispatch_fn=lambda tc, lane, prompt: (0, judge))
    rc, fr = _handle_eval(ctx)
    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["process"]["coherence"] == 0.0
    assert "gated_to" not in saved["process"]                # recorded, not enforced
    assert saved["score"] > 0.0


def test_eval_decomposed_reward_flag_makes_components_primary(tmp_path, monkeypatch):
    """MO_EVAL_DECOMPOSED_REWARD=1: the scalar becomes the symmetric mean of the
    independent components, carrying its own variance (SEVA fix)."""
    monkeypatch.setenv("MO_EVAL_DECOMPOSED_REWARD", "1")
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_verifier(run_dir, "test", {"pass": True})
    from mini_ork.cli.execute import _handle_eval
    ctx = _make_ctx(run_dir, db, dispatch_fn=lambda tc, lane, prompt: (0, _ALL_PASS_JUDGE))
    rc, fr = _handle_eval(ctx)
    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["score"] == pytest.approx(saved["process"]["decomposed_score"])
    assert saved["process"]["components"]  # non-empty component vector recorded


def test_eval_subproblem_partial_progress_recorded(tmp_path):
    """A failing verifier that declares sub-cases still earns partial-progress
    credit (R6) — recorded in the reward vector even when the run fails overall."""
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_verifier(run_dir, "suite", {"pass": False, "subtasks": [
        {"pass": True}, {"pass": True}, {"pass": True}, {"pass": False}, {"pass": False}]})
    from mini_ork.cli.execute import _handle_eval
    ctx = _make_ctx(run_dir, db, dispatch_fn=lambda tc, lane, prompt: (0, _ALL_PASS_JUDGE))
    rc, fr = _handle_eval(ctx)
    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["process"]["subproblem_reward"] == pytest.approx(0.6)  # 3 of 5 sub-cases
