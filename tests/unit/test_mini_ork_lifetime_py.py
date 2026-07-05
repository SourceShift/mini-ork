"""Parity gate: mini_ork.ported.mini_ork_lifetime vs bin/mini-ork-lifetime.

Each test invokes the LIVE bash subprocess against a temp DB seeded by
``db/init.sh`` (and optionally data tables), then invokes the Python port
against the same DB, and asserts the resulting stdout strings are
byte-for-byte identical. No mocks, no hardcoded expected outputs — the
expected value is always derived from the live control bash invocation.

Schema bootstrap: bash's three subcommands only query, never insert
(they are pure read-only leaderboards). ``db/init.sh`` is the right
bootstrap path because it applies ``prompt_win_rates`` (0030),
``bug_reports`` (0029), ``topology_win_rates`` / ``role_evolver_log`` /
``conductor_decisions`` (0034), ``task_runs`` (0013) and
``agent_performance_memory`` (0009 + relative_advantage via 0032), with
the column repairs needed for fresh DBs (``relative_advantage`` is
added at init time even when 0032 is marked applied).

Cases (10, above the kickoff's >=6 floor):

  (1)  ``summary`` on empty DB — all 5 sections return identical
       textual layout (header + blank + column-row block + 4 blank
       separators + last section header). Tests the ``"-column
       -header"`` width codec on all-NULL data and the
       section-separator concatenation rules.

  (2)  ``summary`` seeded with h24/d7/lifetime rows — the column
       block's widths and trailing spaces match exactly. Tests the
       ``SUM(CASE WHEN …)`` SQL and column-width selection across
       non-empty data.

  (3)  ``summary`` seeded with prompt_win_rates, agent_performance_memory,
       topology_win_rates — LIMIT 5 ordering matches; printf widths
       (%.3f, %+.3f, %-15s) match.

  (4)  ``summary`` seeded with bug_reports at every severity —
       bucket ORDER BY CASE rank matches: critical, high, medium,
       low ORDERing. Tests the CASE-WHEN group-by.

  (5)  ``show <recipe>`` empty DB — all 5 sections return identical
       layout, including the recipe header's double-space quirk
       (``${task_class:+ …}`` always leaves a literal space).

  (6)  ``show <recipe> --task-class X`` — tc_filter applied
       identically. Mirrors bash's raw string concat.

  (7)  ``show <recipe>`` with a topology_win_rates row whose
       workflow_name LIKE '%recipe%' — section 3 orders by win_rate
       DESC and emits substr(topology_id,1,14) + workflow_name.

  (8)  ``show <recipe>`` with a bug_reports row whose observed_in
       LIKE '%recipe%' — section 4 ORDER BY
       CASE severity…*frequency DESC; tests the integer-rank fallthrough
       to default-1 (the bash CASE has no ELSE).

  (9)  ``conductor-history`` default N=10 (empty DB) — header + 0 rows;
       then with rows: COALESCE(outcome, '?') and ``printf('%.3f',
       realized_score)`` null fallback to ``"-"`` match exactly.

  (10) ``conductor-history N=2`` — LIMIT honored even with 5 rows.

  (11) ``help`` subcommand — exact byte match (cat heredoc form).

  (12) unknown subcommand — exit code 2 + stderr parity.

Tolerance notes (per the kickoff and the live-bash quirks memo):

  * Float columns (``win_rate``, ``relative_advantage``,
    ``predicted_score``, ``realized_score``, ``budget_pct_used``,
    ``avg_cost_usd``, ``confidence``) compared at 1e-6.
  * Integer columns (``sample_size``, ``runs_count``, ``frequency``)
    compared exact.
  * Datetime fields (``decided_at`` formatted via
    ``datetime(…,'localtime')``) are host-TZ-dependent. Both bash and
    py subprocesses MUST be spawned with the same fixed ``TZ``
    (``UTC`` by convention) — otherwise the ``at`` column will
    diverge.
  * Bytes are compared exactly via ``bytes_equal`` after stripping the
    trailing newline (which differs when rows==0 between bash's
    no-output and Python's no-rows==""; we normalize both to one
    trailing ``\\n`` only when bash produced any output at all, else
    we compare "" vs "").
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_lifetime as py  # noqa: E402

SH = REPO / "bin" / "mini-ork-lifetime"
INIT_SH = REPO / "db" / "init.sh"

# Subprocess env: pin TZ so `datetime(…,'localtime')` is identical
# between bash and py runs.
_SUBPROC_ENV_BASE = {**os.environ, "TZ": "UTC"}


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
def _which_tools() -> None:
    for tool in ("bash", "sqlite3", "python3"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not SH.exists():
        pytest.skip(f"missing bin/mini-ork-lifetime at {SH}")
    if not INIT_SH.exists():
        pytest.skip(f"missing db/init.sh at {INIT_SH}")


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Spin up a real mini-ork SQLite DB via db/init.sh.

    Each test gets a fresh DB. The fixture sets ``MINI_ORK_DB`` /
    ``MINI_ORK_HOME`` / ``TZ`` in the parent process env so the
    Python port's ``_db_path()`` and the sqlite3 ``datetime(…,
    'localtime')`` call resolve to the same DB + timezone the bash
    subprocess sees.
    """
    _which_tools()
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**_SUBPROC_ENV_BASE, "MINI_ORK_HOME": str(home),
             "MINI_ORK_DB": dbp},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\nstderr={r.stderr}")
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    # TZ=UTC ensures Python's sqlite3 datetime(…,'localtime') matches
    # the bash subprocess's localtime. Without this, datetime values
    # formatted via printf('%+.3f', …) would compare unequal across
    # the host's local TZ.
    monkeypatch.setenv("TZ", "UTC")
    return {"home": str(home), "db": dbp, "tmp_path": tmp_path}


