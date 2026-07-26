"""Parity gate: mini_ork.stores.migrate vs lib/migrate.sh.

Drives the live bash functions via subprocess against a temp DB + temp
migrations dir and compares checksums, schema_migrations rows (applied_at
excluded — bash stamps strftime('now')), applied schema, and status/verify.
"""
from __future__ import annotations

import os
import re
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


def test_failing_migration_rollback_parity(tmp_path):
    """A migration with invalid SQL must roll back on BOTH sides: rc=1, the
    partial DDL is NOT committed (the bad table is absent), and the migration is
    NOT recorded in schema_migrations — while a preceding good migration stays
    committed. Mirrors tests/unit/test_migrate.sh case 6 (transactional
    rollback), which the parity gate did not exercise (all MIGS are valid SQL).
    The port's _apply_one wraps each migration in BEGIN;…COMMIT; and ROLLBACKs
    on sqlite3.Error, so no production change is required."""
    d = tmp_path / "migrations"; d.mkdir()
    (d / "001_ok.sql").write_text("CREATE TABLE good (id INTEGER);\n")
    (d / "002_bad.sql").write_text(
        "CREATE TABLE bad (id INTEGER);\nTHIS IS NOT VALID SQL;\n")
    migdir = str(d)
    db_b, db_p = str(tmp_path / "b.db"), str(tmp_path / "p.db")

    _, _, rc_b = _bash("mo_migrate_apply", migdir, "0", db=db_b)
    rc_p, _ = mig.migrate_apply(migdir, db=db_p)
    assert rc_b == rc_p == 1  # both fail on the bad migration

    # identical end-state on both sides: good table + schema_migrations only.
    assert _tables(db_b) == _tables(db_p)
    assert "good" in _tables(db_b) and "bad" not in _tables(db_b)  # partial DDL not committed
    # bad migration NOT recorded; good migration recorded — identical rows.
    assert _sm_rows(db_b) == _sm_rows(db_p)
    recorded = {r[0] for r in _sm_rows(db_b)}
    assert "001_ok.sql" in recorded and "002_bad.sql" not in recorded


def test_status_counts_parity(tmp_path, migdir):
    """mo_migrate_status's 'N applied, M pending, K drifted (of T)' summary must
    agree with the port's (applied, pending, drifted, total) tuple across all
    three counters. Mirrors tests/unit/test_migrate.sh case 7a, which the parity
    gate did not drive (no _py.py case invoked mo_migrate_status). Scenario
    exercises every counter at once: apply the 3 MIGS, add a never-applied
    migration (pending) and drift one applied migration (drifted)."""
    db_b, db_p = str(tmp_path / "b.db"), str(tmp_path / "p.db")
    _bash("mo_migrate_apply", migdir, "0", db=db_b)
    mig.migrate_apply(migdir, db=db_p)

    Path(migdir, "004_pending.sql").write_text("CREATE TABLE pend (id INTEGER);\n")
    for db in (db_b, db_p):
        con = sqlite3.connect(db)
        con.execute("UPDATE schema_migrations SET checksum=? WHERE filename='002_bar.sql'",
                    ("d" * 64,))  # real-but-wrong 64-hex → drift, not legacy
        con.commit(); con.close()

    out_b, _, _ = _bash("mo_migrate_status", migdir, db=db_b)
    port = mig.migrate_status(migdir, db=db_p)  # (applied, pending, drifted, total)

    m = re.search(r"(\d+) applied, (\d+) pending, (\d+) drifted \(of (\d+)\)", out_b)
    assert m, f"no summary line in bash mo_migrate_status output:\n{out_b}"
    bash_counts = tuple(map(int, m.groups()))
    assert bash_counts == port == (3, 1, 1, 4)


# ──────────────────────────────────────────────────────────────────────────────
# WS2: dot-command migrations + init_db (db/init.sh port)
# ──────────────────────────────────────────────────────────────────────────────

INIT_SH = REPO / "db" / "init.sh"

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
    """`.schema` via the sqlite3 CLI — the canonical text both sides produce."""
    r = subprocess.run(["sqlite3", db, ".schema"], capture_output=True, text=True)
    assert r.returncode == 0
    return r.stdout


def test_dot_command_read_pipe_parity(tmp_path):
    """A migration using `.read "|sh -c '…'"` must produce identical DBs on the
    bash runner and the Python port: the shell pipeline's stdout is inlined as
    SQL (with MINI_ORK_DB exported), and its conditional ALTER fires."""
    migdir = _dotcmd_migdir(tmp_path, DOTCMD_MIGS)
    db_b, db_p = str(tmp_path / "b.db"), str(tmp_path / "p.db")

    out_b, err_b, rc_b = _bash("mo_migrate_apply", migdir, "0", db=db_b)
    rc_p, out_p = mig.migrate_apply(migdir, db=db_p)
    assert rc_b == rc_p == 0, f"bash={rc_b} py={rc_p} stderr={err_b}"
    assert _sm_rows(db_b) == _sm_rows(db_p)
    assert _tables(db_b) == _tables(db_p)
    assert _schema_dump(db_b) == _schema_dump(db_p)
    # the conditional ALTER fired on both sides
    assert "extra" in _schema_dump(db_p)
    assert [l.strip() for l in out_b.splitlines() if "[ok]" in l] == \
        [l.strip() for l in out_p if "[ok]" in l]


