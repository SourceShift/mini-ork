"""remote-nodes-13: ``mini-ork nodes`` (ls / ping / doctor).

Every doctor test runs against a real uvicorn node-agent (``--runtime host``)
through the production seams the command drives: the remote factory with an
environment profile, ``RemoteWorkspace.up()``, ``exec`` and
``providers.dispatch_model`` into the doctor's own session.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_ork.cli import nodes as nodes_cli

REPO = Path(__file__).resolve().parents[2]
TOKEN = "tok-doctor-0000"


def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return int(sk.getsockname()[1])


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    import uvicorn

    from mini_ork.remote.node_agent.app import create_app

    os.environ["MO_NODE_AGENT_TOKEN_D"] = TOKEN   # the node's own; tests vary the client's
    app = create_app(state_dir=tmp_path_factory.mktemp("agent"), token_env="MO_NODE_AGENT_TOKEN_D",
                     runtime="host")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=_free_port(), log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.config.port}")
    server.should_exit = True
    thread.join(timeout=10)


def _home(tmp_path, monkeypatch, url: str, *, token: str = TOKEN) -> Path:
    """nodes.yaml + a profile bound to the node, two executable lanes inside
    the target checkout (one answers, one never does), and the run env."""
    home = tmp_path / "home"
    (home / "config" / "environments").mkdir(parents=True)
    (home / "config" / "nodes.yaml").write_text(
        f"nodes:\n  dnode:\n    url: {url}\n    token_env: MO_NODE_TOKEN_D\n    max_sessions: 4\n")
    (home / "config" / "environments" / "dprof.yaml").write_text("node: dnode\nimage: alpine:latest\n")
    repo = tmp_path / "target"
    repo.mkdir()
    for name, body in (("answers.sh", "#!/bin/sh\necho OK\n"), ("silent.sh", "#!/bin/sh\nsleep 30\n")):
        (repo / name).write_text(body)
        (repo / name).chmod(0o755)
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                 ["add", "-A"], ["commit", "-q", "-m", "i"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    reg = tmp_path / "providers.yaml"
    reg.write_text("providers:\n"
                   f"  answers:\n    kind: executable\n    script: {repo / 'answers.sh'}\n"
                   f"  silent:\n    kind: executable\n    script: {repo / 'silent.sh'}\n")
    for key in ("MO_NODE", "MO_NODE_URL", "MO_NODE_TOKEN", "MO_PLACEMENT", "MO_NODE_ENV",
                "MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MO_SANDBOX_SCOPE", "MO_SANDBOX_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    for key, val in {"MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": str(home),
                     "MINI_ORK_DB": str(home / "state.db"), "MINI_ORK_PROVIDERS": str(reg),
                     "MO_NODE_TOKEN_D": token, "MO_REMOTE_ALLOW_DIRTY_ENGINE": "1",
                     "MO_TARGET_CWD": str(repo)}.items():
        monkeypatch.setenv(key, val)
    # The agent image puts the engine on PYTHONPATH; the host runtime needs it said.
    monkeypatch.setattr(nodes_cli, "PYTHON", f"PYTHONPATH={REPO} {sys.executable}")
    return home


def _doctor(**kw) -> tuple[int, list[str]]:
    lines: list[str] = []
    rc = nodes_cli.doctor(env_name="dprof", out=lines.append, **kw)
    return rc, lines


def _statuses(lines: list[str]) -> list[tuple[int, str]]:
    return [(int(ln.split(".", 1)[0]), ln.split("[", 1)[1].split("]", 1)[0]) for ln in lines]


def _no_doctor_sessions_left(url: str, _lines: list[str]) -> bool:
    req = urllib.request.Request(f"{url}/v1/health")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())["sessions"] == 0


def test_doctor_passes_every_check_with_an_answering_lane(tmp_path, monkeypatch, node):
    _home(tmp_path, monkeypatch, node.url)
    rc, lines = _doctor(lanes=["answers"])
    assert rc == 0, "\n".join(lines)
    assert _statuses(lines) == [(i, "PASS") for i in range(1, 10)], "\n".join(lines)
    assert "answers" in lines[6]
    assert _no_doctor_sessions_left(node.url, lines)


def test_doctor_no_llm_passes_all_non_llm_checks(tmp_path, monkeypatch, node):
    _home(tmp_path, monkeypatch, node.url)
    rc, lines = _doctor(lanes=["silent"], no_llm=True)
    assert rc == 0, "\n".join(lines)
    assert _statuses(lines) == [(i, "SKIP" if i == 7 else "PASS") for i in range(1, 10)]


def test_doctor_lane_that_never_answers_reports_the_auth_hint(tmp_path, monkeypatch, node):
    _home(tmp_path, monkeypatch, node.url)
    monkeypatch.setattr(nodes_cli, "AUTH_SMOKE_TIMEOUT_S", 2.0)
    t0 = time.monotonic()
    rc, lines = _doctor(lanes=["silent"])
    assert rc == 1
    assert time.monotonic() - t0 < 25                    # the smoke ceiling, not the lane's timeout
    assert _statuses(lines)[-1] == (7, "FAIL")
    assert "auth: no response in 2 s — check the key" in lines[-1]
    assert _no_doctor_sessions_left(node.url, lines)     # torn down even on failure


def test_doctor_wrong_token_fails_check_2(tmp_path, monkeypatch, node):
    _home(tmp_path, monkeypatch, node.url, token="not-the-token")
    rc, lines = _doctor(no_llm=True)
    assert rc == 1 and _statuses(lines) == [(1, "PASS"), (2, "FAIL")]
    assert "MO_NODE_TOKEN_D" in lines[-1]


def test_doctor_unreachable_node_fails_check_1(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, f"http://127.0.0.1:{_free_port()}")
    rc, lines = _doctor(no_llm=True)
    assert rc == 1 and _statuses(lines) == [(1, "FAIL")]
    assert "node-agent" in lines[0]


def test_nodes_subcommand_runs_as_a_module(tmp_path, monkeypatch, node):
    """``mini-ork nodes`` dispatches as ``python -m mini_ork.cli.nodes`` — the
    module must actually run (the first cut had no __main__ and exited 0 silently)."""
    home = _home(tmp_path, monkeypatch, node.url)
    env = {**os.environ, "MINI_ORK_HOME": str(home), "PYTHONPATH": str(REPO)}
    ls = subprocess.run([sys.executable, "-m", "mini_ork.cli.nodes", "ls"], env=env,
                        capture_output=True, text=True, timeout=60)
    assert ls.returncode == 0 and f"dnode\t{node.url}\tmax_sessions=4" in ls.stdout
    ping = subprocess.run([sys.executable, "-m", "mini_ork.cli.nodes", "ping", "dnode"], env=env,
                          capture_output=True, text=True, timeout=60)
    assert ping.returncode == 0 and "dnode: ok" in ping.stdout