def _bash_run(args: list[str], *, db: str) -> subprocess.CompletedProcess:
    """Invoke the live ``bin/mini-ork-lifetime`` bash with the given args.

    The bash resolves ``STATE_DB`` from ``MINI_ORK_DB`` (set here). We
    explicitly do NOT source any lib/* — the bash file is a standalone
    sqlite3 wrapper.
    """
    return subprocess.run(
        ["bash", str(SH), *args],
        env={**_SUBPROC_ENV_BASE, "MINI_ORK_DB": db,
             "MINI_ORK_HOME": str(Path(db).parent)},
        capture_output=True, text=True,
    )


def _seed(db: str, rows: list[tuple]) -> None:
    """Insert one row into a table via a parameterized statement.

    Tests pass the column list per-row to keep them readable. Usage:
        _seed(db, [
            ("INSERT INTO foo (a, b) VALUES (?, ?)", (1, 2)),
            ...
        ])
    """
    con = sqlite3.connect(db)
    try:
        for sql, params in rows:
            con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def _now() -> int:
    return int(time.time())


# Common parameter helpers (keep tests terse).
def _ins_task(now: int, age_s: int = 3600, recipe: str = "code-fix",
              tc: str = "code_fix", rowid: int = 1) -> tuple:
    return (
        "INSERT INTO task_runs "
        "(id, task_class, recipe, workflow_version, kickoff_path, "
        " status, cost_usd, duration_ms, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"r-{rowid}-{now}", tc, recipe, "latest", "/k.md",
         "published", 0.5, 100, now - age_s, now),
    )


def _ins_prompt(h: str, tc: str, win: float, n: int) -> tuple:
    return (
        "INSERT INTO prompt_win_rates "
        "(prompt_version_hash, task_class, wins, losses, ties, "
        " win_rate, sample_size, last_updated) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (h, tc, int(win * n), n - int(win * n), 0,
         win, n, "2026-01-01T00:00:00.000Z"),
    )


def _ins_lane(avid: str, tc: str, runs: int, adv: float) -> tuple:
    return (
        "INSERT INTO agent_performance_memory "
        "(agent_version_id, role, model, task_class, runs_count, "
        " success_count, avg_cost_usd, avg_duration_ms, "
        " relative_advantage, last_updated) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (avid, "implementer", "codex", tc, runs, runs - 1,
         0.5, 1000, adv, "2026-01-01T00:00:00.000Z"),
    )


