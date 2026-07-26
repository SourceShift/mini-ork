"""Unit tests for the node-handler registry (SOLID M3, OCP)."""
import json
import os

import mini_ork.cli.execute as ex


def test_registry_covers_builtin_node_types():
    assert set(ex.NODE_HANDLER_REGISTRY) == {
        "researcher", "transform", "implementer", "reviewer", "verifier", "publisher", "rollback"}
    assert set(ex.EARLY_NODE_HANDLERS) == {"planner", "reflector"}


def test_unknown_node_type_falls_through(tmp_path, monkeypatch):
    """The bash catch-all: unregistered node types return (0, 'done')."""
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    rd = tmp_path / "run"
    rd.mkdir()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"objective": "o"}))
    rc, fr = ex.dispatch_node(
        ("n1", "mystery_type", "do n1", "", "serial", "", "mystery_type", ""),
        root=os.getcwd(), run_dir=str(rd), plan_path=str(plan),
        task_class="generic", db="", run_id="r",
        dispatch_fn=lambda *a: (0, "ok"))
    assert (rc, fr) == (0, "done")


def test_register_node_handler_main_phase(tmp_path, monkeypatch):
    """A new node type dispatches through the registry — no executor edit."""
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    rd = tmp_path / "run"
    rd.mkdir()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"objective": "o"}))
    seen = {}

    def custom_handler(ctx):
        seen["node_id"] = ctx.node_id
        seen["lane"] = ctx.lane
        return 0, "done"

    ex.register_node_handler("custom_worker", custom_handler)
    try:
        rc, fr = ex.dispatch_node(
            ("cw1", "custom_worker", "do cw1", "", "serial", "", "custom_worker", ""),
            root=os.getcwd(), run_dir=str(rd), plan_path=str(plan),
            task_class="generic", db="", run_id="r",
            dispatch_fn=lambda *a: (0, "ok"))
        assert (rc, fr) == (0, "done")
        assert seen["node_id"] == "cw1"
        assert seen["lane"]  # policy-routed lane reached the handler
    finally:
        ex.NODE_HANDLER_REGISTRY.pop("custom_worker", None)


def test_register_implementer_submode():
    ex.register_implementer_submode("my-recipe", "fan_out", "results.json", "my-recipe/lib/fan.py")
    try:
        assert ex._IMPLEMENTER_SUBMODES[("my-recipe", "fan_out")] == (
            "results.json", "my-recipe/lib/fan.py")
    finally:
        ex._IMPLEMENTER_SUBMODES.pop(("my-recipe", "fan_out"), None)
