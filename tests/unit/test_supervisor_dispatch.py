"""Tests for R8 — the supervisor dispatches ONE ready epic per sweep (opt-in).

Hermetic: a temp home with a fresh sqlite ``epics`` table and a ``dispatch_fn``
spy. The default-dispatch contract is checked against a fake ``bin/mini-ork``
that dumps its own argv/env — no test ever spawns a real scheduler.
"""
from __future__ import annotations

import inspect
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.cli import supervise
from mini_ork.recovery import supervisor


def _epics_db(path, rows):
    """The minimal epics/epic_dependencies schema ``scheduler.pick_ready`` and
    R8's cap query read. A row is ``(id, status, created_at)``."""
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE epics ("
            " id TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL,"
            " priority INTEGER NOT NULL DEFAULT 0,"
            " attempts INTEGER NOT NULL DEFAULT 0,"
            " created_at TEXT, archived_at TEXT)")
        con.execute(
            "CREATE TABLE epic_dependencies ("
            " from_epic_id TEXT NOT NULL, to_epic_id TEXT NOT NULL,"
            " kind TEXT NOT NULL DEFAULT 'hard', resolved_at TEXT)")
        for epic_id, status, created in rows:
            con.execute(
                "INSERT INTO epics(id, title, status, priority, attempts,"
                " created_at, archived_at) VALUES (?,?,?,?,?,?,NULL)",
                (epic_id, epic_id, status, 0, 0, created))
        con.commit()
    finally:
        con.close()
    return str(path)


class Spy:
    def __init__(self, result=None, raise_exc=None):
        self.calls = []
        self.result = {} if result is None else result
        self.raise_exc = raise_exc

    def __call__(self, engine_root, home, db, epic_id):
        self.calls.append({"engine_root": engine_root, "home": home,
                           "db": db, "epic_id": epic_id})
        if self.raise_exc is not None:
            raise self.raise_exc
        return dict(self.result)


@pytest.fixture(autouse=True)
def _no_epic_filter_env(monkeypatch):
    monkeypatch.delenv("MO_SCHED_EPIC_FILTER", raising=False)


@pytest.fixture()
def world(tmp_path):
    home = tmp_path / "home"
    (home / "runs").mkdir(parents=True)
    engine = tmp_path / "eng"
    engine.mkdir()
    return {"home": str(home), "db": str(tmp_path / "state.db"),
            "engine_root": str(engine), "tmp": tmp_path}


def _dispatches(actions):
    return [a for a in actions if a.get("action") == "dispatch_epic"]


def test_sweep_has_dispatch_kwargs_with_today_defaults():
    params = inspect.signature(supervisor.sweep).parameters
    assert params["dispatch_epics"].default is False
    assert params["dispatch_max_in_progress"].default == 1
    assert params["dispatch_only"].default is None
    assert params["dispatch_fn"].default is None


def test_off_by_default_never_dispatches(world):
    _epics_db(world["db"], [("e1", "not started", "2026-01-01T00:00:00Z")])
    spy = Spy()
    for kwargs in ({}, {"dispatch_epics": False}):
        actions = supervisor.sweep(
            world["home"], world["db"], engine_root=world["engine_root"],
            dispatch_fn=spy, **kwargs)
        assert spy.calls == []
        assert not _dispatches(actions)


def test_one_ready_epic_is_dispatched_once(world):
    _epics_db(world["db"], [("e1", "not started", "2026-01-01T00:00:00Z")])
    spy = Spy(result={"pid": 4242, "log": "/tmp/e1.log"})
    actions = supervisor.sweep(
        world["home"], world["db"], engine_root=world["engine_root"],
        dispatch_epics=True, dispatch_fn=spy)
    assert [c["epic_id"] for c in spy.calls] == ["e1"]
    found = _dispatches(actions)
    assert found and found[0]["epic_id"] == "e1"
    assert found[0]["reason"] == "ready"
    assert found[0]["pid"] == 4242 and found[0]["log"] == "/tmp/e1.log"


def test_in_progress_epic_blocks_at_default_cap(world):
    _epics_db(world["db"], [
        ("e1", "in progress", "2026-01-01T00:00:00Z"),
        ("e2", "not started", "2026-01-02T00:00:00Z"),
    ])
    spy = Spy()
    actions = supervisor.sweep(
        world["home"], world["db"], engine_root=world["engine_root"],
        dispatch_epics=True, dispatch_fn=spy)
    assert spy.calls == []
    assert not _dispatches(actions)


