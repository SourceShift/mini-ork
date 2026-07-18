"""Standalone unit tests for ``mini_ork.ported.runs_tracker``.

Replaces the bash-parity gate as part of the bash->Python migration: the
Python port is now the sole implementation, so its coverage no longer runs
``lib/runs-tracker.sh`` in a subprocess -- it asserts the port's behaviour
directly against real (tmp_path-isolated) sqlite3 files. No bash, no git
subprocess for the happy paths (``_git_branch`` is exercised against a real
throwaway git repo since it shells out to ``git`` by design, but that is the
port's own implementation detail, not a bash-oracle comparison).

Coverage (>= 6 cases, matching/exceeding the retired parity gate):

  (a) sql_escape                 -- single-quote doubling; None/empty safe
  (b) resolve_claude_session_id  -- absent file, present file, malformed
                                     JSON, missing/null key, no zellij,
                                     ZELLIJ_SESSION_NAME vs ZELLIJ precedence
  (c) _db_path_or_default        -- explicit arg > MINI_ORK_DB > MINI_ORK_HOME
                                     /state.db > .mini-ork/state.db default
  (d) ensure_schema               -- creates orch_dispatches + indexes, adds
                                     runs.claude_session_id/zellij_session_name,
                                     idempotent, module-level cache short-
                                     circuits repeat connects, and the real
                                     (undocumented) failure mode when the
                                     `runs` table is missing: warns and
                                     leaves orch_dispatches uncreated
  (e) open                       -- inserts a row with the right column
                                     values/NULL branches, run_dir shape,
                                     JOB_ID -> group_id + run_dir, timestamp
                                     format, and the sqlite failure path
                                     (-1 + warning)
  (f) update_progress            -- rationale append semantics across two
                                     calls, no-op on falsy dispatch_id, and
                                     the real double-escaping quirk (verdict
                                     is pre-escaped via sql_escape() AND then
                                     bound as a `?` parameter, so a literal
                                     `'` in a verdict is stored as `''`)
  (g) close                      -- APPROVE/MERGED/SALVAGED -> completed,
                                     everything else -> cancelled, rationale
                                     `final:<verdict>`, closed_at stamped,
                                     no-op on falsy dispatch_id
  (h) _git_branch                -- real git repo -> branch name; missing
                                     dir / non-repo -> 'unknown'
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import runs_tracker as rt  # noqa: E402

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture(autouse=True)
def _clear_schema_cache():
    """Isolate the module-level `_SCHEMA_INIT_DB` guard across tests."""
    rt._SCHEMA_INIT_DB.clear()
    yield
    rt._SCHEMA_INIT_DB.clear()


def _db_with_runs_table(tmp_path: Path, name: str = "state.db") -> str:
    """Create a temp sqlite file pre-seeded with a minimal `runs` table,
    mirroring what db/init.sh would have already created in production
    before runs_tracker's DDL runs against it."""
    db_path = str(tmp_path / name)
    con = sqlite3.connect(db_path)
    try:
        con.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
        con.commit()
    finally:
        con.close()
    return db_path


