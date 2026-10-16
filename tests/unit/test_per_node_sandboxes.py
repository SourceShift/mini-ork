"""Per-node cloud sandboxes — one sandbox per ``(run_id, node_id)``.

Covers :mod:`mini_ork.remote.providers.run_nodes`, the ``node_id`` threading
through the sandbox adapters' shared naming/labelling helpers, and the two
integration seams that make isolation reachable at all:

  * the run-scoped workspace registry, whose key gains the node id — the only
    place that can deliver it (the registry caches one workspace per key and
    calls ``up()`` once, so a per-node provider under the run's single key would
    hand every node the first node's sandbox);
  * ``RemoteWorkspace.ensure_node``, which routes to the per-node acquire when
    the policy says so and to the historical run-level one otherwise.

The default is OFF and stays byte-identical: every assertion about the shared
path is here to pin that, not to exercise a new one.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from mini_ork.remote.providers import run_nodes
from mini_ork.remote.providers.base import (
    NodeEndpoint,
    register_node_provider,
)
from mini_ork.remote.providers.sandbox_node import run_labels, sandbox_name
from mini_ork.runtime import workspace_session as ws_module


# --- naming + labels (the adapters' shared helper) ----------------------------


def test_sandbox_name_without_a_node_is_unchanged():
    """Back-compat: the run-level name is exactly what it was, so a run already
    live on a cloud still finds its sandbox by name after this change."""
    assert sandbox_name("run-1791911354-79155") == "mo-run-1791911354-79155"
    assert sandbox_name("Run_ABC") == "mo-run-abc"


def test_sandbox_name_is_distinct_per_node():
    assert sandbox_name("r1", "implementer_1") != sandbox_name("r1", "reviewer_1")
    assert sandbox_name("r1", "n1") != sandbox_name("r1", "")


def test_sandbox_name_bounded_and_collision_free_when_truncated():
    """Two long node ids sharing a prefix must not collapse onto one sandbox —
    that would silently un-isolate exactly the nodes at risk."""
    prefix = "node-" + "x" * 80
    a, b = f"{prefix}-alpha", f"{prefix}-beta"
    name_a, name_b = sandbox_name("r1", a), sandbox_name("r1", b)
    assert name_a != name_b
    assert len(name_a) <= 63 and len(name_b) <= 63


def test_sandbox_name_separates_ids_that_slug_alike():
    """The name IS the idempotency key, so two node ids that slug to the same
    string must not share one — the second node would attach to the first's
    sandbox, which is precisely the isolation being asked for."""
    pairs = [("implementer:0", "implementer_0"), ("a/b", "a-b"),
             ("a b", "a-b"), ("Review", "review")]
    for left, right in pairs:
        assert sandbox_name("r1", left) != sandbox_name("r1", right), (left, right)
    # Distinct runs stay distinct for the same node.
    assert sandbox_name("r1", "n1") != sandbox_name("r2", "n1")


def test_sandbox_name_stays_bounded_for_an_absurd_node_id():
    name = sandbox_name("r1", "n" * 4000)
    assert len(name) <= 63
    assert name != sandbox_name("r1", "n" * 4001)   # boundedness must not re-collide


def test_run_labels_adds_the_node_label_only_for_a_node():
    base = run_labels("daytona", "r1")
    assert "mo.node" not in base
    labelled = run_labels("daytona", "r1", "implementer_1")
    assert labelled["mo.node"] == "implementer_1"
    # The run-level labels are untouched — only the node dimension is added.
    assert {k: v for k, v in labelled.items() if k != "mo.node"} == base


def test_the_node_label_is_always_a_legal_label_value():
    """These labels go into the cloud's create call, so an invalid character
    fails the sandbox before it boots — and ``implementer:0`` is a real id."""
    import re

    for node in ("implementer:0", "planner/code-impact", "a b", "n" * 200):
        value = run_labels("daytona", "r1", node)["mo.node"]
        assert re.fullmatch(r"[A-Za-z0-9._-]+", value), value
        assert len(value) <= 63


# --- policy -------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("", False), ("0", False), ("no", False), ("maybe", False),
])
def test_isolation_enabled_fails_closed(value, expected):
    env = {"MO_NODE_ISOLATION": value}
    assert run_nodes.isolation_enabled(env) is expected


def test_isolation_enabled_absent_is_off():
    assert run_nodes.isolation_enabled({}) is False


def test_isolated_node_needs_both_the_flag_and_a_node():
    both = {"MO_NODE_ISOLATION": "1", "MO_NODE_ID": "implementer_1"}
    assert run_nodes.isolated_node(both) == "implementer_1"
    # A node without the flag is a caller asking for the historical shared one.
    assert run_nodes.isolated_node({"MO_NODE_ID": "implementer_1"}) == ""
    # The flag with no node has no node to isolate; it falls back rather than
    # stranding the dispatch with nowhere to run.
    assert run_nodes.isolated_node({"MO_NODE_ISOLATION": "1"}) == ""


def test_safe_node_id_leaves_a_plain_id_alone():
    """The common case must round-trip byte-for-byte: a node's sandbox
    directory is then spelled exactly like the ``agent-<node>.stream.jsonl``
    beside it, which is what makes a run dir debuggable."""
    for plain in ("implementer_1", "reviewer", "goal_apply", "n1"):
        assert run_nodes.safe_node_id(plain) == plain


def test_safe_node_id_matches_the_run_dirs_node_naming():
    """A mangled id starts with the artifact index's spelling (``:`` and ``/``
    both become ``_``) — the digest is appended by this function, so the shared
    prefix keeps the two readable as the same node."""
    assert run_nodes.safe_node_id("implementer:0").startswith("implementer_0-")
    assert run_nodes.safe_node_id("planner/code-impact").startswith("planner_code-impact-")


def test_safe_node_id_cannot_leave_the_run_dir():
    for hostile in ("../../etc/passwd", "/etc/passwd", "a/../../b", "..\\..\\win", "a/b"):
        sid = run_nodes.safe_node_id(hostile)
        assert "/" not in sid and "\\" not in sid
        # One segment, in the run dir's node area — never the parent of it.
        assert run_nodes.node_dir("/run", hostile).parent == Path("/run/.nodes")


def test_safe_node_id_refuses_an_id_that_is_not_a_name():
    for unusable in ("", "   ", "\t", "..", ".", "..."):
        # Refusing is the point: a mangled id could collide with a sibling's
        # directory, and sharing a directory IS sharing a sandbox.
        with pytest.raises(ValueError):
            run_nodes.safe_node_id(unusable)


def test_safe_node_id_is_injective_where_it_mangles():
    """The security property, stated directly: two node ids that sanitize alike
    must not land on one directory."""
    assert run_nodes.safe_node_id("implementer:0") != run_nodes.safe_node_id("implementer_0")
    assert run_nodes.safe_node_id("a/b") != run_nodes.safe_node_id("a-b")


def test_safe_node_id_is_idempotent_so_disk_names_round_trip():
    """``list_node_ids`` reads directory names off disk and hands them back, so
    re-mapping a name this function produced must be a no-op — otherwise every
    teardown looks for a directory that does not exist."""
    for node in ("implementer_1", "implementer:0", "../../etc/passwd", ".._.._etc_passwd"):
        once = run_nodes.safe_node_id(node)
        assert run_nodes.safe_node_id(once) == once


def test_safe_node_id_stays_bounded_for_an_absurd_id():
    sid = run_nodes.safe_node_id("x" * 4000 + ":0")
    assert len(sid) <= run_nodes._MAX_NODE_ID


def test_node_dir_stays_inside_the_run_dir(tmp_path):
    nd = run_nodes.node_dir(tmp_path, "implementer_1")
    assert nd == Path(tmp_path) / ".nodes" / "implementer_1"


def test_list_node_ids_reads_the_filesystem_not_a_flag(tmp_path):
    assert run_nodes.list_node_ids(tmp_path) == []
    for node in ("reviewer_1", "implementer_1"):
        nd = run_nodes.node_dir(tmp_path, node)
        nd.mkdir(parents=True)
        (nd / ".node-provider.json").write_text("{}")
    # A directory with no record is not a node that started.
    (run_nodes.nodes_dir(tmp_path) / "empty").mkdir()
    assert run_nodes.list_node_ids(tmp_path) == ["implementer_1", "reviewer_1"]


# --- the three verbs, against a fake provider ---------------------------------


class FakeProvider:
    """A node provider that mints one sandbox per ``(run_id, node_id)``.

    ``handles`` is its sandbox inventory: ``create`` appends, ``release``
    removes. ``attach`` only ever returns a sandbox still in the inventory, so
    the TTL-expired case is modeled by emptying it.
    """

    name = "fakenode"

    def __init__(self) -> None:
        self.created: list[str] = []
        self.released: list[str] = []
        self.attach_calls = 0
        self.handles: list[str] = []
        # Monotonic, never a function of the current inventory: a cloud that
        # mints a replacement sandbox hands back a NEW handle even when the
        # name matches the one that expired, and the test of "a vanished
        # sandbox is replaced" is worthless if the fake cannot express that.
        self._seq = 0

    def create(self, *, run_id: str, profile: Any, env: Any, engine_root: str,
               node_id: str = "") -> NodeEndpoint:
        handle = f"sbx-{run_id}-{node_id or 'run'}-{self._seq}"
        self._seq += 1
        self.handles.append(handle)
        self.created.append(handle)
        return NodeEndpoint(name=handle, url=f"https://{handle}", token="t",
                            handle=handle, provider=self.name)

    def attach(self, handle: str, *, env: Any) -> NodeEndpoint | None:
        self.attach_calls += 1
        if handle not in self.handles:
            return None
        return NodeEndpoint(name=handle, url=f"https://{handle}", token="t",
                            handle=handle, provider=self.name)

    def release(self, handle: str, *, env: Any) -> None:
        self.released.append(handle)
        if handle in self.handles:
            self.handles.remove(handle)

    def reap(self, *, max_age_s: int, dry_run: bool, env: Any) -> list[str]:
        return []


@pytest.fixture
def provider(monkeypatch):
    prov = FakeProvider()
    register_node_provider(prov.name, lambda: prov)
    # A contended lock must fail fast in a test, never wait the 900 s default.
    monkeypatch.setenv("MO_NODE_PROVISION_WAIT_S", "2")
    yield prov


def test_one_sandbox_per_node(provider, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    a = run_nodes.acquire_node(provider, run_id="r1", node_id="implementer_1",
                               run_dir=run_dir, profile=None, env={}, engine_root="")
    b = run_nodes.acquire_node(provider, run_id="r1", node_id="reviewer_1",
                               run_dir=run_dir, profile=None, env={}, engine_root="")
    assert a.handle != b.handle
    assert len(provider.created) == 2
    assert run_nodes.list_node_ids(run_dir) == ["implementer_1", "reviewer_1"]


def test_second_acquire_attaches_never_creates_again(provider, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    first = run_nodes.acquire_node(provider, run_id="r1", node_id="n1",
                                   run_dir=run_dir, profile=None, env={}, engine_root="")
    second = run_nodes.acquire_node(provider, run_id="r1", node_id="n1",
                                    run_dir=run_dir, profile=None, env={}, engine_root="")
    assert first.handle == second.handle
    assert len(provider.created) == 1
    assert provider.attach_calls == 1


def test_a_vanished_sandbox_is_replaced_and_the_old_handle_recorded(provider, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    first = run_nodes.acquire_node(provider, run_id="r1", node_id="n1",
                                   run_dir=run_dir, profile=None, env={}, engine_root="")
    provider.handles.clear()          # TTL-expired / deleted out from under us
    second = run_nodes.acquire_node(provider, run_id="r1", node_id="n1",
                                    run_dir=run_dir, profile=None, env={}, engine_root="")
    assert second.handle != first.handle
    assert len(provider.created) == 2
    record = run_nodes.read_node_record(run_dir, "n1")
    assert record is not None and record["replaced"] == first.handle
    assert record["node_id"] == "n1"


def test_acquire_without_a_node_id_refuses_rather_than_sharing(provider, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(ValueError, match="node_id"):
        run_nodes.acquire_node(provider, run_id="r1", node_id="", run_dir=run_dir,
                               profile=None, env={}, engine_root="")
    assert provider.created == []


def test_attach_node_is_none_without_a_record(provider, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert run_nodes.attach_node(run_dir, {}, "n1") is None


def test_release_all_nodes_frees_every_node(provider, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    for node in ("implementer_1", "reviewer_1", "publisher"):
        run_nodes.acquire_node(provider, run_id="r1", node_id=node, run_dir=run_dir,
                               profile=None, env={}, engine_root="")
    assert len(provider.handles) == 3
    assert run_nodes.release_all_nodes(run_dir, {}) == 3
    assert provider.handles == []
    assert run_nodes.list_node_ids(run_dir) == []
    # Idempotent: a second sweep frees nothing and does not raise.
    assert run_nodes.release_all_nodes(run_dir, {}) == 0


def test_release_all_nodes_is_a_noop_for_a_run_that_never_isolated(provider, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert run_nodes.release_all_nodes(run_dir, {}) == 0
    assert run_nodes.release_all_nodes("", {}) == 0


# --- session registry: the key is the isolation lever -------------------------


class CountingBackend:
    """A ``Workspace`` that records its own ``up()``/``down()`` calls."""

    def __init__(self, **kwargs: Any) -> None:
        self.up_calls = 0
        self.down_calls = 0

    def up(self) -> None:
        self.up_calls += 1

    def down(self) -> None:
        self.down_calls += 1

    def spawn(self, argv, *, stdin, timeout, env, cwd):  # noqa: ARG002
        return 0, "", ""

    def exec(self, cmd, *, cwd, timeout):  # noqa: ARG002
        return 0, ""

    def put(self, content):  # noqa: ARG002
        return "/tmp/x"

    def get(self, path):  # noqa: ARG002
        return ""


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    """Each call to ``get_run_session`` gets a FRESH counting backend, so the
    test can see how many sessions — i.e. how many sandboxes — were minted."""
    from mini_ork.runtime import sandbox

    name = f"fake-{uuid.uuid4().hex[:8]}"
    minted: list[CountingBackend] = []

    def _resolve(backend_name: str, *, env: Any, cwd: Any) -> CountingBackend:
        backend = CountingBackend()
        minted.append(backend)
        return backend

    monkeypatch.setattr(ws_module, "_resolve_backend_workspace", _resolve)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    yield name, run_dir, minted
    try:
        ws_module._reset_registry()
    except Exception:  # noqa: BLE001
        pass
    sandbox._WORKSPACE_BACKENDS.pop(name, None)


def test_isolation_off_keeps_one_shared_session(sessions):
    name, run_dir, minted = sessions
    env = {"MINI_ORK_RUN_DIR": str(run_dir)}
    first = ws_module.get_run_session("r1", name, env=env)
    for _ in range(3):
        assert ws_module.get_run_session("r1", name, env=env) is first
    assert len(minted) == 1


def test_isolation_on_gives_each_node_its_own_session(sessions):
    name, run_dir, minted = sessions
    base = {"MINI_ORK_RUN_DIR": str(run_dir), "MO_NODE_ISOLATION": "1"}
    a = ws_module.get_run_session("r1", name, env={**base, "MO_NODE_ID": "implementer_1"})
    b = ws_module.get_run_session("r1", name, env={**base, "MO_NODE_ID": "reviewer_1"})
    assert a is not b
    assert len(minted) == 2
    # Each node reuses its own session (retries, fallback lanes) — no third boot.
    assert ws_module.get_run_session(
        "r1", name, env={**base, "MO_NODE_ID": "implementer_1"}) is a
    assert len(minted) == 2
    # One marker per node, plus none shared: a sibling's marker must survive
    # this session's teardown.
    names = sorted(p.name for p in ws_module.session_marker_paths(run_dir))
    assert names == [".workspace-session.implementer_1.json",
                     ".workspace-session.reviewer_1.json"]


def test_node_less_dispatch_keeps_the_run_level_session(sessions):
    """A check that runs between nodes has no node to isolate and must still
    land somewhere — the run-level sandbox, exactly as before."""
    name, run_dir, minted = sessions
    base = {"MINI_ORK_RUN_DIR": str(run_dir), "MO_NODE_ISOLATION": "1"}
    shared = ws_module.get_run_session("r1", name, env=base)
    node = ws_module.get_run_session("r1", name, env={**base, "MO_NODE_ID": "n1"})
    assert shared is not node
    assert len(minted) == 2
    assert ws_module.session_marker_path(run_dir).is_file()
    assert ws_module.session_marker_path(run_dir, "n1").is_file()


# --- marker plumbing ----------------------------------------------------------


def test_session_marker_paths_prefers_the_run_level_marker(tmp_path):
    (tmp_path / ".workspace-session.json").write_text(json.dumps({"run_id": "r1"}))
    (tmp_path / ".workspace-session.n1.json").write_text(json.dumps({"node_id": "n1"}))
    paths = ws_module.session_marker_paths(tmp_path)
    assert [p.name for p in paths] == [".workspace-session.json",
                                       ".workspace-session.n1.json"]
    assert ws_module.read_session_marker(tmp_path)["run_id"] == "r1"


def test_session_marker_paths_ignores_the_atomic_write_tempfile(tmp_path):
    (tmp_path / ".workspace-session.n1.json.tmp").write_text("{}")
    assert ws_module.session_marker_paths(tmp_path) == []
    assert ws_module.read_session_marker(tmp_path) == {}


def test_read_session_marker_falls_back_to_a_node_marker(tmp_path):
    (tmp_path / ".workspace-session.n1.json").write_text(json.dumps({"node_id": "n1"}))
    assert ws_module.read_session_marker(tmp_path) == {"node_id": "n1"}
    assert ws_module.read_session_marker(tmp_path / "does-not-exist") == {}


# --- RemoteWorkspace: the resolution seam -------------------------------------


def _workspace(provider_name: str, run_dir: Path, env: dict[str, str]):
    from mini_ork.runtime.backends.remote import RemoteWorkspace, _NodeRef

    return RemoteWorkspace(
        node=_NodeRef(name="", url="", token="", max_sessions=1),
        run_id="r1",
        image="alpine:latest",
        drive_root=str(run_dir),
        run_dir=str(run_dir),
        node_provider=provider_name,
        provider_env=env,
    )


def test_two_nodes_get_two_sandboxes_through_the_workspace(provider, tmp_path):
    # No sandbox flags in the env: these workspaces must be told the node
    # explicitly, which is what the session registry does per dispatch.
    env = {"MO_NODE_ISOLATION": "1"}
    a = _workspace(provider.name, tmp_path, {**env, "MO_NODE_ID": "implementer_1"})
    b = _workspace(provider.name, tmp_path, {**env, "MO_NODE_ID": "reviewer_1"})
    ep_a, ep_b = a.ensure_node(create=True), b.ensure_node(create=True)
    assert ep_a.handle != ep_b.handle
    assert len(provider.created) == 2
    assert a.node_isolation() == "implementer_1"


def test_isolation_off_routes_to_the_run_level_sandbox(provider, tmp_path):
    a = _workspace(provider.name, tmp_path, {})
    b = _workspace(provider.name, tmp_path, {})
    ep_a, ep_b = a.ensure_node(create=True), b.ensure_node(create=True)
    assert ep_a.handle == ep_b.handle          # the run's one sandbox, as before
    assert a.node_isolation() == ""
    assert len(provider.created) == 1


def test_teardown_attach_never_creates(provider, tmp_path):
    """``ensure_node(create=False)`` is the kill_run / run-end path: it must
    only ever attach, so a teardown can never mint a sandbox."""
    env = {"MO_NODE_ISOLATION": "1", "MO_NODE_ID": "implementer_1"}
    ws = _workspace(provider.name, tmp_path, env)
    assert ws.ensure_node(create=False) is None
    assert provider.created == []