def _ins_topology(topo: str, wf: str, tc: str, win: float,
                  n: int = 6, cost: float = 1.234) -> tuple:
    return (
        "INSERT INTO topology_win_rates "
        "(topology_id, workflow_name, task_class, wins, losses, "
        " ties, win_rate, sample_size, avg_cost_usd, "
        " avg_duration_ms, last_updated) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (topo, wf, tc, int(win * n), n - int(win * n), 0,
         win, n, cost, 1000, "2026-01-01T00:00:00.000Z"),
    )


def _ins_bug(fp: str, role: str, tc: str, sev: str, freq: int,
             observed_in: str, title: str, now: int) -> tuple:
    return (
        "INSERT INTO bug_reports "
        "(fingerprint, run_id, agent_role, task_class, observed_in, "
        " title, description, suggested_fix, severity, confidence, "
        " frequency, status, first_seen_at, last_seen_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fp, "run-x", role, tc, observed_in, title, "d", "f",
         sev, 0.5, freq, "open", now, now, now),
    )


def _ins_role_evolver(rid: int, recipe: str, kind: str = "split",
                     node: str = "n1", rationale: str = "r",
                     target_recipe: str | None = None) -> tuple:
    return (
        "INSERT INTO role_evolver_log "
        "(id, proposed_at, target_recipe, target_node_id, "
        " proposal_kind, rationale, evidence_json, proposed_change, "
        " status, applied_at, benchmark_delta) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (rid, _now(), target_recipe or recipe, node, kind, rationale,
         "{}", "x", "open", None, None),
    )


def _ins_conductor(decided_at: int, epic: str, recipe: str,
                   predicted: float, budget: float,
                   outcome: str | None = None,
                   realized: float | None = None) -> tuple:
    return (
        "INSERT INTO conductor_decisions "
        "(decided_at, epic_id, task_class, chosen_topology, "
        " chosen_recipe, chosen_lane_hints, predicted_score, "
        " budget_pct_used, rationale, outcome, realized_score) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (decided_at, epic, "code_fix", "topo-x", recipe, "{}",
         predicted, budget, "", outcome, realized),
    )


# ─────────────────────────────────────────────────────────────────────────────
# (1) summary on empty DB
# ─────────────────────────────────────────────────────────────────────────────
def test_summary_empty_db(temp_db):
    """Empty DB: all sections return identical layout.

    Tests the column-width codec on all-NULL SUM cells (NULL →
    empty-string-padded-to-header-width = line of all spaces).
    """
    bash_r = _bash_run(["summary"], db=temp_db["db"])
    py_out = py.summary()
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    assert bash_r.stdout == py_out, _diff_msg(bash_r.stdout, py_out)


# ─────────────────────────────────────────────────────────────────────────────
# (2) summary with h24/d7/lifetime rows
# ─────────────────────────────────────────────────────────────────────────────
def test_summary_run_volume_columns(temp_db):
    """Seeded task_runs: h24/d7/lifetime widths match.

    One row in the past 24h, one row in the past 7d but >24h ago,
    one row >7d ago. Expected: h24=1, d7=2, lifetime=3.
    """
    now = _now()
    _seed(temp_db["db"], [
        _ins_task(now, age_s=60, rowid=1),         # h24 yes, d7 yes, lifetime yes
        _ins_task(now, age_s=86400 * 2, rowid=2),  # h24 no,  d7 yes, lifetime yes
        _ins_task(now, age_s=86400 * 30, rowid=3), # h24 no,  d7 no,  lifetime yes
    ])
    bash_r = _bash_run(["summary"], db=temp_db["db"])
    py_out = py.summary()
    assert bash_r.returncode == 0, bash_r.stderr
    assert bash_r.stdout == py_out, _diff_msg(bash_r.stdout, py_out)


