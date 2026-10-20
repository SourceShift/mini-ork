"""Post-run reflect is scoped to its own run, detached, and bounded (RB-20/RB-32).

Hermetic by construction: every database is a ``tmp_path`` sqlite file, the
gradient extractor/store are stubbed in-process (no LLM, no network, no
provider credentials), every reflect side channel with an env opt-out is opted
out, and the lifecycle runs in dry-run mode with a scrubbed environment so
neither the operator's live ``state.db`` nor an enclosing run's identity is
read or written (audit RB-04 / RB-27).

Covers the kickoff's four bullets:

1. ``reflect --run-id A`` analyses only A's traces, while the same window
   without the flag still analyses both runs.
2. The post-run tail is spawned with ``start_new_session=True`` and
   ``--run-id`` (``--since`` kept), the lifecycle never waits on it, and the
   ``reflect.log`` header is on disk before the child returns.
3. The bound does not ratchet past 3600 s from a historical pass, and an
   explicit ``MO_REFLECT_TIMEOUT_S`` pin still wins.
4. Two ``lesson_themes`` writes with the same deterministic ``theme_id`` do
   not raise.
"""
from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import time
from contextlib import contextmanager

from mini_ork.learning import reflection_pipeline as _rp

_TRACE_DDL = """
CREATE TABLE IF NOT EXISTS execution_traces (
  trace_id     TEXT PRIMARY KEY,
  run_id       TEXT,
  task_class   TEXT,
  status       TEXT,
  created_at   TEXT,
  duration_ms  INTEGER
)
"""


def _iso(seconds_ago: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - seconds_ago)) + "Z"


@contextmanager
def _scrubbed_env(**overrides):
    """Snapshot the environment, drop every mini-ork key, apply overrides.

    The test process may itself live inside a mini-ork run; a leaked
    ``MINI_ORK_DB``/``MINI_ORK_HOME`` would send writes at the operator's
    live state. Restored in full on exit — reflect and the lifecycle both
    write ``MINI_ORK_*`` keys as a side effect.
    """
    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith(("MINI_ORK_", "MO_")):
            del os.environ[key]
    os.environ.update(overrides)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _seed_two_runs(db_path) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(_TRACE_DDL)
        con.executemany(
            "INSERT INTO execution_traces"
            " (trace_id, run_id, task_class, status, created_at, duration_ms)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("trace-A", "run-A", "code-fix", "success", _iso(30), 1000),
                ("trace-B", "run-B", "code-fix", "success", _iso(30), 1000),
            ],
        )
        con.commit()
    finally:
        con.close()


def _install_recording_extractor() -> list:
    """Stub the pipeline; the seen trace ids are the traces reflect analysed."""
    seen: list = []
    _rp.set_gradient_extract(lambda tid: seen.append(tid) or [])
    _rp.set_gradient_store(lambda _gradient: None)
    _rp.set_gradient_ensure_table(lambda: None)
    return seen


def _reflect_env(db_path, home_path):
    return _scrubbed_env(
        MINI_ORK_DB=str(db_path),
        MINI_ORK_HOME=str(home_path),
        MO_CODE_FINDINGS="0",
        MO_PATTERN_MINER="0",
        MO_THEMES="0",
        MO_PATTERN_INDUCE="0",
        MO_CROSS_EPIC_GRADIENTS="0",
        MO_BUG_REPORT_SWEEP="0",
        MO_RHO_AGGREGATE="0",
        MO_LANE_ROUTER="0",
        MO_REFLECTION_BATCH="25",
    )


# ── 1. --run-id restricts the analysed traces ───────────────────────────────


def test_run_id_restricts_reflect_to_that_runs_traces(tmp_path):
    db = tmp_path / "state.db"
    home = tmp_path / "home"
    home.mkdir()
    _seed_two_runs(db)
    seen = _install_recording_extractor()

    from mini_ork.cli import reflect

    since = str(int(time.time()) - 300)
    with _reflect_env(db, home):
        rc = reflect.main(["--since", since, "--run-id", "run-A"])

    assert rc == 0
    assert "trace-A" in seen, f"run-A's own trace was not analysed (seen={seen})"
    assert "trace-B" not in seen, (
        f"a foreign run's trace was analysed under --run-id run-A (seen={seen})")


