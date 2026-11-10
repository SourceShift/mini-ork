"""Per-lesson node outcomes: the fact table, the resolver, and the contrasts.

The question these tests defend is narrow and the answer must stay honest:
*did lesson X help the node that received it?* That is only answerable from a
randomised arm, so the module reports a named insufficiency everywhere the arm
cannot carry the number, and never a zero. Every assertion below is hermetic —
a temp SQLite file per test, no live DB, no network.

``_BASE_DDL`` mirrors the two upstream tables the resolver joins; the 0092
tables come from the module's own ``ensure_schema`` (and a parity test proves
that DDL matches ``db/migrations/0092_lesson_outcome_exposures.sql``).
"""
from __future__ import annotations

import io
import math
import sqlite3
from pathlib import Path

import pytest

import mini_ork.context_assembler  # noqa: F401 — the arm predicate host
import mini_ork.learning.lesson_outcomes as lo
from mini_ork.cli import metrics_lessons

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "db" / "migrations" / "0092_lesson_outcome_exposures.sql"

_BASE_DDL = """
CREATE TABLE task_runs (
    id         TEXT PRIMARY KEY,
    status     TEXT,
    task_class TEXT,
    created_at INTEGER
);
CREATE TABLE node_attempts (
    run_id     TEXT NOT NULL,
    node_id    TEXT NOT NULL,
    attempt_no INTEGER,
    result     TEXT,
    cost_usd   REAL,
    started_at INTEGER,
    node_type  TEXT
);
CREATE TABLE lesson_injections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    node_type   TEXT,
    lane        TEXT,
    task_class  TEXT,
    attempt     INTEGER,
    source_kind TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    held_out    INTEGER NOT NULL DEFAULT 0,
    ts          INTEGER NOT NULL
);
"""


# ── fixtures ────────────────────────────────────────────────────────────────

def _db(tmp_path: Path) -> str:
    db = str(tmp_path / "state.db")
    con = sqlite3.connect(db)
    con.executescript(_BASE_DDL)
    lo.ensure_schema(con)
    con.commit()
    con.close()
    return db


def _run(db: str, run_id: str, *, status: str = "published",
         task_class: str = "code_fix", created: int = 1000) -> None:
    con = sqlite3.connect(db)
    con.execute("INSERT INTO task_runs(id, status, task_class, created_at)"
                " VALUES (?,?,?,?)", (run_id, status, task_class, created))
    con.commit()
    con.close()


def _attempt(db: str, run_id: str, node_id: str, attempt_no: int,
             result: str, *, node_type: str = "implementer",
             started: int = 1_700_000_000, cost: float | None = None) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO node_attempts(run_id, node_id, attempt_no, result,"
        " cost_usd, started_at, node_type) VALUES (?,?,?,?,?,?,?)",
        (run_id, node_id, attempt_no, result, cost, started, node_type))
    con.commit()
    con.close()


def _inject(db: str, run_id: str, node_id: str, kind: str, ident: str, *,
            attempt: int = 1, held: int = 0, ts: int = 1_700_000_000,
            node_type: str = "implementer") -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO lesson_injections(run_id, node_id, node_type, lane,"
        " task_class, attempt, source_kind, source_id, held_out, ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, node_id, node_type, "fast", "code_fix", attempt, kind,
         ident, held, ts))
    con.commit()
    con.close()


def _row(**kw) -> dict:
    """One fact row with the module's column names and sane defaults."""
    base = {
        "run_id": "r", "node_id": "n", "source_kind": "pattern",
        "source_id": "pat-a", "arm": "delivered", "stable": 1,
        "node_type": "implementer", "task_class": "code_fix",
        "outcome_day": 100, "result_1": "success", "clean": 1, "retried": 0,
        "failed": 0, "attempts": 1, "cost_usd": None, "resolved_at": 0,
    }
    base.update(kw)
    return base


# ── resolution ──────────────────────────────────────────────────────────────

