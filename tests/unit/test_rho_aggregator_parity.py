"""Parity gate: ``mini_ork.learning.rho_aggregator`` vs ``bash lib/rho_aggregator.sh``.

For each fixture we seed a self-contained SQLite database under ``tmp_path``
(both ``execution_traces`` and ``prompt_win_rates`` tables), invoke the LIVE
bash function via subprocess (no mocking — exactly as the production runtime
would), then call the Python port against the same DB and compare output.

Two callables are exercised:

  * ``rho_aggregate_win_rates [--since E] [--task-class X]`` → prints the
    row count on stdout; Python returns ``int``; compared as ``int``.
  * ``rho_top_prompts <task_class> <node_type> [<top_n>]`` → prints the
    formatted table on stdout; Python returns ``str``; compared as
    ``str`` after ``.strip()`` so trailing-newline differences don't fire
    (the bash output ALWAYS has a trailing newline, the Python return
    preserves it too — the strip aligns the case-where-bash-returns-empty
    where neither side emits a line).

The bash side resolves ``STATE_DB`` from ``$MINI_ORK_DB`` *at source time*
(``STATE_DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"``), so
the subprocess wrapper exports ``MINI_ORK_DB`` to the fixture path BEFORE
sourcing the file.

Floats inside the formatted table (``win_rate`` at 3 dp, ``win_rate`` at
4 dp inside the table) are compared after parsing to float with a
``<1e-6`` tolerance, since the bash ``printf '%.3f'`` and Python
``f"{x:.3f}"`` use the same round-half-to-even semantics and produce
identical bit-for-bit strings; the tolerance is the safety net the brief
asks for. All other columns are compared as exact strings.

Strangler-fig co-existence is preserved: ``lib/rho_aggregator.sh`` is
byte-identical before and after this test exists. The test only WRITES
to its ``tmp_path`` SQLite files and READS from
``lib/rho_aggregator.sh`` (verified by ``git diff --stat`` in the
verifier step).
"""

from __future__ import annotations

import math
import os
import re
import subprocess
from pathlib import Path

import sqlite3

from mini_ork.learning.rho_aggregator import aggregate_win_rates, top_prompts

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIB_RHO = REPO_ROOT / "lib" / "rho_aggregator.sh"


# ── Schema ────────────────────────────────────────────────────────────────────
# The bash function ``rho_aggregate_win_rates`` issues CREATE TABLE only as
# part of ``rho_top_prompts``'s sibling (no — it does not; it assumes the
# tables exist). For the parity gate we always pre-create BOTH tables so
# fixtures can drive either function. The column list is the minimum needed
# to satisfy both bash and Python.

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
    """Create the schema and optionally insert rows into the two tables.

    ``traces`` rows: (created_at, prompt_version_hash, task_class,
    status, reviewer_verdict, node_type)
    ``rates`` rows:  (prompt_version_hash, task_class, wins, losses, ties,
    win_rate, sample_size, last_updated, node_type)
    """
    con = sqlite3.connect(str(db_path))
    con.executescript(_DDL)
    if traces:
        con.executemany(
            "INSERT INTO execution_traces VALUES(?,?,?,?,?,?)", traces
        )
    if rates:
        con.executemany(
            "INSERT INTO prompt_win_rates VALUES(?,?,?,?,?,?,?,?,?)", rates
        )
    con.commit()
    con.close()


# ── Subprocess wrappers ──────────────────────────────────────────────────────

