"""Unit tests for mini_ork.context — the canonical env contract."""
import asyncio
import os
import threading

from mini_ork.context import (
    RunContext,
    apply_env_overrides,
    context_env,
    node_env_overrides,
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
