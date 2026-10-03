"""remote-nodes-14 (placement policy + observability) — acceptance tests.

One test per Acceptance bullet, each through a production entrypoint:

* routing through ``providers.dispatch_model`` — the lane kind is read from a
  real providers registry (``MINI_ORK_PROVIDERS``), not passed in by hand;
* the CLI gate through ``main._run_lifecycle_impl``;
* provisioning, node events and the run summary through ``execute.main``
  against a real uvicorn node-agent (``--runtime host``) over the production
  urllib transport, with the session bound by an environment profile;
* the cap through two runs on a ``max_sessions: 1`` node;
* the API through the ``get_task_run`` route handler.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

from mini_ork.dispatch import providers
from mini_ork.dispatch.models import DispatchRequest, DispatchResult
from mini_ork.dispatch.providers import _select_workspace

REPO = Path(__file__).resolve().parents[2]
TOKEN = "tok-placement-0000"


# --------------------------------------------------------------------------- routing


_REGISTRY = """\
providers:
  t_chat:
    kind: openai-chat
    base_url: http://127.0.0.1:9/v1
    api_key_env: T_CHAT_KEY
    model: m
  t_compat:
    kind: anthropic-compat
    base_url: http://127.0.0.1:9
    api_key_env: T_COMPAT_KEY
    model: m