def test_resolver_writes_one_row_per_attempt1_injection(tmp_path):
    db = _db(tmp_path)
    _run(db, "r1")
    _attempt(db, "r1", "n1", 1, "success", cost=0.02)
    _inject(db, "r1", "n1", "pattern", "pat-a")
    _inject(db, "r1", "n1", "theme", "th-a")

    result = lo.resolve_finished(db)

    assert result == {"runs": 1, "exposures": 2, "skipped": 0, "note": ""}
    rows = lo.exposures(db)
    assert {(r["source_kind"], r["source_id"]) for r in rows} == {
        ("pattern", "pat-a"), ("theme", "th-a")}
    one = rows[0]
    assert one["arm"] == "delivered" and one["clean"] == 1
    assert one["node_type"] == "implementer" and one["task_class"] == "code_fix"
    assert one["cost_usd"] == pytest.approx(0.02)


def test_resolver_is_idempotent(tmp_path):
    db = _db(tmp_path)
    _run(db, "r1")
    _attempt(db, "r1", "n1", 1, "success")
    _inject(db, "r1", "n1", "pattern", "pat-a")
    first = lo.resolve_finished(db)
    before = lo.exposures(db)

    second = lo.resolve_finished(db)

    assert first["runs"] == 1 and first["exposures"] == 1
    assert second == {"runs": 0, "exposures": 0, "skipped": 0, "note": ""}
    assert lo.exposures(db) == before


def test_attempt_two_only_injection_creates_no_row(tmp_path):
    """A lesson first injected on a retry cannot be credited for the node's
    first attempt — the node had not seen it yet."""
    db = _db(tmp_path)
    _run(db, "r1")
    _attempt(db, "r1", "n1", 1, "failure")
    _attempt(db, "r1", "n1", 2, "success")
    _inject(db, "r1", "n1", "pattern", "pat-a", attempt=2)

    assert lo.resolve_finished(db)["exposures"] == 0
    assert lo.exposures(db) == []


def test_clean_needs_a_first_attempt_success_and_no_retry(tmp_path):
    db = _db(tmp_path)
    _run(db, "r1")
    _attempt(db, "r1", "n1", 1, "success")
    _attempt(db, "r1", "n1", 2, "success")
    _inject(db, "r1", "n1", "pattern", "pat-a")

    lo.resolve_finished(db)
    row = lo.exposures(db)[0]

    assert row["result_1"] == "success"
    assert row["clean"] == 0, "a node that needed a retry is not a clean first try"
    assert row["retried"] == 1 and row["attempts"] == 2


def test_a_failed_node_is_recorded_as_failed(tmp_path):
    db = _db(tmp_path)
    _run(db, "r1", status="failed")
    _attempt(db, "r1", "n1", 1, "failure")
    _inject(db, "r1", "n1", "pattern", "pat-a")

    lo.resolve_finished(db)
    row = lo.exposures(db)[0]

    assert row["failed"] == 1 and row["clean"] == 0
    assert row["result_1"] == "failure"


def test_positional_context_v2_constraint_ids_are_unstable(tmp_path):
    """``c:<index>`` is a kickoff position, not a lesson: two runs' ``c:0`` are
    different constraints, so the row is stored with ``stable=0`` and excluded
    from every rate."""
    db = _db(tmp_path)
    _run(db, "r1")
    _attempt(db, "r1", "n1", 1, "success")
    _inject(db, "r1", "n1", "context_v2", "c:0")
    _inject(db, "r1", "n1", "context_v2", "t:th-a")

    lo.resolve_finished(db)
    by_id = {r["source_id"]: r for r in lo.exposures(db)}

    assert by_id["c:0"]["stable"] == 0
    assert by_id["t:th-a"]["stable"] == 1
    stats = lo.ledger_stats(db)
    assert stats["rows"] == 1 and stats["lessons"] == 1


def test_a_run_with_no_attempts_is_marked_so_the_window_cannot_wedge(tmp_path):
    db = _db(tmp_path)
    _run(db, "r-empty")

    assert lo.resolve_finished(db)["runs"] == 1
    rows = lo.resolve_runs(db)
    assert [(r["run_id"], r["note"]) for r in rows] == [("r-empty", "no_attempts")]
    assert lo.resolve_finished(db)["runs"] == 0, "it must leave the window"


