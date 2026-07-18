"""Standalone unit tests for ``mini_ork.ported.rho_aggregator``.

Replaces the former ``test_rho_aggregator_parity.py`` (which shelled out to
``lib/rho_aggregator.sh`` via ``subprocess`` and diffed byte-for-byte). The
bash lib was retired once ``mini_ork_reflect`` stopped shelling out to it, so
its parity oracle is gone — these tests assert against **golden expected
values** instead, captured from the parity-verified native output.

Coverage mirrors the retired parity fixtures f01–f08 and adds three cases the
parity gate never had: upsert idempotency, the explicit ``needs_revision`` /
``running`` win-loss-tie rubric, and the ``node_type`` filter in
``top_prompts``. RHO reference: arXiv:2606.05922.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mini_ork.ported.rho_aggregator import aggregate_win_rates, top_prompts

# ── Schema (identical to the retired parity fixture) ─────────────────────────
_DDL = """
CREATE TABLE execution_traces(
    created_at           TEXT,
    prompt_version_hash  TEXT,
    task_class           TEXT,
    status               TEXT,
    reviewer_verdict     TEXT,
    node_type            TEXT);
CREATE TABLE prompt_win_rates(
    prompt_version_hash  TEXT,
    task_class           TEXT,
    wins                 INTEGER,
    losses               INTEGER,
    ties                 INTEGER,
    win_rate             REAL,
    sample_size          INTEGER,
    last_updated         TEXT,
    node_type            TEXT,
    PRIMARY KEY (prompt_version_hash, task_class));
