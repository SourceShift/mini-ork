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


# ── Native oracle-gate evaluators (WS4 bash-removal) ──────────────────────────


def test_native_gate_names_registered():
    from mini_ork.gates import native_gates

    assert set(native_gates.NATIVE_GATE_EVALUATORS) == {
        "coalition", "liveness", "panel-health", "stability",
        "synthesis-promote"}


def test_resolve_native_evaluator_sentinel_and_script_path():
    from mini_ork.gates import native_gates

    for name in native_gates.NATIVE_GATE_EVALUATORS:
        assert native_gates.resolve_native_evaluator(f"native:{name}") is not None
    # Legacy script-path conditions (any directory prefix) resolve natively.
    assert native_gates.resolve_native_evaluator(
        "/repo/gates/coalition.sh") is not None
    assert native_gates.resolve_native_evaluator(
        "relative/gates/panel-health.sh") is not None
    # Unknown sentinel names and non-oracle paths do NOT resolve.
    assert native_gates.resolve_native_evaluator("native:nope") is None
    assert native_gates.resolve_native_evaluator("/tmp/custom.sh") is None
    assert native_gates.resolve_native_evaluator("") is None


def test_register_native_gate_activates_new_sentinel(tmp_path):
    from mini_ork.gates import native_gates

    db = str(tmp_path / "state.db")
    native_gates.register_native_gate(
        "probe-gate", lambda condition, ctx, db_path, root: "pass")
    try:
        gid = gr.gate_register(db, "custom", "native:probe-gate")
        assert gid
        assert gr.gate_evaluate(db, gid, "{}") == "pass"
    finally:
        native_gates.NATIVE_GATE_EVALUATORS.pop("probe-gate", None)


def test_native_evaluator_exception_defers(tmp_path):
    from mini_ork.gates import native_gates

    def _boom(condition, ctx, db_path, root):
        raise RuntimeError("boom")

    native_gates.register_native_gate("boom-gate", _boom)
    try:
        db = str(tmp_path / "state.db")
        gid = gr.gate_register(db, "custom", "native:boom-gate")
        assert gr.gate_evaluate(db, gid, "{}") == "defer"
    finally:
        native_gates.NATIVE_GATE_EVALUATORS.pop("boom-gate", None)
