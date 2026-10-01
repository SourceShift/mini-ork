"""End-to-end test for ``RemoteWorkspace`` tree sync through the in-process
node-agent (remote-nodes-07 kickoff §"Acceptance" last bullet:

  "Through the in-process node-agent, a fake 'agent' spawn
  (``sh -c 'echo x >> file'``) produces that change in the local checkout."

Pattern copied from ``test_workspace_conformance.py``: ``TestClient`` +
``runtime="host"`` so the suite has no docker daemon requirement. Drives
the REAL ``RemoteWorkspace`` -> ``tree_sync.materialize`` /
``tree_sync.apply_delta`` path, not only the new module in isolation
(review-bar bullet 1).
"""
from __future__ import annotations

import os
import subprocess

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
from fastapi.testclient import TestClient  # noqa: E402

from mini_ork.remote.node_agent.app import create_app  # noqa: E402

NODE_TOKEN = "supersecret-tree-sync-token"


@pytest.fixture
def node_agent(tmp_path, monkeypatch):
    """Spin up an in-process node-agent (``host`` runtime) and wire its
    transport into a real ``RemoteWorkspace``. Returns
    ``(client, workspace, target_repo)`` where ``target_repo`` is the
    LOCAL git repo whose tree is mirrored on the node-agent."""
    monkeypatch.setenv("MO_NODE_TOKEN", NODE_TOKEN)
    monkeypatch.setenv("MO_REMOTE_ALLOW_DIRTY_ENGINE", "1")
    state_dir = tmp_path / "node-agent-state"
    state_dir.mkdir()

    # Build a LOCAL git repo on the host. We ``git init`` here so the
    # target root is a real repo (kickoff §1: apply_delta precondition
    # needs HEAD^{tree}).
    target = tmp_path / "target"
    target.mkdir()
    _git(str(target), "init", "-q", "--initial-branch=main")
    _git(str(target), "config", "user.email", "e2e@test")
    _git(str(target), "config", "user.name", "e2e")
    (target / "tracked.txt").write_text("tracked\n")
    _git(str(target), "add", "tracked.txt")
    _git(str(target), "commit", "-q", "-m", "init")

    client = TestClient(
        create_app(state_dir=state_dir, token_env="MO_NODE_TOKEN", runtime="host")
    )
    health = client.get("/v1/health")
    assert health.status_code == 200

    from mini_ork.runtime.backends.remote import (  # noqa: E402
        RemoteWorkspace,
        _NodeRef,
    )
    import mini_ork.runtime.backends.remote as _remote_mod  # noqa: E402

    base_url = str(client.base_url).rstrip("/")
    ws = RemoteWorkspace(
        node=_NodeRef(name="<in-process>", url=base_url, token=NODE_TOKEN, max_sessions=1),
        run_id="tree-sync-e2e",
        image="alpine:latest",
        drive_root=str(tmp_path),
        engine_root=os.environ.get("MINI_ORK_ROOT") or os.getcwd(),
        target_root=str(target),
        token_env="MO_NODE_TOKEN",
        retries=2,
    )

    def _stub_request(method, path, *, body=None, content_type=None, query=None):
        from urllib.parse import urlencode
        url = path
        if query:
            url += "?" + urlencode(query)
        resp = client.request(
            method=method, url=url, content=body,
            headers={
                "Authorization": f"Bearer {NODE_TOKEN}",
                **({"Content-Type": content_type} if content_type else {}),
            },
        )
        if 500 <= resp.status_code < 600:
            raise RuntimeError(f"server transient {resp.status_code}")
        if resp.status_code >= 400:
            from mini_ork.runtime.backends.remote import RemoteUnavailableError
            raise RemoteUnavailableError(
                f"node-agent {method} {path} -> HTTP {resp.status_code}"
            )
        return resp.content

    ws._request = _stub_request  # type: ignore[method-assign]
    ws._token = lambda: NODE_TOKEN  # type: ignore[method-assign]
    monkeypatch.setattr(_remote_mod, "_DEFAULT_BACKOFF_S", 0.0)
    return client, ws, target


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        check=False, timeout=60,
    )


def test_remote_spawn_round_trip(node_agent):
    """A fake 'agent' spawn on the node-agent writes a file in the
    REMOTE worktree; sync-down lands the same file in the LOCAL checkout
    as an uncommitted change. This is the kickoff's last acceptance bullet
    AND the "real seam" review-bar gate (kickoff §139–154 bullet 1).
    """
    _client, ws, target = node_agent
    # 1) up() uploads the local target tree to the node-agent.
    ws.up()
    # 2) Agent side: write a new file under /workspace/target.
    rc, _, _ = ws.spawn(  # type: ignore[unused-ignore]  # noqa: ARG001
        ["/bin/sh", "-c", "echo remote-edit >> from_agent.txt"],
        stdin="", timeout=30, env={"PATH": os.environ.get("PATH", "")},
        cwd="/workspace/target",
    )
    assert rc == 0
    # 3) The local checkout must show the new file as UNCOMMITTED.
    assert (target / "from_agent.txt").exists(), (
        "sync-down did not land the agent's edit in the local checkout"
    )
    status = _git(str(target), "status", "--porcelain").stdout
    assert "from_agent.txt" in status


def test_remote_local_conflict_raises(node_agent):
    """A local edit made WHILE a remote node holds the replica (between the
    spawn's sync-up and sync-down) raises SyncConflictError, and the user's
    edit survives. An edit made BETWEEN spawns is not a conflict — the next
    spawn's sync-up carries it to the replica. The fake agent below edits the
    replica and, standing in for the user, the local checkout at the same time
    (host runtime: both are on this machine)."""
    _client, ws, target = node_agent
    ws.up()
    edited = "USER EDIT — must survive\n"
    user_edit = f"printf %s '{edited.strip()}' > {target}/tracked.txt; echo >> {target}/tracked.txt"
    from mini_ork.remote import tree_sync
    with pytest.raises(tree_sync.SyncConflictError):
        ws.spawn(
            ["/bin/sh", "-c", f"echo remote > from_agent.txt; {user_edit}"],
            stdin="", timeout=30, env={"PATH": os.environ.get("PATH", "")},
            cwd="/workspace/target",
        )
    assert (target / "tracked.txt").read_text() == edited
    assert not (target / "from_agent.txt").exists()   # nothing applied on conflict


def test_remote_session_share_no_clobber(node_agent):
    """Parallel-pool case: two spawns sharing one session each land their
    edit; the second's sync-down does not clobber the first (kickoff
    acceptance bullet 3 + §2 session-level state)."""
    _client, ws, target = node_agent
    ws.up()
    # Spawn #1 writes a.py
    rc, _, _ = ws.spawn(  # type: ignore[unused-ignore]  # noqa: ARG001
        ["/bin/sh", "-c", "echo a > a.py"],
        stdin="", timeout=30, env={"PATH": os.environ.get("PATH", "")},
        cwd="/workspace/target",
    )
    assert rc == 0
    # Spawn #2 writes b.py (the prior a.py must remain after sync-down).
    rc, _, _ = ws.spawn(  # type: ignore[unused-ignore]  # noqa: ARG001
        ["/bin/sh", "-c", "echo b > b.py"],
        stdin="", timeout=30, env={"PATH": os.environ.get("PATH", "")},
        cwd="/workspace/target",
    )
    assert rc == 0
    assert (target / "a.py").is_file()
    assert (target / "b.py").is_file()