"""


def _seed_db(db_path: Path, traces: list[tuple] | None = None,
             rates: list[tuple] | None = None) -> None:
    """Create the schema and optionally insert rows.

    ``traces``: (created_at, prompt_version_hash, task_class, status,
    reviewer_verdict, node_type). ``rates``: (prompt_version_hash, task_class,
    wins, losses, ties, win_rate, sample_size, last_updated, node_type).
    """
    con = sqlite3.connect(str(db_path))
    con.executescript(_DDL)
    if traces:
        con.executemany("INSERT INTO execution_traces VALUES(?,?,?,?,?,?)", traces)
    if rates:
        con.executemany("INSERT INTO prompt_win_rates VALUES(?,?,?,?,?,?,?,?,?)", rates)
    con.commit()
    con.close()


_RATES_BASIC = [
    ("aaaa1111bbbb2222", "tc1", 8, 2, 0, 0.8000, 10, "2025-01-01T00:00:00.000Z", None),
    ("cccc3333dddd4444", "tc1", 6, 4, 0, 0.6000, 10, "2025-01-01T00:00:00.000Z", None),
    ("eeee5555ffff6666", "tc1", 9, 1, 0, 0.9000, 10, "2025-01-01T00:00:00.000Z", None),
]
_RATES_WITH_SMALL_SAMPLE = [
    ("aaaa1111bbbb2222", "tc1", 8, 2, 0, 0.8000, 10, "2025-01-01T00:00:00.000Z", None),
    ("zzzz1111yyyy2222", "tc1", 1, 0, 0, 1.0000,  2, "2025-01-01T00:00:00.000Z", None),
]
_RATES_DIFFERENT_TASK = [
    ("aaaa1111bbbb2222", "tc1", 8, 2, 0, 0.8000, 10, "2025-01-01T00:00:00.000Z", None),
    ("bbbb1111cccc2222", "tc2", 7, 3, 0, 0.7000, 10, "2025-01-01T00:00:00.000Z", None),
]


# ── top_prompts (f01–f04) ─────────────────────────────────────────────────────

def test_f01_top_prompts_basic_sorted_desc(tmp_path):
    """Three rows → sorted by win_rate DESC with the 3-dp / width-4 / 12-char
    printf formatting (golden captured from parity-verified native output)."""
    db = tmp_path / "state.db"
    _seed_db(db, rates=_RATES_BASIC)
    assert top_prompts(str(db), "tc1", "", 5) == (
        "0.900 |   10 | eeee5555ffff | ?\n"
        "0.800 |   10 | aaaa1111bbbb | ?\n"
        "0.600 |   10 | cccc3333dddd | ?\n"
    )


def test_f02_top_prompts_top_n_limits_output(tmp_path):
    """top_n=2 returns exactly the two highest win_rates."""
    db = tmp_path / "state.db"
    _seed_db(db, rates=_RATES_BASIC)
    assert top_prompts(str(db), "tc1", "", 2) == (
        "0.900 |   10 | eeee5555ffff | ?\n"
        "0.800 |   10 | aaaa1111bbbb | ?\n"
    )


def test_f03_top_prompts_excludes_sample_size_lt_3(tmp_path):
    """sample_size=2 row is filtered by the ``sample_size >= 3`` clause."""
    db = tmp_path / "state.db"
    _seed_db(db, rates=_RATES_WITH_SMALL_SAMPLE)
    out = top_prompts(str(db), "tc1", "", 5)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "aaaa1111bbbb" in lines[0]
    assert "zzzz1111yyyy" not in out


def test_f04_top_prompts_task_class_filter(tmp_path):
    """task_class='tc1' returns only the matching row (hash truncated to 12)."""
    db = tmp_path / "state.db"
    _seed_db(db, rates=_RATES_DIFFERENT_TASK)
    out = top_prompts(str(db), "tc1", "", 5)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "aaaa1111bbbb" in lines[0]
    assert "bbbb1111cccc" not in out


def test_top_prompts_node_type_filter_null_passes(tmp_path):
    """``(node_type IS NULL OR node_type=X)`` — a NULL-node_type row passes any
    node_type filter (the retired parity gate never exercised this branch)."""
    db = tmp_path / "state.db"
    _seed_db(db, rates=[
        ("aaaa1111bbbb2222", "tc1", 8, 2, 0, 0.8, 10, "2025-01-01T00:00:00.000Z", None),
        ("cccc3333dddd4444", "tc1", 6, 4, 0, 0.6, 10, "2025-01-01T00:00:00.000Z", "impl"),
        ("eeee5555ffff6666", "tc1", 9, 1, 0, 0.9, 10, "2025-01-01T00:00:00.000Z", "review"),
    ])
    out = top_prompts(str(db), "tc1", "impl", 5)
    # NULL-node_type row (aaaa) + the impl row (cccc) match; review row does not.
    assert "aaaa1111bbbb" in out and "cccc3333dddd" in out
    assert "eeee5555ffff" not in out


# ── aggregate_win_rates (f05–f08) ─────────────────────────────────────────────

def test_f05_aggregate_empty_traces_returns_zero(tmp_path):
    """No traces → 0 groups upserted."""
    db = tmp_path / "state.db"
    _seed_db(db)
    assert aggregate_win_rates(str(db)) == 0


def test_f06_aggregate_basic_three_traces_one_group(tmp_path):
    """3 traces in one (hash, task_class) bucket → 1 group, win_rate=2/3≈0.6667,
    sample_size=3 (2 success, 1 REJECT-loss)."""
    db = tmp_path / "state.db"
    _seed_db(db, traces=[
        ("2025-01-01T00:00:00.000Z", "aaaa1111bbbb2222", "tc1", "success", None,     "impl"),
        ("2025-01-01T00:00:00.000Z", "aaaa1111bbbb2222", "tc1", "success", None,     "impl"),
        ("2025-01-01T00:00:00.000Z", "aaaa1111bbbb2222", "tc1", "success", "REJECT", "impl"),
    ])
    assert aggregate_win_rates(str(db)) == 1
    con = sqlite3.connect(str(db))
    row = con.execute(
        "SELECT wins, losses, ties, win_rate, sample_size FROM prompt_win_rates"
    ).fetchall()
    con.close()
    assert row == [(2, 1, 0, 0.6667, 3)]
    assert top_prompts(str(db), "tc1", "", 5) == "0.667 |    3 | aaaa1111bbbb | ?\n"


def test_f07_aggregate_since_filter_excludes_old(tmp_path):
    """``since`` at year 3000 filters out all 2025 traces → 0."""
    db = tmp_path / "state.db"
    _seed_db(db, traces=[
        ("2025-01-01T00:00:00.000Z", "aaaa1111bbbb2222", "tc1", "success", None, "impl"),
    ])
    assert aggregate_win_rates(str(db), since=32_503_680_000) == 0  # 3000-01-01Z


def test_f08_aggregate_task_class_filter(tmp_path):
    """``task_class='tc2'`` aggregates only the tc2 bucket."""
    db = tmp_path / "state.db"
    _seed_db(db, traces=[
        ("2025-01-01T00:00:00.000Z", "aaaa1111bbbb2222", "tc1", "success", None, "impl"),
        ("2025-01-01T00:00:00.000Z", "aaaa1111bbbb2222", "tc1", "failure", None, "impl"),
        ("2025-01-01T00:00:00.000Z", "bbbb1111cccc2222", "tc2", "success", None, "impl"),
        ("2025-01-01T00:00:00.000Z", "bbbb1111cccc2222", "tc2", "success", None, "impl"),
    ])
    assert aggregate_win_rates(str(db), task_class="tc2") == 1
    con = sqlite3.connect(str(db))
    got = con.execute(
        "SELECT prompt_version_hash FROM prompt_win_rates"
    ).fetchall()
    con.close()
    assert got == [("bbbb1111cccc2222",)]


# ── Rubric + idempotency (new — beyond the parity gate) ───────────────────────

def test_win_loss_tie_rubric_explicit(tmp_path):
    """Exercise every rubric branch in one bucket:
      success + no verdict     → win
      success + needs_revision → loss
      failure                  → loss
      running                  → tie
    win_rate = wins / (wins + losses) = 1 / (1 + 2) = 0.3333."""
    db = tmp_path / "state.db"
    _seed_db(db, traces=[
        ("2025-01-01T00:00:00.000Z", "hhhh1111hhhh2222", "tc1", "success", None,             "impl"),
        ("2025-01-01T00:00:00.000Z", "hhhh1111hhhh2222", "tc1", "success", "needs_revision", "impl"),
        ("2025-01-01T00:00:00.000Z", "hhhh1111hhhh2222", "tc1", "failure", None,             "impl"),
        ("2025-01-01T00:00:00.000Z", "hhhh1111hhhh2222", "tc1", "running", None,             "impl"),
    ])
    assert aggregate_win_rates(str(db)) == 1
    con = sqlite3.connect(str(db))
    row = con.execute(
        "SELECT wins, losses, ties, win_rate, sample_size FROM prompt_win_rates"
    ).fetchone()
    con.close()
    assert row == (1, 2, 1, 0.3333, 4)


def test_aggregate_is_idempotent(tmp_path):
    """Re-aggregating the same traces upserts (not appends) — the row keyed by
    (hash, task_class) stays singular with identical values."""
    db = tmp_path / "state.db"
    traces = [
        ("2025-01-01T00:00:00.000Z", "aaaa1111bbbb2222", "tc1", "success", None, "impl"),
        ("2025-01-01T00:00:00.000Z", "aaaa1111bbbb2222", "tc1", "failure", None, "impl"),
    ]
    _seed_db(db, traces=traces)
    first = aggregate_win_rates(str(db))
    second = aggregate_win_rates(str(db))
    assert first == second == 1
    con = sqlite3.connect(str(db))
    rows = con.execute(
        "SELECT wins, losses, ties, win_rate, sample_size FROM prompt_win_rates"
    ).fetchall()
    con.close()
    assert rows == [(1, 1, 0, 0.5, 2)]  # single row, not two


def test_signatures_stable():
    """Lock the public API the reflect side-channel calls."""
    import inspect
    assert list(inspect.signature(aggregate_win_rates).parameters) == [
        "state_db", "since", "task_class"]
    assert list(inspect.signature(top_prompts).parameters) == [
        "state_db", "task_class", "node_type", "top_n"]