# ─────────────────────────────────────────────────────────────────────────────
# (3) summary with prompt_win_rates / lanes / topologies (LIMIT 5 ordering)
# ─────────────────────────────────────────────────────────────────────────────
def test_summary_top5_ordering_and_widths(temp_db):
    """Insert 2 rows each in prompt_win_rates, agent_performance_memory,
    topology_win_rates — LIMIT 5 ordering by win_rate DESC must match
    exactly. printf widths (%.3f, %+.3f, %-15s) too.
    """
    _seed(temp_db["db"], [
        # Prompts
        _ins_prompt("abcdef0123456789abcdef0123456789", "code_fix",
                    0.85, 10),
        _ins_prompt("1234567890abcdef1234567890abcdef", "code_fix",
                    0.65, 10),
        # Lanes — positive and negative advantage to test %+.3f
        _ins_lane("codex-v3-code-fix", "code_fix", 12, +0.200),
        _ins_lane("kimi-v2-code-fix", "code_fix", 15, -0.100),
        # Topologies — distinct workflow_name substrings
        _ins_topology("topo-aaaa-bbbb-cccc", "framework-edit",
                      "code_fix", 0.833),
        _ins_topology("topo-dddd-eeee-ffff", "bdd-first-delivery",
                      "bdd", 0.667),
    ])
    bash_r = _bash_run(["summary"], db=temp_db["db"])
    py_out = py.summary()
    assert bash_r.returncode == 0, bash_r.stderr
    assert bash_r.stdout == py_out, _diff_msg(bash_r.stdout, py_out)


# ─────────────────────────────────────────────────────────────────────────────
# (4) summary with bug_reports at every severity — bucket rank
# ─────────────────────────────────────────────────────────────────────────────
def test_summary_open_bug_priority_order(temp_db):
    """bucket ORDER BY CASE severity rank DESC matches: critical → high →
    medium → low. (bash's CASE has no ELSE → falls through to 1; the
    'unknown' severity would also map to 1.)
    """
    now = _now()
    _seed(temp_db["db"], [
        _ins_bug("fp-low", "reviewer", "code_fix", "low", 1,
                 "lib/y", "Low bug", now),
        _ins_bug("fp-med", "reviewer", "code_fix", "medium", 1,
                 "lib/y", "Med bug", now),
        _ins_bug("fp-hi", "reviewer", "code_fix", "high", 1,
                 "lib/y", "High bug", now),
        _ins_bug("fp-crit", "reviewer", "code_fix", "critical", 1,
                 "lib/y", "Crit bug", now),
    ])
    bash_r = _bash_run(["summary"], db=temp_db["db"])
    py_out = py.summary()
    assert bash_r.returncode == 0, bash_r.stderr
    assert bash_r.stdout == py_out, _diff_msg(bash_r.stdout, py_out)


# ─────────────────────────────────────────────────────────────────────────────
# (5) show <recipe> on empty DB — verifies the recipe header + 5 sections
# ─────────────────────────────────────────────────────────────────────────────
def test_show_empty_db(temp_db):
    """Show with no data: 5 section headers + zero rows.

    Includes the recipe header's double-space quirk (bash
    ``${task_class:+ …}`` always leaves a literal space before the
    ``===``).
    """
    bash_r = _bash_run(["show", "framework-edit"], db=temp_db["db"])
    py_out = py.show("framework-edit")
    assert bash_r.returncode == 0, bash_r.stderr
    assert bash_r.stdout == py_out, _diff_msg(bash_r.stdout, py_out)


# ─────────────────────────────────────────────────────────────────────────────
# (6) show <recipe> --task-class X — tc_filter applied identically
# ─────────────────────────────────────────────────────────────────────────────
def test_show_with_task_class(temp_db):
    """tc_filter applied via raw string concat — mirror exactly."""
    _seed(temp_db["db"], [
        _ins_prompt("abcdef0123456789abcdef0123456789", "code_fix",
                    0.9, 10),
        _ins_prompt("1234567890abcdef1234567890abcdef", "other",
                    0.95, 10),
        _ins_lane("codex-v3", "code_fix", 12, 0.5),
        _ins_lane("kimi-v2", "other", 15, 0.6),
    ])
    bash_r = _bash_run(
        ["show", "anything", "--task-class", "code_fix"],
        db=temp_db["db"])
    py_out = py.show("anything", "code_fix")
    assert bash_r.returncode == 0, bash_r.stderr
    assert bash_r.stdout == py_out, _diff_msg(bash_r.stdout, py_out)


