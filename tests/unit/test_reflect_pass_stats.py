"""Reflect records its learning-stage health in ``learning_pass_stats``.

Hermetic contracts for the two pass-stats producers wired by gap-s4:

- ``mini_ork/cli/reflect.py::main`` → ``pattern_induction`` (plus the
  ``MO_REFLECT_PASS_ID`` scope around the pipeline call);
- ``mini_ork/learning/reflection_pipeline.py::reflection_extract_gradients``
  → ``gradient_extract``.

Every test builds its own DB with ``mini_ork.stores.migrate.init_db`` in
``tmp_path``, stubs every LLM dispatch, and disables the optional reflect
side channels through their documented opt-out env vars. No network, no
credentials.
"""
from __future__ import annotations

import os
import sqlite3
from types import SimpleNamespace

import pytest

from mini_ork.cli import reflect as reflect_cli
from mini_ork.learning import gradient_extractor
from mini_ork.learning import ledger as learning_ledger
from mini_ork.learning import pattern_induction
from mini_ork.learning import reflection_pipeline as rp
from mini_ork.stores.migrate import init_db

_SIDE_CHANNELS_OFF = (
    "MO_CODE_FINDINGS",
    "MO_PATTERN_MINER",
    "MO_MGM_MINER",
    "MO_THEMES",
    "MO_CROSS_EPIC_GRADIENTS",
    "MO_BUG_REPORT_SWEEP",
    "MO_RHO_AGGREGATE",
    "MO_LANE_ROUTER",
)
_ENV_CLEAN = (
    "MO_REFLECT_PASS_ID",
    "MO_REFLECT_DEADLINE_S",
    "MO_REFLECT_POST_RUN",
    "MINI_ORK_REFLECT_RUN_ID",
    "MINI_ORK_REFLECT_LANE",
    "MINI_ORK_DRY_RUN",
    "MINI_ORK_RUN_DIR",
    "MINI_ORK_GRADIENT_MODEL",
)

_GRADIENT_JSON = (
    '{"gradient_id":"g-%s","target":"t","signal":"s","suggested_change":"c",'
    '"evidence":"e","confidence":0.5}'
)


def _rows(db: str, stage: str | None = None) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        sql = (
            "SELECT pass_id, stage, inputs, outputs, failures, lane, "
            "last_error FROM learning_pass_stats"
        )
        params: tuple = ()
        if stage is not None:
            sql += " WHERE stage = ?"
            params = (stage,)
        return con.execute(sql + " ORDER BY rowid", params).fetchall()
    finally:
        con.close()


@pytest.fixture
def reflect_env(tmp_path):
    """Tmp home + migrated DB, with the whole process env snapshotted.

    ``reflect.main`` writes several ``os.environ`` keys without restoring
    them, so the fixture restores the entire environ on teardown.
    """
    db = str(tmp_path / "state.db")
    init_db(db=db, root=os.getcwd())
    learning_ledger.ensure_schema(db=db)
    home = tmp_path / "home"
    home.mkdir()
    saved = dict(os.environ)
    try:
        os.environ["MINI_ORK_HOME"] = str(home)
        os.environ["MINI_ORK_DB"] = db
        os.environ["MO_LAYER_ROUTING"] = "off"
        for name in _SIDE_CHANNELS_OFF:
            os.environ[name] = "0"
        for name in _ENV_CLEAN:
            os.environ.pop(name, None)
        yield SimpleNamespace(db=db, home=str(home))
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture
def reflect_pass(reflect_env, monkeypatch):
    """A reflect pass with the pipeline and induction stubbed."""
    captured: dict = {}

    def _fake_reflection_run(since_ts):  # noqa: ARG001 — reflect passes it
        captured["pass_id_env"] = os.environ.get("MO_REFLECT_PASS_ID")
        return "[]"

    monkeypatch.setattr(rp, "reflection_run", _fake_reflection_run)
    monkeypatch.setattr(
        pattern_induction, "_induct_enabled", lambda: True
    )

    def install(report):
        monkeypatch.setattr(
            pattern_induction, "induce_pending", lambda **kw: report
        )

    def install_raising(exc):
        def _boom(**kw):  # noqa: ARG001 — mirrors reflect's call site
            raise exc

        monkeypatch.setattr(pattern_induction, "induce_pending", _boom)

    return SimpleNamespace(
        env=reflect_env,
        captured=captured,
        install=install,
        install_raising=install_raising,
        run=lambda: reflect_cli.main([]),
    )


