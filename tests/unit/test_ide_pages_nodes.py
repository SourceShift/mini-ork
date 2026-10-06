"""``board page nodes`` — the Nodes & sandboxes page."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.ide_pages import nodes
from mini_ork.stores import migrate as mig

from test_ide_pages_orch import check_page

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    engine = tmp_path / "engine"
    (engine / "config").mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_ROOT", str(engine))
    monkeypatch.setattr(nodes, "_docker_sandboxes", lambda: [])
    return h


def _run(home: Path, run_id: str, marker: dict | None = None) -> None:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute("INSERT INTO task_runs (id, recipe, status, created_at, updated_at, task_class, kickoff_path, "
                "workflow_version) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, "code-fix", "executing", now, now, "code_fix", "", "latest"))
    con.commit()
    con.close()
    if marker is not None:
        run_dir = home / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / ".workspace-session.json").write_text(json.dumps({"run_id": run_id, **marker}))


def _lease(home: Path, run_id: str) -> None:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute("INSERT INTO run_leases (run_id, owner_token, acquired_at, expires_at, renewed_at) VALUES (?,?,?,?,?)",
                (run_id, "tok", now, now + 600, now))
    con.commit()
    con.close()


def test_every_tab_builds_without_probing(home: Path, monkeypatch) -> None:
    def no_network(*_a, **_k):
        raise AssertionError("the page must not touch the network")

    monkeypatch.setattr("urllib.request.urlopen", no_network)
    for key, _label in nodes.TABS:
        page = nodes.build(home, key, {})
        check_page(page, "nodes")
        assert page["tab"] == key and page["sections"] and page["errors"] == {}
    page = nodes.build(home, "nodes", {})
    assert page["chips"] == [{"t": "local only", "c": "sub"}]
    rows = page["sections"][0]["rows"]
    assert len(rows) == 1 and rows[0]["cells"][0]["t"].startswith("local · ")


def test_registered_nodes_and_live_runs(home: Path, monkeypatch) -> None:
    monkeypatch.setenv("MO_NODE_TOKEN_A", "tok-SECRET-value")
    (home / "config").mkdir()
    (home / "config" / "nodes.yaml").write_text(
        "nodes:\n  hetzner-a:\n    url: http://10.0.0.5:7091\n    token_env: MO_NODE_TOKEN_A\n")
    _run(home, "run-1791000000-local0")
    _lease(home, "run-1791000000-local0")
    _run(home, "run-1791000001-stale0")  # executing but no lease: not live
    _run(home, "run-1791000002-remote", {"backend": "remote", "session_id": "s-1",
                                         "node": {"name": "hetzner-a", "url": "http://10.0.0.5:7091"}})
    _lease(home, "run-1791000002-remote")
    page = nodes.build(home, "nodes", {})
    assert page["chips"] == [{"t": "1 remote · not probed", "c": "sub"}]
    rows = page["sections"][0]["rows"]
    local, remote = rows[0], rows[1]
    assert local["cells"][3]["t"] == "1"
    assert [c["t"] for c in remote["cells"][:4]] == ["hetzner-a", "not probed", "node-agent", "1"]
    assert remote["do"] == {"cli": ["nodes", "ping", "hetzner-a"]}
    assert page["sections"][1]["title"] == "Doctor · hetzner-a"
    remote_runs = page["sections"][2]
    assert remote_runs["title"] == "Remote run · run-remote"
    assert remote_runs["items"][0]["mc"] == "blue"
    assert "tok-SECRET-value" not in json.dumps(page)


def test_environment_profiles(home: Path) -> None:
    envs = home / "config" / "environments"
    envs.mkdir(parents=True)
    (envs / "dev.yaml").write_text("image: python:3.11\nnetwork: full\nsecrets: []\nallow_domains: []\n"
                                   "resources:\n  cpus: 2\n  memory: 4g\n")
    (envs / "bad.yaml").write_text("image: python:3.11\nenv:\n  OPENAI_API_KEY: x\n")
    rows = {r["cells"][0]["t"]: [c["t"] for c in r["cells"]]
            for r in nodes.build(home, "env", {})["sections"][0]["rows"]}
    assert rows["dev"][1].startswith("python:3.11 · network full · cpus 2 memory 4g")
    assert rows["dev"][2] == "local"
    assert rows["bad"][2] == "invalid"


def test_sandboxes_map_to_runs_and_flag_leaks(home: Path, monkeypatch) -> None:
    _run(home, "run-1791000003-sbx000", {"backend": "docker", "session_id": "abc123def456789"})
    monkeypatch.setattr(nodes, "_docker_sandboxes", lambda: [
        {"id": "abc123def456", "name": "mo-ws-1", "created": "2099-01-01 00:00:00 +0000 UTC", "age": "1 minute ago"},
        {"id": "fff000", "name": "mo-ws-2", "created": "2001-01-01 00:00:00 +0000 UTC", "age": "3 days ago"},
    ])
    table = nodes.build(home, "sandboxes", {})["sections"][0]
    rows = {r["cells"][0]["t"]: r for r in table["rows"]}
    assert rows["mo-ws-1"]["cells"][1]["t"] == "run-sbx000"
    assert rows["mo-ws-1"]["do"] == {"run": "run-1791000003-sbx000", "title": ""}
    assert rows["mo-ws-2"]["cells"][1] == {"t": "leaked", "c": "red", "mono": False, "b": False}
    assert "1 waiting" in table["note"]
    assert table["actions"][0]["do"]["cli"] == ["sandbox-gc"]


def test_docker_unavailable(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(nodes, "_docker_sandboxes", lambda: None)
    table = nodes.build(home, "sandboxes", {})["sections"][0]
    assert table["rows"][0]["cells"][0]["t"] == "Docker unavailable"
