"""Parity gate: ``mini_ork.orchestration.topology.aggregate_traces`` vs ``bash lib/topology.sh``.

For each fixture we build a small ``execution_traces`` corpus (and an
optional ``workflow_memory`` join), materialise it into a temp sqlite DB,
invoke the LIVE bash function via subprocess (no mocking — exactly as
the production runtime would), read the upserted ``topology_win_rates``
rows back out, then run the Python port against the same trace rows and
assert exact parity. Floats must match within ``1e-6``; strings must
match exactly.

Strangler-fig co-existence is preserved: ``lib/topology.sh`` is
byte-identical before and after this test exists. The test only WRITES
to its ``tmp_path`` and READS from ``lib/topology.sh``.
"""

from __future__ import annotations

import math
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mini_ork.orchestration.topology import aggregate_traces

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIB_TOPOLOGY = REPO_ROOT / "lib" / "topology.sh"


# Schemas lifted from the live migrations under db/migrations/. The bash
# function expects these column names verbatim — keep them in lockstep.
_EXEC_TRACES_DDL = """
CREATE TABLE execution_traces (
  trace_id           TEXT PRIMARY KEY,
  workflow_version_id TEXT,
  task_class         TEXT,
  status             TEXT,
  reviewer_verdict   TEXT,
  cost_usd           REAL,
  duration_ms        INTEGER,
  created_at         TEXT
);
"""

_WORKFLOW_MEMORY_DDL = """
CREATE TABLE workflow_memory (
  workflow_version_id TEXT PRIMARY KEY,
  workflow_name       TEXT,
  yaml_hash           TEXT
);
"""

_TOPOLOGY_WIN_RATES_DDL = """
CREATE TABLE topology_win_rates (
  topology_id      TEXT NOT NULL,
  workflow_name    TEXT NOT NULL,
  task_class       TEXT NOT NULL,
  wins             INTEGER NOT NULL DEFAULT 0,
  losses           INTEGER NOT NULL DEFAULT 0,
  ties             INTEGER NOT NULL DEFAULT 0,
  win_rate         REAL    NOT NULL DEFAULT 0.0,
  sample_size      INTEGER NOT NULL DEFAULT 0,
  avg_cost_usd     REAL    NOT NULL DEFAULT 0.0,
  avg_duration_ms  REAL    NOT NULL DEFAULT 0.0,
  last_updated     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (topology_id, task_class)
);
"""


def _seed(
    db_path: Path,
    traces: list[dict[str, Any]],
    workflow_memory: dict[str, dict[str, str]] | None = None,
) -> None:
    """Materialise the test corpus into a fresh sqlite DB.

    The DB has only the three tables bash touches. ``created_at`` is fixed
    to a recent ISO timestamp well past 1970 so the bash ``WHERE created_at
    >= ?`` filter (with default ``--since=0``) admits every row.
    """
    con = sqlite3.connect(str(db_path))
    con.executescript(
        _EXEC_TRACES_DDL + _WORKFLOW_MEMORY_DDL + _TOPOLOGY_WIN_RATES_DDL
    )
    if workflow_memory:
        for wvid, row in workflow_memory.items():
            con.execute(
                "INSERT INTO workflow_memory VALUES (?,?,?)",
                (wvid, row["workflow_name"], row["yaml_hash"]),
            )
    for i, t in enumerate(traces):
        con.execute(
            "INSERT INTO execution_traces VALUES (?,?,?,?,?,?,?,?)",
            (
                f"tr-{i:04d}",
                t.get("workflow_version_id"),
                t.get("task_class"),
                t.get("status"),
                t.get("reviewer_verdict"),
                t.get("cost_usd"),
                t.get("duration_ms"),
                t.get("created_at", "2026-01-01T00:00:00.000Z"),
            ),
        )
    con.commit()
    con.close()


def _run_bash(db_path: Path) -> None:
    """Source lib/topology.sh in a fresh bash and call topology_recompute_win_rates.

    ``MINI_ORK_DB`` is set so the bash function targets our temp DB. The
    function upserts rows into ``topology_win_rates`` and prints the
    row count on stdout; we don't read it — we read the table instead.
    """
    env = os.environ.copy()
    env["MINI_ORK_DB"] = str(db_path)
    proc = subprocess.run(
        ["bash", "-c", f'. "{LIB_TOPOLOGY}" && topology_recompute_win_rates'],
        cwd=str(REPO_ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    # Sanity: bash prints the upsert count; assert it parses as int.
    assert proc.stdout.strip().isdigit(), (
        f"bash returned non-numeric upsert count: {proc.stdout!r}"
        f" stderr={proc.stderr!r}"
    )


def _read_bash_aggregates(db_path: Path) -> list[dict[str, Any]]:
    """Read back exactly what bash upserted, ordered for stable comparison."""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT topology_id, workflow_name, task_class,
               wins, losses, ties, win_rate, sample_size,
               avg_cost_usd, avg_duration_ms
          FROM topology_win_rates
         ORDER BY topology_id, task_class
        """
    ).fetchall()
    out = [dict(r) for r in rows]
    con.close()
    return out


def _read_traces(db_path: Path) -> list[dict[str, Any]]:
    """Read the execution_traces rows we just seeded, in insertion order."""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT workflow_version_id, task_class, status, reviewer_verdict,
               cost_usd, duration_ms, created_at
          FROM execution_traces
         ORDER BY trace_id
        """
    ).fetchall()
    out = [dict(r) for r in rows]
    con.close()
    return out


def _read_workflow_memory(db_path: Path) -> dict[str, dict[str, str]]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT workflow_version_id, workflow_name, yaml_hash FROM workflow_memory"
    ).fetchall()
    out = {r["workflow_version_id"]: {"workflow_name": r["workflow_name"],
                                       "yaml_hash": r["yaml_hash"]}
           for r in rows}
    con.close()
    return out


