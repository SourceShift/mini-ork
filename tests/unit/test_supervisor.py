"""Tests for the self-driving supervisor sweep (SDE-4a R1–R3) and its CLI.

Hermetic: a tmp home with a freshly created ``state.db`` and a throwaway git
repo as ``engine_root``. Nothing reaches the operator's real trees.
"""
from __future__ import annotations

import inspect
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from mini_ork.recovery import supervisor

REPO_ROOT = Path(__file__).resolve().parents[2]
T = 2_000_000  # deterministic "now" for age comparisons

SCHEMA = """
CREATE TABLE IF NOT EXISTS epics (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
  last_run_id TEXT);
CREATE TABLE IF NOT EXISTS task_runs (
  id TEXT PRIMARY KEY, task_class TEXT NOT NULL, recipe TEXT,
  kickoff_path TEXT NOT NULL, status TEXT NOT NULL,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, ended_at INTEGER);
CREATE TABLE IF NOT EXISTS node_checkpoints (
  run_id TEXT NOT NULL, node_id TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL, input_hash TEXT NOT NULL, recipe_version TEXT NOT NULL,
  config_hash TEXT NOT NULL, artifact_manifest_json TEXT NOT NULL,
  created_at INTEGER NOT NULL, PRIMARY KEY (run_id, node_id));
CREATE TABLE IF NOT EXISTS run_leases (
  run_id TEXT PRIMARY KEY, owner_token TEXT NOT NULL,
  acquired_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  renewed_at INTEGER NOT NULL, check (expires_at >= acquired_at));
CREATE TABLE IF NOT EXISTS mo_inbox_gates (
  inbox_id INTEGER PRIMARY KEY AUTOINCREMENT, gate_id TEXT NOT NULL,
  feature TEXT NOT NULL, phase TEXT, context_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', review_note TEXT,
  enqueued_at INTEGER NOT NULL, resolved_at INTEGER, blocks_dispatch_for TEXT);
CREATE TABLE IF NOT EXISTS diagnosis_ledger (
  id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, source TEXT NOT NULL,
  layer TEXT, routes_to TEXT, confidence REAL, model_lane TEXT, signature TEXT,
  action TEXT, fix_epic_id TEXT, created_at INTEGER NOT NULL, label INTEGER,
  labelled_at INTEGER, label_reason TEXT);
"""


