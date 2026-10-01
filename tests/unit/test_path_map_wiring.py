"""remote-nodes-03 acceptance: the path map is actually WIRED into dispatch.

test_path_map.py covers the PathMap class in isolation. These tests drive the
real seams — ``providers._attach_isolation``, ``core.dispatch`` →
``_spawn_in_workspace``, the portable transport argv, and the sandbox MCP copy —
because the first cut shipped all of them gated on a ``request.path_map`` that
nothing ever set.
"""
import json
import os
import sys

import pytest

import mini_ork.dispatch.core as core
from mini_ork.dispatch import providers
from mini_ork.dispatch.core import DispatchRequest, dispatch
from mini_ork.runtime import sandbox
from mini_ork.runtime.path_map import PathMap, UnmappedHostPathError
from mini_ork.runtime.run_roots import RunRoots


@pytest.fixture
def roots(tmp_path):
    base = os.path.realpath(str(tmp_path))
    r = RunRoots(target=f"{base}/target", run_dir=f"{base}/home/runs/r1",
                 home=f"{base}/home", engine=f"{base}/engine")
    for d in (r.target, r.run_dir, r.engine):
        os.makedirs(d, exist_ok=True)
    return r


class _Recorder:
    def __init__(self):
        self.spawned = None

    def up(self):
        pass

    def down(self):
        pass

    def spawn(self, argv, *, stdin, timeout, env, cwd):
        self.spawned = {"argv": list(argv), "stdin": stdin, "env": dict(env), "cwd": cwd}
        return 0, '{"result": "ok", "usage": {}}', ""


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    sandbox.register_workspace_backend("fake-pm", lambda **_kw: rec)
    # Hermetic: no ambient env (a developer's MO_* paths) leaks into the check.
    monkeypatch.setattr(core, "context_env_snapshot", lambda: {})
    yield rec
    sandbox._WORKSPACE_BACKENDS.pop("fake-pm", None)


def _all_strings(spawned):
    return [*spawned["argv"], spawned["stdin"], spawned["cwd"] or "", *spawned["env"].values()]


def test_isolated_dispatch_translates_every_channel(roots, recorder, tmp_path):
    pm = PathMap.from_roots(roots)
    dispatch(
        DispatchRequest(
            model="test",
            prompt=f"Write your output to: {roots.run_dir}/research.md",
            workspace="fake-pm",
            cwd=roots.target,
            env={"MINI_ORK_HOME": roots.home, "MINI_ORK_DB": f"{roots.home}/state.db",
                 "MINI_ORK_RUN_DIR": roots.run_dir, "MO_TARGET_CWD": roots.target},
            path_map=pm,
        ),
        ["claude", "--mcp-config", f"{roots.run_dir}/.mcp-config.sandbox.json"],
        parse_text=lambda out: out,
    )
    s = recorder.spawned
    assert s["cwd"] == "/workspace/target"
    assert s["argv"][2] == "/workspace/run/.mcp-config.sandbox.json"
    assert "/workspace/run/research.md" in s["stdin"]
    env = s["env"]
    assert "MINI_ORK_DB" not in env
    assert env["MO_REMOTE_NODE"] == "1"
    assert env["MINI_ORK_ALLOW_CHILD_SPAWN"] == "0"
    assert env["MINI_ORK_HOME"] == "/workspace/mo-home"
    assert env["MINI_ORK_RUN_DIR"] == "/workspace/run"
    assert env["MO_TARGET_CWD"] == "/workspace/target"
    host = os.path.realpath(str(tmp_path))
    assert not [v for v in _all_strings(s) if host in v], "a host path reached the sandbox"


def test_remote_leak_raises_naming_the_channel(roots, recorder):
    with pytest.raises(UnmappedHostPathError) as exc:
        dispatch(
            DispatchRequest(model="test", prompt="also read /Users/someone/notes.md",
                            workspace="remote", cwd=roots.target, path_map=PathMap.from_roots(roots)),
            ["claude"], parse_text=lambda out: out,
        )
    assert exc.value.channel == "text"


def test_remote_without_a_path_map_refuses(recorder):
    with pytest.raises(ValueError, match="pinned roots"):
        dispatch(DispatchRequest(model="test", prompt="p", workspace="remote"),
                 ["claude"], parse_text=lambda out: out)


def _pin(roots):
    with open(os.path.join(roots.run_dir, "run_profile.json"), "w") as fh:
        json.dump({"roots": {"target": roots.target, "run_dir": roots.run_dir,
                             "home": roots.home, "engine": roots.engine}}, fh)


def test_attach_isolation_builds_the_map_from_pinned_roots(roots):
    _pin(roots)
    req = providers._attach_isolation(
        DispatchRequest(model="m", prompt="p"),
        {"MO_SANDBOX_SCOPE": "agent", "MO_SANDBOX_BACKEND": "docker",
         "MINI_ORK_RUN_DIR": roots.run_dir},
    )
    assert req.workspace == "docker"
    assert req.path_map is not None
    assert req.path_map.path(f"{roots.target}/a.py") == "/workspace/target/a.py"
    assert req.path_map.path(f"{roots.home}/config/x.yaml") == "/workspace/mo-home/config/x.yaml"
    assert req.path_map.path(f"{roots.engine}/recipes/r") == "/opt/mini-ork/recipes/r"


def test_attach_isolation_leaves_host_dispatch_untouched(roots):
    _pin(roots)
    req = DispatchRequest(model="m", prompt="p")
    assert providers._attach_isolation(req, {"MINI_ORK_RUN_DIR": roots.run_dir}) is req


def test_self_edit_target_wins_over_engine_root(tmp_path):
    same = os.path.realpath(str(tmp_path))
    pm = PathMap.from_roots(RunRoots(target=same, run_dir=f"{same}/.mini-ork/runs/r",
                                     home=f"{same}/.mini-ork", engine=same))
    assert pm.path(f"{same}/mini_ork/x.py") == "/workspace/target/mini_ork/x.py"


def test_openai_chat_argv_is_portable_only_under_isolation(roots):
    host_cmd = (sys.executable, "/somewhere/mini_ork/dispatch/openai_chat_transport.py", "--print")
    iso = DispatchRequest(model="m", prompt="p", path_map=PathMap.from_roots(roots))
    assert providers._portable_transport_command(host_cmd, request=iso, env={}) == (
        "python3", "-m", "mini_ork.dispatch.openai_chat_transport", "--print")
    plain = DispatchRequest(model="m", prompt="p")
    assert providers._portable_transport_command(host_cmd, request=plain, env={}) == host_cmd


def test_sandbox_mcp_copy_drops_host_binaries_and_translates_paths(roots):
    os.makedirs(os.path.join(roots.home, "config"), exist_ok=True)
    with open(os.path.join(roots.home, "config", "mcp_servers.json"), "w") as fh:
        json.dump({"mcpServers": {
            "hostonly": {"command": "/usr/local/bin/git-mcp", "args": []},
            "engine": {"command": f"{roots.engine}/bin/mo-mcp", "args": [f"{roots.target}/y"]},
        }}, fh)
    pm = PathMap.from_roots(roots)
    providers._write_node_mcp_config("hostonly,engine", roots.run_dir,
                                     env={"MINI_ORK_HOME": roots.home}, path_map=pm)
    doc = json.load(open(os.path.join(roots.run_dir, ".mcp-config.sandbox.json")))["mcpServers"]
    assert doc["hostonly"]["command"] == "echo"
    assert doc["engine"]["command"] == "/opt/mini-ork/bin/mo-mcp"
    assert doc["engine"]["args"] == ["/workspace/target/y"]