@pytest.fixture
def gradient_pass(reflect_env, monkeypatch):
    """``reflection_extract_gradients`` with every collaborator stubbed."""
    monkeypatch.setattr(
        gradient_extractor, "_last_dispatch_error", "", raising=False
    )

    def configure(trace_ids, extractor, watermark=lambda tid: False):
        monkeypatch.setattr(
            rp,
            "_extract_trace_ids_sql",
            lambda db_path, since_ts, batch: list(trace_ids),  # noqa: ARG001
        )
        monkeypatch.setattr(rp, "_gradient_extract", extractor)
        monkeypatch.setattr(rp, "_gradient_store", lambda gradient: None)
        monkeypatch.setattr(rp, "_gradient_ensure_table", lambda: None)
        monkeypatch.setattr(gradient_extractor, "has_watermark", watermark)

    return SimpleNamespace(
        env=reflect_env,
        configure=configure,
        call=lambda: rp.reflection_extract_gradients(0),
    )


# ── pattern_induction (mini_ork/cli/reflect.py) ─────────────────────────────


def test_induction_report_writes_one_row(reflect_pass):
    """inputs = induced + len(skipped); lane/error from the failing entry."""
    pas = reflect_pass
    pas.install(
        {
            "induced": 2,
            "skipped": [
                {"reason": "dispatch failed", "model": "glm", "error": "boom"},
                {"reason": "too small"},
            ],
        }
    )
    assert pas.run() == 0
    rows = _rows(pas.env.db, "pattern_induction")
    assert len(rows) == 1, rows
    pass_id, stage, inputs, outputs, failures, lane, last_error = rows[0]
    assert stage == "pattern_induction"
    assert (inputs, outputs, failures) == (4, 2, 1), rows[0]
    assert lane == "glm", rows[0]
    assert last_error == "boom", rows[0]
    assert pass_id.startswith("tr-reflect-"), rows[0]


def test_induction_lane_from_last_failure(reflect_pass):
    """lane and last_error come from the LAST failing entry."""
    pas = reflect_pass
    pas.install(
        {
            "induced": 1,
            "skipped": [
                {"reason": "dispatch failed", "model": "m1", "error": "e1"},
                {"reason": "dispatch failed", "model": "m2", "error": "e2"},
            ],
        }
    )
    assert pas.run() == 0
    rows = _rows(pas.env.db, "pattern_induction")
    assert len(rows) == 1, rows
    assert rows[0][2] == 3, rows[0]
    assert rows[0][3] == 1, rows[0]
    assert (rows[0][4], rows[0][5], rows[0][6]) == (2, "m2", "e2"), rows[0]


def test_induction_report_none_writes_nothing(reflect_pass):
    """Induction turned off (or raised) writes no row."""
    pas = reflect_pass
    pas.install(None)
    assert pas.run() == 0
    assert _rows(pas.env.db, "pattern_induction") == []

    pas.install_raising(RuntimeError("induction exploded"))
    assert pas.run() == 0
    assert _rows(pas.env.db, "pattern_induction") == []


def test_pass_id_scoped_around_pipeline(reflect_pass):
    """MO_REFLECT_PASS_ID is visible inside the pipeline and restored after."""
    pas = reflect_pass
    pas.install({"induced": 0, "skipped": []})
    assert pas.run() == 0
    seen = pas.captured.get("pass_id_env")
    assert seen and seen.startswith("tr-reflect-"), seen
    rows = _rows(pas.env.db, "pattern_induction")
    assert rows and rows[0][0] == seen, rows
    assert os.environ.get("MO_REFLECT_PASS_ID") is None


# ── gradient_extract (mini_ork/learning/reflection_pipeline.py) ─────────────


def test_three_new_traces_three_in_three_out(gradient_pass):
    gp = gradient_pass
    os.environ["MO_REFLECT_PASS_ID"] = "tr-reflect-1-1"
    gp.configure(
        ["t1", "t2", "t3"],
        lambda tid: [_GRADIENT_JSON % tid],
    )
    gp.call()
    rows = _rows(gp.env.db, "gradient_extract")
    assert len(rows) == 1, rows
    assert rows[0][:5] == ("tr-reflect-1-1", "gradient_extract", 3, 3, 0), rows[0]


