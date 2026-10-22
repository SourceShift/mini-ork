"""Unit tests for the CoBa revise-round value rule (rsi-gap-coba).

Hermetic: every test uses a temp sqlite file; no shared state, no network.
The env may only tighten the rule, so each test starts from the defaults.
"""

import sqlite3

import pytest

from mini_ork.learning import revise_value

TASK_CLASS = "framework_edit"
TARGET = "implementer"


@pytest.fixture(autouse=True)
def _clean_coba_env(monkeypatch):
    for var in ("MO_COBA", "MO_COBA_MIN_SAMPLES", "MO_COBA_MIN_GAIN"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "state.db")


def _fetch_all(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _seed(db_path, *, round_no=2, passed=0, failed=0,
          task_class=TASK_CLASS, target=TARGET, prefix="run"):
    """Record + settle ``passed``/``failed`` history rows at ``round_no``."""
    for i in range(passed):
        run_id = f"{prefix}-pass-{i}"
        revise_value.record_round(db_path, run_id, task_class, target, round_no)
        revise_value.settle_round(db_path, run_id, target, round_no, True)
    for i in range(failed):
        run_id = f"{prefix}-fail-{i}"
        revise_value.record_round(db_path, run_id, task_class, target, round_no)
        revise_value.settle_round(db_path, run_id, target, round_no, False)


# ── record_round ─────────────────────────────────────────────────────────────

def test_record_round_creates_table_on_unmigrated_db(db):
    revise_value.record_round(db, "run-1", TASK_CLASS, TARGET, 1)
    rows = _fetch_all(
        db, "SELECT run_id, task_class, target_node, round FROM revise_rounds")
    assert rows == [("run-1", TASK_CLASS, TARGET, 1)]


def test_record_round_twice_same_key_one_row(db):
    revise_value.record_round(db, "run-1", TASK_CLASS, TARGET, 1)
    revise_value.record_round(db, "run-1", TASK_CLASS, TARGET, 1, cost_usd=0.5)
    assert _fetch_all(db, "SELECT COUNT(*) FROM revise_rounds")[0][0] == 1


def test_record_round_idempotent_keeps_settled_state(db):
    revise_value.record_round(db, "run-1", TASK_CLASS, TARGET, 2)
    revise_value.settle_round(db, "run-1", TARGET, 2, True)
    revise_value.record_round(db, "run-1", TASK_CLASS, TARGET, 2)
    assert revise_value.next_round_value(db, TASK_CLASS, TARGET, 2) == {
        "p": 1.0,
        "n": 1,
    }


def test_record_round_stores_cost_and_created_at(db):
    revise_value.record_round(db, "run-1", TASK_CLASS, TARGET, 1, cost_usd=0.42)
    cost_usd, created_at = _fetch_all(
        db, "SELECT cost_usd, created_at FROM revise_rounds")[0]
    assert cost_usd == pytest.approx(0.42)
    assert created_at is not None


# ── settle_round ─────────────────────────────────────────────────────────────

def test_settle_round_encodes_passed_as_one_zero(db):
    revise_value.record_round(db, "run-open", TASK_CLASS, TARGET, 1)
    revise_value.record_round(db, "run-pass", TASK_CLASS, TARGET, 1)
    revise_value.record_round(db, "run-fail", TASK_CLASS, TARGET, 1)
    revise_value.settle_round(db, "run-pass", TARGET, 1, True)
    revise_value.settle_round(db, "run-fail", TARGET, 1, False)
    values = dict(_fetch_all(db, "SELECT run_id, passed FROM revise_rounds"))
    assert values["run-open"] is None
    assert values["run-pass"] == 1
    assert values["run-fail"] == 0


# ── next_round_value ─────────────────────────────────────────────────────────

def test_next_round_value_no_history(db):
    assert revise_value.next_round_value(db, TASK_CLASS, TARGET, 2) == {
        "p": None,
        "n": 0,
    }


def test_next_round_value_fraction_passed_at_round(db):
    _seed(db, round_no=2, passed=3, failed=1)
    value = revise_value.next_round_value(db, TASK_CLASS, TARGET, 2)
    assert set(value) == {"p", "n"}
    assert value["n"] == 4
    assert value["p"] == pytest.approx(0.75)


def test_next_round_value_ignores_unsettled_rows(db):
    for i in range(5):
        revise_value.record_round(db, f"run-{i}", TASK_CLASS, TARGET, 2)
    assert revise_value.next_round_value(db, TASK_CLASS, TARGET, 2) == {
        "p": None,
        "n": 0,
    }


def test_next_round_value_scopes_to_class_target_round(db):
    _seed(db, round_no=2, passed=2, failed=1, prefix="in-scope")
    _seed(db, round_no=2, passed=9, task_class="code_fix", prefix="other-class")
    _seed(db, round_no=2, passed=9, target="reviewer", prefix="other-target")
    _seed(db, round_no=1, passed=9, prefix="other-round")
    value = revise_value.next_round_value(db, TASK_CLASS, TARGET, 2)
    assert value["n"] == 3
    assert value["p"] == pytest.approx(2 / 3)


# ── should_stop ──────────────────────────────────────────────────────────────

def test_should_stop_true_25_round2_rows_1_passed(db):
    _seed(db, round_no=2, passed=1, failed=24)
    stop, reason = revise_value.should_stop(db, TASK_CLASS, TARGET, 2)
    assert stop is True
    assert isinstance(reason, str) and reason


def test_should_stop_false_10_rows_below_min_samples(db):
    _seed(db, round_no=2, passed=0, failed=10)
    stop, _ = revise_value.should_stop(db, TASK_CLASS, TARGET, 2)
    assert stop is False


def test_should_stop_false_25_rows_5_passed(db):
    _seed(db, round_no=2, passed=5, failed=20)
    stop, _ = revise_value.should_stop(db, TASK_CLASS, TARGET, 2)
    assert stop is False


def test_min_samples_env_clamps_up_to_20(db, monkeypatch):
    _seed(db, round_no=2, passed=0, failed=10)
    monkeypatch.setenv("MO_COBA_MIN_SAMPLES", "3")
    stop, _ = revise_value.should_stop(db, TASK_CLASS, TARGET, 2)
    assert stop is False


def test_min_samples_env_can_tighten_above_default(db, monkeypatch):
    _seed(db, round_no=2, passed=1, failed=24)
    monkeypatch.setenv("MO_COBA_MIN_SAMPLES", "30")
    stop, _ = revise_value.should_stop(db, TASK_CLASS, TARGET, 2)
    assert stop is False


def test_min_gain_env_clamps_down_to_005(db, monkeypatch):
    _seed(db, round_no=2, passed=5, failed=20)
    monkeypatch.setenv("MO_COBA_MIN_GAIN", "0.5")
    stop, _ = revise_value.should_stop(db, TASK_CLASS, TARGET, 2)
    assert stop is False


def test_min_gain_env_can_tighten_below_default(db, monkeypatch):
    _seed(db, round_no=2, passed=1, failed=24)
    monkeypatch.setenv("MO_COBA_MIN_GAIN", "0.01")
    stop, _ = revise_value.should_stop(db, TASK_CLASS, TARGET, 2)
    assert stop is False


def test_coba_zero_returns_disabled_tuple(db, monkeypatch):
    _seed(db, round_no=2, passed=0, failed=25)
    monkeypatch.setenv("MO_COBA", "0")
    assert revise_value.should_stop(db, TASK_CLASS, TARGET, 2) == (
        False,
        "coba disabled",
    )


def test_coba_one_keeps_rule_armed(db, monkeypatch):
    _seed(db, round_no=2, passed=1, failed=24)
    monkeypatch.setenv("MO_COBA", "1")
    stop, _ = revise_value.should_stop(db, TASK_CLASS, TARGET, 2)
    assert stop is True


# ── failure neutrality ───────────────────────────────────────────────────────

def test_neutral_values_on_db_in_nonexistent_directory(tmp_path):
    db = str(tmp_path / "no-such-dir" / "state.db")
    revise_value.record_round(db, "run-1", TASK_CLASS, TARGET, 1, cost_usd=0.1)
    revise_value.settle_round(db, "run-1", TARGET, 1, True)
    assert revise_value.next_round_value(db, TASK_CLASS, TARGET, 1) == {
        "p": None,
        "n": 0,
    }
    stop, reason = revise_value.should_stop(db, TASK_CLASS, TARGET, 1)
    assert stop is False
    assert isinstance(reason, str)


def test_neutral_values_on_corrupt_db_file(tmp_path):
    db_file = tmp_path / "state.db"
    db_file.write_bytes(b"this is not a sqlite database at all")
    db = str(db_file)
    revise_value.record_round(db, "run-1", TASK_CLASS, TARGET, 1)
    revise_value.settle_round(db, "run-1", TARGET, 1, True)
    assert revise_value.next_round_value(db, TASK_CLASS, TARGET, 1) == {
        "p": None,
        "n": 0,
    }
    stop, reason = revise_value.should_stop(db, TASK_CLASS, TARGET, 1)
    assert stop is False
    assert isinstance(reason, str)