def _git(engine, *args):
    return subprocess.run(["git", "-C", str(engine), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "runs").mkdir(parents=True)
    db = str(home / "state.db")
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.commit()
    con.close()

    engine = tmp_path / "engine"
    engine.mkdir()
    _git(engine, "init", "-q")
    _git(engine, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(engine, "config", "user.email", "t@example.invalid")
    _git(engine, "config", "user.name", "T")
    (engine / "README.md").write_text("initial\n", encoding="utf-8")
    _git(engine, "add", "-A")
    _git(engine, "commit", "-q", "-m", "initial")
    _git(engine, "update-ref", "refs/remotes/origin/main", "HEAD")

    for key in ("MINI_ORK_RUN_DIR", "MINI_ORK_RUN_ID", "MINI_ORK_PLAN_PATH",
                "MO_SUPERVISE_CONFIG"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_ROOT", str(engine))
    return home, db, engine


def _commit(engine, message):
    (engine / f"f-{abs(hash(message))}.txt").write_text(message, encoding="utf-8")
    _git(engine, "add", "-A")
    _git(engine, "commit", "-q", "-m", message)
    _git(engine, "update-ref", "refs/remotes/origin/main", "HEAD")


def _exec(db, sql, args=()):
    con = sqlite3.connect(db)
    try:
        con.execute(sql, args)
        con.commit()
    finally:
        con.close()


def _rows(db, sql, args=()):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _parked(home, run_id, epic_id, attempts=None):
    run_dir = Path(home) / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {"state": "waiting_on_fix", "fix_epic_id": epic_id,
               "attempts": attempts if attempts is not None else
               [{"ts": 5, "from_node": "implementer"}]}
    (run_dir / "repair.json").write_text(json.dumps(payload), encoding="utf-8")
    return run_dir


def _repair(home, run_id):
    return json.loads((Path(home) / "runs" / run_id / "repair.json")
                      .read_text(encoding="utf-8"))


class _Spawn:
    def __init__(self, pid=4242):
        self.calls = []
        self.pid = pid

    def __call__(self, run_id, from_node):
        self.calls.append((run_id, from_node))
        return self.pid


# ── shape ───────────────────────────────────────────────────────────────────


def test_sweep_signature_and_defaults():
    params = inspect.signature(supervisor.sweep).parameters
    assert list(params)[:2] == ["home", "db"]
    for name in ("engine_root", "now", "dry_run", "spawn", "max_revives",
                 "orphan_after_s"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["engine_root"].default is inspect.Parameter.empty
    assert params["max_revives"].default == 2
    assert params["orphan_after_s"].default == 3600


# ── R1 ──────────────────────────────────────────────────────────────────────


def test_r1_revives_when_fix_commit_is_on_origin_main(env):
    home, db, engine = env
    _commit(engine, "fix: lands epc-1")
    _exec(db, "INSERT INTO epics(id,title,status) VALUES (?,?,?)",
          ("epc-1", "e", "done"))
    _parked(home, "run-1", "epc-1")
    spawn = _Spawn()
    actions = supervisor.sweep(str(home), db, engine_root=str(engine),
                               spawn=spawn)
    assert [a["action"] for a in actions if a["run_id"] == "run-1"] == ["revive"]
    assert spawn.calls == [("run-1", "implementer")]
    repair = _repair(home, "run-1")
    assert repair["state"] == "reviving"
    assert repair["revive_from_node"] == "implementer"
    assert repair["revive_pid"] == 4242
    assert repair["fix_epic_id"] == "epc-1"


def test_r1_done_without_landing_signal_waits(env):
    home, db, engine = env
    _commit(engine, "chore: unrelated")
    _exec(db, "INSERT INTO epics(id,title,status,last_run_id) VALUES (?,?,?,?)",
          ("epc-2", "e", "done", "missing-run-dir"))
    _parked(home, "run-2", "epc-2")
    spawn = _Spawn()
    actions = supervisor.sweep(str(home), db, engine_root=str(engine),
                               spawn=spawn)
    action = [a for a in actions if a["run_id"] == "run-2"][0]
    assert action["action"] == "wait"
    assert action["reason"] == "fix epic done but not on origin/main"
    assert _repair(home, "run-2")["state"] == "waiting_on_fix"
    assert spawn.calls == []


def test_r1_terminal_epic_gives_up_and_notifies_once(env):
    home, db, engine = env
    _exec(db, "INSERT INTO epics(id,title,status) VALUES (?,?,?)",
          ("epc-3", "e", "escalated"))
    _parked(home, "run-3", "epc-3")
    spawn = _Spawn()
    for _ in range(2):
        supervisor.sweep(str(home), db, engine_root=str(engine), spawn=spawn)
    repair = _repair(home, "run-3")
    assert repair["state"] == "gave_up"
    assert repair["note"] == "fix epic epc-3 ended escalated"
    rows = _rows(db, "SELECT gate_id, feature, blocks_dispatch_for"
                     " FROM mo_inbox_gates")
    assert rows == [("self-driving-supervisor", "fix epic failed: run-3", "")]
    assert spawn.calls == []


def test_r1_max_revives_caps_per_sweep(env):
    home, db, engine = env
    for i in range(3):
        _commit(engine, f"fix: epc-cap-{i}")
        _exec(db, "INSERT INTO epics(id,title,status) VALUES (?,?,?)",
              (f"epc-cap-{i}", "e", "done"))
        _parked(home, f"run-cap-{i}", f"epc-cap-{i}")
    spawn = _Spawn()
    supervisor.sweep(str(home), db, engine_root=str(engine), spawn=spawn,
                     max_revives=2)
    assert len(spawn.calls) == 2
    states = [_repair(home, f"run-cap-{i}")["state"] for i in range(3)]
    assert states.count("reviving") == 2 and states.count("waiting_on_fix") == 1


def test_r1_skips_when_no_failed_node(env):
    home, db, engine = env
    _commit(engine, "fix: epc-4")
    _exec(db, "INSERT INTO epics(id,title,status) VALUES (?,?,?)",
          ("epc-4", "e", "done"))
    _parked(home, "run-4", "epc-4", attempts=[])
    spawn = _Spawn()
    actions = supervisor.sweep(str(home), db, engine_root=str(engine),
                               spawn=spawn)
    action = [a for a in actions if a["run_id"] == "run-4"][0]
    assert action["action"] == "skip"
    assert action["reason"] == "no failed node recorded"
    assert spawn.calls == []


# ── R2 ──────────────────────────────────────────────────────────────────────


def _reviving(home, db, run_id, epic, *, status, updated_at, node_status=None):
    run_dir = Path(home) / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "repair.json").write_text(json.dumps({
        "state": "reviving", "fix_epic_id": epic, "revived_at": T - 500,
        "revive_from_node": "implementer"}), encoding="utf-8")
    _exec(db, "INSERT INTO task_runs(id,task_class,kickoff_path,status,"
              "created_at,updated_at) VALUES (?,?,?,?,?,?)",
          (run_id, "framework_edit", "k.md", status, updated_at, updated_at))
    if node_status:
        _exec(db, "INSERT INTO node_checkpoints(run_id,node_id,status,"
                  "input_hash,recipe_version,config_hash,"
                  "artifact_manifest_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
              (run_id, "implementer", node_status, "ih", "1", "ch", "[]", 1))
    from mini_ork.stores import diagnosis_ledger
    return diagnosis_ledger.record(
        db, run_id, {"source": "model", "layer": "engine",
                     "confidence": 0.7, "signature": f"sig-{run_id}"},
        action="escalate", fix_epic_id=epic)


def test_r2_published_labels_correct(env):
    home, db, engine = env
    ledger_id = _reviving(home, db, "run-r2a", "epc-a", status="published",
                          updated_at=T - 100)
    supervisor.sweep(str(home), db, engine_root=str(engine), now=T,
                     spawn=_Spawn())
    assert _rows(db, "SELECT label FROM diagnosis_ledger WHERE id=?",
                 (ledger_id,))[0][0] == 1
    assert _repair(home, "run-r2a")["state"] == "revived"


def test_r2_failed_at_same_node_labels_wrong(env):
    home, db, engine = env
    ledger_id = _reviving(home, db, "run-r2b", "epc-b", status="failed",
                          updated_at=T - 100, node_status="failure")
    supervisor.sweep(str(home), db, engine_root=str(engine), now=T,
                     spawn=_Spawn())
    assert _rows(db, "SELECT label FROM diagnosis_ledger WHERE id=?",
                 (ledger_id,))[0][0] == 0
    assert _repair(home, "run-r2b")["state"] == "revive_failed"


def test_r2_labels_a_revived_run_whose_auto_repair_rewrote_the_state(env):
    # The revived run's own auto-repair flips repair.json to ``repaired`` on
    # publish (keeping the supervisor's keys); the label must still land, and
    # the auto-repair's state must survive.
    home, db, engine = env
    ledger_id = _reviving(home, db, "run-r2e", "epc-e", status="published",
                          updated_at=T - 100)
    path = Path(home) / "runs" / "run-r2e" / "repair.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["state"] = "repaired"
    path.write_text(json.dumps(data), encoding="utf-8")
    actions = supervisor.sweep(str(home), db, engine_root=str(engine), now=T,
                               spawn=_Spawn())
    assert [a["action"] for a in actions] == ["label"]
    assert _rows(db, "SELECT label FROM diagnosis_ledger WHERE id=?",
                 (ledger_id,))[0][0] == 1
    repair = _repair(home, "run-r2e")
    assert repair["state"] == "repaired"
    assert repair["labelled_at"] == int(T) and repair["label_correct"] is True
    # Labelled once: a second sweep has nothing to do for this run.
    assert supervisor.sweep(str(home), db, engine_root=str(engine), now=T + 60,
                            spawn=_Spawn()) == []


def test_default_spawn_drops_inherited_run_scoped_env(env, monkeypatch):
    home, db, engine = env
    (Path(home) / "runs" / "run-sp").mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_RUN_ID", "some-other-run")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", "/elsewhere")
    monkeypatch.setenv("MINI_ORK_PLAN_PATH", "/elsewhere/plan.json")
    seen = {}

    class _Proc:
        pid = 4242

    def _popen(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]
        return _Proc()

    monkeypatch.setattr(supervisor.subprocess, "Popen", _popen)
    pid = supervisor._default_spawn(Path(home), "run-sp", "implementer",
                                    str(engine))
    assert pid == 4242
    assert seen["argv"][-4:] == ["run-sp", "--from-node", "implementer", "--force"]
    for key in ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_PLAN_PATH"):
        assert key not in seen["env"]
    assert seen["env"]["MINI_ORK_ROOT"] == str(engine)
    assert seen["env"]["MO_AUTO_REPAIR"] == "1"


def test_r2_still_executing_no_action(env):
    home, db, engine = env
    ledger_id = _reviving(home, db, "run-r2c", "epc-c", status="executing",
                          updated_at=T - 100)
    before = (Path(home) / "runs" / "run-r2c" / "repair.json").read_bytes()
    actions = supervisor.sweep(str(home), db, engine_root=str(engine), now=T,
                               spawn=_Spawn())
    assert [a for a in actions if a["run_id"] == "run-r2c"] == []
    assert _rows(db, "SELECT label FROM diagnosis_ledger WHERE id=?",
                 (ledger_id,))[0][0] is None
    assert (Path(home) / "runs" / "run-r2c" / "repair.json").read_bytes() == before


def test_r2_stale_updated_at_no_action(env):
    home, db, engine = env
    ledger_id = _reviving(home, db, "run-r2d", "epc-d", status="published",
                          updated_at=T - 900)
    supervisor.sweep(str(home), db, engine_root=str(engine), now=T,
                     spawn=_Spawn())
    assert _rows(db, "SELECT label FROM diagnosis_ledger WHERE id=?",
                 (ledger_id,))[0][0] is None


# ── R3 ──────────────────────────────────────────────────────────────────────


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)
    return proc.pid


def _orphan(home, run_id, ts, pid):
    run_dir = Path(home) / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "repair.json").write_text(json.dumps({
        "state": "repairing", "attempts": [{"ts": ts, "from_node": "implementer"}]}),
        encoding="utf-8")
    (run_dir / ".pid").write_text(f"{pid}\n", encoding="utf-8")
    return run_dir


