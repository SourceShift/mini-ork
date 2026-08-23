"""Unit tests for mini_ork.context — the canonical env contract."""
import asyncio
import os
import threading

from mini_ork.context import (
    RunContext,
    apply_env_overrides,
    context_env,
    context_env_snapshot,
    node_env_overrides,
    publish_env,
    run_context_scope,
    scoped_environ,
)


def test_from_env_reads_run_identity(monkeypatch):
    monkeypatch.setenv("MINI_ORK_ROOT", "/r")
    monkeypatch.setenv("MINI_ORK_DB", "/r/state.db")
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-1")
    monkeypatch.delenv("MINI_ORK_RECIPE", raising=False)
    ctx = RunContext.from_env()
    assert ctx.root == "/r"
    assert ctx.db == "/r/state.db"
    assert ctx.run_id == "run-1"
    assert ctx.recipe == ""


def test_db_or_default_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", "/h")
    assert RunContext.from_env().db_or_default() == os.path.join("/h", "state.db")
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    assert RunContext.from_env().db_or_default() == os.path.join(".mini-ork", "state.db")


def test_task_class_default():
    assert RunContext().task_class_or_default() == "generic"
    assert RunContext(task_class="code_fix").task_class_or_default() == "code_fix"


def test_as_env_omits_empty_fields():
    env = RunContext(root="/r", run_id="x").as_env()
    assert env == {"MINI_ORK_ROOT": "/r", "MINI_ORK_RUN_ID": "x"}


def test_apply_publishes_non_empty(monkeypatch):
    target = {}
    RunContext(root="/r", run_dir="/rd").apply(env=target)
    assert target == {"MINI_ORK_ROOT": "/r", "MINI_ORK_RUN_DIR": "/rd"}


def test_child_env_merges_and_removes(monkeypatch):
    monkeypatch.setenv("KEEP", "1")
    monkeypatch.setenv("DROP", "x")
    child = RunContext(root="/r").child_env(DROP=None, NEW="y")
    assert child["KEEP"] == "1"
    assert child["MINI_ORK_ROOT"] == "/r"
    assert child["NEW"] == "y"
    assert "DROP" not in child
    # process env untouched
    assert os.environ["DROP"] == "x"


def test_node_env_overrides_removes_stale_resume():
    ov = node_env_overrides(node_id="n1", run_dir="/rd", dispatch_chain="a,b")
    assert ov["MO_NODE_ID"] == "n1"
    assert ov["MINI_ORK_RUN_DIR"] == "/rd"
    assert ov["MO_DISPATCH_CHAIN"] == "a,b"
    assert ov["MO_RESUME_SESSION_ID"] is None
    assert "MO_TARGET_CWD" not in ov  # untouched unless explicitly passed


def test_node_env_overrides_target_cwd_opt_in():
    ov = node_env_overrides(node_id="n", run_dir="/rd", target_cwd="/t")
    assert ov["MO_TARGET_CWD"] == "/t"


def test_apply_env_overrides_set_and_pop():
    env = {"A": "1", "B": "2"}
    apply_env_overrides({"A": "9", "B": None, "C": "3"}, env=env)
    assert env == {"A": "9", "C": "3"}


def test_scoped_environ_restores(monkeypatch):
    monkeypatch.setenv("SCOPED_A", "orig")
    monkeypatch.delenv("SCOPED_B", raising=False)
    with scoped_environ({"SCOPED_A": "temp", "SCOPED_B": "new"}):
        assert os.environ["SCOPED_A"] == "temp"
        assert os.environ["SCOPED_B"] == "new"
    assert os.environ["SCOPED_A"] == "orig"
    assert "SCOPED_B" not in os.environ


# ── Per-run isolation layer (contextvars) ────────────────────────────────────


def test_context_env_falls_back_to_os_environ(monkeypatch):
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-123")
    # nothing bound in this context → transparent fall-through to process env
    assert context_env("MINI_ORK_RUN_ID") == "run-123"
    assert context_env("NEVER_SET_XYZ", "dflt") == "dflt"


def test_run_context_scope_binds_and_restores(monkeypatch):
    monkeypatch.delenv("MO_NODE_ID", raising=False)
    with run_context_scope({"MO_NODE_ID": "n1"}):
        assert context_env("MO_NODE_ID") == "n1"
        # binding lives in the contextvar, NOT the process env
        assert "MO_NODE_ID" not in os.environ
    assert context_env("MO_NODE_ID", "gone") == "gone"


def test_run_context_scope_nests():
    with run_context_scope({"MO_NODE_ID": "outer", "MO_DISPATCH_CHAIN": "a"}):
        assert context_env("MO_NODE_ID") == "outer"
        with run_context_scope({"MO_NODE_ID": "inner"}):
            assert context_env("MO_NODE_ID") == "inner"
            # sibling key from the outer scope still visible
            assert context_env("MO_DISPATCH_CHAIN") == "a"
        assert context_env("MO_NODE_ID") == "outer"