def test_only_terminal_runs_are_resolved(tmp_path):
    db = _db(tmp_path)
    _run(db, "r-running", status="running")
    _attempt(db, "r-running", "n1", 1, "success")
    _inject(db, "r-running", "n1", "pattern", "pat-a")

    assert lo.resolve_finished(db)["runs"] == 0
    assert lo.exposures(db) == []


def test_a_missing_database_is_unavailable_never_an_empty_zero(tmp_path):
    missing = str(tmp_path / "nope" / "state.db")
    assert lo.connect(missing) is None
    assert lo.exposures(missing) == []
    stats = lo.ledger_stats(missing)
    assert stats["available"] is False
    assert lo.resolve_finished(missing)["note"] == "unavailable"


# ── summarize (pure) ────────────────────────────────────────────────────────

def test_summarize_never_reports_a_rate_for_no_rows():
    out = lo.summarize([])
    assert out["lessons"] == [] and out["unstable"] == 0
    assert lo._summary_from_rows([])["clean_rate"] is None
    assert lo._summary_from_rows([])["status"] == "unavailable"


def test_summarize_statuses():
    rows = [_row(clean=1) for _ in range(40)]
    measured = lo.summarize(rows, min_n_value=30, min_events_value=20)
    arm = measured["lessons"][0]["arms"]["delivered"]
    assert arm["clean_rate"] == 1.0
    assert arm["status"] == "insufficient_events", (
        "40 clean rows have zero non-clean events, so no lift is measurable")

    mixed = [_row(clean=1) for _ in range(30)] + [
        _row(clean=0, result_1="failure", failed=1) for _ in range(20)]
    arm = lo.summarize(mixed, min_n_value=30, min_events_value=20)[
        "lessons"][0]["arms"]["delivered"]
    assert arm["status"] == "measured"
    assert arm["clean_rate"] == pytest.approx(0.6)

    few = lo.summarize([_row()], min_n_value=30)["lessons"][0]["arms"]["delivered"]
    assert few["status"] == "insufficient_n"
    assert few["clean_rate"] == 1.0, "a real rate, just not enough rows"


def test_researcher_only_lessons_are_saturated_not_measured():
    rows = [_row(node_type="researcher", clean=1) for _ in range(60)]
    arm = lo.summarize(rows, min_n_value=30, min_events_value=1)[
        "lessons"][0]["arms"]["delivered"]
    assert arm["status"] == "saturated"
    assert arm["n_primary"] == 0


# ── the clean arm ───────────────────────────────────────────────────────────

def test_clean_arm_reports_which_arm_is_short(tmp_path):
    db = _db(tmp_path)
    for i in range(40):
        _run(db, "d%d" % i)
        _attempt(db, "d%d" % i, "n1", 1, "success")
        _inject(db, "d%d" % i, "n1", "pattern", "pat-a")
    lo.resolve_finished(db)

    out = lo.clean_arm_contrast(db, kind="pattern", ident="pat-a",
                               min_n_value=30, min_events_value=1)

    assert out["status"] == "insufficient_held_out"
    assert out["n_delivered"] == 40 and out["n_held_out"] == 0
    assert out["value"] is None and out["p"] is None


def test_fisher_p_is_exact():
    # Two-sided exact test on a 2x2 table of 10/10 vs 0/10.
    assert lo.fisher_p(10, 10, 0, 10) == pytest.approx(
        2 / math.comb(20, 10), rel=1e-9)
    assert lo.fisher_p(5, 10, 5, 10) == pytest.approx(1.0, abs=0.15)
    assert lo.fisher_p(0, 0, 5, 10) is None
    assert lo.fisher_p(3, 10, 0, 0) is None


def test_clean_arm_needs_a_significant_arm_not_a_point_estimate(tmp_path):
    db = _db(tmp_path)
    for i in range(30):
        _run(db, "d%d" % i)
        _attempt(db, "d%d" % i, "n1", 1, "success")
        _inject(db, "d%d" % i, "n1", "pattern", "pat-a")
    for i in range(30):
        _run(db, "h%d" % i)
        _attempt(db, "h%d" % i, "n1", 1, "success")
        _inject(db, "h%d" % i, "n1", "pattern", "pat-a", held=1)
    lo.resolve_finished(db)

    out = lo.clean_arm_contrast(db, kind="pattern", ident="pat-a",
                               min_n_value=30, min_events_value=1)

    # Both arms clean, so there is no event to contrast: the gate refuses.
    assert out["status"] == "insufficient_events"
    assert out["value"] is None and out["p"] is None