def test_r3_old_attempt_with_dead_owner_is_cleared(env):
    home, db, engine = env
    gone = _dead_pid()
    _orphan(home, "run-orph-old", T - 1200, gone)
    _orphan(home, "run-orph-young", T - 10, gone)
    _orphan(home, "run-orph-alive", T - 1200, os.getpid())
    actions = supervisor.sweep(str(home), db, engine_root=str(engine), now=T,
                               spawn=_Spawn(), orphan_after_s=600)
    acted = {a["run_id"]: a["action"] for a in actions}
    assert acted.get("run-orph-old") == "gave_up"
    assert _repair(home, "run-orph-old")["note"] == (
        "orphaned repairing state cleared by supervisor")
    assert "run-orph-young" not in acted
    assert "run-orph-alive" not in acted


# ── dry run / malformed ─────────────────────────────────────────────────────


def test_dry_run_matches_wet_and_writes_nothing(tmp_path, monkeypatch):
    def build(name):
        home = tmp_path / name / "home"
        (home / "runs").mkdir(parents=True)
        db = str(home / "state.db")
        con = sqlite3.connect(db)
        con.executescript(SCHEMA)
        con.commit()
        con.close()
        engine = tmp_path / name / "engine"
        engine.mkdir()
        _git(engine, "init", "-q")
        _git(engine, "config", "user.email", "t@example.invalid")
        _git(engine, "config", "user.name", "T")
        (engine / "R").write_text("x\n", encoding="utf-8")
        _git(engine, "add", "-A")
        _git(engine, "commit", "-q", "-m", f"fix: epc-{name}")
        _git(engine, "update-ref", "refs/remotes/origin/main", "HEAD")
        _exec(db, "INSERT INTO epics(id,title,status) VALUES (?,?,?)",
              (f"epc-{name}", "e", "done"))
        _parked(home, "run", f"epc-{name}")
        return home, db, engine

    wet_home, wet_db, wet_engine = build("wet")
    dry_home, dry_db, dry_engine = build("dry")
    wet_spawn, dry_spawn = _Spawn(), _Spawn()
    wet = supervisor.sweep(str(wet_home), wet_db, engine_root=str(wet_engine),
                           spawn=wet_spawn, now=T)
    before = (dry_home / "runs" / "run" / "repair.json").read_bytes()
    dry = supervisor.sweep(str(dry_home), dry_db, engine_root=str(dry_engine),
                           spawn=dry_spawn, now=T, dry_run=True)
    assert sorted((a["run_id"], a["action"]) for a in wet) == sorted(
        (a["run_id"], a["action"]) for a in dry)
    assert dry_spawn.calls == []
    assert (dry_home / "runs" / "run" / "repair.json").read_bytes() == before