def test_all_watermarked_pass_writes_zero_inputs_and_never_alarms(gradient_pass):
    gp = gradient_pass
    gp.configure(
        ["t1", "t2", "t3"],
        lambda tid: [_GRADIENT_JSON % tid],
        watermark=lambda tid: True,
    )
    for i in range(3):
        os.environ["MO_REFLECT_PASS_ID"] = f"tr-reflect-2-{i}"
        gp.call()
        rows = _rows(gp.env.db, "gradient_extract")
        assert len(rows) == i + 1, rows
        assert (rows[-1][2], rows[-1][3]) == (0, 0), rows[-1]
    health = learning_ledger.stage_health(window_passes=3, db=gp.env.db)
    entry = next(h for h in health if h["stage"] == "gradient_extract")
    assert len(entry["window"]) == 3, entry
    assert entry["alarm"] is False, entry


def test_dispatch_failure_counts_and_carries_error(gradient_pass):
    gp = gradient_pass

    def _extractor(tid):
        if tid == "t-bad":
            raise RuntimeError("dispatch blew up")
        return [_GRADIENT_JSON % tid]

    os.environ["MO_REFLECT_PASS_ID"] = "tr-reflect-3-3"
    gp.configure(["t-bad", "t-ok"], _extractor)
    gp.call()
    rows = _rows(gp.env.db, "gradient_extract")
    assert len(rows) == 1, rows
    _, _, inputs, outputs, failures, _, last_error = rows[0]
    assert (inputs, outputs, failures) == (2, 1, 1), rows[0]
    assert "dispatch blew up" in last_error, rows[0]


def test_no_row_without_pass_id(gradient_pass):
    gp = gradient_pass
    assert "MO_REFLECT_PASS_ID" not in os.environ
    gp.configure(["t1"], lambda tid: [_GRADIENT_JSON % tid])
    gp.call()
    assert _rows(gp.env.db, "gradient_extract") == []


# ── dry run and fail-soft ───────────────────────────────────────────────────


def test_dry_run_writes_no_rows(reflect_pass):
    pas = reflect_pass
    pas.install({"induced": 2, "skipped": [{"reason": "too small"}]})
    assert reflect_cli.main(["--dry-run"]) == 0
    assert _rows(pas.env.db) == []


def test_raising_record_pass_stat_does_not_fail_induction(reflect_pass, monkeypatch):
    pas = reflect_pass
    pas.install({"induced": 1, "skipped": [{"reason": "too small"}]})

    def _boom(*args, **kwargs):
        raise RuntimeError("ledger is down")

    monkeypatch.setattr(learning_ledger, "record_pass_stat", _boom)
    assert pas.run() == 0
    assert _rows(pas.env.db) == []


def test_raising_record_pass_stat_does_not_fail_gradient_stage(
    gradient_pass, monkeypatch
):
    gp = gradient_pass

    def _boom(*args, **kwargs):
        raise RuntimeError("ledger is down")

    monkeypatch.setattr(learning_ledger, "record_pass_stat", _boom)
    os.environ["MO_REFLECT_PASS_ID"] = "tr-reflect-4-4"
    gp.configure(["t1"], lambda tid: [_GRADIENT_JSON % tid])
    gp.call()  # must not raise
    assert _rows(gp.env.db) == []


def test_cost_circuit_refusal_is_one_failure_not_two(gradient_pass, monkeypatch):
    """A cost-circuit refusal increments dispatch_failed and circuit_open
    before the batch aborts; the row must count it once (review fix)."""
    from mini_ork.learning import gradient_extractor as _ge

    # Restored at teardown: the extractor below overwrites the module global.
    monkeypatch.setattr(_ge, "_last_dispatch_error", "", raising=False)
    gp = gradient_pass

    def _extractor(tid):
        _ge._last_dispatch_error = "cost_circuit_open: daily budget reached"
        raise RuntimeError("refused")

    os.environ["MO_REFLECT_PASS_ID"] = "tr-reflect-4-4"
    gp.configure(["t1", "t2", "t3"], _extractor)
    gp.call()
    rows = _rows(gp.env.db, "gradient_extract")
    assert len(rows) == 1, rows
    _, _, inputs, outputs, failures, _, last_error = rows[0]
    assert (inputs, outputs, failures) == (1, 0, 1), rows[0]
    assert "cost_circuit_open" in last_error, rows[0]
