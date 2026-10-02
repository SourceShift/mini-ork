"""remote-nodes-15: control-plane defects the simulated-remote E2E surfaced.

Each was invisible to the unit suites because they all run the node-agent in
the SAME process and checkout as the control plane; a separate node host (no
shared filesystem, its own interpreter) exposed them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from mini_ork.runtime.backends.remote import RemoteWorkspace, _NodeRef


def _ws(tmp_path: Path, run_dir: Path, sid_reply: str) -> tuple[RemoteWorkspace, list]:
    ws = RemoteWorkspace(node=_NodeRef(name="n", url="http://n", token="t", max_sessions=4),
                         run_id="r-sync", image="i", drive_root=str(tmp_path), run_dir=str(run_dir))
    calls: list = []

    def fake_json(method, path, payload=None, **kw):
        calls.append((method, path))
        if path == "/v1/health":
            return {"sessions": 0, "engine_shas": [{"sha": "abc"}]}
        return {"sid": sid_reply}

    ws._json = fake_json
    ws._current_engine_sha = lambda _sp: "abc"
    ws._upload_mo_home = lambda: None
    ws._emit_sync_event = lambda *a, **k: None
    seen: dict = {}
    ws._sync_up = lambda *, force=False: seen.setdefault("base", ws._last_synced)
    return ws, [seen]


def test_a_new_process_reuses_the_sync_base_only_for_the_same_session(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / ".remote-sync-state.json").write_text(json.dumps(
        {"run_id": "r-sync", "sid": "s1", "commit": "c0", "tree": "t0"}))
    same, [seen] = _ws(tmp_path, run_dir, "s1")
    same.up()
    assert seen["base"] == ("c0", "t0")             # incremental from the saved base
    fresh, [seen2] = _ws(tmp_path, run_dir, "s2")   # the node made a NEW session
    fresh.up()
    assert seen2["base"] is None                    # its replica is empty: full sync


def test_the_sync_base_is_saved_after_each_sync(tmp_path):
    ws = RemoteWorkspace(node=_NodeRef(name="n", url="http://n", token="t"), run_id="r-save",
                         image="i", drive_root=str(tmp_path), run_dir=str(tmp_path))
    ws._sid, ws._last_synced = "s9", ("c1", "t1")
    ws._save_sync_state()
    state = json.loads((tmp_path / ".remote-sync-state.json").read_text())
    assert state == {"run_id": "r-save", "sid": "s9", "commit": "c1", "tree": "t1"}


def test_release_deletes_the_node_session_by_run_id(tmp_path, monkeypatch):
    from mini_ork.runtime import workspace_session
    from mini_ork.runtime.backends import remote

    deleted: list = []
    monkeypatch.setattr(remote.RemoteWorkspace, "_request",
                        lambda self, method, path, **kw: deleted.append((method, path)) or b"")
    (tmp_path / ".remote-sync-state.json").write_text("{}")
    workspace_session.session_marker_path(tmp_path).write_text("{}")
    workspace_session.release_remote_session(
        "run-rel", str(tmp_path), env={"MO_NODE_URL": "http://n", "MO_NODE_TOKEN": "t",
                                       "MO_SANDBOX_IMAGE": "i"})
    assert ("DELETE", "/v1/sessions/run-rel") in deleted
    assert not (tmp_path / ".remote-sync-state.json").exists()
    assert not workspace_session.session_marker_path(tmp_path).exists()


def test_remote_checks_run_under_the_nodes_python(tmp_path):
    """Verifier argv is built with sys.executable — a laptop path (a venv in the
    checkout) that does not exist on the node."""
    from mini_ork.runtime.contract import _node_interpreter

    assert _node_interpreter([sys.executable, "/opt/mini-ork/v/test.py"]) == \
        ["python3", "/opt/mini-ork/v/test.py"]
    assert _node_interpreter(["bash", "x.sh"]) == ["bash", "x.sh"]


def test_session_containers_run_as_the_node_agents_uid(tmp_path):
    """The node-agent creates (and owns) the mounted run dirs; the image's fixed
    `agent` user could not write them on a real node."""
    from mini_ork.remote.node_agent.sessions import SessionManager

    calls: list = []
    SessionManager(tmp_path, runtime="docker",
                   _run_fn=lambda a: calls.append(a) or subprocess.CompletedProcess(a, 0, "cid", "")
                   ).create(run_id="u", image="img")
    run = next(c for c in calls if c[:2] == ["docker", "run"])
    assert run[run.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"


def test_execute_records_the_dispatcher_pid_kill_run_reads(tmp_path, monkeypatch):
    """kill_run signals run_dir/.pid; nothing had written it since the bash
    runtime was removed, so kill_run could not stop a run."""
    import mini_ork.cli.execute as ex

    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / ".mini-ork"
    home.mkdir()
    db = str(home / "state.db")
    subprocess.run(["bash", str(repo / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    rd = home / "runs" / "run-pid"
    rd.mkdir(parents=True)
    plan = rd / "plan.json"
    plan.write_text(json.dumps({"objective": "o", "decomposition": []}))
    wf = tmp_path / "wf.yaml"
    wf.write_text("dispatch_mode: serial\nnodes:\n  - {name: res1, type: researcher, description: r}\n")
    for key, val in {"MINI_ORK_ROOT": str(repo), "MINI_ORK_WORKFLOW": str(wf), "MINI_ORK_HOME": str(home),
                     "MINI_ORK_DB": db, "MINI_ORK_PLAN_PATH": str(plan), "MINI_ORK_RUN_DIR": str(rd),
                     "MINI_ORK_RUN_ID": "run-pid", "MINI_ORK_TASK_CLASS": "code_fix"}.items():
        monkeypatch.setenv(key, val)
    seen: dict = {}

    def llm(_task_class, _node_type, _prompt):
        seen["pid"] = (rd / ".pid").read_text().strip() if (rd / ".pid").exists() else None
        return 0, '{"verdict": "pass"}'

    ex.main([], root=str(repo), dispatch_fn=llm)
    assert seen["pid"] == str(os.getpid())
    assert not (rd / ".pid").exists()          # removed when the run ends


def test_engine_from_a_shallow_clone_stages_on_a_bare_node(tmp_path, monkeypatch):
    """CI checks out shallow; a bundle of HEAD there names the missing parent as
    a prerequisite, which a node with no repo cannot satisfy. The engine bundle
    is one parentless commit of HEAD's tree, so it always stages."""
    import subprocess as sp

    from mini_ork.remote.node_agent.engines import EngineManager

    origin = tmp_path / "origin"
    origin.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        sp.run(["git", *args], cwd=origin, check=True, capture_output=True)
    for i in range(2):
        (origin / "mini_ork.py").write_text(f"V = {i}\n")
        sp.run(["git", "add", "-A"], cwd=origin, check=True, capture_output=True)
        sp.run(["git", "commit", "-q", "-m", f"c{i}"], cwd=origin, check=True, capture_output=True)
    shallow = tmp_path / "shallow"
    sp.run(["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(shallow)], check=True,
           capture_output=True)
    head = sp.run(["git", "-C", str(shallow), "rev-parse", "HEAD"], capture_output=True, text=True,
                  check=True).stdout.strip()
    ws = RemoteWorkspace(node=_NodeRef(name="n", url="http://n", token="t"), run_id="r-eng", image="i",
                         drive_root=str(tmp_path), engine_root=str(shallow))
    bundle = ws._bundle_engine(sp)
    elsewhere = tmp_path / "node"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)                       # no repo around the node-agent
    EngineManager(tmp_path / "state", "0.1.0").stage_bundle(head, bundle)
    assert (tmp_path / "state" / "engines" / head / "mini_ork.py").read_text() == "V = 1\n"
    refs = sp.run(["git", "-C", str(shallow), "for-each-ref", "refs/mo/"], capture_output=True,
                  text=True).stdout
    assert refs == ""                                  # the temp bundle ref is cleaned up
