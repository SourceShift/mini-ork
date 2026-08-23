"""Unit tests for the node-handler registry (SOLID M3, OCP)."""
import json
import os

import mini_ork.cli.execute as ex
from mini_ork.context import context_env


def test_registry_covers_builtin_node_types():
    assert set(ex.NODE_HANDLER_REGISTRY) == {
        "researcher", "transform", "implementer", "reviewer", "verifier", "eval",
        "publisher", "rollback"}
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


def test_dispatch_node_publishes_into_run_context_layer(tmp_path, monkeypatch):
    """Bottleneck #1 writer flip: dispatch_node's node publishes dual-write —
    the contextvar layer carries the node identity while the node runs (probed
    from inside a handler, where the boundary is live), and the boundary around
    one node wipes it, so a second node in another run never sees stale
    bindings."""
    for key in ("MINI_ORK_RUN_DIR", "MO_NODE_ID", "MO_RESUME_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    rd = tmp_path / "run"
    rd.mkdir()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"objective": "o"}))
    fields = ("n1", "mystery_type", "do n1", "", "serial", "", "mystery_type", "")
    probe: dict[str, str] = {}

    def probe_handler(ctx):
        # runs INSIDE dispatch_node's boundary — the contextvar layer is live
        probe["node_id"] = context_env("MO_NODE_ID")
        probe["run_dir"] = context_env("MINI_ORK_RUN_DIR")
        return 0, "done"

    ex.register_node_handler("ctx_probe", probe_handler)
    try:
        rc, fr = ex.dispatch_node(
            fields[:1] + ("ctx_probe",) + fields[2:], root=os.getcwd(),
            run_dir=str(rd), plan_path=str(plan),
            task_class="generic", db="", run_id="r",
            dispatch_fn=lambda *a: (0, "ok"))
        assert (rc, fr) == (0, "done")
        # contextvar layer carried the node identity while the node ran …
        assert probe["node_id"] == "n1"
        assert probe["run_dir"] == str(rd)
        # … dual-write: the legacy os.environ layer carries it too
        assert os.environ["MO_NODE_ID"] == "n1"
    finally:
        ex.NODE_HANDLER_REGISTRY.pop("ctx_probe", None)

    # boundary exit wipes the contextvar layer (os.environ copies removed so
    # the only possible source left would be the contextvar)
    os.environ.pop("MO_NODE_ID", None)
    os.environ.pop("MINI_ORK_RUN_DIR", None)
    assert context_env("MO_NODE_ID", "wiped") == "wiped"
