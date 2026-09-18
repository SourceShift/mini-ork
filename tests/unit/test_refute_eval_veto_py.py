"""The refutation-survival oracle as a veto in the eval node (D2).

``refute_or_promote_gate.check_fabrication_survival`` plants findings it knows
are fabricated and counts how many the validator reported anyway. It was fully
implemented and had no caller in the reward path, so a run could rest its
claimed success on a validator that was demonstrably generating findings and
nothing in the score would say so.

These tests pin the wiring that fixes that: ``_handle_eval`` locates the
campaign artifacts, asks the oracle, and a measured ``REFUTE_FAILED`` multiplies
the reward down — one-way and multiply-only, like ``judge_veto``.

The interesting half is what happens when the oracle does NOT measure. Missing
artifacts, an unreadable manifest, a run that never held a refute campaign: all
of them must leave the reward exactly as it was. ``indeterminate`` is silence,
not permission, and a veto layer that quietly became a pass layer would be worse
than no layer at all — so every unmeasured state is pinned to "no change" below.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.gates import refute_or_promote_gate as rpg
from mini_ork.learning import eval_judge as ej

REPO = Path(__file__).resolve().parents[2]

_ALL_PASS_JUDGE = ('{"axes": {"correctness": 1.0, "completeness": 1.0, '
                   '"groundedness": 1.0, "safety": 1.0}, "verdict": "pass", '
                   '"rationale": "clean", "trajectory_findings": []}')


# ─────────────────────────────────────────────────────────────────────────────
# refute_veto — the veto layer
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("survival", [
    None,                                     # no campaign at all
    {},                                       # malformed
    {"verdict": "indeterminate", "reason": "missing_inputs"},
    {"verdict": "indeterminate", "reason": "python_unavailable"},
    {"verdict": "validator_grounded", "reason": "ok"},
    {"verdict": "", "reason": "ok"},          # absent verdict
])
def test_unmeasured_states_leave_the_reward_untouched(survival):
    """Only a MEASURED REFUTE_FAILED engages. Silence changes nothing.

    This is the load-bearing assertion: ``indeterminate`` is not a pass, and it
    is not a veto either — it is the oracle declining to speak. A veto layer
    that read it as license to downgrade would punish every run that simply
    never held a refute campaign.
    """
    score, meta = ej.refute_veto(0.9, survival)
    assert score == pytest.approx(0.9)
    assert meta.get("applied") is not True


def test_a_surviving_validator_blocks_the_reward():
    """A validator caught fabricating is not grounds for a passing score."""
    score, meta = ej.refute_veto(
        0.9, {"verdict": "REFUTE_FAILED", "reason": "high_fp_survival",
              "fp_rate": 0.4, "fp_ceiling": 0.1})
    assert score == pytest.approx(0.0)
    assert meta["applied"] is True
    assert meta["fp_rate"] == 0.4


@pytest.mark.parametrize("reward", [0.0, 0.25, 0.5, 1.0])
def test_the_veto_can_only_pull_down(reward):
    """Multiply-only: the vetoed score never exceeds the score that went in."""
    score, _ = ej.refute_veto(reward, {"verdict": "REFUTE_FAILED"})
    assert score <= reward


def test_a_softened_penalty_still_only_pulls_down():
    score, meta = ej.refute_veto(0.8, {"verdict": "REFUTE_FAILED"}, penalty=0.5)
    assert score == pytest.approx(0.4)
    assert meta["applied"] is True


# ─────────────────────────────────────────────────────────────────────────────
# _refute_artifacts — locating the campaign
# ─────────────────────────────────────────────────────────────────────────────


def test_artifacts_are_absent_by_default(tmp_path, monkeypatch):
    """A run with no campaign → empty paths → the oracle reports indeterminate."""
    from mini_ork.cli.execute_handlers import _refute_artifacts

    monkeypatch.delenv("MO_REFUTE_FINDINGS", raising=False)
    monkeypatch.delenv("MO_REFUTE_FABRICATIONS", raising=False)
    assert _refute_artifacts(str(tmp_path)) == ("", "")


def test_artifacts_use_the_run_dir_convention(tmp_path, monkeypatch):
    from mini_ork.cli.execute_handlers import _refute_artifacts

    monkeypatch.delenv("MO_REFUTE_FINDINGS", raising=False)
    monkeypatch.delenv("MO_REFUTE_FABRICATIONS", raising=False)
    (tmp_path / "refute-findings.json").write_text("[]")
    (tmp_path / "fabrications.json").write_text("[]")
    findings, fabrications = _refute_artifacts(str(tmp_path))
    assert findings == str(tmp_path / "refute-findings.json")
    assert fabrications == str(tmp_path / "fabrications.json")


def test_env_overrides_win(tmp_path, monkeypatch):
    from mini_ork.cli.execute_handlers import _refute_artifacts

    monkeypatch.setenv("MO_REFUTE_FINDINGS", "/elsewhere/f.json")
    monkeypatch.setenv("MO_REFUTE_FABRICATIONS", "/elsewhere/fab.json")
    assert _refute_artifacts(str(tmp_path)) == ("/elsewhere/f.json",
                                                "/elsewhere/fab.json")


def test_one_side_missing_is_still_no_campaign(tmp_path, monkeypatch):
    """Both sides are needed for the experiment; half an experiment measures nothing."""
    from mini_ork.cli.execute_handlers import _refute_artifacts

    monkeypatch.delenv("MO_REFUTE_FINDINGS", raising=False)
    monkeypatch.delenv("MO_REFUTE_FABRICATIONS", raising=False)
    (tmp_path / "fabrications.json").write_text("[]")
    findings, _ = _refute_artifacts(str(tmp_path))
    assert findings == ""
    survival, rc = rpg.check_fabrication_survival("", str(tmp_path / "fabrications.json"))
    assert (survival["verdict"], rc) == ("indeterminate", 0)


# ─────────────────────────────────────────────────────────────────────────────
# the oracle + the veto together, on real fabrications
# ─────────────────────────────────────────────────────────────────────────────


def _campaign(tmp_path, report_all: bool):
    """A real fabrication manifest, and findings that either report them or not."""
    fab_path = tmp_path / "fabrications.json"
    rpg.generate_fabrications(10, str(fab_path))
    fabs = json.loads(fab_path.read_text())
    findings = tmp_path / "refute-findings.json"
    if report_all:
        # A validator that "found" every plant it was never given: fabricating.
        findings.write_text(json.dumps(
            [{"id": f["id"], "claim": f["claim"]} for f in fabs]))
    else:
        findings.write_text(json.dumps([{"id": "a-real-issue"}]))
    return str(findings), str(fab_path)


def test_a_fabricating_validator_is_caught_end_to_end(tmp_path):
    findings, fabs = _campaign(tmp_path, report_all=True)
    survival, rc = rpg.check_fabrication_survival(findings, fabs,
                                                  report_dir=str(tmp_path))
    assert survival["verdict"] == "REFUTE_FAILED"
    assert rc == 1
    score, meta = ej.refute_veto(1.0, survival)
    assert score == pytest.approx(0.0)
    assert meta["applied"] is True


def test_a_grounded_validator_is_left_alone(tmp_path):
    findings, fabs = _campaign(tmp_path, report_all=False)
    survival, rc = rpg.check_fabrication_survival(findings, fabs,
                                                  report_dir=str(tmp_path))
    assert survival["verdict"] == "validator_grounded"
    assert rc == 0
    score, meta = ej.refute_veto(1.0, survival)
    assert score == pytest.approx(1.0)
    assert meta.get("applied") is not True


# ─────────────────────────────────────────────────────────────────────────────
# the wiring — _handle_eval actually applies it
# ─────────────────────────────────────────────────────────────────────────────


def _migrated_db(home: Path) -> str:
    from mini_ork.stores.migrate import init_db
    home.mkdir(parents=True, exist_ok=True)
    dbp = str(home / "state.db")
    rc, out, err = init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}\n{out}\n{err}"
    return dbp


def _make_ctx(run_dir: Path, db: str):
    from mini_ork.cli.execute import NodeDispatch
    return NodeDispatch(
        node_id="eval", node_type="eval", node_desc="grade the run",
        prompt_ref="", verifier_ref="", model_lane="reviewer",
        node_requires_capabilities="", root=str(REPO), run_dir=str(run_dir),
        plan_path="", task_class="code_fix", db=db, run_id="run-eval-1",
        recipe="code-fix", workflow="", lane="reviewer",
        run_dir_eff=str(run_dir), recipe_dir="", prompt_file="",
        plan_content="the plan", learned="",
        dispatch_fn=lambda tc, lane, prompt: (0, _ALL_PASS_JUDGE),
        trace=lambda *a, **k: None,
        charge=lambda *a, **k: None,
    )


def _clean_eval_env(monkeypatch):
    for var in ("MO_EVAL_JURY_LANES", "MO_EVAL_DECOMPOSED_REWARD",
                "MO_EVAL_COHERENCE_GATE", "MO_REFUTE_FINDINGS",
                "MO_REFUTE_FABRICATIONS", "MO_REFUTE_FP_CEILING"):
        monkeypatch.delenv(var, raising=False)


def _seed_passing_run(run_dir: Path) -> None:
    for name in ("test", "typecheck"):
        (run_dir / f"verifier_{name}.json").write_text(json.dumps({"pass": True}))


def test_eval_node_vetoes_when_the_validator_survives_plants(tmp_path, monkeypatch):
    """The precondition the plan named: _handle_eval really does reach the oracle.

    A perfect execution reward, a clean judge, and a validator caught reporting
    plants it was never given. The run must not score.
    """
    _clean_eval_env(monkeypatch)
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _seed_passing_run(run_dir)
    _campaign(run_dir, report_all=True)

    from mini_ork.cli.execute import _handle_eval
    rc, fr = _handle_eval(_make_ctx(run_dir, db))

    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["execution"]["r_exec"] == 1.0
    assert saved["process"]["refute"]["refute"] == "REFUTE_FAILED"
    assert saved["process"]["refute"]["applied"] is True
    assert saved["score"] == pytest.approx(0.0)
    assert saved["verdict"] == "needs_revision"

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT reward_value, reward_vector_json, reviewer_verdict "
        "FROM execution_traces WHERE run_id=?", ("run-eval-1",)).fetchone()
    con.close()
    assert row["reward_value"] == pytest.approx(0.0)
    assert row["reviewer_verdict"] == "needs_revision"
    vec = json.loads(row["reward_vector_json"])
    assert vec["r_exec"] == 1.0                    # the measurement survives
    assert vec["refute_survival"] == pytest.approx(1.0)   # and so does the oracle's


def test_eval_node_untouched_when_no_campaign_ran(tmp_path, monkeypatch):
    """The no-op proof: an identical run with no artifacts scores as it always did.

    This is what keeps the veto free for every recipe that never held a refute
    campaign — the majority of them.
    """
    _clean_eval_env(monkeypatch)
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _seed_passing_run(run_dir)

    from mini_ork.cli.execute import _handle_eval
    rc, _ = _handle_eval(_make_ctx(run_dir, db))

    assert rc == 0
    saved = json.loads((run_dir / "eval.json").read_text())
    # No artifacts → the oracle's own missing-inputs return, which the veto reads
    # as silence. `absent` would mean no oracle ran at all; here one ran and
    # declined to measure, and the next assertion is the one that matters.
    assert saved["process"]["refute"]["refute"] == "indeterminate"
    assert saved["process"]["refute"]["reason"] == "missing_inputs"
    assert saved["process"]["refute"].get("applied") is not True
    assert saved["score"] == pytest.approx(1.0)
    assert saved["verdict"] == "pass"
    assert "refute_survival" not in json.loads(
        sqlite3.connect(db).execute(
            "SELECT reward_vector_json FROM execution_traces WHERE run_id=?",
            ("run-eval-1",)).fetchone()[0])


def test_eval_node_untouched_by_a_grounded_validator(tmp_path, monkeypatch):
    """A campaign that ran and came back clean is evidence FOR the run, not a veto."""
    _clean_eval_env(monkeypatch)
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _seed_passing_run(run_dir)
    _campaign(run_dir, report_all=False)

    from mini_ork.cli.execute import _handle_eval
    rc, _ = _handle_eval(_make_ctx(run_dir, db))

    assert rc == 0
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["process"]["refute"]["refute"] == "validator_grounded"
    assert saved["score"] == pytest.approx(1.0)


def test_a_corrupt_manifest_does_not_sink_the_run(tmp_path, monkeypatch):
    """The eval node is advisory. A bad artifact degrades to silence, never error."""
    _clean_eval_env(monkeypatch)
    db = _migrated_db(tmp_path / "home")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _seed_passing_run(run_dir)
    (run_dir / "refute-findings.json").write_text("[]")
    (run_dir / "fabrications.json").write_text("{not json")

    from mini_ork.cli.execute import _handle_eval
    rc, fr = _handle_eval(_make_ctx(run_dir, db))

    assert (rc, fr) == (0, "done")
    saved = json.loads((run_dir / "eval.json").read_text())
    assert saved["process"]["refute"]["refute"] == "indeterminate"
    assert saved["score"] == pytest.approx(1.0)
