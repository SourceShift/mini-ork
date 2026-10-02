"""remote-nodes-13: per-lane secret scoping + redaction.

The scoping rules are unit-tested on ``secrets_scope``; every Acceptance
bullet is then proven through the production path — ``providers.
dispatch_model`` into a run session on a real uvicorn node-agent
(``--runtime host``), with the request captured on the wire and the stored
``.out`` file read back from the node's state dir.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_ork.remote.secrets_scope import (
    SecretNotPermittedError,
    lane_egress_hosts,
    lane_secret_names,
    make_redactor,
    scoped_env,
    secret_values,
)

REPO = Path(__file__).resolve().parents[2]
TOKEN = "tok-secrets-0000"


# --------------------------------------------------------------------------- rules


def _profile(*secrets):
    return SimpleNamespace(name="p", secrets=list(secrets))


def test_lane_secret_names_are_the_lanes_own_plus_native_carriers():
    spec_env = {"ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "u", "MO_X": "1"}
    assert lane_secret_names(spec_env) == {"ANTHROPIC_AUTH_TOKEN"}
    runtime = {"CLAUDE_CODE_OAUTH_TOKEN": "o", "OPENAI_API_KEY": "k"}
    assert lane_secret_names({}, kind="anthropic-native", runtime=runtime) == {"CLAUDE_CODE_OAUTH_TOKEN"}
    assert lane_secret_names({}, kind="anthropic-compat", runtime=runtime) == set()


def test_profile_may_name_the_spawn_key_or_the_store_name():
    spec_env = {"ANTHROPIC_AUTH_TOKEN": "glm-value-123"}
    by_key = scoped_env(spec_env, _profile("ANTHROPIC_AUTH_TOKEN"), store={}, lane="glm")
    by_source = scoped_env(spec_env, _profile("GLM_API_KEY"), store={}, lane="glm",
                           api_key_env="GLM_API_KEY")
    assert by_key == by_source == {"ANTHROPIC_AUTH_TOKEN": "glm-value-123"}


def test_an_unlisted_lane_secret_is_refused_with_the_profile_path():
    with pytest.raises(SecretNotPermittedError) as err:
        scoped_env({"ANTHROPIC_AUTH_TOKEN": "v"}, _profile("OTHER_KEY"), store={}, lane="glm",
                   api_key_env="GLM_API_KEY", profile_path="config/environments/p.yaml")
    assert (err.value.lane, err.value.name) == ("glm", "GLM_API_KEY")
    assert "config/environments/p.yaml" in str(err.value)


def test_without_a_profile_only_the_lanes_own_secrets_pass():
    out = scoped_env({"ANTHROPIC_AUTH_TOKEN": "v1"}, None, store={"OPENAI_API_KEY": "x"}, lane="glm")
    assert out == {"ANTHROPIC_AUTH_TOKEN": "v1"}


def test_redaction_masks_only_secret_valued_keys():
    env = {"MY_API_KEY": "s3cret-value-12345", "MO_TARGET": "/workspace/target/longpath",
           "PATH": "/usr/local/bin:/usr/bin"}
    values = secret_values(env)
    assert values == ["s3cret-value-12345"]
    red = make_redactor(values)
    line = "token=s3cret-value-12345 at /workspace/target/longpath"
    assert red.redact(line.encode()) == b"token=*** at /workspace/target/longpath"
    assert red.redact_text(line) == "token=*** at /workspace/target/longpath"
    assert make_redactor(["short"]).redact(b"short") == b"short"      # under the length floor


def test_lane_egress_hosts_cover_base_urls_and_provider_defaults():
    registry = {"glm": {"kind": "anthropic-compat", "base_url": "https://open.bigmodel.cn/api/anthropic"},
                "claude": {"kind": "anthropic-native"}, "local": {"kind": "executable"}}
    assert lane_egress_hosts(registry) == ["api.anthropic.com", "open.bigmodel.cn"]


# --------------------------------------------------------------------------- live node


def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return int(sk.getsockname()[1])


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    import uvicorn

    from mini_ork.remote.node_agent.app import create_app

    os.environ["MO_NODE_TOKEN_S"] = TOKEN
    state = tmp_path_factory.mktemp("agent")
    app = create_app(state_dir=state, token_env="MO_NODE_TOKEN_S", runtime="host")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=_free_port(), log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.config.port}", state=state)
    server.should_exit = True
    thread.join(timeout=10)


_REGISTRY = """\
providers:
  glm:
    kind: anthropic-compat
    base_url: https://glm.example/anthropic
    api_key_env: GLM_API_KEY
    model: glm-x
