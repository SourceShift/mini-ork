"""Delegation ledger — Done-When 1-12.

Every DB is built with ``migrate_apply`` into a tmp dir (a module-scoped
template is copied per test, never mutated in place). Environment is
monkeypatched and every timestamp is passed explicitly as ``now=``.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.stores.migrate import migrate_apply

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "db" / "migrations"
M0091 = "0091_delegation_ledger.sql"
FOUR = ("initiator", "kickoff_copy", "kickoff_sha256", "home_db")


# ── fixtures / helpers ──────────────────────────────────────────────────────

def _migrate(db, migrations_dir=MIGRATIONS):
    rc, _ = migrate_apply(str(migrations_dir), db=str(db))
    assert rc == 0, f"migrate_apply failed for {migrations_dir}"
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    d = tmp_path_factory.mktemp("tmpl")
    db = d / "state.db"
    _migrate(db)
    return db


@pytest.fixture
def db(tmp_path, template):
    path = tmp_path / "state.db"
    shutil.copyfile(template, path)
    return str(path)


def _cols(path):
    con = sqlite3.connect(path)
    try:
        return [r[1] for r in con.execute("PRAGMA table_info(task_runs)")]
    finally:
        con.close()


def _seed(path, run_id, initiator=None, status="classified", created=111):
    con = sqlite3.connect(path)
    try:
        con.execute(
            "INSERT INTO task_runs (id, task_class, kickoff_path, status,"
            " created_at, updated_at, initiator) VALUES (?, 'code_fix',"
            " '/typed/kickoff.md', ?, ?, ?, ?)",
            (run_id, status, created, created, initiator))
        con.commit()
    finally:
        con.close()


def _row(path, run_id):
    con = sqlite3.connect(path)
    try:
        return con.execute(
            "SELECT initiator, kickoff_copy, kickoff_sha256, home_db, updated_at,"
            " artifact_path, artifact_hash FROM task_runs WHERE id=?",
            (run_id,)).fetchone()
    finally:
        con.close()


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _clear_mo(monkeypatch):
    for name in ("MO_INITIATOR", "MO_SCHEDULER_DISPATCH", "MO_RECOVER_OF",
                 "MO_RSI_CYCLE_ID"):
        monkeypatch.delenv(name, raising=False)


def _registry(monkeypatch, path):
    monkeypatch.setenv("MO_HOMES_REGISTRY", str(path))
    return {"MO_HOMES_REGISTRY": str(path)}


# ── 1. migration is idempotent and additive ─────────────────────────────────

def test_migration_is_idempotent_and_additive(template):
    for column in FOUR:
        assert column in _cols(template)
    rc, _ = migrate_apply(str(MIGRATIONS), db=str(template))
    assert rc == 0


def test_migration_file_reexecutes_over_existing_columns(db, tmp_path):
    con = sqlite3.connect(db)
    con.execute("DELETE FROM schema_migrations WHERE filename=?", (M0091,))
    con.commit()
    con.close()
    rc, _ = migrate_apply(str(MIGRATIONS), db=db)
    assert rc == 0, "the guarded ALTER must skip columns that already exist"
    for column in FOUR:
        assert column in _cols(db)


def test_migration_on_a_db_without_task_runs_is_a_noop(tmp_path):
    only = tmp_path / "only"
    only.mkdir()
    shutil.copyfile(MIGRATIONS / M0091, only / M0091)
    path = tmp_path / "state.db"
    rc, _ = migrate_apply(str(only), db=str(path))
    assert rc == 0
    con = sqlite3.connect(str(path))
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "task_runs" not in tables


# ── 2. migration preserves existing rows ────────────────────────────────────

def test_migration_preserves_existing_rows(tmp_path, template):
    pre = tmp_path / "pre"
    pre.mkdir()
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name < "0091":
            shutil.copyfile(f, pre / f.name)
    path = tmp_path / "state.db"
    _migrate(path, pre)
    con = sqlite3.connect(str(path))
    con.execute(
        "INSERT INTO task_runs (id, task_class, kickoff_path, status, created_at,"
        " updated_at) VALUES ('run-keep', 'code_fix', '/typed/k.md', 'executing', 1, 1)")
    con.commit()
    con.close()

    _migrate(path)
    con = sqlite3.connect(str(path))
    row = con.execute(
        "SELECT id, status, kickoff_path, initiator, kickoff_copy, kickoff_sha256,"
        " home_db FROM task_runs").fetchone()
    con.close()
    assert row == ("run-keep", "executing", "/typed/k.md", None, None, None, None)


# ── 3. initiator precedence ─────────────────────────────────────────────────

@pytest.mark.parametrize(("env", "expected"), [
    ({"MO_INITIATOR": "claude-session:zz", "MO_SCHEDULER_DISPATCH": "1",
      "MO_RECOVER_OF": "r7", "MO_RSI_CYCLE_ID": "c9"}, "claude-session:zz"),
    ({"MO_SCHEDULER_DISPATCH": "1", "MO_RECOVER_OF": "r7"}, "scheduler"),
    ({"MO_RECOVER_OF": "r7", "MO_RSI_CYCLE_ID": "c9"}, "recover:r7"),
    ({"MO_RSI_CYCLE_ID": "c9"}, "rsi-cycle:c9"),
])
def test_initiator_precedence(monkeypatch, env, expected):
    from mini_ork.orchestration.delegation import resolve_initiator

    _clear_mo(monkeypatch)
    assert resolve_initiator(env=dict(env)) == expected


def test_initiator_caps_and_strips(monkeypatch):
    from mini_ork.orchestration.delegation import resolve_initiator

    _clear_mo(monkeypatch)
    assert resolve_initiator(env={"MO_INITIATOR": "a" * 300}) == "a" * 120
    assert resolve_initiator(env={"MO_INITIATOR": "ab\x00c\x1bd"}) == "abcd"


def test_initiator_is_manual_when_nothing_is_discoverable(monkeypatch):
    from mini_ork.orchestration import delegation

    _clear_mo(monkeypatch)
    monkeypatch.setattr(delegation, "_claude_ancestor", lambda: "")
    found = delegation.resolve_initiator(env={})
    expected = f"manual:{os.getuid()}" if hasattr(os, "getuid") else "manual"
    assert found == expected


def test_initiator_finds_a_claude_ancestor(monkeypatch):
    from mini_ork.orchestration import delegation

    _clear_mo(monkeypatch)
    monkeypatch.setattr(delegation, "_claude_ancestor", lambda: "claude-session:s1")
    assert delegation.resolve_initiator(env={}) == "claude-session:s1"


# ── 4. copy_kickoff exact and idempotent ────────────────────────────────────

def test_copy_kickoff_is_exact_and_idempotent(tmp_path):
    from mini_ork.orchestration.delegation import copy_kickoff

    kick = tmp_path / "kickoff.md"
    kick.write_bytes(b"# kickoff\nwork\n")
    run_dir = tmp_path / "run.d"
    run_dir.mkdir()

    name, digest = copy_kickoff(str(run_dir), str(kick))
    assert (name, digest) == ("kickoff.md", _sha(kick))
    dest = run_dir / "kickoff.md"
    assert dest.read_bytes() == kick.read_bytes()

    before = dest.stat()
    assert copy_kickoff(str(run_dir), str(kick)) == (name, digest)
    after = dest.stat()
    assert (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size)


def test_copy_kickoff_returns_empty_for_unusable_paths(tmp_path):
    from mini_ork.orchestration.delegation import copy_kickoff

    run_dir = tmp_path / "run.d"
    run_dir.mkdir()
    assert copy_kickoff(str(run_dir), str(tmp_path / "nope.md")) == ("", "")
    assert not (run_dir / "kickoff.md").exists()

    kick = tmp_path / "kick.md"
    kick.write_bytes(b"# k\n")
    missing = tmp_path / "missing-run"
    assert copy_kickoff(str(missing), str(kick)) == ("", "")
    assert not missing.exists()


# ── 5. record_dispatch updates, never inserts ───────────────────────────────

def test_record_dispatch_updates_never_inserts(db, tmp_path, monkeypatch):
    from mini_ork.orchestration.delegation import record_dispatch

    _seed(db, "run-owned")
    kick = tmp_path / "kickoff.md"
    kick.write_bytes(b"# kickoff\n")
    run_dir = tmp_path / "run.d"
    run_dir.mkdir()
    env = _registry(monkeypatch, tmp_path / "homes.json")
    env["MO_INITIATOR"] = "claude-session:s1"

    absent = record_dispatch(db, "run-absent", kickoff_path=str(kick),
                             run_dir=str(run_dir), env=dict(env), now=12345)
    assert not absent
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 1
    con.close()

    values = record_dispatch(db, "run-owned", kickoff_path=str(kick),
                             run_dir=str(run_dir), env=dict(env), now=12345)
    assert values["initiator"] == "claude-session:s1"
    row = _row(db, "run-owned")
    assert row[0] == "claude-session:s1"
    assert row[1] == "kickoff.md" and row[2] == _sha(kick)
    assert os.path.isabs(row[3])
    assert row[4] == 12345
    assert (run_dir / "kickoff.md").read_bytes() == kick.read_bytes()

    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 1
    con.close()


def test_record_dispatch_stores_null_copy_when_run_dir_is_missing(db, tmp_path, monkeypatch):
    from mini_ork.orchestration.delegation import record_dispatch

    _seed(db, "run-child")
    kick = tmp_path / "kick.md"
    kick.write_bytes(b"# k\n")
    env = _registry(monkeypatch, tmp_path / "h.json")
    env["MO_INITIATOR"] = "scheduler"
    missing = tmp_path / "no-run"

    record_dispatch(db, "run-child", kickoff_path=str(kick), run_dir=str(missing),
                    env=dict(env), now=1)
    row = _row(db, "run-child")
    assert row[0] == "scheduler"
    assert row[1] is None and row[2] is None
    assert row[3] is not None
    assert not missing.exists()


def test_record_dispatch_is_silent_on_a_pre0091_schema(db, tmp_path, monkeypatch, capsys):
    from mini_ork.orchestration.delegation import record_dispatch

    con = sqlite3.connect(db)
    con.execute("DROP INDEX IF EXISTS idx_task_runs_initiator")
    for column in FOUR:
        con.execute(f"ALTER TABLE task_runs DROP COLUMN {column}")
    con.commit()
    con.close()
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO task_runs (id, task_class, kickoff_path, status, created_at,"
        " updated_at) VALUES ('run-old-schema', 'code_fix', '/k.md',"
        " 'classified', 111, 111)")
    con.commit()
    con.close()
    kick = tmp_path / "kick.md"
    kick.write_bytes(b"# k\n")
    kickoff_dir = tmp_path / "run.d"
    kickoff_dir.mkdir()
    _registry(monkeypatch, tmp_path / "h.json")

    assert record_dispatch(db, "run-old-schema", kickoff_path=str(kick),
                           run_dir=str(kickoff_dir), now=1) == {}
    assert capsys.readouterr().err == ""


# ── 6. record_dispatch is fail-soft ─────────────────────────────────────────

def test_record_dispatch_warns_once_and_returns(tmp_path, monkeypatch, capsys):
    from mini_ork.orchestration.delegation import record_dispatch

    _clear_mo(monkeypatch)
    _registry(monkeypatch, tmp_path / "homes.json")
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_bytes(b"file, not a directory")
    kick = tmp_path / "kick.md"
    kick.write_bytes(b"# k\n")
    run_dir = tmp_path / "run.d"
    run_dir.mkdir()

    result = record_dispatch(str(not_a_dir / "state.db"), "run-42",
                             kickoff_path=str(kick), run_dir=str(run_dir), now=1)
    assert result == {}
    assert capsys.readouterr().err.count("[warn]") == 1


# ── 7. homes registry ───────────────────────────────────────────────────────

def test_add_home_dedupes_and_honours_the_override(tmp_path):
    from mini_ork.orchestration.delegation import add_home

    reg = tmp_path / "reg" / "homes.json"
    reg.parent.mkdir()
    db_a = tmp_path / "a" / "state.db"
    db_b = tmp_path / "b" / "state.db"
    for path in (db_a, db_b):
        path.parent.mkdir()
        path.write_bytes(b"x")

    add_home(str(db_a), env={"MO_HOMES_REGISTRY": str(reg)})
    add_home(str(db_a), env={"MO_HOMES_REGISTRY": str(reg)})
    data = json.loads(reg.read_text())
    assert data["version"] == 1
    assert data["homes"].count(str(db_a)) == 1

    add_home(str(db_b), env={"MO_HOMES_REGISTRY": str(reg)})
    assert let_homes(reg) == [str(db_a), str(db_b)]
    assert not os.path.exists(str(reg) + ".lockdir")


def let_homes(path):
    return json.loads(Path(path).read_text())["homes"]


def test_add_home_default_location_and_corrupt_rewrite(tmp_path, monkeypatch):
    from mini_ork.orchestration.delegation import add_home

    fake_home = tmp_path / "home"
    (fake_home / ".mini-ork").mkdir(parents=True)
    monkeypatch.delenv("MO_HOMES_REGISTRY", raising=False)
    db = tmp_path / "c" / "state.db"
    db.parent.mkdir()
    db.write_bytes(b"x")
    add_home(str(db), env={"HOME": str(fake_home)})
    default_reg = fake_home / ".mini-ork" / "homes.json"
    assert default_reg.is_file()
    assert str(db) in json.loads(default_reg.read_text())["homes"]

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json at all!!")
    add_home(str(db), env={"MO_HOMES_REGISTRY": str(corrupt)})
    data = json.loads(corrupt.read_text())
    assert data["version"] == 1
    assert data["homes"] == [str(db)]


# ── 8. set_artifact_path ────────────────────────────────────────────────────

def test_set_artifact_path_records_hash_or_null(db, tmp_path):
    from mini_ork.orchestration.delegation import set_artifact_path

    _seed(db, "run-a")
    _seed(db, "run-b")
    artifact = tmp_path / "framework-edit.diff"
    artifact.write_bytes(b"diff\n+delivered\n")

    set_artifact_path(db, "run-a", str(artifact), now=777)
    row = _row(db, "run-a")
    assert row[5] == str(artifact) and row[6] == _sha(artifact)
    assert row[4] == 777

    set_artifact_path(db, "run-b", str(tmp_path / "missing.diff"), now=888)
    row_b = _row(db, "run-b")
    assert row_b[5] is None and row_b[6] is None

    # fail-soft: an unopenable DB prints one warning and raises nothing
    set_artifact_path(str(tmp_path / "no-dir" / "state.db"), "run-a",
                      str(artifact), now=1)


# ── 9-11. runs command ──────────────────────────────────────────────────────

def _pre0091_dir(tmp_path):
    pre = tmp_path / "pre"
    pre.mkdir()
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name < "0091":
            shutil.copyfile(f, pre / f.name)
    return pre


def _run_cmd(monkeypatch, capsys, db_path, argv, registry=None):
    monkeypatch.setenv("MINI_ORK_HOME", str(Path(db_path).parent))
    monkeypatch.setenv("MINI_ORK_DB", str(db_path))
    if registry is not None:
        monkeypatch.setenv("MO_HOMES_REGISTRY", str(registry))
    else:
        monkeypatch.delenv("MO_HOMES_REGISTRY", raising=False)
    from mini_ork.cli import runs_cmd

    rc = runs_cmd.main(list(argv))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_runs_on_pre0091_db_lists_with_a_dash(monkeypatch, capsys, tmp_path):
    home = tmp_path / "pre-home"
    home.mkdir()
    path = home / "state.db"
    _migrate(path, _pre0091_dir(tmp_path))
    assert "initiator" not in _cols(str(path))
    con = sqlite3.connect(str(path))
    con.execute(
        "INSERT INTO task_runs (id, task_class, kickoff_path, status, created_at,"
        " updated_at) VALUES ('run-old', 'code_fix', '/k.md', 'classified', 111, 111)")
    con.commit()
    con.close()

    rc, out, _err = _run_cmd(monkeypatch, capsys, path, ["--json"])
    assert rc == 0
    rows = json.loads(out)["rows"]
    assert [r["run_id"] for r in rows] == ["run-old"]
    assert rows[0]["initiator"] is None

    rc2, text, _ = _run_cmd(monkeypatch, capsys, path, [])
    assert rc2 == 0 and "run-old" in text and "-" in text


def test_runs_all_homes_unions_and_dedupes(monkeypatch, capsys, tmp_path, template):
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    for home in (home_a, home_b):
        home.mkdir()
    db_a = home_a / "state.db"
    db_b = home_b / "state.db"
    shutil.copyfile(template, db_a)
    shutil.copyfile(template, db_b)
    _seed(str(db_a), "run-shared", initiator="claude-session:x", created=100)
    _seed(str(db_b), "run-shared", initiator="scheduler", created=200)
    reg = tmp_path / "homes.json"
    reg.write_text(json.dumps({"version": 1, "homes": [str(db_a), str(db_b), str(db_a)]}))

    rc, out, _err = _run_cmd(monkeypatch, capsys, db_a, ["--all-homes", "--json"], reg)
    assert rc == 0
    obj = json.loads(out)
    shared = [r for r in obj["rows"] if r["run_id"] == "run-shared"]
    # A run's row lives in exactly ONE home, so a run id in two databases is a
    # copied state.db, not two runs. The union keeps the first row and REPORTS
    # the collision — the count stays honest without hiding the divergence.
    assert len(shared) == 1
    assert obj["collisions"] == [
        {"run_id": "run-shared",
         "homes": [os.path.abspath(str(db_a)), os.path.abspath(str(db_b))]}]
    assert obj["incomplete"] == []


def test_runs_all_homes_reports_missing_home_as_incomplete(monkeypatch, capsys,
                                                           tmp_path, template):
    home = tmp_path / "a"
    home.mkdir()
    db = home / "state.db"
    shutil.copyfile(template, db)
    _seed(str(db), "run-a1", initiator="scheduler")
    missing = tmp_path / "gone" / "state.db"
    reg = tmp_path / "homes.json"
    reg.write_text(json.dumps({"version": 1, "homes": [str(db), str(missing)]}))

    rc, out, err = _run_cmd(monkeypatch, capsys, db, ["--all-homes", "--json"], reg)
    assert rc == 0
    obj = json.loads(out)
    assert len(obj["incomplete"]) == 1
    assert obj["incomplete"][0].endswith(os.path.join("gone", "state.db"))
    assert {r["run_id"] for r in obj["rows"]} == {"run-a1"}
    assert str(missing) in err


def test_runs_all_homes_never_presents_a_partial_union_as_complete(monkeypatch, capsys,
                                                                   tmp_path, template):
    home = tmp_path / "a"
    home.mkdir()
    db = home / "state.db"
    shutil.copyfile(template, db)
    _seed(str(db), "run-a1", initiator="scheduler")
    reg = tmp_path / "homes.json"
    reg.write_text("{not json at all!!")

    rc, out, err = _run_cmd(monkeypatch, capsys, db, ["--all-homes", "--json"], reg)
    assert rc == 0
    obj = json.loads(out)
    assert {r["run_id"] for r in obj["rows"]} == {"run-a1"}
    assert obj["incomplete"]
    assert err.strip()


def test_runs_initiator_prefix_filter(monkeypatch, capsys, tmp_path, template):
    home = tmp_path / "a"
    home.mkdir()
    db = home / "state.db"
    shutil.copyfile(template, db)
    _seed(str(db), "run-c1", initiator="claude-session:abc")
    _seed(str(db), "run-c2", initiator="claude-session:zzz")
    _seed(str(db), "run-s1", initiator="scheduler")
    _seed(str(db), "run-m1", initiator="manual:1")

    rc, out, _ = _run_cmd(monkeypatch, capsys, db, ["--initiator", "claude-session", "--json"])
    assert rc == 0
    assert {r["run_id"] for r in json.loads(out)["rows"]} == {"run-c1", "run-c2"}

    rc2, out2, _ = _run_cmd(monkeypatch, capsys, db, ["--initiator", "scheduler", "--json"])
    assert rc2 == 0
    assert {r["run_id"] for r in json.loads(out2)["rows"]} == {"run-s1"}


def test_runs_status_since_and_limit(monkeypatch, capsys, tmp_path, template):
    home = tmp_path / "a"
    home.mkdir()
    db = home / "state.db"
    shutil.copyfile(template, db)
    now = int(time.time())
    _seed(str(db), "run-p", initiator="scheduler", status="published", created=now)
    _seed(str(db), "run-f", initiator="scheduler", status="failed", created=now)

    rc, out, _ = _run_cmd(monkeypatch, capsys, db, ["--status", "published", "--json"])
    assert rc == 0 and {r["run_id"] for r in json.loads(out)["rows"]} == {"run-p"}

    rc2, out2, _ = _run_cmd(monkeypatch, capsys, db, ["--since", "24h", "--json"])
    assert rc2 == 0 and {r["run_id"] for r in json.loads(out2)["rows"]} == {"run-p", "run-f"}

    rc3, out3, _ = _run_cmd(monkeypatch, capsys, db, ["--limit", "1", "--json"])
    assert rc3 == 0 and len(json.loads(out3)["rows"]) == 1


def test_runs_usage_error_exits_2(monkeypatch, capsys, tmp_path, template):
    home = tmp_path / "a"
    home.mkdir()
    db = home / "state.db"
    shutil.copyfile(template, db)
    rc, _out, _err = _run_cmd(monkeypatch, capsys, db, ["--definitely-not-a-flag"])
    assert rc == 2


# ── 12. both call sites are wired ───────────────────────────────────────────

def test_classify_lifecycle_records_the_dispatch(tmp_path, monkeypatch, template):
    from mini_ork.cli import classify

    home = tmp_path / "home"
    run_dir = home / "runs" / "run-e2e"
    run_dir.mkdir(parents=True)
    path = home / "state.db"
    shutil.copyfile(template, path)
    kick = tmp_path / "kickoff.md"
    kick.write_bytes(b"# kickoff\ndelegation ledger end to end\n")

    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(path))
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-e2e")
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.delenv("MINI_ORK_DRY_RUN", raising=False)
    for name in ("MO_SCHEDULER_DISPATCH", "MO_RECOVER_OF", "MO_RSI_CYCLE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MO_INITIATOR", "claude-session:e2e")
    monkeypatch.setenv("MO_HOMES_REGISTRY", str(tmp_path / "homes.json"))

    assert classify.main([str(kick)], db=str(path)) == 0

    con = sqlite3.connect(str(path))
    row = con.execute(
        "SELECT initiator, kickoff_copy, kickoff_sha256, home_db, kickoff_path"
        " FROM task_runs WHERE id='run-e2e'").fetchone()
    con.close()
    initiator, copy_name, digest, home_db, kickoff_path = row
    assert initiator == "claude-session:e2e"
    assert digest == _sha(kick)
    assert home_db and os.path.isabs(home_db)
    assert copy_name == "kickoff.md"
    assert (run_dir / "kickoff.md").read_bytes() == kick.read_bytes()
    assert kickoff_path == str(kick)


def test_recursive_spawn_records_the_dispatch(tmp_path, monkeypatch, template):
    from mini_ork.orchestration import recursive

    home = tmp_path / "home"
    child_dir = home / "runs" / "child-x"
    child_dir.mkdir(parents=True)
    path = home / "state.db"
    shutil.copyfile(template, path)
    con = sqlite3.connect(str(path))
    con.execute(
        "INSERT INTO task_runs (id, task_class, recipe, kickoff_path, status,"
        " created_at, updated_at) VALUES ('parent-x', 'code_fix', NULL,"
        " '/k.md', 'classified', 0, 0)")
    con.commit()
    con.close()
    kick = tmp_path / "kick.md"
    kick.write_bytes(b"# child kickoff\n")

    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(path))
    monkeypatch.setenv("MO_INITIATOR", "scheduler")
    monkeypatch.setenv("MO_HOMES_REGISTRY", str(tmp_path / "homes.json"))

    recursive.mo_recursive_approve_spawn(
        "parent-x", "child-x", "code-fix", str(kick), str(tmp_path / "ws"), 1, 0.4, 1)

    row = _row(str(path), "child-x")
    assert row[0] == "scheduler"
    assert row[1] == "kickoff.md" and row[2] == _sha(kick)
    assert row[3] and os.path.isabs(row[3])
    assert (child_dir / "kickoff.md").read_bytes() == kick.read_bytes()


def test_recursive_call_site_is_after_commit():
    src = (REPO / "mini_ork" / "orchestration" / "recursive.py").read_text()
    tree = ast.parse(src)
    functions = [f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
                 and "INSERT INTO task_runs" in (ast.get_source_segment(src, f) or "")]
    assert functions, "the function holding the task_runs INSERT must exist"
    calls = [c for f in functions for c in ast.walk(f)
             if isinstance(c, ast.Call)
             and ((isinstance(c.func, ast.Name) and c.func.id == "record_dispatch")
                  or (isinstance(c.func, ast.Attribute) and c.func.attr == "record_dispatch"))]
    assert calls, "record_dispatch must be wired into spawn registration"
    assert "child_run_id" in " ".join(ast.unparse(a) for c in calls for a in c.args)
    commits = [c.lineno for f in functions for c in ast.walk(f)
               if isinstance(c, ast.Call)
               and isinstance(c.func, ast.Attribute) and c.func.attr == "commit"]
    assert commits and min(c.lineno for c in calls) > max(commits)


def test_publisher_call_site_records_after_publish():
    src = (REPO / "mini_ork" / "cli" / "publisher.py").read_text()
    tree = ast.parse(src)
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "publisher_node")
    stamps = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
              and isinstance(c.func, ast.Name) and c.func.id == "_mark_published"]
    artifacts = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
                 and isinstance(c.func, ast.Name) and c.func.id == "set_artifact_path"]
    assert stamps and artifacts
    assert any(a.lineno > m.lineno for a in artifacts for m in stamps)
    assert "review-diff.patch" in src
