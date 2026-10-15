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
    assert remote["do"]["cli"] == ["nodes", "ping", "hetzner-a"]
    assert remote["do"]["home"] is False
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
    assert rows["dev"][2].startswith("python:3.11 · network full · cpus 2 memory 4g")
    assert rows["dev"][1] == "local"
    assert rows["bad"][3] == "invalid"


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


# ── cloud providers (Daytona, E2B, Modal) ───────────────────────────────────


def _cloud_profile(home: Path, name: str = "daytona", provider: str = "daytona") -> None:
    envs = home / "config" / "environments"
    envs.mkdir(parents=True, exist_ok=True)
    (envs / f"{name}.yaml").write_text(
        f"provider: {provider}\nprovider_options:\n  snapshot: mo-agent\n"
        "secrets: [ANTHROPIC_AUTH_TOKEN]\nsetup: |\n  echo hi\n")


def _remote_node_start(home: Path, run_id: str, host: str, *, status: str = "published") -> None:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute("INSERT INTO task_runs (id, recipe, status, created_at, updated_at, task_class, kickoff_path, "
                "workflow_version) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, "code-fix", status, now, now, "code_fix", "", "latest"))
    con.execute("INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (f"ev-{run_id}", run_id, "node_start",
                 json.dumps({"node_id": "implementer", "placement": "remote", "node_host": host}),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))))
    con.commit()
    con.close()


def test_cloud_profile_shows_its_provider_not_local(home: Path, monkeypatch) -> None:
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    _cloud_profile(home)
    row = nodes.build(home, "env", {})["sections"][0]["rows"][0]
    cells = [c["t"] for c in row["cells"]]
    assert cells[0] == "daytona"
    assert cells[1].startswith("daytona · ") and cells[1] != "local"
    assert "snapshot mo-agent" in cells[2] and "setup script" in cells[2]
    assert row["do"]["cli"] == ["nodes", "doctor", "--env", "daytona", "--no-llm"]


def test_readiness_names_the_missing_sdk_or_credential(home: Path, monkeypatch) -> None:
    _cloud_profile(home)
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    monkeypatch.setattr(nodes.importlib.util, "find_spec", lambda name: None)
    ready = nodes.build(home, "env", {})["sections"][0]["rows"][0]["cells"][3]
    assert ready["c"] == "red" and "mini-ork[daytona]" in ready["t"]

    monkeypatch.setattr(nodes.importlib.util, "find_spec", lambda name: object())
    ready = nodes.build(home, "env", {})["sections"][0]["rows"][0]["cells"][3]
    assert ready == {"t": "needs DAYTONA_API_KEY", "c": "red", "mono": False, "b": False}

    # the SELECTED home's store counts (names only), not the server process's
    (home / "config" / "secrets.local.sh").write_text("export DAYTONA_API_KEY='dtn_SECRET_VALUE'\n")
    (home / "config" / "secrets.local.sh").chmod(0o600)
    page = nodes.build(home, "env", {})
    ready = page["sections"][0]["rows"][0]["cells"][3]
    assert ready["t"] == "ready" and ready["c"] == "green"
    assert "dtn_SECRET_VALUE" not in json.dumps(page)


def test_cloud_providers_table_lists_every_cloud_with_its_profiles(home: Path, monkeypatch) -> None:
    monkeypatch.setenv("E2B_API_KEY", "e2b_SECRET")
    _cloud_profile(home, "e2b-run", "e2b")
    page = nodes.build(home, "nodes", {})
    table = next(s for s in page["sections"] if s.get("title") == "Cloud providers")
    rows = {r["cells"][0]["t"]: [c["t"] for c in r["cells"]] for r in table["rows"]}
    assert {"daytona", "e2b", "modal"} <= set(rows) and "static" not in rows
    assert rows["e2b"][4] == "e2b-run"
    assert "E2B_API_KEY (env)" in rows["e2b"][3]
    assert "MODAL_TOKEN_ID (missing)" in rows["modal"][3]
    assert "e2b_SECRET" not in json.dumps(page)


def test_finished_remote_runs_still_show_with_their_provider(home: Path) -> None:
    # No session marker: a remote run deletes it when it ends.
    _remote_node_start(home, "run-1791915762-20011", "daytona:mo-run-1791915762-20011")
    _remote_node_start(home, "run-1791917474-996000", "e2b:icg2z2j4eby35i3znpkur")
    page = nodes.build(home, "nodes", {})
    remote = next(s for s in page["sections"] if str(s.get("title", "")).startswith("Remote run"))
    titles = [it["t"] for it in remote["items"]]
    assert "run-20011 · daytona" in titles and "run-996000 · e2b" in titles
    assert any("on daytona:mo-run-1791915762-20011" in it["sub"] for it in remote["items"])


def test_unreleased_cloud_sandbox_is_listed_and_flagged(home: Path) -> None:
    _run(home, "run-1791000009-cloud0")
    (home / "runs" / "run-1791000009-cloud0").mkdir(parents=True)
    (home / "runs" / "run-1791000009-cloud0" / ".node-provider.json").write_text(json.dumps(
        {"provider": "daytona", "handle": "13dca0c8-2143-435a", "run_id": "run-1791000009-cloud0",
         "created_at": "2026-10-13T17:00:35Z"}))
    page = nodes.build(home, "sandboxes", {})
    cloud = next(s for s in page["sections"] if s.get("title") == "Cloud sandboxes")
    cells = [c["t"] for c in cloud["rows"][0]["cells"]]
    assert cells[:4] == ["daytona", "13dca0c8-2143-435a", "run-cloud0", "not released"]
    assert "1 not released" in cloud["note"]
    assert cloud["actions"][0]["do"]["cli"] == ["sandbox-gc"]
