"""ECOSYSTEM §6 K6: stop / kill / answers refuse foreign-origin callers.

``mini_ork/web/routes/control.py`` used to trust the loopback bind alone for
its three mutating routes. A browser page from any origin can fire a
cross-origin ``no-cors`` POST (the CORS middleware limits reads, not the side
effect), and DNS rebinding reaches the loopback listener too. The routes now
carry :func:`mini_ork.web.routes.agent_server.require_local_caller`, which
refuses a foreign ``Origin`` with 403 while admitting callers that send no
``Origin`` at all (the IDE, ACP, the SDK, curl).

Hermetic: a tmp ``MINI_ORK_HOME`` with one live ``task_runs`` row, and
``mini_ork.web.control.kill_run`` monkeypatched so no signal is ever sent.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mini_ork.web import control as web_control
from mini_ork.web.db import db_for
from mini_ork.web.deps import get_db, get_home
from mini_ork.web.routes import agent_server
from mini_ork.web.routes import control as control_routes

RUN_ID = "run-1770000000-4242"
MISSING_ID = "run-1999999999-1"
FOREIGN_ORIGIN = "https://evil.example"
LOOPBACK_ORIGIN = "http://127.0.0.1:7090"
ALLOW_REMOTE_ENV = "MO_AGENT_SERVER_ALLOW_REMOTE"

SENTINEL = ".stop-requested"
ANSWERS_FILE = "profile-answers.json"
PROFILE_FILE = "run_profile.json"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    """A hermetic MINI_ORK_HOME: state.db with one live task_runs row."""
    monkeypatch.delenv(ALLOW_REMOTE_ENV, raising=False)
    home = tmp_path / "mini-ork-home"
    (home / "runs" / RUN_ID).mkdir(parents=True)
    con = sqlite3.connect(home / "state.db")
    con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    con.execute(
        "INSERT INTO task_runs (id, status) VALUES (?, ?)", (RUN_ID, "executing")
    )
    con.commit()
    con.close()
    return home


@pytest.fixture()
def client(home: Path) -> TestClient:
    """A bare app mounting exactly the router under test."""
    app = FastAPI()
    app.include_router(control_routes.router)
    app.dependency_overrides[get_home] = lambda: home
    app.dependency_overrides[get_db] = lambda: db_for(home)
    return TestClient(app)


@pytest.fixture()
def kill_calls(monkeypatch) -> list[str]:
    """Replace ``mini_ork.web.control.kill_run`` so no signal is ever sent."""
    calls: list[str] = []

    def fake_kill_run(home, db, task_run_id):
        calls.append(task_run_id)
        return {"ok": True, "task_run_id": task_run_id, "killed": [], "survived": []}

    monkeypatch.setattr(web_control, "kill_run", fake_kill_run)
    return calls


def _run_dir(home: Path, run_id: str) -> Path:
    return home / "runs" / run_id


# ── stop ─────────────────────────────────────────────────────────────────


def test_stop_foreign_origin_is_403_and_writes_no_sentinel(client, home):
    resp = client.post(
        f"/api/v1/task-runs/{RUN_ID}/stop", headers={"Origin": FOREIGN_ORIGIN}
    )
    assert resp.status_code == 403, resp.text
    assert not (_run_dir(home, RUN_ID) / SENTINEL).exists()


def test_stop_no_origin_succeeds_and_writes_sentinel(client, home):
    resp = client.post(f"/api/v1/task-runs/{RUN_ID}/stop")
    assert resp.status_code == 200, resp.text
    assert (_run_dir(home, RUN_ID) / SENTINEL).exists()


def test_stop_no_origin_missing_run_keeps_409(client, home):
    resp = client.post(f"/api/v1/task-runs/{MISSING_ID}/stop")
    assert resp.status_code == 409, resp.text
    assert not (_run_dir(home, MISSING_ID) / SENTINEL).exists()


def test_stop_loopback_origin_is_admitted(client, home):
    resp = client.post(
        f"/api/v1/task-runs/{RUN_ID}/stop", headers={"Origin": LOOPBACK_ORIGIN}
    )
    assert resp.status_code == 200, resp.text
    assert (_run_dir(home, RUN_ID) / SENTINEL).exists()


# ── kill ─────────────────────────────────────────────────────────────────


def test_kill_foreign_origin_is_403_and_never_calls_kill_run(client, kill_calls):
    resp = client.post(
        f"/api/v1/task-runs/{RUN_ID}/kill", headers={"Origin": FOREIGN_ORIGIN}
    )
    assert resp.status_code == 403, resp.text
    assert kill_calls == []


def test_kill_no_origin_still_reaches_kill_run(client, kill_calls):
    resp = client.post(f"/api/v1/task-runs/{RUN_ID}/kill")
    assert resp.status_code == 200, resp.text
    assert kill_calls == [RUN_ID]


# ── answers ──────────────────────────────────────────────────────────────


def test_answers_foreign_origin_is_403_and_writes_nothing(client, home):
    run_dir = _run_dir(home, RUN_ID)
    resp = client.post(
        f"/api/v1/task-runs/{RUN_ID}/answers",
        json={"q": "a"},
        headers={"Origin": FOREIGN_ORIGIN},
    )
    assert resp.status_code == 403, resp.text
    assert not (run_dir / ANSWERS_FILE).exists()
    assert not (run_dir / PROFILE_FILE).exists()


def test_answers_no_origin_still_persists(client, home):
    resp = client.post(f"/api/v1/task-runs/{RUN_ID}/answers", json={"q": "a"})
    assert resp.status_code == 200, resp.text
    answers = json.loads(
        (_run_dir(home, RUN_ID) / ANSWERS_FILE).read_text(encoding="utf-8")
    )
    assert answers == {"q": "a"}


# ── wiring and the untouched neighbours ──────────────────────────────────


def test_control_imports_the_shared_guard():
    assert control_routes.require_local_caller is agent_server.require_local_caller


def test_docstring_documents_the_guard():
    doc = control_routes.__doc__ or ""
    assert "require_local_caller" in doc
    assert "loopback-trust-based" not in doc


def test_cost_routes_keep_require_token(client):
    """A foreign Origin on a cost route must yield 401 (no bearer), not 403."""
    for path in ("pause-cost", "resume-cost"):
        resp = client.post(
            f"/api/v1/task-runs/{MISSING_ID}/{path}",
            json={} if path == "pause-cost" else None,
            headers={"Origin": FOREIGN_ORIGIN},
        )
        assert resp.status_code == 401, f"{path}: {resp.status_code} {resp.text}"