"""

_PLACEMENT_KEYS = ("MO_PLACEMENT", "MO_NODE_ENV", "MO_PLACEMENT_LOCAL_ROLES", "MO_NODE_TYPE",
                   "MO_SANDBOX_SCOPE", "MO_SANDBOX_BACKEND", "MINI_ORK_RUN_DIR", "MO_NODE_ID")


@pytest.fixture
def route(tmp_path, monkeypatch):
    """``route(model, **env) -> workspace`` the REAL dispatch_model picked."""
    reg = tmp_path / "providers.yaml"
    reg.write_text(_REGISTRY)
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(reg))
    monkeypatch.setenv("T_CHAT_KEY", "k-chat-0000")
    monkeypatch.setenv("T_COMPAT_KEY", "k-compat-0000")
    monkeypatch.setenv("MO_TARGET_CWD", str(tmp_path))   # a target outside the framework tree
    for key in _PLACEMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    seen: dict[str, str] = {}

    def capture(request, spec):
        seen[request.model] = request.workspace
        return DispatchResult(ok=True, rc=0, text="ok", model=request.model)

    for model in ("t_chat", "t_compat"):
        monkeypatch.setitem(providers.MODEL_DISPATCH_BACKENDS, model, capture)

    def _route(model: str, workspace: str = "host", **env: str) -> str:
        with monkeypatch.context() as m:
            for key, val in env.items():
                m.setenv(key, val)
            res = providers.dispatch_model(DispatchRequest(model=model, prompt="p",
                                                           workspace=workspace), str(REPO))
        assert res.ok, res.error
        return seen.pop(model)

    return _route


def test_dispatch_routing_truth_table(route):
    """placement × lane kind × explicit request × MO_PLACEMENT_LOCAL_ROLES,
    decided by dispatch_model with the kind read from the registry."""
    remote = {"MO_PLACEMENT": "remote"}
    assert route("t_compat", **remote) == "remote"          # anthropic-compat reviewer goes remote
    assert route("t_chat", **remote) == "host"              # openai-chat stays local
    assert route("t_compat", workspace="docker", **remote) == "docker"   # explicit request wins
    roles = {**remote, "MO_PLACEMENT_LOCAL_ROLES": "planner,reviewer"}
    assert route("t_compat", MO_NODE_TYPE="planner", **roles) == "host"
    assert route("t_compat", MO_NODE_TYPE="implementer", **roles) == "remote"
    assert route("t_compat", MO_PLACEMENT="local") == "host"


def test_placement_unset_routes_exactly_as_before(route):
    """With placement unset nothing new applies — including to an openai-chat
    lane under scope=agent, which the first cut silently pulled back to host."""
    agent = {"MO_SANDBOX_SCOPE": "agent", "MO_SANDBOX_BACKEND": "docker"}
    assert route("t_chat", **agent) == "docker"
    assert route("t_compat", **agent) == "docker"
    assert route("t_compat") == "host"
    assert route("t_chat", MO_SANDBOX_SCOPE="tool", MO_SANDBOX_BACKEND="docker") == "host"
    # The pure selector, same matrix as tests/test_dispatch_isolation_selector_py.py.
    assert _select_workspace("docker", {}) == "docker"
    assert _select_workspace("host", {"MO_SANDBOX_SCOPE": "agent"}) == "host"
    assert _select_workspace("host", {}) == "host"


# --------------------------------------------------------------------------- CLI


def _profiles_home(tmp_path) -> Path:
    home = tmp_path / "home"
    envs = home / "config" / "environments"
    envs.mkdir(parents=True)
    (envs / "alpha.yaml").write_text("image: alpine:latest\n")
    (envs / "beta.yaml").write_text("image: alpine:latest\nnetwork: allowlist\n")
    (envs / "broken.yaml").write_text("image: alpine:latest\ncolour: blue\n")
    return home


@pytest.mark.parametrize("argv, why", [
    (["--placement", "remote", "--env", "missing"], "'missing' not found"),
    (["--placement", "remote"], "requires --env"),
    (["--placement=remote", "--env=broken"], "'broken' is invalid"),
])
def test_cli_remote_without_resolvable_env_exits_2_listing_profiles(
        tmp_path, monkeypatch, capsys, argv, why):
    from mini_ork.cli import main as cli

    home = _profiles_home(tmp_path)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    for key in _PLACEMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    rc = cli._run_lifecycle_impl([*argv, "code-fix", str(tmp_path / "k.md")], str(REPO), None)
    err = capsys.readouterr().err
    assert rc == 2, err
    assert why in err
    assert "Available profiles: alpha, beta, broken" in err
    assert not os.environ.get("MO_PLACEMENT")        # a refused run publishes nothing


def test_gen_profile_persists_placement_only_when_set(tmp_path, monkeypatch):
    from mini_ork.cli import main as cli

    kickoff = tmp_path / "k.md"
    kickoff.write_text("# Fix\n\n## Success\n- ok\n")
    agents = tmp_path / "agents.yaml"
    agents.write_text("lanes: {}\n")
    monkeypatch.chdir(tmp_path)
    for key in _PLACEMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    plain = cli.gen_profile(kickoff, str(REPO), "code-fix", "code_fix",
                            tmp_path / "p1.json", agents)
    assert "placement" not in plain and "environment" not in plain
    monkeypatch.setenv("MO_PLACEMENT", "remote")
    monkeypatch.setenv("MO_NODE_ENV", "alpha")
    cli.gen_profile(kickoff, str(REPO), "code-fix", "code_fix", tmp_path / "p2.json", agents)
    on_disk = json.loads((tmp_path / "p2.json").read_text())
    assert (on_disk["placement"], on_disk["environment"]) == ("remote", "alpha")


# --------------------------------------------------------------------------- live node


def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return int(sk.getsockname()[1])


@pytest.fixture(scope="module")
def node_url(tmp_path_factory):
    """One real node-agent for the module (``--runtime host``: no docker), so
    the engine bundle is uploaded once."""
    import uvicorn

    from mini_ork.remote.node_agent.app import create_app

    os.environ["MO_NODE_TOKEN_T"] = TOKEN
    app = create_app(state_dir=tmp_path_factory.mktemp("agent"), token_env="MO_NODE_TOKEN_T",
                     runtime="host")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=_free_port(),
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "node-agent did not start"
    yield f"http://127.0.0.1:{server.config.port}"
    server.should_exit = True
    thread.join(timeout=10)


def _get_session(url: str, run_id: str) -> dict | None:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"{url}/v1/sessions/{run_id}",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _target_repo(base: Path) -> str:
    repo = base / "target-repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("target\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return str(repo)


@pytest.fixture
def remote_home(tmp_path, monkeypatch, node_url):
    """A home with the node registered, a profile bound to it, a seeded DB, the
    test lanes and a target checkout — everything ``--placement remote --env
    tprof`` resolves at run time."""
    home = tmp_path / ".mini-ork"
    (home / "config" / "environments").mkdir(parents=True)
    (home / "config" / "nodes.yaml").write_text(
        f"nodes:\n  tnode:\n    url: {node_url}\n    token_env: MO_NODE_TOKEN_T\n"
        "    max_sessions: 1\n")
    (home / "config" / "environments" / "tprof.yaml").write_text(
        "node: tnode\nimage: alpine:latest\nresources: {cpus: 2, memory: 1g}\nnetwork: full\n")
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    reg = tmp_path / "providers.yaml"
    reg.write_text(_REGISTRY)
    for key in _PLACEMENT_KEYS + ("MO_NODE", "MO_NODE_URL", "MO_NODE_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key, val in {"MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db,
                     "MINI_ORK_PROVIDERS": str(reg), "MO_NODE_TOKEN_T": TOKEN,
                     "MO_REMOTE_ALLOW_DIRTY_ENGINE": "1", "MO_PLACEMENT": "remote",
                     "MO_NODE_ENV": "tprof", "MO_TARGET_CWD": _target_repo(tmp_path),
                     "MINI_ORK_TASK_CLASS": "code_fix"}.items():
        monkeypatch.setenv(key, val)
    return home, db


def _start_run(home: Path, db: str, run_id: str, monkeypatch, workflow: str) -> Path:
    rd = home / "runs" / run_id
    rd.mkdir(parents=True)
    wf = rd / "wf.yaml"
    wf.write_text(workflow)
    plan = rd / "plan.json"
    plan.write_text(json.dumps({"objective": "o", "decomposition": []}))
    con = sqlite3.connect(db)
    try:
        con.execute("INSERT INTO task_runs (id, task_class, workflow_version, kickoff_path, status,"
                    " cost_usd, created_at, updated_at) VALUES (?, 'x', 'v1', 'k.md', 'planned',"
                    " 0, strftime('%s','now'), strftime('%s','now'))", (run_id,))
        con.commit()
    finally:
        con.close()
    for key, val in {"MINI_ORK_WORKFLOW": str(wf), "MINI_ORK_PLAN_PATH": str(plan),
                     "MINI_ORK_RUN_DIR": str(rd), "MINI_ORK_RUN_ID": run_id}.items():
        monkeypatch.setenv(key, val)
    return rd


def _events(db: str, run_id: str) -> list[tuple[str, dict]]:
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT event_type, payload_json FROM run_events WHERE run_id=? "
                           "ORDER BY rowid", (run_id,)).fetchall()
    finally:
        con.close()
    return [(etype, json.loads(payload or "{}")) for etype, payload in rows]


_TWO_NODES = ("dispatch_mode: serial\nnodes:\n"
              "  - {name: impl, type: researcher, description: edit, model_lane: t_compat}\n"
              "  - {name: chat, type: reviewer, description: review, model_lane: t_chat}\n")


def test_remote_run_provisions_first_and_tags_where_each_node_ran(remote_home, monkeypatch, node_url):
    """execute.main under --placement remote --env tprof: the profile-bound
    session is set up (remote.setup.step) before the first node_start; the
    tree-bound node is tagged remote with its host + session, the tool-less
    HTTP lane local; teardown reports remote.run.summary."""
    import mini_ork.cli.execute as ex

    home, db = remote_home
    run_id = f"run-pl-{uuid.uuid4().hex[:8]}"
    _start_run(home, db, run_id, monkeypatch, _TWO_NODES)
    during: dict = {}

    def fake_llm(_task_class, node_type, _prompt):
        during.setdefault("session", _get_session(node_url, run_id))   # probe mid-run
        return 0, '{"verdict": "pass"}'

    assert ex.main([], root=str(REPO), dispatch_fn=fake_llm) == 0
    sess = during["session"]
    assert sess and sess["resources"] == {"cpus": 2, "memory": "1g"} and sess["network"] == "full"

    events = _events(db, run_id)
    kinds = [e for e, _ in events]
    first_node = kinds.index("node_start")
    setup = [(p["step"], p["status"]) for e, p in events[:first_node] if e == "remote.setup.step"]
    for step in ("health", "engine", "session_up", "initial_sync", "post_sync"):
        assert (step, "ok") in setup, setup
    assert not any(e == "remote.setup.step" for e in kinds[first_node:])   # set up once, up front

    starts = {p["node_id"]: p for e, p in events if e == "node_start"}
    assert starts["impl"]["placement"] == "remote"
    assert starts["impl"]["node_host"] == "tnode" and starts["impl"]["session_id"]
    assert starts["chat"]["placement"] == "local" and "node_host" not in starts["chat"]

    summary = [p for e, p in events if e == "remote.run.summary"]
    assert len(summary) == 1
    assert summary[0]["node_host"] == "tnode" and summary[0]["sync_up_bytes"] > 0
    assert summary[0]["setup_s"] > 0 and summary[0]["remote_wall_s"] >= 0
    assert _get_session(node_url, run_id) is None        # torn down with the run


def test_full_node_queues_then_fails_remote_unavailable_before_any_node(
        remote_home, monkeypatch, node_url):
    """max_sessions: 1 and one live session: a second run waits (emitting
    remote.queue.wait), then fails remote_unavailable — before any LLM spend.
    The run that holds the slot is never queued behind itself after a restart."""
    import mini_ork.cli.execute as ex
    from mini_ork.runtime.backends.remote import _factory
    from mini_ork.runtime.workspace_session import close_run_session

    home, db = remote_home
    holder = f"run-hold-{uuid.uuid4().hex[:8]}"
    holder_dir = _start_run(home, db, holder, monkeypatch, _TWO_NODES)
    assert ex._provision_remote_session(holder, str(holder_dir), db)
    try:
        assert _get_session(node_url, holder)
        # A restarted control plane re-creating the holder's workspace passes the gate.
        restarted = _factory(env={**os.environ, "MINI_ORK_RUN_ID": holder,
                                  "MO_REMOTE_QUEUE_WAIT_S": "0"})
        assert restarted._await_capacity()["sessions"] == 1

        waiter = f"run-wait-{uuid.uuid4().hex[:8]}"
        _start_run(home, db, waiter, monkeypatch, _TWO_NODES)
        monkeypatch.setenv("MO_REMOTE_QUEUE_WAIT_S", "1.5")
        calls: list[str] = []
        t0 = time.monotonic()
        rc = ex.main([], root=str(REPO),
                     dispatch_fn=lambda *a: (calls.append(a[1]), (0, "{}"))[1])
        assert rc == 1 and calls == []                    # no node ran, nothing was spent
        assert time.monotonic() - t0 >= 1.5
        events = _events(db, waiter)
        assert any(e == "remote.queue.wait" for e, _ in events)
        failed = [p for e, p in events if e == "remote.run.failed"]
        assert failed and failed[0]["failure_class"] == "remote_unavailable"
        assert not any(e == "node_start" for e, _ in events)
        con = sqlite3.connect(db)
        try:
            status = con.execute("SELECT status FROM task_runs WHERE id=?", (waiter,)).fetchone()[0]
        finally:
            con.close()
        assert status == "failed"
        assert _get_session(node_url, waiter) is None
    finally:
        close_run_session(holder)


# --------------------------------------------------------------------------- node events


def test_node_start_placement_by_node_type(tmp_path, monkeypatch):
    """Under --placement remote implementer + verifier events say remote (with
    the session from the marker), classify + transform say local. Unset
    placement leaves the payload exactly as before (no placement key)."""
    import mini_ork.cli.execute as ex
    from mini_ork.runtime.workspace_session import session_marker_path

    home = tmp_path / "home"
    home.mkdir()
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    reg = tmp_path / "providers.yaml"
    reg.write_text(_REGISTRY)
    for key in _PLACEMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(reg))
    rd = tmp_path / "run"
    rd.mkdir()
    session_marker_path(rd).write_text(json.dumps(
        {"backend": "remote", "session_id": "sid-1", "node": {"name": "tnode", "url": "u"}}))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"objective": "o"}))
    for node_type in ("implementer", "verifier", "transform", "classify"):
        monkeypatch.setitem(ex.NODE_HANDLER_REGISTRY, node_type, lambda ctx: (0, "done"))

    def dispatch(run_id: str) -> dict:
        for node_type in ("implementer", "verifier", "transform", "classify"):
            ex.dispatch_node((f"n-{node_type}", node_type, "d", "", "serial", "", "t_compat", ""),
                             root=str(REPO), run_dir=str(rd), plan_path=str(plan),
                             task_class="code_fix", db=db, run_id=run_id,
                             dispatch_fn=lambda *a: (0, "{}"))
        return {p["node_type"]: p for e, p in _events(db, run_id) if e == "node_start"}

    plain = dispatch("run-plain")
    assert all("placement" not in p for p in plain.values())

    monkeypatch.setenv("MO_PLACEMENT", "remote")
    tagged = dispatch("run-remote")
    for node_type in ("implementer", "verifier"):
        assert tagged[node_type]["placement"] == "remote"
        assert (tagged[node_type]["node_host"], tagged[node_type]["session_id"]) == ("tnode", "sid-1")
    for node_type in ("transform", "classify"):
        assert tagged[node_type]["placement"] == "local"


# --------------------------------------------------------------------------- API


def test_run_detail_reports_placement_and_node_host(tmp_path):
    """GET /api/v1/task-runs/{id} carries placement, environment and node_host
    — from the live marker, and still after teardown (from the summary)."""
    from mini_ork.observability.node_events import mo_node_emit
    from mini_ork.runtime.workspace_session import session_marker_path
    from mini_ork.web.db import StateDB
    from mini_ork.web.routes.run_detail import get_task_run

    home = tmp_path / "home"
    home.mkdir()
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    con = sqlite3.connect(db)
    try:
        con.execute("INSERT INTO task_runs (id, task_class, workflow_version, kickoff_path, status,"
                    " cost_usd, created_at, updated_at) VALUES ('r-api', 'x', 'v1', 'k.md',"
                    " 'executing', 0, strftime('%s','now'), strftime('%s','now'))")
        con.commit()
    finally:
        con.close()
    rd = home / "runs" / "r-api"
    rd.mkdir(parents=True)
    (rd / "run_profile.json").write_text(json.dumps({"placement": "remote", "environment": "tprof"}))
    marker = session_marker_path(rd)
    marker.write_text(json.dumps({"session_id": "s", "node": {"name": "tnode", "url": "u"}}))

    live = get_task_run("r-api", db=StateDB(Path(db)), home=home)
    assert (live["placement"], live["environment"], live["node_host"]) == ("remote", "tprof", "tnode")

    marker.unlink()
    mo_node_emit("r-api", "remote-workspace", "workspace", "remote.run.summary",
                 json.dumps({"node_host": "tnode"}), db=db)
    done = get_task_run("r-api", db=StateDB(Path(db)), home=home)
    assert done["node_host"] == "tnode"

    (rd / "run_profile.json").write_text("{}")
    default = get_task_run("r-api", db=StateDB(Path(db)), home=home)
    assert not {"placement", "environment", "node_host"} & set(default)


def test_profile_setup_runs_image_prepare_and_the_session_uses_its_tag(tmp_path, monkeypatch):
    """A profile with a setup script provisions on the PREPARED image (epic 12
    cache); a failing setup is a `fail` step and no session is created."""
    from types import SimpleNamespace

    from mini_ork.runtime.backends.remote import RemoteUnavailableError, RemoteWorkspace, _NodeRef

    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    prof = SimpleNamespace(name="p", setup="apt-get install -y jq", setup_timeout_s=60.0,
                           resources={"cpus": 2}, network="allowlist", allow_domains=["pypi.org"])

    def workspace(prepare_reply):
        ws = RemoteWorkspace(node=_NodeRef(name="n", url="http://n", token="t"), run_id="r-img",
                             image="base:1", drive_root=str(tmp_path), profile=prof)
        calls: list[tuple[str, str, dict | None]] = []

        def fake_json(method, path, payload=None, **kw):
            calls.append((method, path, payload))
            if path == "/v1/health":
                return {"sessions": 0, "engine_shas": [{"sha": "abc"}]}
            if path == "/v1/images/prepare":
                if isinstance(prepare_reply, Exception):
                    raise prepare_reply
                return prepare_reply
            return {"sid": "s1"}

        ws._json = fake_json
        ws._current_engine_sha = lambda _sp: "abc"
        ws._sync_up = lambda *, force=False: None
        ws._upload_mo_home = lambda: None
        steps: list[tuple[str, str]] = []
        ws._emit_sync_event = lambda etype, **f: steps.append((f.get("step", etype), f.get("status", "")))
        return ws, calls, steps

    ws, calls, steps = workspace({"tag": "mo-prep:k1", "cached": False})
    ws.up()
    paths = [p for _m, p, _b in calls]
    assert paths.index("/v1/images/prepare") < paths.index("/v1/sessions")
    prep = next(b for _m, p, b in calls if p == "/v1/images/prepare")
    assert (prep["base_image"], prep["setup"]) == ("base:1", "apt-get install -y jq")
    sess = next(b for _m, p, b in calls if p == "/v1/sessions")
    domains = sess.pop("allow_domains")
    assert sess.pop("engine_sha") == "abc"   # D8: the node mounts this engine (epic 15)
    assert sess == {"run_id": "r-img", "image": "mo-prep:k1", "resources": {"cpus": 2},
                    "network": "allowlist"}
    assert domains[0] == "pypi.org"          # the profile's own, then the lanes' endpoints (epic 13)
    assert ("image_prepare", "ok") in steps

    ws, calls, steps = workspace(RemoteUnavailableError("HTTP 500: image_prepare_failed"))
    with pytest.raises(RemoteUnavailableError):
        ws.up()
    assert ("image_prepare", "fail") in steps
    assert "/v1/sessions" not in [p for _m, p, _b in calls]
