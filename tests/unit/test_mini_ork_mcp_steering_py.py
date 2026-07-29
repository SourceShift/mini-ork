"""Native contract tests for operator-steering retrieval.

Each case seeds two independent, identical databases and compares their native
results. This catches accidental dependence on SQLite row state while keeping
the consuming-read behavior entirely within the canonical Python service.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from collections.abc import Iterable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.steering import mcp_server as mcp_ops
from mini_ork.stores import migrate

STRIP_KEYS = {"id", "created_at", "expires_at"}


def _init_db(tmp_path_factory: pytest.TempPathFactory, name: str) -> tuple[str, str]:
    home = tmp_path_factory.mktemp(name)
    db = str(home / "state.db")
    rc, _out, err = migrate.init_db(db, root=str(REPO))
    assert rc == 0, err
    return db, str(home)


def _point_python_env(monkeypatch: pytest.MonkeyPatch, db: str, home: str) -> None:
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_HOME", home)


def _seed_row(
    db: str,
    *,
    run_id: str | None,
    role_target: str,
    severity: str,
    message: str,
    source: str = "",
    confidence: float = 0.8,
    created_at: int | None = None,
    expires_at: int | None = None,
    consumed_at: int | None = None,
) -> None:
    now = int(time.time() * 1000)
    created_at = now if created_at is None else created_at
    expires_at = now + 3600 * 1000 if expires_at is None else expires_at
    con = sqlite3.connect(db)
    try:
        con.execute(
            """INSERT INTO operator_steering
                 (run_id, role_target, severity, message, source,
                  confidence, created_at, expires_at, consumed_at)
               VALUES (NULLIF(?, ''), ?, ?, ?, NULLIF(?, ''), ?, ?, ?, ?)""",
            (
                run_id if run_id is not None else "",
                role_target,
                severity,
                message,
                source,
                float(confidence),
                int(created_at),
                int(expires_at),
                consumed_at,
            ),
        )
        con.commit()
    finally:
        con.close()


def _seed_many(db: str, rows: Iterable[dict]) -> None:
    for row in rows:
        _seed_row(db, **row)


def _independent_dbs(
    tmp_path_factory: pytest.TempPathFactory,
    name: str,
    rows: Iterable[dict],
) -> tuple[str, str, str, str]:
    baseline_db, baseline_home = _init_db(tmp_path_factory, f"{name}_baseline")
    py_db, py_home = _init_db(tmp_path_factory, f"{name}_native")
    row_list = list(rows)
    _seed_many(baseline_db, row_list)
    _seed_many(py_db, row_list)
    return baseline_db, baseline_home, py_db, py_home


def _native_fetch(
    monkeypatch: pytest.MonkeyPatch, run_id: str, role: str, db: str, home: str
) -> list[dict]:
    _point_python_env(monkeypatch, db, home)
    return mcp_ops.get_operator_steering(run_id, role)


def _norm(row: dict) -> dict:
    out = {key: value for key, value in row.items() if key not in STRIP_KEYS}
    if "confidence" in out:
        out["confidence"] = round(float(out["confidence"]), 6)
    return out


def _assert_rows_equal(left_rows: list[dict], right_rows: list[dict]) -> None:
    assert [_norm(row) for row in left_rows] == [_norm(row) for row in right_rows]
    assert len(left_rows) == len(right_rows)
    for left_row, right_row in zip(left_rows, right_rows, strict=True):
        assert abs(float(left_row["confidence"]) - float(right_row["confidence"])) <= 1e-6


def test_get_operator_steering_happy_path_parity(tmp_path_factory, monkeypatch):
    rows = [{
        "run_id": "r-a",
        "role_target": "implementer",
        "severity": "warn",
        "message": "hello-from-seed",
        "source": "seed-src",
        "confidence": 0.7,
    }]
    baseline_db, baseline_home, py_db, py_home = _independent_dbs(tmp_path_factory, "a", rows)

    baseline_rows = _native_fetch(monkeypatch, "r-a", "implementer", baseline_db, baseline_home)
    _point_python_env(monkeypatch, py_db, py_home)
    py_rows = mcp_ops.get_operator_steering("r-a", "implementer")

    _assert_rows_equal(py_rows, baseline_rows)
    assert [row["message"] for row in py_rows] == ["hello-from-seed"]


def test_get_operator_steering_empty_for_unseen_run_parity(tmp_path_factory, monkeypatch):
    rows = [{
        "run_id": "r-other",
        "role_target": "any",
        "severity": "info",
        "message": "not-for-me",
    }]
    baseline_db, baseline_home, py_db, py_home = _independent_dbs(tmp_path_factory, "b", rows)

    baseline_rows = _native_fetch(monkeypatch, "r-missing", "any", baseline_db, baseline_home)
    _point_python_env(monkeypatch, py_db, py_home)
    py_rows = mcp_ops.get_operator_steering("r-missing", "any")

    assert py_rows == baseline_rows == []


def test_get_operator_steering_role_or_semantics_parity(tmp_path_factory, monkeypatch):
    rows = [
        {"run_id": "r-c", "role_target": "any", "severity": "info", "message": "row-any"},
        {"run_id": "r-c", "role_target": "implementer", "severity": "info", "message": "row-impl"},
        {"run_id": "r-c", "role_target": "reviewer", "severity": "info", "message": "row-rev"},
    ]
    baseline_db, baseline_home, py_db, py_home = _independent_dbs(tmp_path_factory, "c1", rows)

    baseline_rows = _native_fetch(monkeypatch, "r-c", "implementer", baseline_db, baseline_home)
    _point_python_env(monkeypatch, py_db, py_home)
    py_rows = mcp_ops.get_operator_steering("r-c", "implementer")

    _assert_rows_equal(py_rows, baseline_rows)
    assert sorted(row["message"] for row in py_rows) == ["row-any", "row-impl"]

    baseline_db, baseline_home, py_db, py_home = _independent_dbs(tmp_path_factory, "c2", rows)
    baseline_rows = _native_fetch(monkeypatch, "r-c", "reviewer", baseline_db, baseline_home)
    _point_python_env(monkeypatch, py_db, py_home)
    py_rows = mcp_ops.get_operator_steering("r-c", "reviewer")

    _assert_rows_equal(py_rows, baseline_rows)
    assert sorted(row["message"] for row in py_rows) == ["row-any", "row-rev"]

    baseline_db, baseline_home, py_db, py_home = _independent_dbs(tmp_path_factory, "c3", rows)
    baseline_rows = _native_fetch(monkeypatch, "r-c", "any", baseline_db, baseline_home)
    _point_python_env(monkeypatch, py_db, py_home)
    py_rows = mcp_ops.get_operator_steering("r-c", "any")

    _assert_rows_equal(py_rows, baseline_rows)
    assert [row["message"] for row in py_rows] == ["row-any"]


def test_get_operator_steering_ordering_parity(tmp_path_factory, monkeypatch):
    now = int(time.time() * 1000)
    rows = [
        {
            "run_id": "r-d",
            "role_target": "any",
            "severity": "info",
            "message": "msg-A",
            "source": "seed",
            "confidence": 0.50,
            "created_at": now,
            "expires_at": now + 3600 * 1000,
        },
        {
            "run_id": "r-d",
            "role_target": "any",
            "severity": "warn",
            "message": "msg-B",
            "source": "seed",
            "confidence": 0.95,
            "created_at": now + 1000,
            "expires_at": now + 3600 * 1000,
        },
        {
            "run_id": "r-d",
            "role_target": "any",
            "severity": "critical",
            "message": "msg-C",
            "source": "seed",
            "confidence": 0.30,
            "created_at": now + 2000,
            "expires_at": now + 3600 * 1000,
        },
        {
            "run_id": "r-d",
            "role_target": "any",
            "severity": "critical",
            "message": "msg-D",
            "source": "seed",
            "confidence": 0.95,
            "created_at": now + 3000,
            "expires_at": now + 3600 * 1000,
        },
        {
            "run_id": "r-d",
            "role_target": "any",
            "severity": "critical",
            "message": "msg-E",
            "source": "seed",
            "confidence": 0.95,
            "created_at": now + 2500,
            "expires_at": now + 3600 * 1000,
        },
    ]
    baseline_db, baseline_home, py_db, py_home = _independent_dbs(tmp_path_factory, "d", rows)

    baseline_rows = _native_fetch(monkeypatch, "r-d", "any", baseline_db, baseline_home)
    _point_python_env(monkeypatch, py_db, py_home)
    py_rows = mcp_ops.get_operator_steering("r-d", "any")

    _assert_rows_equal(py_rows, baseline_rows)
    assert [row["message"] for row in py_rows] == ["msg-D", "msg-E", "msg-C", "msg-B", "msg-A"]


def test_get_operator_steering_consumed_mark_parity(tmp_path_factory, monkeypatch):
    rows = [{
        "run_id": "r-e",
        "role_target": "any",
        "severity": "info",
        "message": "once",
    }]
    baseline_db, baseline_home, py_db, py_home = _independent_dbs(tmp_path_factory, "e", rows)
    baseline_first = _native_fetch(monkeypatch, "r-e", "any", baseline_db, baseline_home)
    baseline_second = _native_fetch(monkeypatch, "r-e", "any", baseline_db, baseline_home)

    _point_python_env(monkeypatch, py_db, py_home)
    py_first = mcp_ops.get_operator_steering("r-e", "any")
    py_second = mcp_ops.get_operator_steering("r-e", "any")

    _assert_rows_equal(py_first, baseline_first)
    assert py_second == baseline_second == []


def test_get_operator_steering_expiry_and_consumed_filters_parity(tmp_path_factory, monkeypatch):
    now = int(time.time() * 1000)
    rows = [
        {
            "run_id": "r-f",
            "role_target": "any",
            "severity": "info",
            "message": "row-fresh",
            "created_at": now,
            "expires_at": now + 3600 * 1000,
        },
        {
            "run_id": "r-f",
            "role_target": "any",
            "severity": "info",
            "message": "row-expired",
            "created_at": now - 7200 * 1000,
            "expires_at": now - 3600 * 1000,
        },
        {
            "run_id": "r-f",
            "role_target": "any",
            "severity": "info",
            "message": "row-consumed",
            "created_at": now,
            "expires_at": now + 3600 * 1000,
            "consumed_at": now - 1000,
        },
    ]
    baseline_db, baseline_home, py_db, py_home = _independent_dbs(tmp_path_factory, "f", rows)

    baseline_rows = _native_fetch(monkeypatch, "r-f", "any", baseline_db, baseline_home)
    _point_python_env(monkeypatch, py_db, py_home)
    py_rows = mcp_ops.get_operator_steering("r-f", "any")

    _assert_rows_equal(py_rows, baseline_rows)
    assert [row["message"] for row in py_rows] == ["row-fresh"]


def test_get_operator_steering_float_confidence_parity(tmp_path_factory, monkeypatch):
    rows = [{
        "run_id": "r-g",
        "role_target": "any",
        "severity": "info",
        "message": "float-row",
        "confidence": 0.123456789,
    }]
    baseline_db, baseline_home, py_db, py_home = _independent_dbs(tmp_path_factory, "g", rows)

    baseline_rows = _native_fetch(monkeypatch, "r-g", "any", baseline_db, baseline_home)
    _point_python_env(monkeypatch, py_db, py_home)
    py_rows = mcp_ops.get_operator_steering("r-g", "any")

    _assert_rows_equal(py_rows, baseline_rows)
    assert abs(py_rows[0]["confidence"] - 0.123456789) <= 1e-6