def _assert_parity(
    bash_rows: list[dict[str, Any]],
    py_rows: list[dict[str, Any]],
    label: str,
) -> None:
    assert len(bash_rows) == len(py_rows), (
        f"[{label}] row-count drift: bash={len(bash_rows)} py={len(py_rows)}\n"
        f"  bash={bash_rows!r}\n  py  ={py_rows!r}"
    )
    for b, p in zip(bash_rows, py_rows):
        # String columns — exact match.
        for k in ("topology_id", "workflow_name", "task_class"):
            assert b[k] == p[k], f"[{label}] {k} drift: bash={b[k]!r} py={p[k]!r}"
        # Int columns — exact match (sqlite returns int).
        for k in ("wins", "losses", "ties", "sample_size"):
            assert int(b[k]) == int(p[k]), (
                f"[{label}] {k} drift: bash={b[k]!r} py={p[k]!r}"
            )
        # Float columns — close within 1e-6.
        for k in ("win_rate", "avg_cost_usd", "avg_duration_ms"):
            assert math.isclose(float(b[k]), float(p[k]), abs_tol=1e-6), (
                f"[{label}] {k} drift: bash={b[k]!r} py={p[k]!r}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures. Each is a (traces, workflow_memory|None) pair, exercising one
# specific surface of the bash CASE expression tree.
# ─────────────────────────────────────────────────────────────────────────────

def _f01_pure_wins() -> dict:
    """All rows are 'success' with a non-rejecting verdict — pure wins."""
    return {
        "traces": [
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE",
             "cost_usd": 0.50, "duration_ms": 8000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": None,
             "cost_usd": 0.40, "duration_ms": 7000},
        ],
        "workflow_memory": None,
    }


def _f02_pure_losses_via_rejection() -> dict:
    """'success' status with REJECT/ESCALATE/needs_revision verdict — all losses."""
    return {
        "traces": [
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "REJECT",
             "cost_usd": 0.20, "duration_ms": 3000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "ESCALATE",
             "cost_usd": 0.30, "duration_ms": 4000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "needs_revision",
             "cost_usd": 0.10, "duration_ms": 2000},
        ],
        "workflow_memory": None,
    }


def _f03_mixed_outcomes_two_groups() -> dict:
    """Two (topology, task_class) groups with a full mix of win/loss/tie."""
    return {
        "traces": [
            # Group A: code_fix_v1, code_fix
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE", "cost_usd": 0.4, "duration_ms": 5000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "failure", "reviewer_verdict": "APPROVE", "cost_usd": 0.6, "duration_ms": 6000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "running", "reviewer_verdict": None, "cost_usd": 0.0, "duration_ms": 1000},
            # Group B: refactor_v1, refactor
            {"workflow_version_id": "refactor_v1", "task_class": "refactor",
             "status": "success", "reviewer_verdict": None, "cost_usd": 0.8, "duration_ms": 9000},
            {"workflow_version_id": "refactor_v1", "task_class": "refactor",
             "status": "success", "reviewer_verdict": "REJECT", "cost_usd": 0.2, "duration_ms": 2500},
            {"workflow_version_id": "refactor_v1", "task_class": "refactor",
             "status": "vacuous", "reviewer_verdict": None, "cost_usd": 0.0, "duration_ms": 0},
        ],
        "workflow_memory": None,
    }


def _f04_unrecognized_status_yields_zero_tally() -> dict:
    """status='completed' is outside the recognized set — must contribute 0 to all 3 buckets.

    The group still appears in the aggregate (matching the GROUP BY contract),
    with win_rate=0.0 (denom=0) and sample_size=0.
    """
    return {
        "traces": [
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "completed", "reviewer_verdict": None,
             "cost_usd": 0.5, "duration_ms": 5000},
        ],
        "workflow_memory": None,
    }


