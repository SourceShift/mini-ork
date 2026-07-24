"""Unit tests for the gate-evaluator registry (SOLID M5, OCP)."""
import pytest

from mini_ork.gates import gate_registry as gr


def test_builtin_gate_types_registered():
    assert set(gr.GATE_EVALUATORS) == {
        "budget_gate", "human_gate", "scope_gate", "liveness_gate",
        "deployment_gate", "reviewer_gate", "deterministic_verifier", "custom"}


def test_unregistered_type_defers_and_refuses_registration(tmp_path):
    db = str(tmp_path / "state.db")
    with pytest.raises(ValueError, match="unknown gate_type"):
        gr.gate_register(db, "brand_new_type", "")


def test_register_gate_evaluator_activates_new_type(tmp_path):
    db = str(tmp_path / "state.db")
    gr.register_gate_evaluator(
        "brand_new_type", lambda condition, ctx, db_path, root: "pass")
    try:
        gid = gr.gate_register(db, "brand_new_type", "")
        assert gid
        assert gr.gate_evaluate(db, gid, "{}") == "pass"
    finally:
        gr.GATE_EVALUATORS.pop("brand_new_type", None)


def test_missing_gate_fails_closed(tmp_path):
    db = str(tmp_path / "state.db")
    gr.ensure_table(db)
    assert gr.gate_evaluate(db, "nonexistent", "{}") == "fail"


def test_builtin_evaluation_unchanged(tmp_path):
    db = str(tmp_path / "state.db")
    gid = gr.gate_register(db, "human_gate", "")
    assert gr.gate_evaluate(db, gid, "{}") == "defer"
