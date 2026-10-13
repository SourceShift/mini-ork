"""Cloud-sandbox provider seam: a ``RemoteWorkspace`` obtains its node from a
node provider, lazily and exactly once per run, and the teardown paths
(``release_remote_session``, ``control.kill_run``, ``mini-ork nodes doctor``)
destroy it.

Every acceptance test below drives a PRODUCTION entrypoint — ``get_run_session``,
``release_remote_session``, ``control.kill_run``, ``mini_ork.cli.nodes.doctor`` —
against a real uvicorn node-agent (``--runtime host``) over the real urllib
transport, with a fake provider registered through ``register_node_provider``.
Nothing here mocks ``RemoteWorkspace``: the seam itself is the code under test.

The fake provider mints its own bearer and returns a provider auth header
(``x-fake-provider``), so the tests can prove two properties of the seam:

  * every request to the node-agent carries the provider header, and
  * no token value and no header value ever reaches a file under the run dir.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mini_ork.remote.providers import base
from mini_ork.remote.providers.base import NodeEndpoint

REPO = Path(__file__).resolve().parents[2]

# The token the node-agent holds and the fake provider mints. Distinctive so a
# grep of the run dir can prove it was never written there.
FAKE_TOKEN = "seam-node-token-c0ffee"
# The env var the node-agent reads its own bearer from (test-owned).
AGENT_TOKEN_ENV = "MO_FAKE_AGENT_TOKEN"
# The provider-level auth header every request must carry. The value is
# deliberately distinctive so the leak grep can search for it byte-for-byte.
HEADER_NAME = "x-fake-provider"
HEADER_VALUE = "h-provider-auth-7f2a"

# Env keys a test must never inherit from the developer's shell: they would
# steer node/profile resolution or arm the mo-home upload at the real home.
_SCRUB = ("MO_NODE", "MO_NODE_URL", "MO_NODE_TOKEN", "MO_NODE_PROVIDER", "MO_PLACEMENT",
          "MO_NODE_ENV", "MO_RECIPE", "MO_LIVE_FILE", "MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR",
          "MO_SANDBOX_BACKEND", "MO_SANDBOX_SCOPE")


# ---------------------------------------------------------------------------
# Fake providers: a real endpoint, no cloud.
# ---------------------------------------------------------------------------


class FakeProvider:
    """Counts lifecycle calls; ``alive`` is the set of handles still "up"."""

    name = "fake"

    def __init__(self, *, url: str, token: str, headers: dict[str, str]) -> None:
        self.url = url
        self.token = token
        self.headers = dict(headers)
        self.created: list[str] = []
        self.attached: list[str] = []
        self.released: list[str] = []
        self.alive: set[str] = set()
        self.seen_headers: list[dict[str, str]] = []
        self.setup_applied = False
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self.created.clear()
            self.attached.clear()
            self.released.clear()
            self.alive.clear()
            self.seen_headers.clear()
            self.setup_applied = False

    def _endpoint(self, handle: str) -> NodeEndpoint:
        # token_env points at the DEFAULT node-token name on purpose: a workspace
        # that (wrongly) fell back to os.environ would read an ambient
        # MO_NODE_TOKEN instead of the provider-minted bearer.
        return NodeEndpoint(name=f"fake-{handle}", url=self.url, token=self.token,
                            token_env="MO_NODE_TOKEN", headers=dict(self.headers),
                            max_sessions=4, handle=handle, provider=self.name,
                            setup_applied=self.setup_applied)

    def create(self, *, run_id: str, profile: Any, env: Any, engine_root: str) -> NodeEndpoint:
        with self._lock:
            handle = f"sb-{len(self.created)}"
            self.created.append(run_id)
            self.alive.add(handle)
        return self._endpoint(handle)

    def attach(self, handle: str, *, env: Any) -> NodeEndpoint | None:
        with self._lock:
            self.attached.append(handle)
            alive = handle in self.alive
        return self._endpoint(handle) if alive else None

    def release(self, handle: str, *, env: Any) -> None:
        with self._lock:
            self.released.append(handle)
            self.alive.discard(handle)

    def reap(self, *, max_age_s: int, dry_run: bool, env: Any) -> list[str]:
        with self._lock:
            return sorted(self.alive)


class _BoomProvider:
    """A provider whose create always fails — the ``remote_unavailable`` case."""

    name = "boom"

    def create(self, *, run_id: str, profile: Any, env: Any, engine_root: str) -> NodeEndpoint:
        raise base.ProviderUnavailableError("boom: no quota left")

    def attach(self, handle: str, *, env: Any) -> NodeEndpoint | None:
        return None

    def release(self, handle: str, *, env: Any) -> None:
        return None

    def reap(self, *, max_age_s: int, dry_run: bool, env: Any) -> list[str]:
        return []


# ---------------------------------------------------------------------------
# Real in-process node-agent + header recorder.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return int(sk.getsockname()[1])


class _HeaderRecorder:
    """ASGI shim recording the headers of every HTTP request the agent sees."""

    def __init__(self, app: Any, sink: list[dict[str, str]]) -> None:
        self.app = app
        self.sink = sink

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            seen = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            seen[":path"] = str(scope.get("path") or "")
            self.sink.append(seen)
        await self.app(scope, receive, send)


@pytest.fixture(scope="module")
def seam(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """A running node-agent plus a fake provider bound to its URL."""
    import uvicorn

    from mini_ork.remote.node_agent.app import create_app

    provider = FakeProvider(url="", token=FAKE_TOKEN,
                            headers={HEADER_NAME: HEADER_VALUE})
    os.environ[AGENT_TOKEN_ENV] = FAKE_TOKEN
    app = create_app(state_dir=tmp_path_factory.mktemp("agent"),
                     token_env=AGENT_TOKEN_ENV, runtime="host")
    recorder = _HeaderRecorder(app, provider.seen_headers)
    server = uvicorn.Server(uvicorn.Config(recorder, host="127.0.0.1",
                                           port=_free_port(), log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "node-agent did not start"
    provider.url = f"http://127.0.0.1:{server.config.port}"
    base._PROVIDERS["fake"] = lambda: provider
    try:
        yield provider
    finally:
        base._PROVIDERS.pop("fake", None)
        os.environ.pop(AGENT_TOKEN_ENV, None)
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _fresh(seam: Any) -> Any:
    """Every test starts from a clean provider (and thus a clean agent)."""
    seam.reset()
    yield


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("target\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path,
                   check=True, capture_output=True)
    return path


def _seam_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_id: str, *,
              provider: str = "fake", profile: str = "seam",
              setup: str = "") -> SimpleNamespace:
    """A home whose ``<profile>.yaml`` selects ``provider``, the run env, and
    the matching ``os.environ`` (production entrypoints read os.environ)."""
    home = tmp_path / "home"
    (home / "config" / "environments").mkdir(parents=True, exist_ok=True)
    (home / "config" / "environments" / f"{profile}.yaml").write_text(
        f"provider: {provider}\nimage: alpine:latest\n"
        + (f"setup: {json.dumps(setup)}\n" if setup else ""), encoding="utf-8")
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    target = _git_repo(tmp_path / "target")
    env = {
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_DB": str(home / "state.db"),
        "MINI_ORK_RUN_ID": run_id,
        "MINI_ORK_RUN_DIR": str(run_dir),
        "MO_NODE_ENV": profile,
        "MO_PLACEMENT": "remote",
        "MO_REMOTE_ALLOW_DIRTY_ENGINE": "1",
        "MO_TARGET_CWD": str(target),
        "PATH": os.environ.get("PATH", ""),
    }
    for key in _SCRUB:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return SimpleNamespace(home=home, run_dir=run_dir, target=target, env=env)


def _agent_status(provider: FakeProvider, path: str) -> int:
    """GET ``path`` on the agent with the provider's auth; return the status."""
    req = urllib.request.Request(
        provider.url + path,
        headers={HEADER_NAME: HEADER_VALUE, "Authorization": f"Bearer {FAKE_TOKEN}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _leaked_files(run_dir: Path, needle: str) -> list[str]:
    leaked: list[str] = []
    for path in run_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if needle.encode() in data:
            leaked.append(str(path.relative_to(run_dir)))
    return leaked


def _assert_no_secrets_in_run_dir(run_dir: Path) -> None:
    for needle, label in ((FAKE_TOKEN, "token"), (HEADER_VALUE, "header value")):
        leaked = _leaked_files(run_dir, needle)
        assert not leaked, f"{label} written to run dir: {leaked}"


def _insert_task_run(home: Path, run_id: str) -> Any:
    """init_db + a minimal ``task_runs`` row so ``control.kill_run`` can act."""
    from mini_ork.stores import migrate as _mig
    from mini_ork.web.db import StateDB

    dbp = home / "state.db"
    rc, _out, err = _mig.init_db(db=str(dbp), root=str(REPO))
    assert rc == 0, err
    import sqlite3 as _sqlite

    con = _sqlite.connect(dbp)
    cols = {r[1]: r for r in con.execute("PRAGMA table_info(task_runs)")}
    row: dict[str, Any] = {"id": run_id, "status": "executing"}
    for name, info in cols.items():   # satisfy any other NOT NULL column
        if name not in row and info[3] and info[4] is None and not info[5]:
            row[name] = 0 if "INT" in (info[2] or "").upper() else ""
    con.execute(f"INSERT INTO task_runs ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                list(row.values()))
    con.commit()
    con.close()
    return StateDB(dbp)


# ---------------------------------------------------------------------------
# Acceptance: the seam end to end.
# ---------------------------------------------------------------------------


def test_seam_end_to_end(tmp_path, monkeypatch, seam):
    """get_run_session provisions ONE node, syncs the tree, runs a spawn; the
    agent sees the provider header on every request; no secret lands on disk."""
    ctx = _seam_env(tmp_path, monkeypatch, "seam-e2e")
    from mini_ork.runtime.workspace_session import close_run_session, get_run_session

    ws = get_run_session("seam-e2e", "remote", env=ctx.env)
    try:
        rc, out, err = ws.spawn(
            ["/bin/sh", "-c", "echo seam-ok"],
            stdin="", timeout=30.0,
            env={"PATH": os.environ.get("PATH", "")},
            cwd=str(ctx.run_dir),
        )
        assert rc == 0, f"spawn failed: rc={rc} err={err!r}"
        assert "seam-ok" in out
        assert seam.created == ["seam-e2e"], seam.created
        record = json.loads((ctx.run_dir / ".node-provider.json").read_text())
        assert record["provider"] == "fake" and record["handle"]
        # every request the agent received carried the provider auth header
        assert seam.seen_headers, "the node-agent saw no requests"
        missing = [h for h in seam.seen_headers if h.get(HEADER_NAME) != HEADER_VALUE]
        assert not missing, f"{len(missing)} request(s) lacked {HEADER_NAME}"
        _assert_no_secrets_in_run_dir(ctx.run_dir)
    finally:
        close_run_session("seam-e2e")


def test_one_node_per_run(tmp_path, monkeypatch, seam):
    """A second _factory(env) + up() attaches to the recorded node; the
    provider's create runs exactly once."""
    ctx = _seam_env(tmp_path, monkeypatch, "seam-once")
    from mini_ork.runtime.backends.remote import _factory
    from mini_ork.runtime.workspace_session import close_run_session, get_run_session

    first = get_run_session("seam-once", "remote", env=ctx.env)
    second = _factory(env=ctx.env)
    try:
        second.up()
        assert seam.created == ["seam-once"]
        assert seam.attached == ["sb-0"]
        assert second._node.url == first._node.url
        # both processes converged on the SAME node session (the agent's sid,
        # minted once for the run) — the one-node-per-run property.
        assert second.session_id() == first.session_id()
        assert first.session_id()
    finally:
        second.down()
        close_run_session("seam-once")


def test_construction_never_provisions(tmp_path, monkeypatch, seam):
    """_factory and release_remote_session on a run with no record touch
    neither create nor attach (a teardown must not mint a sandbox)."""
    ctx = _seam_env(tmp_path, monkeypatch, "seam-noprov")
    from mini_ork.runtime.backends.remote import _factory
    from mini_ork.runtime.workspace_session import release_remote_session

    ws = _factory(env=ctx.env)
    assert ws._node_provider == "fake"
    assert ws._node.url == ""
    assert seam.created == [] and seam.attached == []

    release_remote_session("seam-noprov", str(ctx.run_dir), env=ctx.env)
    assert seam.created == [] and seam.attached == []
    assert seam.released == []
    assert not (ctx.run_dir / ".node-provider.json").exists()


def test_release_destroys_node(tmp_path, monkeypatch, seam):
    """release_remote_session deletes the node session, releases the provider
    node, and drops the run-dir record."""
    ctx = _seam_env(tmp_path, monkeypatch, "seam-release")
    from mini_ork.runtime.workspace_session import get_run_session, release_remote_session

    get_run_session("seam-release", "remote", env=ctx.env)
    handle = json.loads((ctx.run_dir / ".node-provider.json").read_text())["handle"]
    assert _agent_status(seam, "/v1/sessions/seam-release") == 200

    release_remote_session("seam-release", str(ctx.run_dir), env=ctx.env)

    assert seam.released == [handle]
    assert not (ctx.run_dir / ".node-provider.json").exists()
    assert _agent_status(seam, "/v1/sessions/seam-release") == 404


def test_kill_run_destroys_node(tmp_path, monkeypatch, seam):
    """The real kill_run entrypoint kills the run, deletes the session, and
    releases the provider node through the run-dir marker."""
    ctx = _seam_env(tmp_path, monkeypatch, "seam-kill")
    from mini_ork.runtime.workspace_session import get_run_session

    get_run_session("seam-kill", "remote", env=ctx.env)
    handle = json.loads((ctx.run_dir / ".node-provider.json").read_text())["handle"]

    # A dead pid stands in for the dispatcher (no pgrep fallback in a test).
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (ctx.run_dir / ".pid").write_text(str(dead.pid))

    from mini_ork.web import control

    db = _insert_task_run(ctx.home, "seam-kill")
    control.kill_run(ctx.home, db, "seam-kill")

    assert seam.released == [handle]
    assert not (ctx.run_dir / ".node-provider.json").exists()
    assert _agent_status(seam, "/v1/sessions/seam-kill") == 404


def test_token_isolation(tmp_path, monkeypatch, seam):
    """With an ambient MO_NODE_TOKEN=wrong, a provider-backed workspace still
    authenticates with the provider-minted bearer (never os.environ)."""
    ctx = _seam_env(tmp_path, monkeypatch, "seam-token")
    monkeypatch.setenv("MO_NODE_TOKEN", "wrong-ambient-token")
    from mini_ork.runtime.backends.remote import _factory

    ws = _factory(env=ctx.env)
    ws.up()
    try:
        assert ws._provider_owned_token is True
        assert ws._token() == FAKE_TOKEN
        rc, out, _err = ws.spawn(
            ["/bin/sh", "-c", "echo authed"],
            stdin="", timeout=30.0,
            env={"PATH": os.environ.get("PATH", "")},
            cwd=str(ctx.run_dir),
        )
        assert rc == 0 and "authed" in out
    finally:
        ws.down()


def test_provider_unavailable_failure_class(tmp_path, monkeypatch):
    """A provider whose create fails makes up() raise RemoteUnavailableError,
    and the executor provisioning path reports failure_class remote_unavailable."""
    ctx = _seam_env(tmp_path, monkeypatch, "seam-unavail", provider="boom", profile="boom")
    monkeypatch.setitem(base._PROVIDERS, "boom", _BoomProvider)

    from mini_ork.runtime.backends.remote import RemoteUnavailableError, _factory

    ws = _factory(env=ctx.env)
    with pytest.raises(RemoteUnavailableError):
        ws.up()

    from mini_ork.cli import execute

    emitted: list[str] = []
    monkeypatch.setattr("mini_ork.observability.node_events.mo_node_emit",
                        lambda *a, **k: emitted.append(a[4]))
    ok = execute._provision_remote_session("seam-unavail", str(ctx.run_dir), None)
    assert ok is False
    assert emitted, "the executor emitted no failure event"
    assert json.loads(emitted[-1])["failure_class"] == "remote_unavailable"


# ---------------------------------------------------------------------------
# Acceptance: nodes doctor through the seam.
# ---------------------------------------------------------------------------


def test_nodes_doctor_fake_profile(tmp_path, monkeypatch, seam):
    """nodes doctor passes checks 0..9 for a provider-backed profile and leaves
    neither a session nor a recorded node behind."""
    from mini_ork.cli import nodes as nodes_cli

    home = tmp_path / "dhome"
    (home / "config" / "environments").mkdir(parents=True)
    (home / "config" / "environments" / "dseam.yaml").write_text(
        "provider: fake\nimage: alpine:latest\n", encoding="utf-8")
    repo = _git_repo(tmp_path / "dtarget")
    answers = repo / "answers.sh"
    answers.write_text("#!/bin/sh\necho OK\n")
    answers.chmod(0o755)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "a"],
                   check=True, capture_output=True)
    reg = tmp_path / "providers.yaml"
    reg.write_text("providers:\n"
                   f"  answers:\n    kind: executable\n    script: {answers}\n")
    for key in _SCRUB:
        monkeypatch.delenv(key, raising=False)
    for key, value in {"MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": str(home),
                       "MINI_ORK_DB": str(home / "state.db"), "MINI_ORK_PROVIDERS": str(reg),
                       "MO_REMOTE_ALLOW_DIRTY_ENGINE": "1", "MO_TARGET_CWD": str(repo)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(nodes_cli, "PYTHON", f"PYTHONPATH={REPO} {sys.executable}")

    lines: list[str] = []
    rc = nodes_cli.doctor(env_name="dseam", lanes=["answers"], out=lines.append)
    assert rc == 0, "\n".join(lines)
    statuses = [(int(ln.split(".", 1)[0]), ln.split("[", 1)[1].split("]", 1)[0])
                for ln in lines]
    assert statuses == [(i, "PASS") for i in range(0, 10)], "\n".join(lines)

    assert seam.alive == set(), "the provider node was not destroyed"
    run_dirs = [p for p in (home / "runs").iterdir() if p.is_dir()]
    assert run_dirs
    for run_dir in run_dirs:
        assert not (run_dir / ".node-provider.json").exists()
        assert not (run_dir / ".workspace-session.json").exists()


# ---------------------------------------------------------------------------
# Acceptance: the no-provider default path is byte-identical.
# ---------------------------------------------------------------------------


def test_default_path_byte_identical(tmp_path, monkeypatch):
    """With no provider anywhere, _factory builds the same RemoteWorkspace as
    before, keeps the image requirement, and Environment.to_dict() for the
    shipped default profile is unchanged."""
    from mini_ork.runtime.backends.remote import RemoteWorkspace, _NodeRef, _factory

    for key in _SCRUB:
        monkeypatch.delenv(key, raising=False)
    env = {
        "MO_NODE_URL": "http://node.example:7091",
        "MO_NODE_TOKEN": "tok",
        "MO_SANDBOX_IMAGE": "alpine:latest",
        "MINI_ORK_RUN_ID": "plain-run",
        "MINI_ORK_RUN_DIR": str(tmp_path / "runs" / "plain-run"),
    }
    ws = _factory(env=env)
    assert isinstance(ws, RemoteWorkspace)
    assert ws._node_provider is None
    assert ws._provider_owned_token is False
    assert ws._node == _NodeRef(name="<one-off>", url="http://node.example:7091",
                                token="tok", max_sessions=1)
    assert ws._token_env == "MO_NODE_TOKEN"
    assert ws._image == "alpine:latest"
    # The image requirement is NOT waived without a provider.
    with pytest.raises(RuntimeError, match="image"):
        _factory(env={**env, "MO_SANDBOX_IMAGE": ""})

    # Environment.to_dict() for the shipped default profile is unchanged.
    home = tmp_path / "dhome"
    (home / "config" / "environments").mkdir(parents=True)
    (home / "config" / "environments" / "default.yaml").write_text(
        (REPO / "config" / "environments" / "default.yaml.example").read_text(),
        encoding="utf-8")
    from mini_ork.remote.environments import load_profile

    profile = load_profile("default", env={"MINI_ORK_HOME": str(home),
                                           "MINI_ORK_ROOT": str(REPO)})
    out = profile.to_dict()
    assert "provider" not in out and "provider_options" not in out
    assert set(out) == {"name", "image", "setup", "setup_timeout_s", "env",
                        "secrets", "network", "allow_domains", "resources"}


def test_provider_applied_setup_skips_image_prepare(tmp_path, monkeypatch, seam):
    """A provider that ran the profile's setup on the node itself (a cloud
    sandbox at boot) makes up() skip the node-agent's docker image
    preparation, which a host-runtime node cannot do (the live Daytona e2e
    failed on POST /v1/images/prepare -> 500 before this)."""
    seam.setup_applied = True
    ctx = _seam_env(tmp_path, monkeypatch, "seam-setup-applied", setup="echo prepared")
    from mini_ork.runtime.workspace_session import close_run_session, get_run_session

    ws = get_run_session("seam-setup-applied", "remote", env=ctx.env)
    try:
        paths = [h.get(":path", "") for h in seam.seen_headers]
        assert "/v1/images/prepare" not in paths, paths
        assert "/v1/sessions" in paths
        rc, out, _ = ws.spawn(["/bin/sh", "-c", "echo ok"], stdin="", timeout=30.0,
                              env={"PATH": os.environ.get("PATH", "")}, cwd=str(ctx.run_dir))
        assert rc == 0 and "ok" in out
    finally:
        close_run_session("seam-setup-applied")


def test_setup_not_applied_by_provider_still_prepares_the_image(tmp_path, monkeypatch, seam):
    """Without the provider's claim, a profile setup still goes to the
    node-agent's image preparation, exactly as before (never a silent skip)."""
    ctx = _seam_env(tmp_path, monkeypatch, "seam-setup-prepare", setup="echo prepared")
    from mini_ork.runtime.backends.remote import RemoteUnavailableError
    from mini_ork.runtime.workspace_session import close_run_session, get_run_session

    try:
        try:
            get_run_session("seam-setup-prepare", "remote", env=ctx.env)
        except RemoteUnavailableError:
            pass        # the host-runtime test agent has no docker to build with
        paths = [h.get(":path", "") for h in seam.seen_headers]
        assert "/v1/images/prepare" in paths, paths
    finally:
        close_run_session("seam-setup-prepare")