def _row(db_path: str, row_id: int) -> dict:
    con = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(orch_dispatches);").fetchall()]
        row = con.execute(
            "SELECT * FROM orch_dispatches WHERE id=?;", (row_id,)
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return {}
    return dict(zip(cols, row))


# ─────────────────────────────────────────────────────────────────────────────
# (a) sql_escape
# ─────────────────────────────────────────────────────────────────────────────
class TestSqlEscape:
    def test_plain_string_unchanged(self):
        assert rt.sql_escape("no-quotes") == "no-quotes"

    def test_none_is_empty_string(self):
        assert rt.sql_escape(None) == ""

    def test_empty_string_is_empty(self):
        assert rt.sql_escape("") == ""

    def test_single_quote_doubled(self):
        assert rt.sql_escape("it's") == "it''s"

    def test_multiple_quotes_all_doubled(self):
        assert rt.sql_escape("two'quotes''in'one") == "two''quotes''''in''one"

    def test_leading_and_trailing_quotes(self):
        assert rt.sql_escape("'leading") == "''leading"
        assert rt.sql_escape("trailing'") == "trailing''"

    def test_backslash_passed_through(self):
        # No special sqlite meaning for backslash; only ' is escaped.
        assert rt.sql_escape("back\\slash") == "back\\slash"


# ─────────────────────────────────────────────────────────────────────────────
# (b) resolve_claude_session_id
# ─────────────────────────────────────────────────────────────────────────────
class TestResolveClaudeSessionId:
    def test_no_zellij_arg_no_env_returns_empty(self, monkeypatch):
        monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
        monkeypatch.delenv("ZELLIJ", raising=False)
        assert rt.resolve_claude_session_id() == ""

    def test_zellij_given_but_status_file_absent(self, tmp_path):
        assert rt.resolve_claude_session_id("zellij-x", home_dir=tmp_path) == ""

    def test_status_file_present_with_session_id(self, tmp_path):
        status_dir = tmp_path / ".claude" / "status"
        status_dir.mkdir(parents=True)
        (status_dir / "zellij-y.json").write_text(
            '{"session_id": "sess-uuid-12345"}\n', encoding="utf-8"
        )
        assert (
            rt.resolve_claude_session_id("zellij-y", home_dir=tmp_path)
            == "sess-uuid-12345"
        )

    def test_malformed_json_returns_empty(self, tmp_path):
        status_dir = tmp_path / ".claude" / "status"
        status_dir.mkdir(parents=True)
        (status_dir / "zellij-bad.json").write_text("{not valid json", encoding="utf-8")
        assert rt.resolve_claude_session_id("zellij-bad", home_dir=tmp_path) == ""

    def test_missing_session_id_key_returns_empty(self, tmp_path):
        status_dir = tmp_path / ".claude" / "status"
        status_dir.mkdir(parents=True)
        (status_dir / "zellij-nokey.json").write_text('{"other": "x"}', encoding="utf-8")
        assert rt.resolve_claude_session_id("zellij-nokey", home_dir=tmp_path) == ""

    def test_null_session_id_returns_empty(self, tmp_path):
        status_dir = tmp_path / ".claude" / "status"
        status_dir.mkdir(parents=True)
        (status_dir / "zellij-null.json").write_text(
            '{"session_id": null}', encoding="utf-8"
        )
        assert rt.resolve_claude_session_id("zellij-null", home_dir=tmp_path) == ""

    def test_env_zellij_session_name_used_when_arg_omitted(self, tmp_path, monkeypatch):
        status_dir = tmp_path / ".claude" / "status"
        status_dir.mkdir(parents=True)
        (status_dir / "from-env.json").write_text(
            '{"session_id": "env-sess"}', encoding="utf-8"
        )
        monkeypatch.setenv("ZELLIJ_SESSION_NAME", "from-env")
        monkeypatch.delenv("ZELLIJ", raising=False)
        assert rt.resolve_claude_session_id(home_dir=tmp_path) == "env-sess"

    def test_zellij_session_name_takes_precedence_over_zellij(self, tmp_path, monkeypatch):
        status_dir = tmp_path / ".claude" / "status"
        status_dir.mkdir(parents=True)
        (status_dir / "primary.json").write_text(
            '{"session_id": "primary-sess"}', encoding="utf-8"
        )
        (status_dir / "fallback.json").write_text(
            '{"session_id": "fallback-sess"}', encoding="utf-8"
        )
        monkeypatch.setenv("ZELLIJ_SESSION_NAME", "primary")
        monkeypatch.setenv("ZELLIJ", "fallback")
        assert rt.resolve_claude_session_id(home_dir=tmp_path) == "primary-sess"

    def test_zellij_env_fallback_when_session_name_unset(self, tmp_path, monkeypatch):
        status_dir = tmp_path / ".claude" / "status"
        status_dir.mkdir(parents=True)
        (status_dir / "only-zellij.json").write_text(
            '{"session_id": "only-zellij-sess"}', encoding="utf-8"
        )
        monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
        monkeypatch.setenv("ZELLIJ", "only-zellij")
        assert rt.resolve_claude_session_id(home_dir=tmp_path) == "only-zellij-sess"


# ─────────────────────────────────────────────────────────────────────────────
# (c) _db_path_or_default resolution order
# ─────────────────────────────────────────────────────────────────────────────
class TestDbPathResolution:
    def test_explicit_arg_wins_over_everything(self, monkeypatch):
        monkeypatch.setenv("MINI_ORK_DB", "/bar/explicit.db")
        monkeypatch.setenv("MINI_ORK_HOME", "/foo/home")
        assert rt._db_path_or_default("/baz/x.db") == "/baz/x.db"

    def test_mini_ork_db_env_used_when_arg_none(self, monkeypatch):
        monkeypatch.setenv("MINI_ORK_DB", "/bar/explicit.db")
        monkeypatch.delenv("MINI_ORK_HOME", raising=False)
        assert rt._db_path_or_default(None) == "/bar/explicit.db"

    def test_mini_ork_home_env_used_when_db_env_unset(self, monkeypatch):
        monkeypatch.delenv("MINI_ORK_DB", raising=False)
        monkeypatch.setenv("MINI_ORK_HOME", "/foo/home")
        assert rt._db_path_or_default(None) == "/foo/home/state.db"

    def test_default_dot_mini_ork_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("MINI_ORK_DB", raising=False)
        monkeypatch.delenv("MINI_ORK_HOME", raising=False)
        assert rt._db_path_or_default(None) == ".mini-ork/state.db"


# ─────────────────────────────────────────────────────────────────────────────
# (d) ensure_schema
# ─────────────────────────────────────────────────────────────────────────────
class TestEnsureSchema:
    def test_creates_orch_dispatches_table_and_indexes(self, tmp_path):
        db_path = _db_with_runs_table(tmp_path)
        rt.ensure_schema(db_path)
        con = sqlite3.connect(db_path)
        try:
            tables = {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table';"
                ).fetchall()
            }
            indexes = {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='index';"
                ).fetchall()
            }
        finally:
            con.close()
        assert "orch_dispatches" in tables
        assert {
            "idx_orch_dispatches_epic",
            "idx_orch_dispatches_status",
            "idx_orch_dispatches_session",
        } <= indexes

    def test_adds_claude_session_and_zellij_columns_to_runs(self, tmp_path):
        db_path = _db_with_runs_table(tmp_path)
        con = sqlite3.connect(db_path)
        before = {r[1] for r in con.execute("PRAGMA table_info(runs);").fetchall()}
        con.close()
        assert "claude_session_id" not in before

        rt.ensure_schema(db_path)

        con = sqlite3.connect(db_path)
        after = {r[1] for r in con.execute("PRAGMA table_info(runs);").fetchall()}
        con.close()
        assert {"claude_session_id", "zellij_session_name"} <= after

    def test_idempotent_after_cache_cleared_no_duplicate_column_error(self, tmp_path):
        db_path = _db_with_runs_table(tmp_path)
        rt.ensure_schema(db_path)
        rt._SCHEMA_INIT_DB.clear()  # force a real second DDL pass, not just cache short-circuit
        rt.ensure_schema(db_path)  # must not raise "duplicate column name"
        con = sqlite3.connect(db_path)
        cols = [r[1] for r in con.execute("PRAGMA table_info(runs);").fetchall()]
        con.close()
        # No duplicate columns produced by the re-run.
        assert cols.count("claude_session_id") == 1
        assert cols.count("zellij_session_name") == 1

    def test_module_cache_short_circuits_second_call(self, tmp_path, monkeypatch):
        db_path = _db_with_runs_table(tmp_path)
        rt.ensure_schema(db_path)  # populates the cache for this abspath

        calls = {"n": 0}
        real_connect = sqlite3.connect

        def counting_connect(*a, **kw):
            calls["n"] += 1
            return real_connect(*a, **kw)

        monkeypatch.setattr(sqlite3, "connect", counting_connect)
        rt.ensure_schema(db_path)  # cache hit -> should not touch sqlite3.connect at all
        assert calls["n"] == 0

    def test_missing_runs_table_warns_and_leaves_orch_dispatches_uncreated(self, tmp_path):
        """Real (documented-as-gotcha) behaviour: unlike bash -- which issues
        the ALTER TABLE and the CREATE TABLE as two separate sqlite3
        invocations so a failed ALTER doesn't block the CREATE -- the Python
        port does both in one connection/transaction, so an ALTER failure
        (no `runs` table) aborts before the orch_dispatches DDL ever runs."""
        db_path = str(tmp_path / "empty.db")
        # Touch the file into existence via a throwaway connection with no tables.
        sqlite3.connect(db_path).close()

        with pytest.warns(UserWarning, match="no such table: runs"):
            rt.ensure_schema(db_path)

        con = sqlite3.connect(db_path)
        tables = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        }
        con.close()
        assert "orch_dispatches" not in tables