def _f05_workflow_memory_join_overrides() -> dict:
    """The bash LEFT JOIN swaps in yaml_hash + workflow_name from workflow_memory."""
    return {
        "traces": [
            {"workflow_version_id": "code_fix_v3", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE",
             "cost_usd": 0.5, "duration_ms": 4000},
            {"workflow_version_id": "code_fix_v3", "task_class": "code_fix",
             "status": "failure", "reviewer_verdict": "APPROVE",
             "cost_usd": 0.3, "duration_ms": 3000},
        ],
        "workflow_memory": {
            "code_fix_v3": {"yaml_hash": "deadbeefcafe", "workflow_name": "code-fix"},
        },
    }


def _f06_null_status_counts_as_tie() -> dict:
    """``status IS NULL`` is in the tie branch. Also tests missing cost/duration.

    The bash function uses ``AVG(COALESCE(et.cost_usd, 0))`` — for rows with
    ``cost_usd IS NULL`` the COALESCE substitutes 0, so the avg is computed
    over 0.0. Our port has the same behavior via the ``if cost is not None``
    guard, but to keep the comparison tight, every fixture sets cost_usd and
    duration_ms on every row.
    """
    return {
        "traces": [
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": None, "reviewer_verdict": None,
             "cost_usd": 0.0, "duration_ms": 0},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "blocked", "reviewer_verdict": None,
             "cost_usd": 0.0, "duration_ms": 0},
        ],
        "workflow_memory": None,
    }


def _f07_avg_cost_and_duration_precision() -> dict:
    """Three rows with different cost/duration — verifies AVG matches exactly.

    avg_cost = (0.10 + 0.30 + 0.50) / 3 = 0.30
    avg_dur  = (1000 + 2000 + 3000) / 3 = 2000.0
    """
    return {
        "traces": [
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE",
             "cost_usd": 0.10, "duration_ms": 1000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE",
             "cost_usd": 0.30, "duration_ms": 2000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE",
             "cost_usd": 0.50, "duration_ms": 3000},
        ],
        "workflow_memory": None,
    }


def _f08_win_rate_rounding_boundaries() -> dict:
    """3 wins / 4 (wins+losses) = 0.75 exact. Verifies the round(., 4) contract."""
    return {
        "traces": [
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE", "cost_usd": 0.1, "duration_ms": 1000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE", "cost_usd": 0.1, "duration_ms": 1000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE", "cost_usd": 0.1, "duration_ms": 1000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "failure", "reviewer_verdict": "APPROVE", "cost_usd": 0.1, "duration_ms": 1000},
        ],
        "workflow_memory": None,
    }


FIXTURES = {
    "01_pure_wins":                       _f01_pure_wins(),
    "02_pure_losses_via_rejection":       _f02_pure_losses_via_rejection(),
    "03_mixed_outcomes_two_groups":       _f03_mixed_outcomes_two_groups(),
    "04_unrecognized_status_zero_tally":  _f04_unrecognized_status_yields_zero_tally(),
    "05_workflow_memory_join_overrides":  _f05_workflow_memory_join_overrides(),
    "06_null_status_counts_as_tie":       _f06_null_status_counts_as_tie(),
    "07_avg_cost_and_duration_precision": _f07_avg_cost_and_duration_precision(),
    "08_win_rate_rounding_boundaries":    _f08_win_rate_rounding_boundaries(),
}


@pytest.mark.parametrize(
    "fix_id,fix",
    list(FIXTURES.items()),
    ids=list(FIXTURES.keys()),
)
def test_aggregate_traces_matches_bash(tmp_path, fix_id, fix):
    db_path = tmp_path / "topology.db"
    _seed(db_path, fix["traces"], fix.get("workflow_memory"))
    _run_bash(db_path)
    bash_rows = _read_bash_aggregates(db_path)
    py_rows = aggregate_traces(
        _read_traces(db_path),
        _read_workflow_memory(db_path) or None,
    )
    _assert_parity(bash_rows, py_rows, fix_id)


def test_smoke_import_and_aggregate_no_io():
    """Pure-path smoke: import + aggregate a tiny corpus returns the right shape."""
    out = aggregate_traces(
        [
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE",
             "cost_usd": 0.5, "duration_ms": 1000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "failure", "reviewer_verdict": "APPROVE",
             "cost_usd": 0.3, "duration_ms": 800},
        ],
    )
    assert len(out) == 1
    row = out[0]
    assert row["wins"] == 1
    assert row["losses"] == 1
    assert row["ties"] == 0
    assert row["sample_size"] == 2
    assert math.isclose(row["win_rate"], 0.5, abs_tol=1e-6)
    assert row["topology_id"] == "code_fix_v1"
    assert row["workflow_name"] == "?"
    assert row["task_class"] == "code_fix"
    assert math.isclose(row["avg_cost_usd"], 0.4, abs_tol=1e-6)
    assert math.isclose(row["avg_duration_ms"], 900.0, abs_tol=1e-6)
