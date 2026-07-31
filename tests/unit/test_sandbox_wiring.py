"""Wiring tests for the opt-in sandbox seam (sandbox P2, Piece 2).

Covers the resolver (:func:`resolve_agent_workspace`), the MinimalAgent tool-exec
routing, and ``run_minimal`` lifecycle. Host-only tests prove the opt-in no-op
and ``local`` parity (acceptance P2 #1/#2). A daemon-gated test runs a real
**two-node** scenario: two distinct containers bind-mounting one shared drive,
where the second node reads a file the first wrote — the "each agent in its own
environment, all sharing one directory" ask, proven live (acceptance P2 #3).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from mini_ork.agent.minimal import MinimalAgent, run_minimal
from mini_ork.runtime.agent_workspace import resolve_agent_workspace
from mini_ork.runtime.contract import mo_runtime_exec
from mini_ork.runtime.sandbox import LocalWorkspace

TEST_IMAGE = os.environ.get("MO_SANDBOX_TEST_IMAGE", "alpine:latest")


def _docker_available() -> bool:
    exe = shutil.which("docker")
    if not exe:
        return False
    try:
        return (
            subprocess.run(
                [exe, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=20,
            ).returncode
            == 0
        )
    except Exception:
        return False


def _image_ready(image: str) -> bool:
    exe = shutil.which("docker")
    if not exe:
        return False
    if subprocess.run([exe, "image", "inspect", image], capture_output=True).returncode == 0:
        return True
    try:
        return subprocess.run([exe, "pull", image], capture_output=True, timeout=180).returncode == 0
    except Exception:
        return False


def _bind_visible_dir(base: str) -> str | None:
    exe = shutil.which("docker")
    if not exe:
        return None
    try:
        probe = tempfile.mkdtemp(prefix=".mo-bindprobe-", dir=base)
    except OSError:
        return None
    try:
        r = subprocess.run(
            [exe, "run", "--rm", "-v", f"{probe}:/probe", TEST_IMAGE,
             "sh", "-c", "echo ok > /probe/sentinel"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and os.path.exists(os.path.join(probe, "sentinel")):
            return probe
    except Exception:
        pass
    shutil.rmtree(probe, ignore_errors=True)
    return None


requires_docker = pytest.mark.skipif(
    not _docker_available(), reason="docker daemon not available"
)


# --- resolver behavior (no daemon) ----------------------------------------


def test_unset_backend_returns_no_workspace(monkeypatch, tmp_path):
    monkeypatch.delenv("MO_SANDBOX_BACKEND", raising=False)
    ws, exec_cwd = resolve_agent_workspace(str(tmp_path))
    assert ws is None  # dead-code proof: caller uses mo_runtime_exec
    assert exec_cwd == str(tmp_path)


def test_local_backend_returns_localworkspace(monkeypatch, tmp_path):
    monkeypatch.setenv("MO_SANDBOX_BACKEND", "local")
    ws, exec_cwd = resolve_agent_workspace(str(tmp_path))
    assert isinstance(ws, LocalWorkspace)
    assert exec_cwd == str(tmp_path)


def test_unknown_backend_raises_valueerror(monkeypatch, tmp_path):
    monkeypatch.setenv("MO_SANDBOX_BACKEND", "wat-no-such-backend")
    with pytest.raises(ValueError, match="unknown workspace backend"):
        resolve_agent_workspace(str(tmp_path))


def test_docker_backend_resolves_to_mount_path(monkeypatch, tmp_path):
    # Construction only — no daemon needed. Proves the exec cwd is the
    # in-container mount path and the drive root is threaded through.
    monkeypatch.setenv("MO_SANDBOX_BACKEND", "docker")
    monkeypatch.setenv("MO_SANDBOX_IMAGE", TEST_IMAGE)
    ws, exec_cwd = resolve_agent_workspace(
        str(tmp_path), drive_root=str(tmp_path)
    )
    from mini_ork.runtime.backends.docker import DockerWorkspace

    assert isinstance(ws, DockerWorkspace)
    assert exec_cwd == "/workspace"


# --- MinimalAgent tool-exec routing (no daemon) ---------------------------


class _RecordingWorkspace:
    """A stub Workspace that records exec calls and returns a fixed result."""

    def __init__(self) -> None:
        self.exec_calls: list[tuple[str, str, int]] = []
        self.up_calls = 0
        self.down_calls = 0

    def exec(self, cmd: str, *, cwd: str, timeout: int) -> tuple[int, str]:
        self.exec_calls.append((cmd, cwd, timeout))
        return 7, "OUTPUT-FROM-WS"

    def put(self, content: str) -> str:  # pragma: no cover - unused here
        return "/workspace/x"

    def get(self, path: str) -> str:  # pragma: no cover - unused here
        return ""

    def up(self) -> None:
        self.up_calls += 1

    def down(self) -> None:
        self.down_calls += 1


def test_run_bash_routes_through_workspace_and_flips_tuple():
    ws = _RecordingWorkspace()
    agent = MinimalAgent(cwd="/host", workspace=ws, exec_cwd="/workspace", timeout=42)
    out, rc = agent._run_bash("echo x")
    # Workspace.exec returns (rc, output); _run_bash must return (output, rc).
    assert (out, rc) == ("OUTPUT-FROM-WS", 7)
    assert ws.exec_calls == [("echo x", "/workspace", 42)]


def test_run_bash_without_workspace_runs_on_host(tmp_path):
    agent = MinimalAgent(cwd=str(tmp_path))  # no workspace
    out, rc = agent._run_bash("echo hi")
    assert rc == 0
    assert out.strip() == "hi"
    # Parity: identical to a direct host runtime call.
    host_out, host_rc = mo_runtime_exec("echo hi", cwd=str(tmp_path), timeout=60)
    assert (out, rc) == (host_out, host_rc)


# --- run_minimal lifecycle (no daemon; fake dispatch + fake workspace) -----


def test_run_minimal_unset_backend_never_provisions(monkeypatch, tmp_path):
    monkeypatch.delenv("MO_SANDBOX_BACKEND", raising=False)

    class _Resp:
        text = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT ok"

    monkeypatch.setattr("mini_ork.agent.minimal.dispatch_model", lambda req: _Resp())
    res = run_minimal("do it", cwd=str(tmp_path))
    assert res.completed  # ran the host path with no Workspace involved


def test_run_minimal_brings_workspace_up_and_down(monkeypatch, tmp_path):
    ws = _RecordingWorkspace()
    monkeypatch.setattr(
        "mini_ork.runtime.agent_workspace.resolve_agent_workspace",
        lambda cwd, **kw: (ws, "/workspace"),
    )

    # Turn 1 emits a bash block (routed to the workspace); turn 2 completes.
    replies = iter([
        type("R", (), {"text": "```bash\necho hi\n```"}),
        type("R", (), {"text": "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT done"}),
    ])
    monkeypatch.setattr(
        "mini_ork.agent.minimal.dispatch_model", lambda req: next(replies)
    )
    res = run_minimal("do it", cwd=str(tmp_path))
    assert res.completed
    assert ws.up_calls == 1 and ws.down_calls == 1  # lifecycle managed
    assert ws.exec_calls and ws.exec_calls[0][1] == "/workspace"  # ran in sandbox


# --- REAL two-node shared-drive proof (requires a live daemon) -------------


@requires_docker
def test_two_agents_distinct_containers_share_one_drive(tmp_path):
    if not _image_ready(TEST_IMAGE):
        pytest.skip(f"test image {TEST_IMAGE} unavailable/unpullable")
    chosen: str | None = None
    for base in (str(tmp_path), os.path.expanduser("~")):
        chosen = _bind_visible_dir(base)
        if chosen:
            break
    if not chosen:
        pytest.skip("no docker-bind-visible directory available")

    env = {
        "MO_SANDBOX_BACKEND": "docker",
        "MO_SANDBOX_IMAGE": TEST_IMAGE,
        "MO_SHARED_DRIVE_ROOT": chosen,
    }
    try:
        # Node A — its own container — writes to the shared drive.
        ws_a, cwd_a = resolve_agent_workspace(chosen, env=env)
        assert cwd_a == "/workspace"
        ws_a.up()
        try:
            rc, _ = ws_a.exec(
                "echo hello-from-A > /workspace/from_a.txt",
                cwd="/workspace",
                timeout=30,
            )
            assert rc == 0
        finally:
            ws_a.down()

        # Node B — a DIFFERENT container — reads what A wrote.
        ws_b, cwd_b = resolve_agent_workspace(chosen, env=env)
        assert ws_b._name != ws_a._name  # distinct environments
        ws_b.up()
        try:
            rc, out = ws_b.exec(
                "cat /workspace/from_a.txt", cwd="/workspace", timeout=30
            )
            assert rc == 0
            assert out.strip() == "hello-from-A"  # cross-agent shared dir
        finally:
            ws_b.down()
    finally:
        shutil.rmtree(chosen, ignore_errors=True)