# ─────────────────────────────────────────────────────────────────────────────
# (e) open
# ─────────────────────────────────────────────────────────────────────────────
class TestOpen:
    def test_inserts_row_with_expected_defaults(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JOB_ID", raising=False)
        monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
        monkeypatch.delenv("ZELLIJ", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))  # no .claude/status -> claude_sid ''

        db_path = _db_with_runs_table(tmp_path)
        dispatch_id = rt.open(db_path, "epic-open", str(tmp_path))

        assert dispatch_id > 0
        row = _row(db_path, dispatch_id)
        assert row["epic_id"] == "epic-open"
        assert row["group_id"] is None
        assert row["dispatched_by"] == "claude-session"
        assert row["claude_session_id"] is None
        assert row["zellij_session_name"] is None
        assert row["status"] == "in_progress"
        assert row["rationale"] is None
        assert row["closed_at"] is None
        assert row["run_dir"].startswith("mini-ork/unknown/epic-open/")
        assert TS_RE.match(row["created_at"])
        assert TS_RE.match(row["updated_at"])

    def test_job_id_populates_group_id_and_run_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JOB_ID", "job-77")
        monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
        monkeypatch.delenv("ZELLIJ", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

        db_path = _db_with_runs_table(tmp_path)
        dispatch_id = rt.open(db_path, "epic-job", str(tmp_path))
        row = _row(db_path, dispatch_id)
        assert row["group_id"] == "job-77"
        assert row["run_dir"].startswith("mini-ork/job-77/epic-job/")

    def test_zellij_session_name_populates_column(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JOB_ID", raising=False)
        monkeypatch.setenv("ZELLIJ_SESSION_NAME", "zj-1")
        monkeypatch.setenv("HOME", str(tmp_path))

        db_path = _db_with_runs_table(tmp_path)
        dispatch_id = rt.open(db_path, "epic-zj", str(tmp_path))
        row = _row(db_path, dispatch_id)
        assert row["zellij_session_name"] == "zj-1"

    def test_epic_with_single_quote_round_trips_via_sql_literal_escaping(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("JOB_ID", raising=False)
        monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
        monkeypatch.delenv("ZELLIJ", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

        db_path = _db_with_runs_table(tmp_path)
        dispatch_id = rt.open(db_path, "epic's-name", str(tmp_path))
        row = _row(db_path, dispatch_id)
        # open() builds raw SQL text via sql_escape + literal quoting (like
        # bash), so this round-trips to the exact original string -- unlike
        # update_progress/close (see TestUpdateProgress double-escape test).
        assert row["epic_id"] == "epic's-name"

    def test_sequential_opens_return_increasing_ids(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        db_path = _db_with_runs_table(tmp_path)
        id1 = rt.open(db_path, "epic-a", str(tmp_path))
        id2 = rt.open(db_path, "epic-b", str(tmp_path))
        assert id2 > id1 > 0

    def test_sqlite_failure_returns_negative_one_and_warns(self):
        with pytest.warns(UserWarning):
            result = rt.open("/nonexistent-dir-xyz-mini-ork/state.db", "epic-y", "/nowhere")
        assert result == -1


# ─────────────────────────────────────────────────────────────────────────────
# (f) update_progress
# ─────────────────────────────────────────────────────────────────────────────
class TestUpdateProgress:
    def test_noop_on_falsy_dispatch_id(self, tmp_path):
        db_path = _db_with_runs_table(tmp_path)
        # Must not raise even though there is no orch_dispatches row/table yet.
        rt.update_progress(db_path, 0, "FAIL")
        rt.update_progress(db_path, None, "FAIL")  # type: ignore[arg-type]  # deliberate: falsy-guard no-op

    def test_two_calls_append_with_pipe_separator(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        db_path = _db_with_runs_table(tmp_path)
        dispatch_id = rt.open(db_path, "epic-up", str(tmp_path))

        rt.update_progress(db_path, dispatch_id, "FAIL")
        rt.update_progress(db_path, dispatch_id, "WARN")

        row = _row(db_path, dispatch_id)
        assert row["rationale"] == "iter:FAIL | iter:WARN"
        assert TS_RE.match(row["updated_at"])

    def test_first_call_has_no_leading_separator(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        db_path = _db_with_runs_table(tmp_path)
        dispatch_id = rt.open(db_path, "epic-up2", str(tmp_path))
        rt.update_progress(db_path, dispatch_id, "OK")
        row = _row(db_path, dispatch_id)
        assert row["rationale"] == "iter:OK"

    def test_single_quote_in_verdict_is_double_escaped_due_to_param_binding(
        self, tmp_path, monkeypatch
    ):
        """Real (documented-as-gotcha) behaviour: update_progress pre-escapes
        the verdict with sql_escape() (doubling `'` -> `''`) and THEN binds
        it as a `?` parameter. Parameter binding needs no escaping, so the
        doubled quote is stored literally -- unlike open(), which builds raw
        SQL text and therefore round-trips a single `'` correctly."""
        monkeypatch.setenv("HOME", str(tmp_path))
        db_path = _db_with_runs_table(tmp_path)
        dispatch_id = rt.open(db_path, "epic-quote", str(tmp_path))
        rt.update_progress(db_path, dispatch_id, "it's a test")
        row = _row(db_path, dispatch_id)
        assert row["rationale"] == "iter:it''s a test"


# ─────────────────────────────────────────────────────────────────────────────
# (g) close
# ─────────────────────────────────────────────────────────────────────────────
class TestClose:
    def test_noop_on_falsy_dispatch_id(self, tmp_path):
        db_path = _db_with_runs_table(tmp_path)
        rt.close(db_path, 0, "epic", "APPROVE")
        rt.close(db_path, None, "epic", "APPROVE")  # type: ignore[arg-type]  # deliberate: falsy-guard no-op

    @pytest.mark.parametrize("verdict", ["APPROVE", "MERGED", "SALVAGED"])
    def test_success_verdicts_map_to_completed(self, tmp_path, monkeypatch, verdict):
        monkeypatch.setenv("HOME", str(tmp_path))
        db_path = _db_with_runs_table(tmp_path)
        dispatch_id = rt.open(db_path, f"epic-{verdict}", str(tmp_path))
        rt.close(db_path, dispatch_id, f"epic-{verdict}", verdict)
        row = _row(db_path, dispatch_id)
        assert row["status"] == "completed"
        assert row["rationale"] == f"final:{verdict}"
        assert TS_RE.match(row["closed_at"])

    @pytest.mark.parametrize("verdict", ["FAIL", "REJECT", "", "WEIRD"])
    def test_other_verdicts_map_to_cancelled(self, tmp_path, monkeypatch, verdict):
        monkeypatch.setenv("HOME", str(tmp_path))
        db_path = _db_with_runs_table(tmp_path)
        dispatch_id = rt.open(db_path, "epic-cancel", str(tmp_path))
        rt.close(db_path, dispatch_id, "epic-cancel", verdict)
        row = _row(db_path, dispatch_id)
        assert row["status"] == "cancelled"
        assert row["rationale"] == f"final:{verdict}"

    def test_epic_argument_is_unused_by_the_update(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        db_path = _db_with_runs_table(tmp_path)
        dispatch_id = rt.open(db_path, "epic-real", str(tmp_path))
        # Passing a totally different `epic` string must not affect the row
        # (bash's mo_runs_close ignores it too; it's kept only for signature
        # parity).
        rt.close(db_path, dispatch_id, "some-other-epic-entirely", "APPROVE")
        row = _row(db_path, dispatch_id)
        assert row["epic_id"] == "epic-real"
        assert row["status"] == "completed"

    def test_update_progress_then_close_appends_final_after_iters(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        db_path = _db_with_runs_table(tmp_path)
        dispatch_id = rt.open(db_path, "epic-combo", str(tmp_path))
        rt.update_progress(db_path, dispatch_id, "FAIL")
        rt.update_progress(db_path, dispatch_id, "WARN")
        rt.close(db_path, dispatch_id, "epic-combo", "APPROVE")
        row = _row(db_path, dispatch_id)
        assert row["rationale"] == "iter:FAIL | iter:WARN | final:APPROVE"
        assert row["status"] == "completed"


# ─────────────────────────────────────────────────────────────────────────────
# (h) _git_branch
# ─────────────────────────────────────────────────────────────────────────────
class TestGitBranch:
    def test_nonexistent_directory_returns_unknown(self):
        assert rt._git_branch("/nonexistent-dir-xyz-mini-ork") == "unknown"

    def test_non_repo_directory_returns_unknown(self, tmp_path):
        assert rt._git_branch(str(tmp_path)) == "unknown"

    def test_real_repo_returns_branch_name(self, tmp_path):
        if not shutil.which("git"):
            pytest.skip("git not on PATH")
        repo = tmp_path / "wt"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        (repo / "f").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "f"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feat-x"], check=True)
        assert rt._git_branch(str(repo)) == "feat-x"