# ── population standardization ──────────────────────────────────────────────

def test_population_gap_standardizes_to_the_treated_weights():
    """Direct standardization is what stops a treated-heavy stratum from
    flattering the lesson: the control is re-weighted to the lesson's own
    ``(node_type, task_class)`` mix, not to the pool's."""
    treated = [
        _row(source_id="pat-a", node_type="implementer", task_class="code_fix",
             clean=1) for _ in range(10)
    ] + [
        _row(source_id="pat-a", node_type="reviewer",
             task_class="framework_edit", clean=0, failed=1)
        for _ in range(10)
    ]
    control = [
        _row(source_id="pat-other", node_type="implementer",
             task_class="code_fix", clean=1) for _ in range(5)
    ] + [
        _row(source_id="pat-other", node_type="reviewer",
             task_class="framework_edit", clean=0, failed=1)
        for _ in range(90)
    ]

    out = lo._gap_from_rows(treated + control, kind="pattern", ident="pat-a",
                            window=3650, min_n_value=10)

    assert out["status"] == "measured_correlational"
    assert out["treated_rate"] == pytest.approx(0.5)
    # Direct standardization re-weights the control to 50/50: the raw pool
    # rate would be 5/95 = 0.0526, which would read as a 0.45 "lift".
    assert out["control_rate"] == pytest.approx(0.5)
    assert out["value"] == pytest.approx(0.0)
    assert out["coverage"] == pytest.approx(1.0)


def test_population_gap_without_a_control_stratum_says_so():
    rows = [_row(source_id="pat-a", clean=1) for _ in range(5)]
    out = lo._gap_from_rows(rows, kind="pattern", ident="pat-a", window=3650)
    assert out["status"] == "insufficient_strata"
    assert out["value"] is None


def test_effect_is_zero_unless_every_gate_passes(tmp_path, monkeypatch):
    """The only function allowed to change a prompt. Unmeasured, disabled, or
    short of coverage all resolve to exactly 0.0 — today's behaviour."""
    db = _db(tmp_path)
    monkeypatch.setenv("MO_LESSON_EFFECT_RERANK", "0")
    assert lo.effect(db, kind="pattern", ident="pat-a")["value"] == 0.0
    monkeypatch.delenv("MO_LESSON_EFFECT_RERANK", raising=False)
    out = lo.effect(db, kind="pattern", ident="pat-a")
    assert out["value"] == 0.0
    assert out["status"] in ("insufficient_n", "disabled")


# ── the arm diagnostic ──────────────────────────────────────────────────────

def test_arm_diagnostic_separates_history_from_a_missing_sink(tmp_path):
    """A lesson inside the arm whose injections all predate the first held row
    is history, not a defect; one injected *since* with no held row is the
    only shape that means "this channel supplies no sink"."""
    db = _db(tmp_path)
    days = 86400
    base = 1_700_000_000

    # The first held row defines the arm's start.
    _inject(db, "r", "n", "pattern", "pat-held", held=1, ts=base)
    # In-arm id (bucket-wise) injected only *before* the arm existed.
    history = next(
        "h%04d" % i for i in range(500)
        if mini_ork.context_assembler.lesson_held_out("context_v2", "h%04d" % i))
    for i in range(40):
        _inject(db, "r", "n", "context_v2", history, ts=base - days)
    # In-arm id injected *after* the arm existed, never withheld.
    live = next(
        "l%04d" % i for i in range(500)
        if mini_ork.context_assembler.lesson_held_out("context_v2", "l%04d" % i))
    for i in range(40):
        _inject(db, "r", "n", "context_v2", live, ts=base + days)

    out = lo.arm_diagnostic(db)

    assert out["status"] == "measured"
    assert out["held_rows"] == 1
    assert [e["lesson"] for e in out["missing_expected"]] == [
        "context_v2:%s" % live]
    assert out["missing_expected"][0]["since_arm"] == 40