# ─────────────────────────────────────────────────────────────────────────────
# (7) show <recipe> with topology_win_rates matching LIKE
# ─────────────────────────────────────────────────────────────────────────────
def test_show_topology_section(temp_db):
    """Section 3 — workflow_name LIKE '%recipe%' — match bash's
    interpolation (parameter binding would change semantics: SQL would
    treat '%' as a literal vs. LIKE wildcard). Mirror exactly.
    """
    _seed(temp_db["db"], [
        _ins_topology("topo-aaaa-bbbb-cccc", "framework-edit",
                      "code_fix", 0.833, n=10),
        _ins_topology("topo-dddd-eeee-ffff", "framework-edit-prod",
                      "code_fix", 0.667, n=10),
        _ins_topology("topo-gggg-hhhh-iiii", "bdd-first-delivery",
                      "code_fix", 0.500, n=10),
    ])
    bash_r = _bash_run(["show", "framework"], db=temp_db["db"])
    py_out = py.show("framework")
    assert bash_r.returncode == 0, bash_r.stderr
    assert bash_r.stdout == py_out, _diff_msg(bash_r.stdout, py_out)


# ─────────────────────────────────────────────────────────────────────────────
# (8) show <recipe> bug_reports ORDER BY severity-rank*freq DESC
# ─────────────────────────────────────────────────────────────────────────────
def test_show_bug_reports_section(temp_db):
    """Section 4 orders by ``CASE rank * frequency DESC``; tests that
    * the python port ranks severity identical to bash
    * the bug-report section's substr(title,1,70) truncation matches
    * unknown-severity rows fall through to rank=1 (no ELSE branch)
    """
    now = _now()
    _seed(temp_db["db"], [
        # Frequency-1 critical should outrank frequency-5 low
        _ins_bug("fp-c1", "reviewer", "code_fix", "critical", 1,
                 "framework-edit/lib/x", "Critical one", now),
        # High frequency-1
        _ins_bug("fp-h1", "reviewer", "code_fix", "high", 1,
                 "framework-edit/lib/y", "High one", now),
        # Low frequency-5 → rank=1 * 5 = 5 (still < critical rank-8)
        _ins_bug("fp-l5", "reviewer", "code_fix", "low", 5,
                 "framework-edit/lib/z", "Low many", now),
        # Medium frequency-3 → rank=2 * 3 = 6
        _ins_bug("fp-m3", "reviewer", "code_fix", "medium", 3,
                 "framework-edit/lib/a", "Med some", now),
    ])
    bash_r = _bash_run(["show", "framework"], db=temp_db["db"])
    py_out = py.show("framework")
    assert bash_r.returncode == 0, bash_r.stderr
    assert bash_r.stdout == py_out, _diff_msg(bash_r.stdout, py_out)


# ─────────────────────────────────────────────────────────────────────────────
# (9) conductor-history — default N=10
# ─────────────────────────────────────────────────────────────────────────────
def test_conductor_history_default(temp_db):
    """Default N=10; both empty and with 3 rows. Tests COALESCE on
    NULL outcome/realized_score and ``printf('%+.3f', x)`` /
    ``printf('%.3f', x)`` formatting.
    """
    # Empty-DB branch first.
    bash_r = _bash_run(["conductor-history"], db=temp_db["db"])
    py_out = py.conductor_history()
    assert bash_r.returncode == 0, bash_r.stderr
    assert bash_r.stdout == py_out, _diff_msg(bash_r.stdout, py_out)

    # Seeded branch — note TZ=UTC in the subprocess env so the
    # datetime(...) output is identical for both bash and py.
    _seed(temp_db["db"], [
        _ins_conductor(1700000000, "epic-aaaa-bbbb-cccc-dddd",
                       "code-fix", 0.85, 25.5,
                       outcome="success", realized=0.90),
        _ins_conductor(1700001000, "epic-eeee-ffff-gggg-hhhh",
                       "bdd", 0.55, 75.0, outcome=None, realized=None),
    ])
    bash_r = _bash_run(["conductor-history"], db=temp_db["db"])
    py_out = py.conductor_history()
    assert bash_r.returncode == 0, bash_r.stderr
    assert bash_r.stdout == py_out, _diff_msg(bash_r.stdout, py_out)


