"""Unit tests: LF-R11b — a finding whose run's diff landed on origin/main.

Covers the landing primitives (``run_patch_id``, ``landed_patch_ids``,
``landing_enabled``, ``annotate_landing``, ``check_landing``), the read-path
decay in ``context_v2`` and the learn "Your code" tab, and both apply paths of
``0079_code_findings_landing``.

Hermetic: every repo is a temp checkout whose ``origin/main`` is a local
remote-tracking ref (no network, no push), and every DB is a temp file. Nothing
here touches the live DB under ``.mini-ork/``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import mini_ork
from mini_ork import context_v2 as cv
from mini_ork.ide_pages.learn import code as code_tab
from mini_ork.learning import code_findings as cf
from mini_ork.stores import migrate

MIGRATIONS_DIR = Path(mini_ork.__file__).resolve().parent.parent / "db" / "migrations"
LANDING_TABLE_SQL = ("SELECT name FROM sqlite_master WHERE type='table' "
                     "AND name='code_findings_landing'")
TAB_FILE = "mini_ork/learning/code_findings.py"
KICKOFF_TEXT = "# Spec kickoff\n\n## Files in scope\n\n- `src/app.py`\n"

_CLEAN_ENV = ("MINI_ORK_DB", "MINI_ORK_HOME", "MO_FINDINGS_LANDING",
              "MO_FINDINGS_STALENESS", "MO_CONTEXT_V2_MAX_AGE_DAYS",
              "MO_CONTEXT_V2_MIN_SEVERITY", "MO_TARGET_CWD")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in _CLEAN_ENV:
        monkeypatch.delenv(var, raising=False)
    yield


# ── hermetic builders ────────────────────────────────────────────────────────

def _git(repo, *args, check=True):
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "LF R11b")
    env.setdefault("GIT_AUTHOR_EMAIL", "r11b@test.invalid")
    env.setdefault("GIT_COMMITTER_NAME", "LF R11b")
    env.setdefault("GIT_COMMITTER_EMAIL", "r11b@test.invalid")
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false",
         *[str(a) for a in args]],
        capture_output=True, text=True, check=check, env=env,
    )


def _write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _raw_patch_id(diff_text: str) -> str:
    out = subprocess.run(["git", "patch-id", "--stable"], input=diff_text,
                         capture_output=True, text=True)
    tokens = out.stdout.split()
    return tokens[0] if tokens else ""


def _create_db(path) -> str:
    con = sqlite3.connect(str(path))
    con.close()
    return str(path)


def _open(path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def _insert_finding(con, *, run_id, issue, file="src/app.py", snippet=None,
                    ts=None, target_repo=None, severity="high",
                    category="correctness", line=10, source="review:spec"):
    con.execute(
        "INSERT INTO code_findings (fingerprint, run_id, source, file, line, "
        "severity, category, issue, snippet, verdict, ts, target_repo) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"fp::{run_id}::{issue}", run_id, source, file, line, severity,
         category, issue, snippet, None,
         int(ts if ts is not None else time.time()), target_repo),
    )


def _insert_landing(con, run_id, patch_id, ref="origin/main", commit=None):
    con.execute(
        "INSERT OR REPLACE INTO code_findings_landing "
        "(run_id, patch_id, checked_ref, landed_commit, checked_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, patch_id, ref, commit, int(time.time())),
    )


def _week_old() -> int:
    return int(time.time()) - 7 * 86400


@pytest.fixture
def world(tmp_path):
    """Two-commit checkout with a genuine ``origin/main``; two run dirs.

    ``run-landed-1``'s ``framework-edit.diff`` is the staged diff of the fix
    commit, so its patch-id is on ``origin/main``. ``run-plain-2``'s diff was
    never committed.
    """
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)],
                   check=True, capture_output=True)
    _git(work, "config", "user.email", "r11b@test.invalid")
    _git(work, "config", "user.name", "LF R11b")

    _write(work / "a.txt", "base\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-q", "-m", "base")
    sha_base = _git(work, "rev-parse", "HEAD").stdout.strip()

    home = tmp_path / "home"
    run_landed = home / "runs" / "run-landed-1"
    run_plain = home / "runs" / "run-plain-2"

    _write(work / "a.txt", "base\nCHANGED BY THE FIX\n")
    _git(work, "add", "a.txt")
    staged = _git(work, "diff", "--cached").stdout
    _write(run_landed / "framework-edit.diff", staged)
    _git(work, "commit", "-q", "-m", "fix")
    sha_fix = _git(work, "rev-parse", "HEAD").stdout.strip()
    # A real ``origin/main`` without a remote or a push.
    _git(work, "update-ref", "refs/remotes/origin/main", sha_fix)

    _write(work / "c.txt", "an unrelated change\n")
    _git(work, "add", "c.txt")
    _write(run_plain / "framework-edit.diff",
           _git(work, "diff", "--cached").stdout)
    _git(work, "reset", "-q")

    for run_dir in (run_landed, run_plain):
        _write(run_dir / "run_profile.json",
               json.dumps({"roots": {"exec_cwd": str(work)}}))

    return SimpleNamespace(
        work=work, home=home, run_landed=run_landed, run_plain=run_plain,
        sha_base=sha_base, sha_fix=sha_fix,
        ident=cf.repo_identity(str(work)), staged_diff=staged,
    )


def _findings_db(world, tmp_path, *, with_landing=True):
    db = _create_db(tmp_path / "state.db")
    cf.ensure_schema(db)
    con = _open(db)
    _insert_finding(con, run_id="run-landed-1", issue="LNDX landed finding",
                    ts=_week_old(), target_repo=world.ident)
    _insert_finding(con, run_id="run-plain-2", issue="LNDX plain finding",
                    ts=_week_old(), target_repo=world.ident)
    if with_landing:
        _insert_landing(con, "run-landed-1", "pid-landed", commit=world.sha_fix)
        _insert_landing(con, "run-plain-2", "pid-plain", commit=None)
    con.commit()
    con.close()
    return db


# ── run_patch_id ─────────────────────────────────────────────────────────────

def test_run_patch_id_roundtrips_into_landed_patch_ids(world):
    pid = cf.run_patch_id(str(world.run_landed))
    assert isinstance(pid, str) and pid
    assert pid == _raw_patch_id(world.staged_diff)
    landed = cf.landed_patch_ids(str(world.work))
    assert pid in landed
    assert landed[pid] == world.sha_fix


def test_run_patch_id_differs_for_a_different_diff(world):
    landed_pid = cf.run_patch_id(str(world.run_landed))
    plain_pid = cf.run_patch_id(str(world.run_plain))
    assert landed_pid and plain_pid
    assert plain_pid != landed_pid
    assert plain_pid not in cf.landed_patch_ids(str(world.work))


def test_run_patch_id_missing_or_empty_diff_yields_none(tmp_path):
    empty = tmp_path / "runs" / "run-empty"
    _write(empty / "framework-edit.diff", "")
    nodiff = tmp_path / "runs" / "run-nodiff"
    nodiff.mkdir(parents=True)
    assert cf.run_patch_id(str(empty)) is None
    assert cf.run_patch_id(str(nodiff)) is None
    assert cf.run_patch_id(str(tmp_path / "runs" / "never")) is None


def test_run_patch_id_falls_back_to_a_diff_file(tmp_path, world):
    run_dir = tmp_path / "run"
    _write(run_dir / "other.diff", world.staged_diff)
    # No framework-edit.diff: the first *.diff (sorted) is used.
    assert cf.run_patch_id(str(run_dir)) == _raw_patch_id(world.staged_diff)


# ── landed_patch_ids ─────────────────────────────────────────────────────────

def test_landed_patch_ids_returns_both_commits_of_history(world):
    landed = cf.landed_patch_ids(str(world.work))
    values = set(landed.values())
    assert world.sha_base in values and world.sha_fix in values
    assert len(landed) == 2


def test_landed_patch_ids_empty_for_non_git_and_unknown_ref(world, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    _write(plain / "not-a-repo.txt", "nope")
    assert cf.landed_patch_ids(str(plain)) == {}
    assert cf.landed_patch_ids(str(tmp_path / "no-such-dir")) == {}
    assert cf.landed_patch_ids(str(world.work), "no-such-ref-lf-r11b") == {}


def test_landed_patch_ids_since_bounds_the_walk(world):
    # The fixture commits are "now", so a since far in the future sees nothing.
    future = int(time.time()) + 10 * 86400
    assert cf.landed_patch_ids(str(world.work), since=future) == {}


# ── landing_enabled ──────────────────────────────────────────────────────────

def test_landing_enabled_default_on_and_gated(monkeypatch):
    assert cf.landing_enabled()
    for off in ("0", "false", "no", "off", "FALSE"):
        monkeypatch.setenv("MO_FINDINGS_LANDING", off)
        assert not cf.landing_enabled(), off
    monkeypatch.setenv("MO_FINDINGS_LANDING", "1")
    assert cf.landing_enabled()


# ── annotate_landing ─────────────────────────────────────────────────────────

def test_annotate_landing_marks_landed_and_unknown(world, tmp_path):
    db = _findings_db(world, tmp_path)
    rows = [
        {"run_id": "run-landed-1", "issue": "a"},
        {"run_id": "run-plain-2", "issue": "b"},
        {"run_id": "run-never-checked", "issue": "c"},
    ]
    out = cf.annotate_landing(rows, db=db)
    by_run = {r["run_id"]: r for r in out}
    assert by_run["run-landed-1"]["landed"] is True
    assert by_run["run-landed-1"]["landed_commit"] == world.sha_fix
    assert by_run["run-plain-2"]["landed"] is False
    assert by_run["run-never-checked"]["landed"] is False
    # Additive: the caller's keys survive.
    assert by_run["run-landed-1"]["issue"] == "a"


def test_annotate_landing_fails_open_without_table(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        "CREATE TABLE code_findings (id INTEGER PRIMARY KEY, run_id TEXT, "
        "issue TEXT);")
    con.execute("INSERT INTO code_findings (run_id, issue) VALUES ('r1', 'x')")
    con.commit()
    con.close()
    out = cf.annotate_landing([{"run_id": "r1"}, {"run_id": "r2"}], db=str(db))
    assert [r["landed"] for r in out] == [False, False]


# ── check_landing ────────────────────────────────────────────────────────────

def _landing_row(db, run_id):
    con = _open(db)
    try:
        row = con.execute(
            "SELECT patch_id, checked_ref, landed_commit "
            "FROM code_findings_landing WHERE run_id = ?", (run_id,)).fetchone()
        return (dict(row) if row is not None else None)
    finally:
        con.close()


def test_check_landing_records_and_is_idempotent(world, tmp_path):
    db = _create_db(tmp_path / "state.db")
    cf.ensure_schema(db)
    con = _open(db)
    _insert_finding(con, run_id="run-landed-1", issue="CKL landed",
                    ts=_week_old(), target_repo=world.ident)
    _insert_finding(con, run_id="run-plain-2", issue="CKL plain",
                    ts=_week_old(), target_repo=world.ident)
    con.commit()
    con.close()

    first = cf.check_landing(str(world.home), db=db)
    assert first["runs_checked"] == 2
    assert first["landed"] == 1
    assert first["reason"] == "ok"

    landed = _landing_row(db, "run-landed-1")
    assert landed is not None
    assert landed["landed_commit"] == world.sha_fix
    assert landed["checked_ref"] == "origin/main"
    assert landed["patch_id"] == cf.run_patch_id(str(world.run_landed))

    plain = _landing_row(db, "run-plain-2")
    assert plain is not None and plain["landed_commit"] is None

    second = cf.check_landing(str(world.home), db=db)
    assert second["runs_checked"] == 0

    # Hiding is a read-path decision: the findings rows survive.
    con = _open(db)
    try:
        n = con.execute("SELECT COUNT(*) FROM code_findings").fetchone()[0]
    finally:
        con.close()
    assert n == 2


def test_check_landing_fails_open_without_a_diff(world, tmp_path):
    db = _create_db(tmp_path / "state.db")
    cf.ensure_schema(db)
    con = _open(db)
    _insert_finding(con, run_id="run-landed-1", issue="CKL landed",
                    ts=_week_old(), target_repo=world.ident)
    con.commit()
    con.close()
    (world.run_landed / "framework-edit.diff").unlink()

    result = cf.check_landing(str(world.home), db=db)
    assert isinstance(result, dict)
    row = _landing_row(db, "run-landed-1")
    assert row is None or row["landed_commit"] is None


def test_check_landing_missing_home_or_runs_dir(tmp_path):
    assert cf.check_landing("")["reason"] == "no_home"
    assert cf.check_landing(str(tmp_path / "nohome"))["reason"] == "no_runs_dir"


def test_check_landing_cli_runs_the_real_check(world, tmp_path):
    db = _create_db(tmp_path / "state.db")
    cf.ensure_schema(db)
    con = _open(db)
    _insert_finding(con, run_id="run-landed-1", issue="CKL landed",
                    ts=_week_old(), target_repo=world.ident)
    con.commit()
    con.close()

    rc = cf.main(["check-landing", "--db", db, "--home", str(world.home)])
    assert rc == 0
    assert _landing_row(db, "run-landed-1")["landed_commit"] == world.sha_fix

    with pytest.raises(SystemExit) as excinfo:
        cf.main(["--help"])
    assert excinfo.value.code == 0
    assert cf.main(["check-landing", "--json", "--db", db,
                    "--home", str(world.home)]) == 0


# ── context_v2 read path ─────────────────────────────────────────────────────

def _scope_db(world, tmp_path):
    return _findings_db(world, tmp_path)


def test_scope_findings_drops_landed_keeps_plain(world, tmp_path, monkeypatch):
    monkeypatch.setenv("MO_CONTEXT_V2_MAX_AGE_DAYS", "30")
    db = _scope_db(world, tmp_path)
    fresh, stats = cv.scope_findings_with_stats(
        ["src/app.py"], db=db, target_repo=world.ident)
    run_ids = {r["run_id"] for r in fresh}
    assert "run-plain-2" in run_ids
    assert "run-landed-1" not in run_ids
    # Separate counts: stale stays 0, landed reports the one dropped row.
    assert stats["n_findings_scanned"] == 2
    assert stats["n_findings_stale"] == 0
    assert stats["n_landed"] == 1
    con = _open(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM code_findings").fetchone()[0] == 2
    finally:
        con.close()


def test_scope_findings_flag_off_restores_landed(world, tmp_path, monkeypatch):
    monkeypatch.setenv("MO_CONTEXT_V2_MAX_AGE_DAYS", "30")
    monkeypatch.setenv("MO_FINDINGS_LANDING", "0")
    db = _scope_db(world, tmp_path)
    fresh, stats = cv.scope_findings_with_stats(
        ["src/app.py"], db=db, target_repo=world.ident)
    assert {r["run_id"] for r in fresh} == {"run-landed-1", "run-plain-2"}
    assert stats["n_landed"] == 0
    assert {r["run_id"] for r in cv.scope_findings(
        ["src/app.py"], db=db, target_repo=world.ident)} == {
            "run-landed-1", "run-plain-2"}


def test_scope_findings_without_landing_table_fails_open(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        "CREATE TABLE code_findings (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "fingerprint TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL, source TEXT "
        "NOT NULL, file TEXT, line INTEGER, severity TEXT NOT NULL, category "
        "TEXT NOT NULL, issue TEXT NOT NULL, snippet TEXT, verdict TEXT, ts "
        "INTEGER NOT NULL, target_repo TEXT);")
    con.execute(
        "INSERT INTO code_findings (fingerprint, run_id, source, file, severity,"
        " category, issue, ts) VALUES ('fp1', 'r1', 'review:spec', "
        "'src/app.py', 'high', 'correctness', 'NOTAB finding', ?)",
        (_week_old(),))
    con.commit()
    con.close()
    fresh, stats = cv.scope_findings_with_stats(["src/app.py"], db=str(db))
    assert [r["run_id"] for r in fresh] == ["r1"]
    assert stats.get("n_landed", 0) == 0


def test_pack_reports_n_findings_landed(world, tmp_path, monkeypatch):
    monkeypatch.setenv("MO_CONTEXT_V2_MAX_AGE_DAYS", "30")
    db = _scope_db(world, tmp_path)
    pack = cv.build(str(tmp_path / "kick.md"), kickoff_text=KICKOFF_TEXT,
                    db=db, run_id="run-current", target_repo=world.ident)
    assert pack["n_findings_landed"] == 1
    assert pack["n_findings_stale"] == 0
    assert pack["n_findings_scanned"] == 2


# ── the "Your code" tab ─────────────────────────────────────────────────────

def _tab_db(tmp_path, *, landing_for_landed=True):
    db = _create_db(tmp_path / "state.db")
    cf.ensure_schema(db)
    con = _open(db)
    _insert_finding(con, run_id="run-landed-1", issue="TABX LANDING-HIDDEN row",
                    file=TAB_FILE, snippet=None, ts=_week_old(),
                    target_repo=None)
    _insert_finding(con, run_id="run-plain-2", issue="TABX VISIBLE row",
                    file=TAB_FILE, snippet=None, ts=_week_old(),
                    target_repo=None)
    _insert_finding(con, run_id="run-stale-3", issue="TABX STALE-HIDDEN row",
                    file=TAB_FILE, snippet="THIS SNIPPET IS NOT AT HEAD 9q7z",
                    ts=_week_old(), target_repo=None)
    if landing_for_landed:
        _insert_landing(con, "run-landed-1", "0" * 40, commit="0" * 40)
    con.commit()
    con.close()
    return db


def _render_area(home, db):
    area = code_tab._group_key(TAB_FILE)
    out = code_tab._area_detail(home, {"days": "30", "area": area}, area)
    return area, json.dumps(out, default=str)


def test_tab_hides_landed_with_its_own_count_line(tmp_path):
    db = _tab_db(tmp_path)
    area, blob = _render_area(tmp_path, db)
    assert "TABX VISIBLE row" in blob
    assert "TABX LANDING-HIDDEN row" not in blob
    assert "TABX STALE-HIDDEN row" not in blob
    assert "1 finding(s) hidden as landed on origin/main" in blob
    assert "1 finding(s) hidden as stale at HEAD" in blob
    con = _open(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM code_findings").fetchone()[0] == 3
    finally:
        con.close()


def test_tab_no_landed_line_when_nothing_landed(tmp_path):
    db = _tab_db(tmp_path, landing_for_landed=False)
    _area, blob = _render_area(tmp_path, db)
    assert "hidden as landed" not in blob
    assert "TABX VISIBLE row" in blob
    assert "1 finding(s) hidden as stale at HEAD" in blob


def test_tab_flag_off_shows_the_landed_row(tmp_path, monkeypatch):
    db = _tab_db(tmp_path)
    monkeypatch.setenv("MO_FINDINGS_LANDING", "0")
    _area, blob = _render_area(tmp_path, db)
    assert "TABX LANDING-HIDDEN row" in blob
    assert "hidden as landed" not in blob


# ── the migration and its ensure_schema twin ────────────────────────────────

def test_ensure_schema_creates_the_landing_table(tmp_path):
    db = _create_db(tmp_path / "twin.db")
    cf.ensure_schema(db)
    con = sqlite3.connect(db)
    try:
        assert con.execute(LANDING_TABLE_SQL).fetchone()
        cols = {r[1] for r in con.execute(
            "PRAGMA table_info(code_findings_landing)")}
        assert {"run_id", "patch_id", "checked_ref", "landed_commit",
                "checked_at"} <= cols
    finally:
        con.close()
    cf.ensure_schema(db)  # idempotent


def test_migration_applies_on_a_fresh_db(tmp_path):
    db = _create_db(tmp_path / "fresh.db")
    err: list[str] = []
    rc, _lines = migrate.migrate_apply(str(MIGRATIONS_DIR), db=db, err_out=err)
    assert rc == 0, err
    con = sqlite3.connect(db)
    try:
        assert con.execute(LANDING_TABLE_SQL).fetchone()
        recorded = con.execute(
            "SELECT filename FROM schema_migrations WHERE filename = "
            "'0079_code_findings_landing.sql'").fetchone()
        assert recorded
    finally:
        con.close()


def test_migration_applies_after_ensure_schema(tmp_path):
    db = _create_db(tmp_path / "twice.db")
    cf.ensure_schema(db)
    err: list[str] = []
    rc, _lines = migrate.migrate_apply(str(MIGRATIONS_DIR), db=db, err_out=err)
    assert rc == 0, err
    con = sqlite3.connect(db)
    try:
        assert con.execute(LANDING_TABLE_SQL).fetchone()
        assert con.execute(
            "SELECT filename FROM schema_migrations WHERE filename = "
            "'0079_code_findings_landing.sql'").fetchone()
    finally:
        con.close()
