"""Unit tests: mini_ork.stores.migrate (bash parity halves removed; formerly vs lib/migrate.sh + db/init.sh).

Drives the Python port against a temp DB + temp migrations dir and asserts
checksums, schema_migrations rows, applied schema, status/verify, the
dot-command subset, and init_db's real-repo schema build.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.stores import migrate as mig

MIGS = {
    "001_foo.sql": "CREATE TABLE foo (id INTEGER PRIMARY KEY, v TEXT);\n"
                   "INSERT INTO foo(v) VALUES ('a');\n",
    "002_bar.sql": "CREATE TABLE bar (id INTEGER PRIMARY KEY);\n",
    "003_begin.sql": "BEGIN;\nCREATE TABLE baz (id INTEGER);\nCOMMIT;\n",
}


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


def _set_checksum(db, filename, value):
    con = sqlite3.connect(db)
    con.execute("UPDATE schema_migrations SET checksum=? WHERE filename=?",
                (value, filename))
    con.commit(); con.close()


def test_checksum(tmp_path):
    f = tmp_path / "x.sql"; f.write_text("CREATE TABLE q (a);\n-- comment\n")
    assert mig.checksum(str(f)) == hashlib.sha256(f.read_bytes()).hexdigest()


@pytest.mark.parametrize("s,legacy", [
    ("runner-applied", True), ("v1", True), ("", True), ("phase-a-1", True),
    ("a" * 64, False), ("a" * 63, True), ("A" * 64, True),  # uppercase = non-hex
    ("0123456789abcdef" * 4, False),
])
def test_is_legacy_checksum(s, legacy):
    assert mig.is_legacy_checksum(s) == legacy


def test_apply(tmp_path, migdir):
    db_p = str(tmp_path / "p.db")
    rc_p, out_p = mig.migrate_apply(migdir, dry_run=False, db=db_p)
    assert rc_p == 0
    rows = _sm_rows(db_p)
    assert [r[0] for r in rows] == sorted(MIGS)
    for _, cksum, _ in rows:
        assert len(cksum) == 64 and all(c in "0123456789abcdef" for c in cksum)
    for t in ("foo", "bar", "baz"):
        assert t in _tables(db_p)
    # seed row from 001_foo.sql landed
    con = sqlite3.connect(db_p)
    assert con.execute("SELECT v FROM foo").fetchall() == [("a",)]
    con.close()
    # the ok/apply lines mention each migration
    applied = [l.strip() for l in out_p if "[apply]" in l or "[ok]" in l]
    assert len(applied) >= 3
    # idempotent re-apply: no changes, rc 0
    rc2, _ = mig.migrate_apply(migdir, db=db_p)
    assert rc2 == 0 and len(_sm_rows(db_p)) == 3


def test_dry_run(tmp_path, migdir):
    db_p = str(tmp_path / "p.db")
    rc_p, out_p = mig.migrate_apply(migdir, dry_run=True, db=db_p)
    assert rc_p == 0
    pend_p = sorted(l.strip() for l in out_p if "[pending]" in l)
    assert len(pend_p) == 3
    assert _sm_rows(db_p) == []  # nothing applied on dry run


def test_rehash_legacy(tmp_path, migdir):
    db_p = str(tmp_path / "p.db")
    # apply, then clobber one checksum to a legacy placeholder
    mig.migrate_apply(migdir, db=db_p)
    _set_checksum(db_p, "001_foo.sql", "runner-applied")
    rc_p, out_p = mig.migrate_apply(migdir, db=db_p)
    assert rc_p == 0
    assert any("[rehash]" in l for l in out_p)
    # re-hashed to the real sha256
    rows = dict((r[0], r[1]) for r in _sm_rows(db_p))
    assert rows["001_foo.sql"] == mig.checksum(
        str(Path(migdir) / "001_foo.sql"))


def test_drift_fails(tmp_path, migdir):
    db_p = str(tmp_path / "p.db")
    mig.migrate_apply(migdir, db=db_p)
    # set a REAL-but-wrong checksum (64 hex) → drift, not legacy
    _set_checksum(db_p, "001_foo.sql", "b" * 64)
    rc_p, _ = mig.migrate_apply(migdir, db=db_p)
    assert rc_p == 1
    # with MO_MIGRATE_ALLOW_DRIFT=1 → rc 0
    os.environ["MO_MIGRATE_ALLOW_DRIFT"] = "1"
    try:
        rc_p2, _ = mig.migrate_apply(migdir, db=db_p)
    finally:
        del os.environ["MO_MIGRATE_ALLOW_DRIFT"]
    assert rc_p2 == 0


def test_verify(tmp_path, migdir):
    db_p = str(tmp_path / "p.db")
    mig.migrate_apply(migdir, db=db_p)
    assert mig.migrate_verify(migdir, db=db_p) == 0
    # introduce drift
    _set_checksum(db_p, "002_bar.sql", "c" * 64)
    assert mig.migrate_verify(migdir, db=db_p) == 1


def test_failing_migration_rollback(tmp_path):
    """A migration with invalid SQL must roll back: rc=1, the partial DDL is
    NOT committed (the bad table is absent), and the migration is NOT
    recorded in schema_migrations — while a preceding good migration stays
    committed."""
    d = tmp_path / "migrations"; d.mkdir()
    (d / "001_ok.sql").write_text("CREATE TABLE good (id INTEGER);\n")
    (d / "002_bad.sql").write_text(
        "CREATE TABLE bad (id INTEGER);\nTHIS IS NOT VALID SQL;\n")
    migdir = str(d)
    db_p = str(tmp_path / "p.db")

    rc_p, _ = mig.migrate_apply(migdir, db=db_p)
    assert rc_p == 1  # fails on the bad migration

    tables = _tables(db_p)
    assert "good" in tables and "bad" not in tables  # partial DDL not committed
    recorded = {r[0] for r in _sm_rows(db_p)}
    assert "001_ok.sql" in recorded and "002_bad.sql" not in recorded


def test_status_counts(tmp_path, migdir):
    """migrate_status's (applied, pending, drifted, total) tuple across all
    three counters: apply the 3 MIGS, add a never-applied migration
    (pending) and drift one applied migration (drifted)."""
    db_p = str(tmp_path / "p.db")
    mig.migrate_apply(migdir, db=db_p)

    Path(migdir, "004_pending.sql").write_text("CREATE TABLE pend (id INTEGER);\n")
    _set_checksum(db_p, "002_bar.sql", "d" * 64)  # real-but-wrong → drift

    port = mig.migrate_status(migdir, db=db_p)  # (applied, pending, drifted, total)
    assert port == (3, 1, 1, 4)


# ──────────────────────────────────────────────────────────────────────────────
# WS2: dot-command migrations + init_db (db/init.sh port)
# ──────────────────────────────────────────────────────────────────────────────

# .read "|sh -c '…'" in the shape of the shipped 0042/0044/0049 guards: a shell
# pipeline whose stdout (conditional ALTER TABLE) becomes SQL. Uses the same
# backslash-escaped quoting style as the real migrations (\" inside the
# double-quoted dot-command arg), which the sqlite3 CLI resolves via
# resolve_backslashes() — the port mirrors that in _unquote.
DOTCMD_MIGS = {
    "001_seed.sql": "CREATE TABLE seeded (id INTEGER PRIMARY KEY, v TEXT);\n",
    "002_dotcmd.sql": (
        "-- exercises .read \"|sh -c …\" with MINI_ORK_DB from the environment\n"
        ".read \"|sh -c 'db=\\\"${MINI_ORK_DB:?}\\\"; "
        "have=$(sqlite3 \\\"$db\\\" \\\"SELECT COUNT(*) FROM pragma_table_info(\\\\\\\"seeded\\\\\\\") WHERE name = \\\\\\\"extra\\\\\\\";\\\"); "
        "if [ \\\"${have:-0}\\\" = \\\"0\\\" ]; then printf \\\"%s\\\\n\\\" \\\"ALTER TABLE seeded ADD COLUMN extra TEXT DEFAULT NULL;\\\"; fi'\"\n"
    ),
}

# .once / .read <file> / .shell in the shape of shipped 0039: generate SQL via a
# SELECT into a temp file, read it back, then clean up.
ONCE_MIGS = {
    "001_seed.sql": "CREATE TABLE seed2 (id INTEGER PRIMARY KEY, v TEXT);\n",
    "002_once.sql": (
        ".once {tmp}\n"
        "SELECT 'CREATE INDEX IF NOT EXISTS idx_seed2_v ON seed2(v);';\n"
        ".read {tmp}\n"
        ".shell rm -f {tmp}\n"
    ),
}


def _dotcmd_migdir(tmp_path, migs):
    d = tmp_path / "migrations"
    d.mkdir()
    for name, sql in migs.items():
        (d / name).write_text(sql.replace("{tmp}", str(tmp_path / "gen.sql")))
    return str(d)


def _schema_dump(db):
    """`.schema` via the sqlite3 CLI — the canonical text."""
    r = subprocess.run(["sqlite3", db, ".schema"], capture_output=True, text=True)
    assert r.returncode == 0
    return r.stdout


def test_dot_command_read_pipe(tmp_path):
    """A migration using `.read "|sh -c '…'"`: the shell pipeline's stdout is
    inlined as SQL (with MINI_ORK_DB exported), and its conditional ALTER
    fires."""
    migdir = _dotcmd_migdir(tmp_path, DOTCMD_MIGS)
    db_p = str(tmp_path / "p.db")

    rc_p, out_p = mig.migrate_apply(migdir, db=db_p)
    assert rc_p == 0
    # the conditional ALTER fired
    assert "extra" in _schema_dump(db_p)
    assert any("[ok]" in l for l in out_p)


def test_dot_command_once_read_shell(tmp_path):
    """`.once <file>` + SELECT + `.read <file>` + `.shell rm`: generated SQL
    is applied and the scratch file is removed."""
    migdir = _dotcmd_migdir(tmp_path, ONCE_MIGS)
    db_p = str(tmp_path / "p.db")

    rc_p, _ = mig.migrate_apply(migdir, db=db_p)
    assert rc_p == 0
    assert "idx_seed2_v" in _schema_dump(db_p)
    assert not (tmp_path / "gen.sql").exists()  # .shell rm fired
    assert len(_sm_rows(db_p)) == 2


def test_dot_command_unsupported_fails_closed(tmp_path):
    """A dot-command outside the supported subset must fail the migration
    (rolled back, rc=1) rather than being silently skipped."""
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "001_ok.sql").write_text("CREATE TABLE good (id INTEGER);\n")
    (d / "002_bad.sql").write_text(".mode csv\nCREATE TABLE bad (id INTEGER);\n")
    db = str(tmp_path / "x.db")
    rc, _ = mig.migrate_apply(str(d), db=db)
    assert rc == 1
    tables = _tables(db)
    assert "good" in tables and "bad" not in tables


def test_init_db_real_repo_schema(tmp_path):
    """init_db on a fresh DB against the REAL db/migrations + db/views
    builds the full production schema."""
    db_b = str(tmp_path / "b.db")

    old = os.environ.pop("MINI_ORK_DB", None)
    try:
        rc_p, out_p, err_p = mig.init_db(db=db_b, root=str(REPO))
    finally:
        if old is not None:
            os.environ["MINI_ORK_DB"] = old
    assert rc_p == 0, f"init_db failed:\n{out_p}\n{err_p}"

    dump = _schema_dump(db_b)
    # the full 111-table schema really got built
    assert dump.count("CREATE TABLE") >= 100
    # every migration file recorded
    n_migs = len(list((REPO / "db" / "migrations").glob("*.sql"))) + \
        len(list((REPO / "db" / "views").glob("*.sql")))
    assert len(_sm_rows(db_b)) == n_migs
    for t in ("epics", "task_runs", "execution_traces"):
        assert t in _tables(db_b)


def test_init_db_idempotent_rerun(tmp_path):
    """Second init_db run applies nothing and stays rc=0 (DB line + Done
    line only)."""
    db_b = str(tmp_path / "b.db")

    old = os.environ.pop("MINI_ORK_DB", None)
    try:
        rc1, _, _ = mig.init_db(db=db_b, root=str(REPO))
        rc2, out2, err2 = mig.init_db(db=db_b, root=str(REPO))
    finally:
        if old is not None:
            os.environ["MINI_ORK_DB"] = old
    assert rc1 == rc2 == 0
    assert "[apply]" not in out2
    assert "Done" in out2


def test_init_db_checksum_drift_refusal(tmp_path):
    """A real-but-wrong checksum on an applied migration must make init_db
    refuse (rc=1) with a 'checksum drift' stderr line — never silently
    re-run or skip."""
    db_b = str(tmp_path / "b.db")
    old = os.environ.pop("MINI_ORK_DB", None)
    try:
        rc0, _, _ = mig.init_db(db=db_b, root=str(REPO))
    finally:
        if old is not None:
            os.environ["MINI_ORK_DB"] = old
    assert rc0 == 0

    con = sqlite3.connect(db_b)
    victim = con.execute(
        "SELECT filename FROM schema_migrations ORDER BY filename LIMIT 1"
    ).fetchone()[0]
    con.execute("UPDATE schema_migrations SET checksum=? WHERE filename=?",
                ("b" * 64, victim))
    con.commit()
    con.close()

    old = os.environ.pop("MINI_ORK_DB", None)
    try:
        rc_p, out_p, err_p = mig.init_db(db=db_b, root=str(REPO))
    finally:
        if old is not None:
            os.environ["MINI_ORK_DB"] = old
    assert rc_p == 1
    assert "checksum drift" in err_p