def test_without_run_id_reflect_analyses_the_whole_window(tmp_path):
    db = tmp_path / "state.db"
    home = tmp_path / "home"
    home.mkdir()
    _seed_two_runs(db)
    seen = _install_recording_extractor()

    from mini_ork.cli import reflect

    since = str(int(time.time()) - 300)
    with _reflect_env(db, home):
        rc = reflect.main(["--since", since])

    assert rc == 0
    assert set(seen) == {"trace-A", "trace-B"}, (
        "the restriction belongs to --run-id, not to the window")


def test_run_id_reflect_reconciles_that_runs_cost(tmp_path):
    """The detached child's last step lifts task_runs.cost_usd for its run."""
    db = tmp_path / "state.db"
    home = tmp_path / "home"
    home.mkdir()
    con = sqlite3.connect(db)
    try:
        con.execute(_TRACE_DDL)
        con.execute(
            "CREATE TABLE task_runs (id TEXT PRIMARY KEY, cost_usd REAL,"
            " updated_at INTEGER)")
        con.execute(
            "CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, run_id TEXT,"
            " cost_usd REAL)")
        con.executemany(
            "INSERT INTO task_runs (id, cost_usd) VALUES (?, ?)",
            [("run-A", 0.05), ("run-B", 0.01)])
        con.executemany(
            "INSERT INTO llm_calls (run_id, cost_usd) VALUES (?, ?)",
            [("run-A", 0.25), ("run-A", 0.17), ("run-B", 0.99)])
        con.commit()
    finally:
        con.close()
    _install_recording_extractor()

    from mini_ork.cli import reflect

    since = str(int(time.time()) - 300)
    with _reflect_env(db, home):
        assert reflect.main(["--since", since, "--run-id", "run-A"]) == 0

    con = sqlite3.connect(db)
    try:
        cost_a = con.execute(
            "SELECT cost_usd FROM task_runs WHERE id='run-A'").fetchone()[0]
        cost_b = con.execute(
            "SELECT cost_usd FROM task_runs WHERE id='run-B'").fetchone()[0]
    finally:
        con.close()
    assert abs(cost_a - 0.42) < 1e-9, (
        f"run-A cost_usd is {cost_a!r}, expected the ledger total 0.42")
    assert abs(cost_b - 0.01) < 1e-9, (
        f"run-B cost_usd changed to {cost_b!r}; the reconcile is run-scoped")


# ── 2. the post-run spawn ────────────────────────────────────────────────────

_KICKOFF = """# Spec-test kickoff

## Goal

Exercise the post-run reflect tail of a dry run.

## Success criteria

- The run completes.
"""

_REFLECT_MODULE = "mini_ork.cli.reflect"


class _NeverExitingChild:
    """Stand-in for the detached reflect child; records every blocking call."""

    def __init__(self, argv, kwargs):
        self.argv = [str(a) for a in argv]
        self.args = self.argv  # subprocess.run builds CompletedProcess off it
        self.kwargs = dict(kwargs)
        self.pid = os.getpid()
        self.returncode = 0
        self.waits: list = []
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")

    def poll(self, *_a, **_k):
        return 0

    def wait(self, *_a, **_k):
        self.waits.append("wait")
        return 0

    def communicate(self, *_a, **_k):
        self.waits.append("communicate")
        return ("", "")

    def kill(self, *_a, **_k):
        self.waits.append("kill")

    terminate = kill

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@contextmanager
def _spy_popen():
    """Fake only the reflect child; every other spawn passes through."""
    fakes: list = []
    original = subprocess.Popen

    def spy(args=None, *rest, **kwargs):
        if isinstance(args, (list, tuple)) and any(
                _REFLECT_MODULE in str(part) for part in args):
            child = _NeverExitingChild(args, kwargs)
            fakes.append(child)
            return child
        return original(args, *rest, **kwargs)

    subprocess.Popen = spy
    try:
        yield fakes
    finally:
        subprocess.Popen = original