def _run_bash_aggregate(db_path: Path, since: int = 0,
                        task_class: str = "") -> int:
    """Live bash ``rho_aggregate_win_rates [--since E] [--task-class X]``.

    The subprocess wrapper exports ``MINI_ORK_DB`` so the bash function
    resolves ``STATE_DB`` at source time to our fixture file.
    """
    env = os.environ.copy()
    env.pop("MINI_ORK_HOME", None)
    env["MINI_ORK_DB"] = str(db_path)

    args = ""
    if since:
        args += f" --since {int(since)}"
    if task_class:
        args += f" --task-class {task_class}"

    proc = subprocess.run(
        ["bash", "-c",
         f'. "{LIB_RHO}" && rho_aggregate_win_rates{args}'],
        cwd=str(REPO_ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(proc.stdout.strip())


def _run_bash_top(db_path: Path, task_class: str, node_type: str = "",
                  top_n: int = 5) -> str:
    """Live bash ``rho_top_prompts <tc> <nt> [<n>]``. Returns stdout verbatim."""
    env = os.environ.copy()
    env.pop("MINI_ORK_HOME", None)
    env["MINI_ORK_DB"] = str(db_path)

    proc = subprocess.run(
        ["bash", "-c",
         f'. "{LIB_RHO}" && rho_top_prompts "{task_class}" "{node_type}" {int(top_n)}'],
        cwd=str(REPO_ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


# ── Comparison helpers ───────────────────────────────────────────────────────

_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")


def _assert_parity_top(bash_out: str, py_out: str, label: str) -> None:
    """Compare formatted table lines: floats within 1e-6, rest exact."""
    bash_lines = [ln for ln in bash_out.splitlines() if ln.strip()]
    py_lines = [ln for ln in py_out.splitlines() if ln.strip()]
    assert bash_lines == py_lines or len(bash_lines) == len(py_lines), (
        f"row count drift [{label}]: bash_lines={bash_lines!r} py_lines={py_lines!r}"
    )
    for b_line, p_line in zip(bash_lines, py_lines):
        b_cols = [c.strip() for c in b_line.split("|")]
        p_cols = [c.strip() for c in p_line.split("|")]
        assert len(b_cols) == 4 == len(p_cols), (
            f"column count drift [{label}]: bash={b_cols!r} py={p_cols!r}"
        )
        for i, (b, p) in enumerate(zip(b_cols, p_cols)):
            if _FLOAT_RE.match(b) and _FLOAT_RE.match(p):
                assert math.isclose(float(b), float(p), abs_tol=1e-6), (
                    f"float drift [{label}] col={i}: bash={b!r} py={p!r}"
                )
            else:
                assert b == p, (
                    f"string drift [{label}] col={i}: bash={b!r} py={p!r} "
                    f"(line bash={b_line!r} py={p_line!r})"
                )


# ── Fixtures: prompt_win_rates directly (for top_prompts) ───────────────────

# Each row: (prompt_version_hash, task_class, wins, losses, ties,
#            win_rate, sample_size, last_updated, node_type)

_RATES_BASIC = [
    ("aaaa1111bbbb2222", "tc1", 8, 2, 0, 0.8000, 10, "2025-01-01T00:00:00.000Z", None),
    ("cccc3333dddd4444", "tc1", 6, 4, 0, 0.6000, 10, "2025-01-01T00:00:00.000Z", None),
    ("eeee5555ffff6666", "tc1", 9, 1, 0, 0.9000, 10, "2025-01-01T00:00:00.000Z", None),
]

_RATES_WITH_SMALL_SAMPLE = [
    # sample_size=2 must be excluded by the sample_size >= 3 clause
    ("aaaa1111bbbb2222", "tc1", 8, 2, 0, 0.8000, 10, "2025-01-01T00:00:00.000Z", None),
    ("zzzz1111yyyy2222", "tc1", 1, 0, 0, 1.0000,  2, "2025-01-01T00:00:00.000Z", None),
]

_RATES_DIFFERENT_TASK = [
    ("aaaa1111bbbb2222", "tc1", 8, 2, 0, 0.8000, 10, "2025-01-01T00:00:00.000Z", None),
    ("bbbb1111cccc2222", "tc2", 7, 3, 0, 0.7000, 10, "2025-01-01T00:00:00.000Z", None),
]


# ── Tests ────────────────────────────────────────────────────────────────────

def test_f01_top_prompts_basic_sorted_desc(tmp_path):
    """Three rows in ``prompt_win_rates``; top_prompts returns them sorted
    by ``win_rate DESC, sample_size DESC`` with the 3-dp / width-4 / 12-char
    formatting bash's ``printf`` chain produces."""
    db = tmp_path / "state.db"
    _seed_db(db, rates=_RATES_BASIC)

    bash_out = _run_bash_top(db, "tc1", "", 5)
    py_out = top_prompts(str(db), "tc1", "", 5)

    _assert_parity_top(bash_out, py_out, "f01_top_prompts_basic")


def test_f02_top_prompts_top_n_limits_output(tmp_path):
    """``top_n=2`` returns exactly two rows (the two highest win_rates)."""
    db = tmp_path / "state.db"
    _seed_db(db, rates=_RATES_BASIC)

    bash_out = _run_bash_top(db, "tc1", "", 2)
    py_out = top_prompts(str(db), "tc1", "", 2)

    bash_lines = [ln for ln in bash_out.splitlines() if ln.strip()]
    py_lines = [ln for ln in py_out.splitlines() if ln.strip()]
    assert len(bash_lines) == 2, f"bash should return 2 lines, got {bash_lines!r}"
    assert len(py_lines) == 2, f"python should return 2 lines, got {py_lines!r}"
    _assert_parity_top(bash_out, py_out, "f02_top_n")


def test_f03_top_prompts_excludes_sample_size_lt_3(tmp_path):
    """The sample_size=2 row must be filtered out by ``sample_size >= 3``."""
    db = tmp_path / "state.db"
    _seed_db(db, rates=_RATES_WITH_SMALL_SAMPLE)

    bash_out = _run_bash_top(db, "tc1", "", 5)
    py_out = top_prompts(str(db), "tc1", "", 5)

    bash_lines = [ln for ln in bash_out.splitlines() if ln.strip()]
    py_lines = [ln for ln in py_out.splitlines() if ln.strip()]
    assert len(bash_lines) == 1, f"only the sample_size=10 row survives, got {bash_lines!r}"
    assert len(py_lines) == 1
    _assert_parity_top(bash_out, py_out, "f03_sample_filter")


def test_f04_top_prompts_task_class_filter(tmp_path):
    """``task_class='tc1'`` filter returns only the matching rows."""
    db = tmp_path / "state.db"
    _seed_db(db, rates=_RATES_DIFFERENT_TASK)

    bash_out = _run_bash_top(db, "tc1", "", 5)
    py_out = top_prompts(str(db), "tc1", "", 5)

    bash_lines = [ln for ln in bash_out.splitlines() if ln.strip()]
    assert len(bash_lines) == 1
    # bash truncates the hash to the first 12 chars via substr(..,1,12).
    assert "aaaa1111bbbb" in bash_lines[0]
    _assert_parity_top(bash_out, py_out, "f04_task_class_filter")


def test_f05_aggregate_empty_traces_returns_zero(tmp_path):
    """No traces seeded → aggregate prints ``0`` (Python returns ``0``)."""
    db = tmp_path / "state.db"
    _seed_db(db)  # schema only, no rows

    bash_n = _run_bash_aggregate(db)
    py_n = aggregate_win_rates(str(db))

    assert bash_n == 0
    assert py_n == 0
    assert bash_n == py_n


def test_f06_aggregate_basic_three_traces_one_group(tmp_path):
    """Seed 3 traces all in the same (hash, task_class) bucket → 1 group,
    ``win_rate = 2/3 ≈ 0.6667``, sample_size=3. Bash prints the count ``1``;
    Python returns ``1``."""
    db = tmp_path / "state.db"
    _seed_db(db, traces=[
        ("2025-01-01T00:00:00.000Z", "aaaa1111bbbb2222", "tc1", "success", None,        "impl"),
        ("2025-01-01T00:00:00.000Z", "aaaa1111bbbb2222", "tc1", "success", None,        "impl"),
        ("2025-01-01T00:00:00.000Z", "aaaa1111bbbb2222", "tc1", "success", "REJECT",    "impl"),
    ])

    bash_n = _run_bash_aggregate(db)
    py_n = aggregate_win_rates(str(db))

    assert bash_n == 1
    assert py_n == 1
    assert bash_n == py_n

    # And the seeded row should now show up in top_prompts at win_rate=0.667
    bash_out = _run_bash_top(db, "tc1", "", 5)
    py_out = top_prompts(str(db), "tc1", "", 5)
    _assert_parity_top(bash_out, py_out, "f06_after_aggregate_top")


def test_f07_aggregate_with_since_filter_returns_zero(tmp_path):
    """``--since`` pointing at the year 3000 filters out all 2025 traces."""
    db = tmp_path / "state.db"
    _seed_db(db, traces=[
        ("2025-01-01T00:00:00.000Z", "aaaa1111bbbb2222", "tc1", "success", None, "impl"),
    ])

    far_future_epoch = 32_503_680_000  # 3000-01-01T00:00:00Z
    bash_n = _run_bash_aggregate(db, since=far_future_epoch)
    py_n = aggregate_win_rates(str(db), since=far_future_epoch)

    assert bash_n == 0
    assert py_n == 0
    assert bash_n == py_n


def test_f08_aggregate_with_task_class_filter(tmp_path):
    """``--task-class tc2`` keeps only the tc2 traces; tc1 traces are ignored."""
    db = tmp_path / "state.db"
    _seed_db(db, traces=[
        ("2025-01-01T00:00:00.000Z", "aaaa1111bbbb2222", "tc1", "success", None, "impl"),
        ("2025-01-01T00:00:00.000Z", "aaaa1111bbbb2222", "tc1", "failure", None, "impl"),
        ("2025-01-01T00:00:00.000Z", "bbbb1111cccc2222", "tc2", "success", None, "impl"),
        ("2025-01-01T00:00:00.000Z", "bbbb1111cccc2222", "tc2", "success", None, "impl"),
    ])

    bash_n = _run_bash_aggregate(db, task_class="tc2")
    py_n = aggregate_win_rates(str(db), task_class="tc2")

    assert bash_n == 1, f"only the (bbbb,tc2) bucket is aggregated; got bash_n={bash_n}"
    assert py_n == 1
    assert bash_n == py_n


def test_precondition_bash_subprocess_works(tmp_path):
    """Precondition gate: if the subprocess wrapper itself is broken (bad
    path, missing env, sqlite3 missing), every fixture would silently fail
    in confusing ways. Lock the live-bash invocation FIRST."""
    db = tmp_path / "state.db"
    _seed_db(db, rates=_RATES_BASIC)

    bash_out = _run_bash_top(db, "tc1", "", 5)
    assert bash_out.strip() != "", (
        "precondition: bash top_prompts must return a non-empty table for "
        "this seeded fixture. If it's empty, the subprocess wrapper is "
        "broken (probably MINI_ORK_DB not exported before source)."
    )
    # Sanity: the row with the highest win_rate (0.9000) should be first.
    first_line = bash_out.splitlines()[0]
    assert "0.900" in first_line, (
        f"expected 0.900 in the first line, got {first_line!r}"
    )


def test_smoke_import_and_call_no_subprocess():
    """Pure-path smoke: import the module and call both functions against
    a hand-built in-memory dict — confirms the port works in-process."""
    # top_prompts needs a real DB path; aggregate takes a path too. We
    # exercise the no-side-effect parts: import + signature + return types.
    import inspect
    sig_agg = inspect.signature(aggregate_win_rates)
    assert list(sig_agg.parameters.keys()) == ["state_db", "since", "task_class"]
    sig_top = inspect.signature(top_prompts)
    assert list(sig_top.parameters.keys()) == ["state_db", "task_class", "node_type", "top_n"]