def test_corrupt_repair_records_error_and_continues(env):
    home, db, engine = env
    bad = home / "runs" / "run-bad"
    bad.mkdir()
    (bad / "repair.json").write_text("{ not json", encoding="utf-8")
    _commit(engine, "fix: epc-good")
    _exec(db, "INSERT INTO epics(id,title,status) VALUES (?,?,?)",
          ("epc-good", "e", "done"))
    _parked(home, "run-good", "epc-good")
    actions = supervisor.sweep(str(home), db, engine_root=str(engine),
                               spawn=_Spawn())
    by_run = {a["run_id"]: a for a in actions}
    assert by_run["run-bad"]["action"] == "error"
    assert by_run["run-bad"]["reason"]
    assert by_run["run-good"]["action"] == "revive"


# ── CLI ─────────────────────────────────────────────────────────────────────


def _run_cli(args, home, db, engine):
    env = dict(os.environ)
    for key in ("MINI_ORK_RUN_DIR", "MINI_ORK_RUN_ID", "MINI_ORK_PLAN_PATH",
                "MO_SUPERVISE_CONFIG"):
        env.pop(key, None)
    env.update({"MINI_ORK_HOME": str(home), "MINI_ORK_DB": str(db),
                "MINI_ORK_ROOT": str(engine)})
    return subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.supervise", *args],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=180)


def test_cli_second_once_while_lease_held_exits_zero(env):
    home, db, engine = env
    from mini_ork.stores.lease import acquire_lease
    assert acquire_lease(db, "__supervisor__", ttl_s=900)
    proc = _run_cli(["--once"], home, db, engine)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "another supervisor holds the lease" in (proc.stdout + proc.stderr)


def test_cli_once_releases_lease(env):
    home, db, engine = env
    proc = _run_cli(["--once", "--dry-run", "--json"], home, db, engine)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert json.loads(proc.stdout.strip()) == []
    assert _rows(db, "SELECT owner_token FROM run_leases"
                     " WHERE run_id='__supervisor__'") == []


def test_cli_missing_named_config_exits_two(env, tmp_path):
    home, db, engine = env
    missing = tmp_path / "nope.yaml"
    proc = _run_cli(["--once", "--config", str(missing)], home, db, engine)
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert str(missing) in (proc.stdout + proc.stderr)


def test_cli_help_line_is_wired():
    from mini_ork.cli import main as cli_main
    assert cli_main._NATIVE_MODULE_SUBS["supervise"] == "mini_ork.cli.supervise"
    assert "supervise" in cli_main._HELP