def _reflect_logs(home):
    logs = []
    runs_dir = home / "runs"
    if runs_dir.is_dir():
        for run_dir in runs_dir.iterdir():
            if run_dir.is_dir():
                logs.extend(run_dir.glob("reflect*.log"))
    return logs


def test_post_run_reflect_is_detached_and_run_scoped(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text(_KICKOFF, encoding="utf-8")
    from mini_ork.cli import main as mo_main

    with _scrubbed_env(
        MINI_ORK_DRY_RUN="1",
        MINI_ORK_HOME=str(home),
        MINI_ORK_DB=str(home / "state.db"),
        MO_AUTO_REPAIR="0",
        MO_RUBRIC="0",
    ), _spy_popen() as fakes:
        rc = mo_main.main(["run", "code-fix", str(kickoff)])

    assert rc == 0, f"dry-run lifecycle failed (rc={rc})"
    assert fakes, "the post-run reflect child was not spawned through Popen"
    child = fakes[0]
    argv = child.argv
    assert "--run-id" in argv, f"spawn argv carries no --run-id: {argv}"
    run_id = argv[argv.index("--run-id") + 1]
    assert run_id
    run_dirs = [p.name for p in (home / "runs").iterdir() if p.is_dir()]
    assert run_id in run_dirs, (
        f"--run-id {run_id!r} is not this run's own id (run dirs: {run_dirs})")
    assert "--since" in argv, f"--since must be kept: {argv}"
    assert child.kwargs.get("start_new_session") is True, (
        "the post-run reflect must be detached (start_new_session=True)")
    stdout_kw = child.kwargs.get("stdout")
    assert stdout_kw is not None and stdout_kw is not subprocess.PIPE, (
        "the child's stdout must stream to reflect.log, not a pipe")
    assert not child.waits, (
        f"the lifecycle waited on the detached reflect child: {child.waits}")

    logs = _reflect_logs(home)
    assert logs, "no reflect*.log was written under the run dir"
    content = logs[0].read_text(encoding="utf-8", errors="replace")
    assert content.strip(), "reflect.log is empty — header must precede the spawn"
    assert "reflect" in content.lower()


def test_inline_reflect_mode_still_runs(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text(_KICKOFF, encoding="utf-8")
    from mini_ork.cli import main as mo_main

    with _scrubbed_env(
        MINI_ORK_DRY_RUN="1",
        MINI_ORK_HOME=str(home),
        MINI_ORK_DB=str(home / "state.db"),
        MO_AUTO_REPAIR="0",
        MO_RUBRIC="0",
        MO_REFLECT_INLINE="1",
    ), _spy_popen():
        rc = mo_main.main(["run", "code-fix", str(kickoff)])

    assert rc == 0
    logs = _reflect_logs(home)
    assert logs and logs[0].read_text(encoding="utf-8", errors="replace").strip()


# ── 3. the bound ────────────────────────────────────────────────────────────


_TIMEOUT_ENV_KEYS = (
    "MO_REFLECT_TIMEOUT_S",
    "MO_REFLECT_TIMEOUT_SECONDS",
    "MO_REFLECT_TIMEOUT_MAX_SECONDS",
)


def _seed_reflect_pass(db_path, seconds: int, *, post_run: bool) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(_TRACE_DDL)
        con.execute("ALTER TABLE execution_traces ADD COLUMN verifier_output TEXT")
        marker = '{"caller":"post_run"}' if post_run else "{}"
        con.execute(
            "INSERT INTO execution_traces"
            " (trace_id, run_id, task_class, status, created_at, duration_ms,"
            "  verifier_output)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "tr-reflect-historical",
                "run-history",
                "__reflect__",
                "success",
                _iso(60),
                seconds * 1000,
                marker,
            ),
        )
        con.commit()
    finally:
        con.close()


def test_9000s_pass_does_not_push_the_bound_past_3600s(tmp_path, monkeypatch):
    for key in _TIMEOUT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    db = tmp_path / "state.db"
    _seed_reflect_pass(db, 9000, post_run=True)

    from mini_ork.cli.main import _reflect_timeout

    bound, _why = _reflect_timeout(str(db))
    assert isinstance(bound, int) and bound >= 1
    assert bound <= 3600, (
        f"a 9000 s historical pass pushed the bound to {bound}s; "
        "the post-run bound is capped at 3600 s")


def test_in_dag_pass_is_not_part_of_the_history(tmp_path, monkeypatch):
    """An unmarked (in-DAG) 9000 s pass must not size the post-run bound."""
    for key in _TIMEOUT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    db = tmp_path / "state.db"
    _seed_reflect_pass(db, 9000, post_run=False)

    from mini_ork.cli.main import _REFLECT_TIMEOUT_FALLBACK, _reflect_timeout

    bound, _why = _reflect_timeout(str(db))
    assert bound == _REFLECT_TIMEOUT_FALLBACK


def test_pinned_timeout_env_beats_the_derived_bound(tmp_path, monkeypatch):
    for key in _TIMEOUT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    db = tmp_path / "state.db"
    _seed_reflect_pass(db, 9000, post_run=True)
    monkeypatch.setenv("MO_REFLECT_TIMEOUT_S", "7")
    monkeypatch.setenv("MO_REFLECT_TIMEOUT_SECONDS", "7")

    from mini_ork.cli.main import _reflect_timeout

    bound, _why = _reflect_timeout(str(db))
    assert bound == 7


# ── 4. the lesson_themes upsert ─────────────────────────────────────────────

_GRADIENT_DDL = """
CREATE TABLE IF NOT EXISTS gradient_records (
  gradient_id       TEXT PRIMARY KEY,
  target            TEXT,
  signal            TEXT,
  suggested_change  TEXT,
  evidence          TEXT,
  confidence        REAL,
  created_at        INTEGER,
  task_class        TEXT
)
"""


def test_two_lesson_theme_writes_with_same_theme_id_do_not_raise(
        tmp_path, monkeypatch):
    from mini_ork.learning import themes

    db = tmp_path / "state.db"
    themes.ensure_schema(str(db))
    con = sqlite3.connect(db)
    try:
        con.execute(_GRADIENT_DDL)
        now = int(time.time())
        con.executemany(
            "INSERT INTO gradient_records"
            " (gradient_id, target, signal, suggested_change, evidence,"
            "  confidence, created_at, task_class)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("g-spec-A", "node:planner", "alpha beta gamma delta",
                 "tweak one", "trace-A", 0.5, now, "code-fix"),
                ("g-spec-B", "node:reviewer", "epsilon zeta eta theta",
                 "tweak two", "trace-B", 0.5, now, "code-fix"),
            ],
        )
        con.commit()
    finally:
        con.close()

    # No shared tokens -> both take the new-theme branch; the pinned id makes
    # the second INSERT collide, as two same-second runs do.
    monkeypatch.setattr(themes, "_theme_id", lambda kind, gid: "th-spec-collide")

    report = themes.assign_new(str(db))

    assert report["assigned"] == 2, f"both gradients must be assigned: {report}"
    con = sqlite3.connect(db)
    try:
        theme_rows = con.execute(
            "SELECT COUNT(*) FROM lesson_themes WHERE theme_id = 'th-spec-collide'"
        ).fetchone()[0]
        member_rows = con.execute(
            "SELECT COUNT(*) FROM gradient_theme"
            " WHERE theme_id = 'th-spec-collide'"
        ).fetchone()[0]
    finally:
        con.close()
    assert theme_rows == 1
    assert member_rows == 2