def test_below_cap_still_dispatches(world):
    _epics_db(world["db"], [
        ("e1", "in progress", "2026-01-01T00:00:00Z"),
        ("e2", "not started", "2026-01-02T00:00:00Z"),
    ])
    spy = Spy(result={"pid": 1})
    supervisor.sweep(
        world["home"], world["db"], engine_root=world["engine_root"],
        dispatch_epics=True, dispatch_max_in_progress=2, dispatch_fn=spy)
    assert [c["epic_id"] for c in spy.calls] == ["e2"]


def test_allow_list_restricts_the_pick(world):
    _epics_db(world["db"], [
        ("e1", "not started", "2026-01-01T00:00:00Z"),
        ("e2", "not started", "2026-01-02T00:00:00Z"),
    ])
    spy = Spy(result={"pid": 1})
    supervisor.sweep(
        world["home"], world["db"], engine_root=world["engine_root"],
        dispatch_epics=True, dispatch_only=["e2"], dispatch_fn=spy)
    assert [c["epic_id"] for c in spy.calls] == ["e2"]


def test_empty_allow_list_is_no_filter(world):
    _epics_db(world["db"], [
        ("e1", "not started", "2026-01-01T00:00:00Z"),
        ("e2", "not started", "2026-01-02T00:00:00Z"),
    ])
    spy = Spy(result={"pid": 1})
    supervisor.sweep(
        world["home"], world["db"], engine_root=world["engine_root"],
        dispatch_epics=True, dispatch_only=[], dispatch_fn=spy)
    assert [c["epic_id"] for c in spy.calls] == ["e1"]


def test_dry_run_reports_without_spawning(world):
    _epics_db(world["db"], [("e1", "not started", "2026-01-01T00:00:00Z")])
    spy = Spy()
    actions = supervisor.sweep(
        world["home"], world["db"], engine_root=world["engine_root"],
        dry_run=True, dispatch_epics=True, dispatch_fn=spy)
    assert spy.calls == []
    found = _dispatches(actions)
    assert found and found[0]["epic_id"] == "e1"
    assert found[0]["dry_run"] is True
    assert found[0]["reason"] == "ready; dry-run"


def test_dispatch_error_is_contained(world):
    _epics_db(world["db"], [("e1", "not started", "2026-01-01T00:00:00Z")])
    spy = Spy(raise_exc=RuntimeError("boom"))
    actions = supervisor.sweep(
        world["home"], world["db"], engine_root=world["engine_root"],
        dispatch_epics=True, dispatch_fn=spy)
    assert isinstance(actions, list)
    assert any(a.get("action") == "error"
               and "dispatch rule" in a.get("reason", "") for a in actions)


def test_rule_runs_after_backlink_only_when_enabled(world, monkeypatch):
    order = []
    monkeypatch.setattr(supervisor, "_rule_backlink",
                        lambda *a, **k: order.append("backlink") or [])
    monkeypatch.setattr(supervisor, "_rule_dispatch_epic",
                        lambda *a, **k: order.append("dispatch") or [])
    supervisor.sweep(world["home"], world["db"],
                     engine_root=world["engine_root"], dispatch_epics=True,
                     dispatch_fn=Spy())
    assert order == ["backlink", "dispatch"]
    order.clear()
    supervisor.sweep(world["home"], world["db"],
                     engine_root=world["engine_root"], dispatch_epics=False,
                     dispatch_fn=Spy())
    assert order == ["backlink"]


# ── _default_dispatch_epic: the spawn contract ──────────────────────────────


def _fake_engine(engine_p, dump):
    (engine_p / "bin").mkdir(exist_ok=True)
    (engine_p / "bin" / "mini-ork").write_text(
        "import json, os, sys\n"
        "json.dump({'argv': sys.argv, 'env': dict(os.environ),"
        " 'cwd': os.getcwd()}, open(%r, 'w'))\n" % str(dump),
        encoding="utf-8")
    return dump


def _read_dump(path):
    deadline = time.time() + 10
    last = None
    while time.time() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                last = exc
        time.sleep(0.05)
    raise AssertionError(f"dispatch child never reported: {last or 'no dump'}")