"""


def _run(tmp_path, monkeypatch, node, secrets: str):
    """A provisioned --placement remote run whose profile permits ``secrets``;
    returns (run_id, the session workspace)."""
    import mini_ork.cli.execute as ex
    from mini_ork.runtime.run_roots import resolve_run_roots
    from mini_ork.runtime.workspace_session import _SESSION_REGISTRY

    home = tmp_path / "home"
    (home / "config" / "environments").mkdir(parents=True)
    (home / "config" / "nodes.yaml").write_text(
        f"nodes:\n  snode:\n    url: {node.url}\n    token_env: MO_NODE_TOKEN_S\n    max_sessions: 4\n")
    (home / "config" / "environments" / "sprof.yaml").write_text(
        f"node: snode\nimage: alpine:latest\nsecrets: {secrets}\n")
    reg = tmp_path / "providers.yaml"
    reg.write_text(_REGISTRY)
    repo = tmp_path / "target"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("t\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=repo, check=True, capture_output=True)
    run_id = f"run-sec-{uuid.uuid4().hex[:8]}"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    for key in ("MO_NODE", "MO_NODE_URL", "MO_NODE_TOKEN", "MO_SANDBOX_SCOPE", "MO_SANDBOX_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    for key, val in {"MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": str(home),
                     "MINI_ORK_DB": str(home / "state.db"), "MINI_ORK_PROVIDERS": str(reg),
                     "MO_NODE_TOKEN_S": TOKEN, "MO_REMOTE_ALLOW_DIRTY_ENGINE": "1",
                     "MO_PLACEMENT": "remote", "MO_NODE_ENV": "sprof", "MO_TARGET_CWD": str(repo),
                     "MINI_ORK_RUN_ID": run_id, "MINI_ORK_RUN_DIR": str(run_dir),
                     "GLM_API_KEY": "glm-secret-1234567", "OPENAI_API_KEY": "ambient-openai-999999",
                     "ANTHROPIC_API_KEY": "ambient-anthropic-888888",
                     "MINI_ORK_SECRETS": str(tmp_path / "no-store.sh")}.items():
        monkeypatch.setenv(key, val)
    from dataclasses import asdict
    (run_dir / "run_profile.json").write_text(json.dumps(
        {"roots": asdict(resolve_run_roots(str(run_dir), env=os.environ))}))
    assert ex._provision_remote_session(run_id, str(run_dir), None)
    return run_id, _SESSION_REGISTRY[(run_id, "remote")].workspace


class _Captured(Exception):
    pass


def _record(ws) -> list[tuple[str, str, dict | None]]:
    """Record every request the session sends; stop the spawn at the wire."""
    seen: list[tuple[str, str, dict | None]] = []
    real = ws._json

    def recording(method, path, payload=None, **kw):
        seen.append((method, path, payload))
        if method == "POST" and path.endswith("/procs"):
            raise _Captured()
        return real(method, path, payload, **kw)

    ws._json = recording
    return seen


def test_remote_spawn_carries_only_the_lanes_secret(tmp_path, monkeypatch, node):
    """A glm anthropic-compat lane's spawn request carries its ANTHROPIC_AUTH_TOKEN
    and ANTHROPIC_BASE_URL; ambient OPENAI_API_KEY / ANTHROPIC_API_KEY — and the
    store name GLM_API_KEY itself — never reach the node."""
    from mini_ork.dispatch import providers
    from mini_ork.dispatch.models import DispatchRequest
    from mini_ork.runtime.workspace_session import close_run_session

    run_id, ws = _run(tmp_path, monkeypatch, node, "[GLM_API_KEY]")
    seen = _record(ws)
    try:
        with pytest.raises(_Captured):
            providers.dispatch_model(DispatchRequest(model="glm", prompt="hi", timeout_s=20), str(REPO))
        spawn_env = next(p for m, path, p in seen if path.endswith("/procs"))["env"]
        assert spawn_env["ANTHROPIC_AUTH_TOKEN"] == "glm-secret-1234567"
        assert spawn_env["ANTHROPIC_BASE_URL"] == "https://glm.example/anthropic"
        for leaked in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GLM_API_KEY", "MO_NODE_TOKEN_S",
                       "MO_LANE_SECRET_KEYS"):
            assert leaked not in spawn_env, leaked
    finally:
        close_run_session(run_id)


def test_unlisted_lane_secret_is_refused_before_any_request(tmp_path, monkeypatch, node):
    from mini_ork.dispatch import providers
    from mini_ork.dispatch.models import DispatchRequest
    from mini_ork.runtime.workspace_session import close_run_session

    run_id, ws = _run(tmp_path, monkeypatch, node, "[SOME_OTHER_KEY]")
    seen = _record(ws)
    try:
        res = providers.dispatch_model(DispatchRequest(model="glm", prompt="hi", timeout_s=20), str(REPO))
        assert not res.ok and "GLM_API_KEY" in res.error and "not permitted" in res.error
        assert "config/environments/sprof.yaml" in res.error
        assert seen == []                                   # nothing went over the wire
    finally:
        close_run_session(run_id)


def test_echoed_token_is_masked_on_disk_in_the_stream_and_in_the_live_file(tmp_path, monkeypatch, node):
    from mini_ork.runtime.workspace_session import close_run_session

    run_id, ws = _run(tmp_path, monkeypatch, node, "[]")
    secret = "s3cret-value-12345"
    live = tmp_path / "agent-echo.live.jsonl"
    try:
        rc, out, _err = ws.spawn(
            ["sh", "-c", 'echo "token=$MY_API_KEY"; echo "where=$MO_WHERE"'], stdin="",
            timeout=30.0, env={"MY_API_KEY": secret, "MO_WHERE": "plain-not-a-secret-value",
                               "PATH": "/usr/bin:/bin"},
            cwd="/workspace/target", live_file_path=str(live))
        assert rc == 0
        assert "token=***" in out and secret not in out
        assert "where=plain-not-a-secret-value" in out        # non-secrets untouched
        stored = [p for p in node.state.rglob("*.out") if "token=" in p.read_text(errors="replace")]
        assert stored, "the proc's .out file was not found on the node"
        assert all(secret not in p.read_text() for p in stored)
        assert live.is_file() and secret not in live.read_text() and "***" in live.read_text()
    finally:
        close_run_session(run_id)