def test_context_env_masks_none_binding(monkeypatch):
    # a stale value leaked in the process env must not resurface when masked
    monkeypatch.setenv("MO_RESUME_SESSION_ID", "stale-sess")
    with run_context_scope({"MO_RESUME_SESSION_ID": None}):
        assert context_env("MO_RESUME_SESSION_ID", "fresh") == "fresh"
    # scope exit restores the fall-through to the process env
    assert context_env("MO_RESUME_SESSION_ID") == "stale-sess"


def test_run_context_scope_isolates_across_threads(monkeypatch):
    monkeypatch.delenv("MO_NODE_ID", raising=False)
    results: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        with run_context_scope({"MO_NODE_ID": name}):
            barrier.wait()  # hold both scopes live simultaneously
            results[name] = context_env("MO_NODE_ID")

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == {"a": "a", "b": "b"}
    assert "MO_NODE_ID" not in os.environ  # global env never touched


def test_run_context_scope_isolates_across_asyncio_tasks():
    async def worker(name: str) -> str:
        with run_context_scope({"MO_NODE_ID": name}):
            await asyncio.sleep(0)  # yield so the two tasks interleave
            return context_env("MO_NODE_ID")

    async def main() -> list[str]:
        return list(await asyncio.gather(worker("task-a"), worker("task-b")))

    assert asyncio.run(main()) == ["task-a", "task-b"]


# ── Writer half: publish_env dual-write + child-env snapshot ─────────────────


def test_publish_env_dual_writes_and_accumulates(monkeypatch):
    # pre-seed so monkeypatch teardown restores os.environ regardless of the
    # publish_env legacy writes inside the test
    monkeypatch.setenv("MO_PUB_A", "seed")
    monkeypatch.delenv("MO_PUB_B", raising=False)

    with run_context_scope({}):
        publish_env({"MO_PUB_A": "v1"})
        publish_env({"MO_PUB_B": "v2"})  # accumulates, doesn't reset A
        assert context_env("MO_PUB_A") == "v1"
        assert context_env("MO_PUB_B") == "v2"
        # legacy layer still written (byte-for-byte old executor semantics)
        assert os.environ["MO_PUB_A"] == "v1"
        assert os.environ["MO_PUB_B"] == "v2"

    # boundary exit wipes the contextvar layer: B's os.environ copy removed, so
    # the only place "v2" could still live is the contextvar — and it's gone
    os.environ.pop("MO_PUB_B", None)
    assert context_env("MO_PUB_B", "gone") == "gone"
    # … but the os.environ write persists (legacy leak-forever semantics kept),
    # and context_env's fallback observes it exactly as legacy readers do
    assert os.environ["MO_PUB_A"] == "v1"
    assert context_env("MO_PUB_A") == "v1"


def test_publish_env_none_removes_from_both_layers(monkeypatch):
    monkeypatch.setenv("MO_PUB_MASK", "stale")
    with run_context_scope({}):
        publish_env({"MO_PUB_MASK": None})
        assert context_env("MO_PUB_MASK", "masked") == "masked"
        assert "MO_PUB_MASK" not in os.environ


def test_context_env_snapshot_overlays_and_masks(monkeypatch):
    monkeypatch.setenv("SNAP_PLAIN", "from-env")
    monkeypatch.setenv("SNAP_MASKED", "stale-env")
    monkeypatch.delenv("SNAP_BOUND", raising=False)

    with run_context_scope({"SNAP_BOUND": "ctx", "SNAP_MASKED": None}):
        snap = context_env_snapshot()
        assert snap["SNAP_BOUND"] == "ctx"       # binding wins
        assert "SNAP_MASKED" not in snap         # masked → removed
        assert snap["SNAP_PLAIN"] == "from-env"  # unbound → process env
    # with nothing bound the snapshot is a plain os.environ copy
    plain = context_env_snapshot()
    assert plain["SNAP_PLAIN"] == "from-env"
    assert "SNAP_BOUND" not in plain


def test_two_runs_isolated_through_publish_env(monkeypatch):
    """The payoff property: two concurrent runs in one process, each publishing
    through the real writer path, never observe one another's run/node vars —
    even though the legacy os.environ layer IS raced (last writer wins)."""
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.delenv("MO_NODE_ID", raising=False)
    barrier = threading.Barrier(2)
    results: dict[str, dict[str, str]] = {}

    def run(name: str) -> None:
        # mirrors the executor: run-level scope boundary, publish inside
        with run_context_scope({}):
            publish_env({"MINI_ORK_RUN_DIR": f"/runs/{name}"})
            publish_env({"MO_NODE_ID": f"{name}-n1"})
            barrier.wait()  # both runs' bindings live simultaneously
            results[name] = {
                "run_dir": context_env("MINI_ORK_RUN_DIR"),
                "node_id": context_env("MO_NODE_ID"),
                "child_run_dir": context_env_snapshot()["MINI_ORK_RUN_DIR"],
            }

    threads = [threading.Thread(target=run, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results["a"] == {
        "run_dir": "/runs/a", "node_id": "a-n1", "child_run_dir": "/runs/a"}
    assert results["b"] == {
        "run_dir": "/runs/b", "node_id": "b-n1", "child_run_dir": "/runs/b"}
    # the legacy layer was raced (one of the two) but never polluted with a mix
    assert os.environ["MINI_ORK_RUN_DIR"] in {"/runs/a", "/runs/b"}