def test_default_dispatch_spawns_the_scheduler_once(world):
    dump = _fake_engine(Path(world["engine_root"]), world["tmp"] / "a.json")
    result = supervisor._default_dispatch_epic(
        world["engine_root"], world["home"], world["db"], "e1")
    assert "error" not in result, result
    assert isinstance(result["pid"], int) and result["pid"] > 0
    data = _read_dump(dump)
    assert data["argv"][0].replace("\\", "/").endswith("bin/mini-ork")
    assert data["argv"][1:] == ["scheduler", "--once", "--epic", "e1"]


def test_default_dispatch_runs_in_engine_root(world):
    dump = _fake_engine(Path(world["engine_root"]), world["tmp"] / "b.json")
    supervisor._default_dispatch_epic(
        world["engine_root"], world["home"], world["db"], "e1")
    data = _read_dump(dump)
    assert os.path.realpath(data["cwd"]) == os.path.realpath(
        world["engine_root"])


def test_default_dispatch_pins_env_and_pops_run_scoped_keys(
        world, monkeypatch):
    dump = _fake_engine(Path(world["engine_root"]), world["tmp"] / "c.json")
    monkeypatch.setenv("MINI_ORK_RUN_ID", "outer")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", "/outer/run")
    monkeypatch.setenv("MINI_ORK_PLAN_PATH", "/outer/plan.json")
    supervisor._default_dispatch_epic(
        world["engine_root"], world["home"], world["db"], "e1")
    env = _read_dump(dump)["env"]
    assert env["MINI_ORK_ROOT"] == world["engine_root"]
    assert env["MINI_ORK_HOME"] == world["home"]
    assert env["MINI_ORK_DB"] == world["db"]
    for key in ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_PLAN_PATH"):
        assert key not in env


def test_default_dispatch_logs_into_home_state(world):
    home_p = Path(world["home"])
    assert not (home_p / "state").exists()
    result = supervisor._default_dispatch_epic(
        world["engine_root"], world["home"], world["db"], "e1")
    expected = str(home_p / "state" / "supervisor-dispatch-e1.log")
    assert result["log"] == expected
    assert Path(expected).is_file()


def test_default_dispatch_returns_error_dict_on_oserror(world):
    blocked = Path(world["tmp"]) / "blocked"
    blocked.mkdir()
    (blocked / "state").write_text("not a directory", encoding="utf-8")
    result = supervisor._default_dispatch_epic(
        world["engine_root"], str(blocked), world["db"], "e1")
    assert isinstance(result.get("error"), str) and result["error"]
    assert "pid" not in result


# ── config parsing ──────────────────────────────────────────────────────────


def _write_cfg(tmp_path, text):
    path = tmp_path / "sup.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_dispatch_block_parses(tmp_path):
    cfg = _write_cfg(tmp_path,
                     "dispatch_epics:\n"
                     "  enabled: true\n"
                     "  max_in_progress: 2\n"
                     "  only: [a, b]\n")
    assert supervise._load_config(cfg)["dispatch_epics"] == {
        "enabled": True, "max_in_progress": 2, "only": ["a", "b"]}


def test_absent_block_means_off(tmp_path):
    cfg = _write_cfg(tmp_path, "interval_s: 60\n")
    assert supervise._load_config(cfg)["dispatch_epics"] == {
        "enabled": False, "max_in_progress": 1, "only": []}


@pytest.mark.parametrize("text,subkey", [
    ('dispatch_epics:\n  enabled: "yes"\n', "enabled"),
    ("dispatch_epics:\n  enabled: 1\n", "enabled"),
    ("dispatch_epics:\n  enabled: true\n  max_in_progress: 0\n",
     "max_in_progress"),
    ('dispatch_epics:\n  enabled: true\n  only: "e1"\n', "only"),
    ("dispatch_epics:\n  enabled: true\n  only: [1, 2]\n", "only"),
])
def test_wrong_type_is_config_error(tmp_path, text, subkey):
    path = _write_cfg(tmp_path, text)
    with pytest.raises(supervise.ConfigError) as excinfo:
        supervise._load_config(path)
    message = str(excinfo.value)
    assert "dispatch_epics" in message or subkey in message


def test_defaults_dict_gains_the_disabled_block():
    assert any("dispatch" in str(key) for key in supervise.DEFAULTS)


def test_example_config_carries_the_disabled_block():
    import yaml

    repo_root = Path(supervisor.__file__).resolve().parents[2]
    parsed = yaml.safe_load(
        (repo_root / "config" / "self-driving.example.yaml")
        .read_text(encoding="utf-8"))
    assert parsed["dispatch_epics"] == {
        "enabled": False, "max_in_progress": 1, "only": []}
