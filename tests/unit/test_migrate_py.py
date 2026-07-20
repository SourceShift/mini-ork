"""Parity gate: mini_ork.stores.migrate vs lib/migrate.sh.

Drives the live bash functions via subprocess against a temp DB + temp
migrations dir and compares checksums, schema_migrations rows (applied_at
excluded — bash stamps strftime('now')), applied schema, and status/verify.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.stores import migrate as mig

SH = REPO / "lib" / "migrate.sh"

MIGS = {
    "001_foo.sql": "CREATE TABLE foo (id INTEGER PRIMARY KEY, v TEXT);\n"
                   "INSERT INTO foo(v) VALUES ('a');\n",
    "002_bar.sql": "CREATE TABLE bar (id INTEGER PRIMARY KEY);\n",
    "003_begin.sql": "BEGIN;\nCREATE TABLE baz (id INTEGER);\nCOMMIT;\n",
}


def _bash(fn, *args, db=None, env_extra=None):
    env = {**os.environ, "MINI_ORK_ROOT": str(REPO)}
    if db:
        env["MINI_ORK_DB"] = db
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(
        ["bash", "-c", f'. "{SH}" && {fn} "$@"', "_", *args],
        env=env, capture_output=True, text=True)
    return r.stdout.strip(), r.stderr.strip(), r.returncode


@pytest.fixture
def migdir(tmp_path):
    d = tmp_path / "migrations"; d.mkdir()
    for name, sql in MIGS.items():
        (d / name).write_text(sql)
    return str(d)


def _sm_rows(db):
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT filename, checksum, mini_ork_version FROM schema_migrations ORDER BY filename"
    ).fetchall()
    con.close()
    return rows


def _tables(db):
    con = sqlite3.connect(db)
    t = sorted(r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall())
    con.close()
    return t


def test_checksum_parity(tmp_path):
    f = tmp_path / "x.sql"; f.write_text("CREATE TABLE q (a);\n-- comment\n")
    out_b, _, rc = _bash("mo_migrate_checksum", str(f))
    assert rc == 0 and out_b == mig.checksum(str(f))


@pytest.mark.parametrize("s,legacy", [
    ("runner-applied", True), ("v1", True), ("", True), ("phase-a-1", True),
    ("a" * 64, False), ("a" * 63, True), ("A" * 64, True),  # uppercase = non-hex
    ("0123456789abcdef" * 4, False),
])
def test_is_legacy_checksum_parity(s, legacy):
    _, _, rc = _bash("_mo_migrate_is_legacy_checksum", s)
    # bash: return 0 == legacy(true); return 1 == real
    assert (rc == 0) == legacy
    assert mig.is_legacy_checksum(s) == legacy


def test_apply_parity(tmp_path, migdir):
    db_b, db_p = str(tmp_path / "b.db"), str(tmp_path / "p.db")
    out_b, _, rc_b = _bash("mo_migrate_apply", migdir, "0", db=db_b)
    rc_p, out_p = mig.migrate_apply(migdir, dry_run=False, db=db_p)
    assert rc_b == rc_p == 0
    assert _sm_rows(db_b) == _sm_rows(db_p)
    assert _tables(db_b) == _tables(db_p)
    # the ok/apply lines match (compare stripped — outer .strip() ate line-1 indent)
    assert [l.strip() for l in out_b.splitlines() if "[apply]" in l or "[ok]" in l] == \
        [l.strip() for l in out_p if "[apply]" in l or "[ok]" in l]
    # idempotent re-apply: no changes, rc 0
    _bash("mo_migrate_apply", migdir, "0", db=db_b)
    rc2, _ = mig.migrate_apply(migdir, db=db_p)
    assert rc2 == 0 and _sm_rows(db_b) == _sm_rows(db_p)


def test_dry_run_parity(tmp_path, migdir):
    db_b, db_p = str(tmp_path / "b.db"), str(tmp_path / "p.db")
    out_b, _, rc_b = _bash("mo_migrate_apply", migdir, "1", db=db_b)
    rc_p, out_p = mig.migrate_apply(migdir, dry_run=True, db=db_p)
    assert rc_b == rc_p == 0
    pend_b = sorted(l.strip() for l in out_b.splitlines() if "[pending]" in l)
    pend_p = sorted(l.strip() for l in out_p if "[pending]" in l)
    assert pend_b == pend_p
    assert _sm_rows(db_b) == _sm_rows(db_p) == []  # nothing applied on dry run


def test_rehash_legacy_parity(tmp_path, migdir):
    db_b, db_p = str(tmp_path / "b.db"), str(tmp_path / "p.db")
    # apply, then clobber one checksum to a legacy placeholder on both DBs
    _bash("mo_migrate_apply", migdir, "0", db=db_b)
    mig.migrate_apply(migdir, db=db_p)
    for db in (db_b, db_p):
        con = sqlite3.connect(db)
        con.execute("UPDATE schema_migrations SET checksum='runner-applied' WHERE filename='001_foo.sql'")
        con.commit(); con.close()
    out_b, _, rc_b = _bash("mo_migrate_apply", migdir, "0", db=db_b)
    rc_p, out_p = mig.migrate_apply(migdir, db=db_p)
    assert rc_b == rc_p == 0
    assert any("[rehash]" in l for l in out_b.splitlines())
    assert any("[rehash]" in l for l in out_p)
    assert _sm_rows(db_b) == _sm_rows(db_p)  # both re-hashed to the real sha256


def test_drift_fails_parity(tmp_path, migdir):
    db_b, db_p = str(tmp_path / "b.db"), str(tmp_path / "p.db")
    _bash("mo_migrate_apply", migdir, "0", db=db_b)
    mig.migrate_apply(migdir, db=db_p)
    # set a REAL-but-wrong checksum (64 hex) → drift, not legacy
    for db in (db_b, db_p):
        con = sqlite3.connect(db)
        con.execute("UPDATE schema_migrations SET checksum=? WHERE filename='001_foo.sql'", ("b" * 64,))
        con.commit(); con.close()
    _, _, rc_b = _bash("mo_migrate_apply", migdir, "0", db=db_b)
    rc_p, _ = mig.migrate_apply(migdir, db=db_p)
    assert rc_b == rc_p == 1
    # with MO_MIGRATE_ALLOW_DRIFT=1 → rc 0 on both
    _, _, rc_b2 = _bash("mo_migrate_apply", migdir, "0", db=db_b, env_extra={"MO_MIGRATE_ALLOW_DRIFT": "1"})
    os.environ["MO_MIGRATE_ALLOW_DRIFT"] = "1"
    try:
        rc_p2, _ = mig.migrate_apply(migdir, db=db_p)
    finally:
        del os.environ["MO_MIGRATE_ALLOW_DRIFT"]
    assert rc_b2 == rc_p2 == 0


def test_verify_parity(tmp_path, migdir):
    db_b, db_p = str(tmp_path / "b.db"), str(tmp_path / "p.db")
    _bash("mo_migrate_apply", migdir, "0", db=db_b)
    mig.migrate_apply(migdir, db=db_p)
    _, _, rc_b = _bash("mo_migrate_verify", migdir, db=db_b)
    assert rc_b == mig.migrate_verify(migdir, db=db_p) == 0
    # introduce drift on both
    for db in (db_b, db_p):
        con = sqlite3.connect(db)
        con.execute("UPDATE schema_migrations SET checksum=? WHERE filename='002_bar.sql'", ("c" * 64,))
        con.commit(); con.close()
    _, _, rc_b2 = _bash("mo_migrate_verify", migdir, db=db_b)
    assert rc_b2 == mig.migrate_verify(migdir, db=db_p) == 1