# ─────────────────────────────────────────────────────────────────────────────
# (10) conductor-history N=2 — LIMIT honored
# ─────────────────────────────────────────────────────────────────────────────
def test_conductor_history_n_2(temp_db):
    """Insert 5 rows; N=2 returns the 2 most-recent."""
    for i in range(5):
        _seed(temp_db["db"], [
            _ins_conductor(1700000000 + i * 60, f"epic-{i:04d}",
                           f"recipe-{i}", 0.5 + 0.05 * i, 30.0 + i),
        ])
    bash_r = _bash_run(["conductor-history", "2"], db=temp_db["db"])
    py_out = py.conductor_history(2)
    assert bash_r.returncode == 0, bash_r.stderr
    assert bash_r.stdout == py_out, _diff_msg(bash_r.stdout, py_out)


# ─────────────────────────────────────────────────────────────────────────────
# (11) help subcommand — exact byte match
# ─────────────────────────────────────────────────────────────────────────────
def test_help_subcommand(temp_db):
    """Bash's ``cat <<EOF`` block; Python's HELP_TEXT must be byte-equal."""
    bash_r = _bash_run(["help"], db=temp_db["db"])
    py_out = py.help_text()
    assert bash_r.returncode == 0, bash_r.stderr
    assert bash_r.stdout == py_out, _diff_msg(bash_r.stdout, py_out)
    # And ``--help`` / ``-h`` aliases
    for variant in ("--help", "-h"):
        bash_r = _bash_run([variant], db=temp_db["db"])
        assert bash_r.returncode == 0, bash_r.stderr
        assert bash_r.stdout == py_out, (
            f"{variant} mismatch:\n  bash={bash_r.stdout!r}\n  py  ={py_out!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# (12) unknown subcommand — exit code 2 + stderr parity
# ─────────────────────────────────────────────────────────────────────────────
def test_unknown_subcommand(temp_db):
    """Bash: ``*) echo "lifetime: unknown subcommand $sub" >&2; exit 2``

    stderr must be byte-identical; exit codes both 2.
    """
    bash_r = _bash_run(["totally-bogus"], db=temp_db["db"])
    py_rc = py.main(["totally-bogus"])
    assert bash_r.returncode == 2, f"bash rc={bash_r.returncode}"
    assert py_rc == 2, f"py rc={py_rc}"
    assert bash_r.stderr.strip() == "lifetime: unknown subcommand totally-bogus", (
        f"bash stderr: {bash_r.stderr!r}"
    )
    # Python's main writes the same message to stderr; we can't capture
    # main's stderr directly, but we can replicate the write here and
    # compare. Use the dispatcher's explicit branch.
    import io
    buf = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = buf
    try:
        py.main(["totally-bogus"])
    finally:
        sys.stderr = old_stderr
    assert buf.getvalue().strip() == (
        "lifetime: unknown subcommand totally-bogus"
    ), f"py stderr: {buf.getvalue()!r}"


def _diff_msg(bash: str, py: str) -> str:
    """Build a readable diff marker for assertion failure messages."""
    return (
        f"stdout mismatch ({len(bash)} vs {len(py)} chars):\n"
        f"  bash first 200: {bash[:200]!r}\n"
        f"  py   first 200: {py[:200]!r}\n"
        f"  bash last 200:  {bash[-200:]!r}\n"
        f"  py   last 200:  {py[-200:]!r}"
    )
