"""Parity gate: mini_ork.ported.mini_ork_mcp_steering vs live bash fetch.

The bash function consumes rows as it reads them, so parity cases use paired
temp DBs seeded with identical rows: one for live bash and one for the Python
port. Every DB is initialized by db/init.sh, and bash output is parsed from
JSONL before comparison.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_mcp_steering as mcp_ops  # noqa: E402

SH = REPO / "lib" / "operator_steering.sh"
INIT_SH = REPO / "db" / "init.sh"
STRIP_KEYS = {"id", "created_at", "expires_at"}


def _init_db(tmp_path_factory: pytest.TempPathFactory, name: str) -> tuple[str, str]:
    home = tmp_path_factory.mktemp(name)
    db = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
        capture_output=True,
        text=True,
        check=True,
    )
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


def _paired_dbs(
    tmp_path_factory: pytest.TempPathFactory,
    name: str,
    rows: Iterable[dict],
) -> tuple[str, str, str, str]:
    bash_db, bash_home = _init_db(tmp_path_factory, f"{name}_bash")
    py_db, py_home = _init_db(tmp_path_factory, f"{name}_py")
    row_list = list(rows)
    _seed_many(bash_db, row_list)
    _seed_many(py_db, row_list)
    return bash_db, bash_home, py_db, py_home


def _bash_fetch(run_id: str, role: str, db: str, home: str) -> list[dict]:
    wrapper = f'. "{SH}"\noperator_steering_fetch_for "{run_id}" "{role}"\n'
    result = subprocess.run(
        ["bash", "-c", wrapper],
        env={**os.environ, "MINI_ORK_HOME": home, "MINI_ORK_DB": db},
        capture_output=True,
        text=True,
        check=True,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line]


def _norm(row: dict) -> dict:
    out = {key: value for key, value in row.items() if key not in STRIP_KEYS}
    if "confidence" in out:
        out["confidence"] = round(float(out["confidence"]), 6)
    return out


def _assert_rows_equal(py_rows: list[dict], bash_rows: list[dict]) -> None:
    assert [_norm(row) for row in py_rows] == [_norm(row) for row in bash_rows]
    assert len(py_rows) == len(bash_rows)
    for py_row, bash_row in zip(py_rows, bash_rows, strict=True):
        assert abs(float(py_row["confidence"]) - float(bash_row["confidence"])) <= 1e-6


def test_get_operator_steering_happy_path_parity(tmp_path_factory, monkeypatch):
    rows = [{
        "run_id": "r-a",
        "role_target": "implementer",
        "severity": "warn",
        "message": "hello-from-seed",
        "source": "seed-src",
        "confidence": 0.7,
    }]
    bash_db, bash_home, py_db, py_home = _paired_dbs(tmp_path_factory, "a", rows)

    bash_rows = _bash_fetch("r-a", "implementer", bash_db, bash_home)
    _point_python_env(monkeypatch, py_db, py_home)
    py_rows = mcp_ops.get_operator_steering("r-a", "implementer")

    _assert_rows_equal(py_rows, bash_rows)
    assert [row["message"] for row in py_rows] == ["hello-from-seed"]


def test_get_operator_steering_empty_for_unseen_run_parity(tmp_path_factory, monkeypatch):
    rows = [{
        "run_id": "r-other",
        "role_target": "any",
        "severity": "info",
        "message": "not-for-me",
    }]
    bash_db, bash_home, py_db, py_home = _paired_dbs(tmp_path_factory, "b", rows)

    bash_rows = _bash_fetch("r-missing", "any", bash_db, bash_home)
    _point_python_env(monkeypatch, py_db, py_home)
    py_rows = mcp_ops.get_operator_steering("r-missing", "any")

    assert py_rows == bash_rows == []


def test_get_operator_steering_role_or_semantics_parity(tmp_path_factory, monkeypatch):
    rows = [
        {"run_id": "r-c", "role_target": "any", "severity": "info", "message": "row-any"},
        {"run_id": "r-c", "role_target": "implementer", "severity": "info", "message": "row-impl"},
        {"run_id": "r-c", "role_target": "reviewer", "severity": "info", "message": "row-rev"},
    ]
    bash_db, bash_home, py_db, py_home = _paired_dbs(tmp_path_factory, "c1", rows)

    bash_rows = _bash_fetch("r-c", "implementer", bash_db, bash_home)
    _point_python_env(monkeypatch, py_db, py_home)
    py_rows = mcp_ops.get_operator_steering("r-c", "implementer")

    _assert_rows_equal(py_rows, bash_rows)
    assert sorted(row["message"] for row in py_rows) == ["row-any", "row-impl"]

    bash_db, bash_home, py_db, py_home = _paired_dbs(tmp_path_factory, "c2", rows)
    bash_rows = _bash_fetch("r-c", "reviewer", bash_db, bash_home)
    _point_python_env(monkeypatch, py_db, py_home)
    py_rows = mcp_ops.get_operator_steering("r-c", "reviewer")

    _assert_rows_equal(py_rows, bash_rows)
    assert sorted(row["message"] for row in py_rows) == ["row-any", "row-rev"]

    bash_db, bash_home, py_db, py_home = _paired_dbs(tmp_path_factory, "c3", rows)
    bash_rows = _bash_fetch("r-c", "any", bash_db, bash_home)
    _point_python_env(monkeypatch, py_db, py_home)
    py_rows = mcp_ops.get_operator_steering("r-c", "any")

    _assert_rows_equal(py_rows, bash_rows)
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
    bash_db, bash_home, py_db, py_home = _paired_dbs(tmp_path_factory, "d", rows)

    bash_rows = _bash_fetch("r-d", "any", bash_db, bash_home)
    _point_python_env(monkeypatch, py_db, py_home)
    py_rows = mcp_ops.get_operator_steering("r-d", "any")

    _assert_rows_equal(py_rows, bash_rows)
    assert [row["message"] for row in py_rows] == ["msg-D", "msg-E", "msg-C", "msg-B", "msg-A"]


def test_get_operator_steering_consumed_mark_parity(tmp_path_factory, monkeypatch):
    rows = [{
        "run_id": "r-e",
        "role_target": "any",
        "severity": "info",
        "message": "once",
    }]
    bash_db, bash_home, py_db, py_home = _paired_dbs(tmp_path_factory, "e", rows)
    wrapper = (
        f'. "{SH}"\n'
        'operator_steering_fetch_for "r-e" "any"\n'
        'operator_steering_fetch_for "r-e" "any"\n'
    )
    result = subprocess.run(
        ["bash", "-c", wrapper],
        env={**os.environ, "MINI_ORK_HOME": bash_home, "MINI_ORK_DB": bash_db},
        capture_output=True,
        text=True,
        check=True,
    )
    bash_rows = [json.loads(line) for line in result.stdout.splitlines() if line]

    _point_python_env(monkeypatch, py_db, py_home)
    py_first = mcp_ops.get_operator_steering("r-e", "any")
    py_second = mcp_ops.get_operator_steering("r-e", "any")

    _assert_rows_equal(py_first, bash_rows)
    assert py_second == []


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
    bash_db, bash_home, py_db, py_home = _paired_dbs(tmp_path_factory, "f", rows)

    bash_rows = _bash_fetch("r-f", "any", bash_db, bash_home)
    _point_python_env(monkeypatch, py_db, py_home)
    py_rows = mcp_ops.get_operator_steering("r-f", "any")

    _assert_rows_equal(py_rows, bash_rows)
    assert [row["message"] for row in py_rows] == ["row-fresh"]


def test_get_operator_steering_float_confidence_parity(tmp_path_factory, monkeypatch):
    rows = [{
        "run_id": "r-g",
        "role_target": "any",
        "severity": "info",
        "message": "float-row",
        "confidence": 0.123456789,
    }]
    bash_db, bash_home, py_db, py_home = _paired_dbs(tmp_path_factory, "g", rows)

    bash_rows = _bash_fetch("r-g", "any", bash_db, bash_home)
    _point_python_env(monkeypatch, py_db, py_home)
    py_rows = mcp_ops.get_operator_steering("r-g", "any")

    _assert_rows_equal(py_rows, bash_rows)
    assert abs(py_rows[0]["confidence"] - 0.123456789) <= 1e-6