def test_dot_command_once_read_shell_parity(tmp_path):
    """`.once <file>` + SELECT + `.read <file>` + `.shell rm` (shipped 0039's
    shape) must run identically on both backends: generated SQL is applied and
    the scratch file is removed."""
    migdir = _dotcmd_migdir(tmp_path, ONCE_MIGS)
    db_b, db_p = str(tmp_path / "b.db"), str(tmp_path / "p.db")

    _, _, rc_b = _bash("mo_migrate_apply", migdir, "0", db=db_b)
    rc_p, _ = mig.migrate_apply(migdir, db=db_p)
    assert rc_b == rc_p == 0
    assert _schema_dump(db_b) == _schema_dump(db_p)
    assert "idx_seed2_v" in _schema_dump(db_p)
    assert not (tmp_path / "gen.sql").exists()  # .shell rm fired
    assert _sm_rows(db_b) == _sm_rows(db_p)


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


def _clean_init_env(db):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MO_")}
    env.pop("MINI_ORK_DB", None)
    env["MINI_ORK_ROOT"] = str(REPO)
    return env


def test_init_db_real_repo_schema_parity(tmp_path):
    """The A/B gate: bash db/init.sh on a fresh DB (A) vs init_db on a fresh
    DB (B) against the REAL db/migrations + db/views — .schema dumps and
    sqlite_master listings must be byte-identical, schema_migrations rows
    identical (applied_at excluded), and stdout/stderr byte-equal modulo the
    DB path."""
    db_a, db_b = str(tmp_path / "a.db"), str(tmp_path / "b.db")
    env = _clean_init_env(db_a)
    rb = subprocess.run(["bash", str(INIT_SH)], env={**env, "MINI_ORK_DB": db_a},
                        capture_output=True, text=True)
    assert rb.returncode == 0, f"bash init.sh failed:\n{rb.stdout}\n{rb.stderr}"

    old = os.environ.pop("MINI_ORK_DB", None)
    try:
        rc_p, out_p, err_p = mig.init_db(db=db_b, root=str(REPO))
    finally:
        if old is not None:
            os.environ["MINI_ORK_DB"] = old
    assert rc_p == 0, f"init_db failed:\n{out_p}\n{err_p}"

    # stdout/stderr byte-equal modulo the db path spelling
    assert rb.stdout.replace(db_a, "<DB>") == out_p.replace(db_b, "<DB>")
    assert rb.stderr == err_p

    # the core deliverable: schema A/B diff must be empty
    assert _schema_dump(db_a) == _schema_dump(db_b)
    names_a = [r[0] for r in sqlite3.connect(db_a).execute(
        "SELECT name FROM sqlite_master ORDER BY name").fetchall()]
    names_b = [r[0] for r in sqlite3.connect(db_b).execute(
        "SELECT name FROM sqlite_master ORDER BY name").fetchall()]
    assert names_a == names_b
    assert _sm_rows(db_a) == _sm_rows(db_b)
    # sanity: the full 111-table schema really got built
    assert _schema_dump(db_b).count("CREATE TABLE") >= 100


def test_init_db_idempotent_rerun(tmp_path):
    """Second init_db run applies nothing and stays rc=0 with the same
    stdout shape as bash's rerun (DB line + Done line only)."""
    db_a, db_b = str(tmp_path / "a.db"), str(tmp_path / "b.db")
    env = _clean_init_env(db_a)
    for args, kw in ((["bash", str(INIT_SH)], {"env": {**env, "MINI_ORK_DB": db_a}}),
                     (["bash", str(INIT_SH)], {"env": {**env, "MINI_ORK_DB": db_a}})):
        r = subprocess.run(args, capture_output=True, text=True, **kw)
        assert r.returncode == 0
    rerun_bash = r

    old = os.environ.pop("MINI_ORK_DB", None)
    try:
        rc1, _, _ = mig.init_db(db=db_b, root=str(REPO))
        rc2, out2, err2 = mig.init_db(db=db_b, root=str(REPO))
    finally:
        if old is not None:
            os.environ["MINI_ORK_DB"] = old
    assert rc1 == rc2 == 0
    assert rerun_bash.stdout.replace(db_a, "<DB>") == out2.replace(db_b, "<DB>")
    assert rerun_bash.stderr == err2
    assert "[apply]" not in out2
    assert _schema_dump(db_a) == _schema_dump(db_b)


def test_init_db_checksum_drift_refusal(tmp_path):
    """A real-but-wrong checksum on an applied migration must make init_db
    refuse (rc=1) with the same stderr FAIL lines as bash — never silently
    re-run or skip."""
    db_a, db_b = str(tmp_path / "a.db"), str(tmp_path / "b.db")
    env = _clean_init_env(db_a)
    r = subprocess.run(["bash", str(INIT_SH)], env={**env, "MINI_ORK_DB": db_a},
                       capture_output=True, text=True)
    assert r.returncode == 0
    old = os.environ.pop("MINI_ORK_DB", None)
    try:
        rc0, _, _ = mig.init_db(db=db_b, root=str(REPO))
    finally:
        if old is not None:
            os.environ["MINI_ORK_DB"] = old
    assert rc0 == 0

    for db in (db_a, db_b):
        con = sqlite3.connect(db)
        victim = con.execute(
            "SELECT filename FROM schema_migrations ORDER BY filename LIMIT 1"
        ).fetchone()[0]
        con.execute("UPDATE schema_migrations SET checksum=? WHERE filename=?",
                    ("b" * 64, victim))
        con.commit()
        con.close()

    rb = subprocess.run(["bash", str(INIT_SH)], env={**env, "MINI_ORK_DB": db_a},
                        capture_output=True, text=True)
    old = os.environ.pop("MINI_ORK_DB", None)
    try:
        rc_p, out_p, err_p = mig.init_db(db=db_b, root=str(REPO))
    finally:
        if old is not None:
            os.environ["MINI_ORK_DB"] = old
    assert rb.returncode == rc_p == 1
    assert "checksum drift" in rb.stderr and "checksum drift" in err_p
    assert rb.stdout.replace(db_a, "<DB>") == out_p.replace(db_b, "<DB>")
    assert rb.stderr == err_p