def test_arm_diagnostic_flags_a_held_row_todays_bucket_would_deliver(tmp_path):
    db = _db(tmp_path)
    out_of_arm = next(
        "x%04d" % i for i in range(500)
        if not mini_ork.context_assembler.lesson_held_out("context_v2", "x%04d" % i))
    _inject(db, "r", "n", "context_v2", out_of_arm, held=1)

    out = lo.arm_diagnostic(db)

    assert out["inconsistent_count"] == 1
    assert out["inconsistent"][0]["lesson"] == "context_v2:%s" % out_of_arm


def test_arm_diagnostic_without_the_ledger_is_unavailable(tmp_path):
    db = str(tmp_path / "empty.db")
    sqlite3.connect(db).close()
    assert lo.arm_diagnostic(db)["status"] == "unavailable"


# ── the DDL and the migration must agree ────────────────────────────────────

def test_module_ddl_matches_the_migration_columns():
    """``ensure_schema`` is a test/dev bridge for the same tables the migration
    creates; if the two drift, a dev DB and a production DB answer differently."""
    con = sqlite3.connect(":memory:")
    try:
        lo.ensure_schema(con)
        from_module = {
            table: [r[1] for r in con.execute('PRAGMA table_info("%s")' % table)]
            for table in ("lesson_outcome_exposures", "lesson_outcome_runs")
        }
    finally:
        con.close()

    sql = MIGRATION.read_text(encoding="utf-8")
    con = sqlite3.connect(":memory:")
    try:
        # Strip the bookkeeping row: the in-memory DB has no schema_migrations.
        body = "\n".join(line for line in sql.splitlines()
                         if not line.strip().upper().startswith("INSERT"))
        con.executescript(body)
        from_migration = {
            table: [r[1] for r in con.execute('PRAGMA table_info("%s")' % table)]
            for table in ("lesson_outcome_exposures", "lesson_outcome_runs")
        }
    finally:
        con.close()

    assert from_module == from_migration


# ── the CLI contract ────────────────────────────────────────────────────────

def test_cli_exits_1_without_a_state_db(tmp_path):
    err = io.StringIO()
    code = metrics_lessons.main(
        ["--db", str(tmp_path / "missing.db")], stdout=io.StringIO(), stderr=err)
    assert code == 1
    assert "no state.db" in err.getvalue()


def test_cli_exits_2_when_the_migration_has_not_run(tmp_path):
    db = str(tmp_path / "old.db")
    con = sqlite3.connect(db)
    con.executescript(_BASE_DDL)
    con.commit()
    con.close()
    err = io.StringIO()
    code = metrics_lessons.main(["--db", db], stdout=io.StringIO(), stderr=err)
    assert code == 2
    assert "0092_lesson_outcome_exposures.sql" in err.getvalue()


def test_cli_reports_a_ledger_whose_arm_is_empty(tmp_path):
    db = _db(tmp_path)
    _run(db, "r1")
    _attempt(db, "r1", "n1", 1, "success")
    _inject(db, "r1", "n1", "pattern", "pat-a")
    out = io.StringIO()
    code = metrics_lessons.main(["--db", db, "--json"],
                                stdout=out, stderr=io.StringIO())
    assert code == 0
    import json

    payload = json.loads(out.getvalue())
    assert payload["ledger"]["rows"] == 1
    assert payload["arm"]["status"] == "measured"
    lesson = payload["lessons"][0]
    assert lesson["lesson"] == "pattern:pat-a"
    assert lesson["arms"]["delivered"]["clean_rate"] == 1.0


def test_cli_resolve_only_writes_nothing_new_on_a_second_pass(tmp_path):
    db = _db(tmp_path)
    _run(db, "r1")
    _attempt(db, "r1", "n1", 1, "success")
    _inject(db, "r1", "n1", "pattern", "pat-a")

    out = io.StringIO()
    for _ in range(2):
        assert metrics_lessons.main(["--db", db, "--resolve-only"],
                                    stdout=out, stderr=io.StringIO()) == 0
    assert "exposures written 0" in out.getvalue()
