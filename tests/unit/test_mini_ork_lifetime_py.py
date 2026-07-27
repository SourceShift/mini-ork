"""Unit tests: mini_ork.orchestration.lifetime (bash parity halves removed; formerly vs bin/mini-ork-lifetime).

Each test invokes the Python port against a temp DB seeded by
``db/init.sh`` (and optionally data tables) and asserts the rendered
output semantically: section headers present, rows in the documented
order, printf-formatted values (``%.3f``, ``%+.3f``).

Schema bootstrap: the three subcommands only query, never insert
(they are pure read-only leaderboards). ``db/init.sh`` applies
``prompt_win_rates`` (0030), ``bug_reports`` (0029),
``topology_win_rates`` / ``role_evolver_log`` / ``conductor_decisions``
(0034), ``task_runs`` (0013) and ``agent_performance_memory`` (0009 +
relative_advantage via 0032).

Cases (12):
  (1)  ``summary`` on empty DB — all 5 sections render.
  (2)  ``summary`` run-volume columns h24/d7/lifetime = 1/2/3.
  (3)  ``summary`` top-5 ordering (prompts / lanes / topologies).
  (4)  ``summary`` open-bug severity rank order.
  (5)  ``show <recipe>`` on empty DB — recipe header + 5 sections.
  (6)  ``show <recipe> --task-class X`` — tc_filter applied.
  (7)  ``show <recipe>`` topology LIKE section.
  (8)  ``show <recipe>`` bug_reports rank*freq ordering.
  (9)  ``conductor-history`` — default N=10, COALESCE NULLs.
  (10) ``conductor-history N=2`` — LIMIT honored.
  (11) ``help`` — HELP_TEXT via help/--help/-h.
  (12) unknown subcommand — exit 2 + stderr message.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.orchestration import lifetime as py

INIT_SH = REPO / "db" / "init.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Spin up a real mini-ork SQLite DB via db/init.sh."""
    if not INIT_SH.exists():
        pytest.skip(f"missing db/init.sh at {INIT_SH}")
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "TZ": "UTC", "MINI_ORK_HOME": str(home),
             "MINI_ORK_DB": dbp},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\nstderr={r.stderr}")
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    # TZ=UTC keeps sqlite3 datetime(…,'localtime') deterministic.
    monkeypatch.setenv("TZ", "UTC")
    return {"home": str(home), "db": dbp, "tmp_path": tmp_path}


def _seed(db: str, rows: list[tuple]) -> None:
    """Insert rows via parameterized statements."""
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


def _section(out: str, header: str) -> str:
    """Return the text after ``header`` up to the next ``## `` section."""
    i = out.index(header) + len(header)
    j = out.find("\n## ", i)
    return out[i:] if j == -1 else out[i:j]


# ─────────────────────────────────────────────────────────────────────────────
# (1) summary on empty DB
# ─────────────────────────────────────────────────────────────────────────────
def test_summary_empty_db(temp_db):
    """Empty DB: all 5 sections render with their headers."""
    out = py.summary()
    assert out.startswith("=== mini-ork lifetime summary ===\n")
    for header in (
        "## Run volume (24h / 7d / lifetime)",
        "## Top 5 prompts by win_rate (all classes, sample_size >= 5)",
        "## Top 5 lanes by relative_advantage (all classes)",
        "## Top 5 topologies by win_rate (all classes)",
        "## Open bug_reports priority",
    ):
        assert header in out, f"missing section: {header}"
    # column-mode header row for run volume
    assert "h24" in out and "d7" in out and "lifetime" in out


# ─────────────────────────────────────────────────────────────────────────────
# (2) summary with h24/d7/lifetime rows
# ─────────────────────────────────────────────────────────────────────────────
def test_summary_run_volume_columns(temp_db):
    """One row in the past 24h, one in the past 7d but >24h ago, one >7d
    ago. Expected: h24=1, d7=2, lifetime=3."""
    now = _now()
    _seed(temp_db["db"], [
        _ins_task(now, age_s=60, rowid=1),         # h24 yes, d7 yes, lifetime yes
        _ins_task(now, age_s=86400 * 2, rowid=2),  # h24 no,  d7 yes, lifetime yes
        _ins_task(now, age_s=86400 * 30, rowid=3), # h24 no,  d7 no,  lifetime yes
    ])
    out = py.summary()
    vol = _section(out, "## Run volume (24h / 7d / lifetime)")
    # data row after the header/dashes lines carries 1, 2, 3 in order
    import re
    assert re.search(r"\b1\s+2\s+3\b", vol), f"run-volume row: {vol!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (3) summary with prompt_win_rates / lanes / topologies (LIMIT 5 ordering)
# ─────────────────────────────────────────────────────────────────────────────
def test_summary_top5_ordering_and_widths(temp_db):
    """LIMIT 5 ordering by win_rate / relative_advantage DESC with printf
    widths (%.3f, %+.3f)."""
    _seed(temp_db["db"], [
        _ins_prompt("abcdef0123456789abcdef0123456789", "code_fix",
                    0.85, 10),
        _ins_prompt("1234567890abcdef1234567890abcdef", "code_fix",
                    0.65, 10),
        _ins_lane("codex-v3-code-fix", "code_fix", 12, +0.200),
        _ins_lane("kimi-v2-code-fix", "code_fix", 15, -0.100),
        _ins_topology("topo-aaaa-bbbb-cccc", "framework-edit",
                      "code_fix", 0.833),
        _ins_topology("topo-dddd-eeee-ffff", "bdd-first-delivery",
                      "bdd", 0.667),
    ])
    out = py.summary()
    prompts = _section(out, "## Top 5 prompts by win_rate (all classes, sample_size >= 5)")
    assert "0.850" in prompts and "0.650" in prompts
    assert prompts.index("0.850") < prompts.index("0.650")  # DESC order
    assert "abcdef0123456789" in prompts
    lanes = _section(out, "## Top 5 lanes by relative_advantage (all classes)")
    assert "+0.200" in lanes and "-0.100" in lanes
    assert lanes.index("+0.200") < lanes.index("-0.100")
    topos = _section(out, "## Top 5 topologies by win_rate (all classes)")
    assert "0.833" in topos and "0.667" in topos
    assert topos.index("0.833") < topos.index("0.667")


# ─────────────────────────────────────────────────────────────────────────────
# (4) summary with bug_reports at every severity — bucket rank
# ─────────────────────────────────────────────────────────────────────────────
def test_summary_open_bug_priority_order(temp_db):
    """bucket ORDER BY severity rank DESC: critical → high → medium → low."""
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
    out = py.summary()
    bugs = _section(out, "## Open bug_reports priority")
    order = [bugs.index(s) for s in ("critical", "high", "medium", "low")]
    assert order == sorted(order), f"severity order: {bugs!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (5) show <recipe> on empty DB — verifies the recipe header + 5 sections
# ─────────────────────────────────────────────────────────────────────────────
def test_show_empty_db(temp_db):
    """Show with no data: recipe header (with the double-space quirk) +
    5 section headers + zero rows."""
    out = py.show("framework-edit")
    assert out.startswith("=== lifetime: recipe=framework-edit  ===\n")
    for header in (
        "## Top prompts by win_rate (sample_size >= 3)",
        "## Lanes by relative_advantage (runs_count >= 3)",
        "## Topologies for this recipe (workflow_name LIKE '%framework-edit%')",
        "## Top open bug_reports tagged at this recipe's agents",
        "## Pending role-evolver proposals for this recipe",
    ):
        assert header in out, f"missing section: {header}"


# ─────────────────────────────────────────────────────────────────────────────
# (6) show <recipe> --task-class X — tc_filter applied
# ─────────────────────────────────────────────────────────────────────────────
def test_show_with_task_class(temp_db):
    """Only rows matching the task_class filter are rendered."""
    _seed(temp_db["db"], [
        _ins_prompt("abcdef0123456789abcdef0123456789", "code_fix",
                    0.9, 10),
        _ins_prompt("1234567890abcdef1234567890abcdef", "other",
                    0.95, 10),
        _ins_lane("codex-v3", "code_fix", 12, 0.5),
        _ins_lane("kimi-v2", "other", 15, 0.6),
    ])
    out = py.show("anything", "code_fix")
    assert "class=code_fix" in out.splitlines()[0]
    prompts = _section(out, "## Top prompts by win_rate (sample_size >= 3)")
    assert "0.900" in prompts           # code_fix row visible
    assert "0.950" not in prompts       # 'other' filtered out
    lanes = _section(out, "## Lanes by relative_advantage (runs_count >= 3)")
    assert "codex-v3" in lanes
    assert "kimi-v2" not in lanes


# ─────────────────────────────────────────────────────────────────────────────
# (7) show <recipe> with topology_win_rates matching LIKE
# ─────────────────────────────────────────────────────────────────────────────
def test_show_topology_section(temp_db):
    """Section 3 — workflow_name LIKE '%recipe%'."""
    _seed(temp_db["db"], [
        _ins_topology("topo-aaaa-bbbb-cccc", "framework-edit",
                      "code_fix", 0.833, n=10),
        _ins_topology("topo-dddd-eeee-ffff", "framework-edit-prod",
                      "code_fix", 0.667, n=10),
        _ins_topology("topo-gggg-hhhh-iiii", "bdd-first-delivery",
                      "code_fix", 0.500, n=10),
    ])
    out = py.show("framework")
    topos = _section(out, "## Topologies for this recipe")
    assert "0.833" in topos and "0.667" in topos
    assert "0.500" not in topos         # bdd-first-delivery not LIKE %framework%


# ─────────────────────────────────────────────────────────────────────────────
# (8) show <recipe> bug_reports ORDER BY severity-rank*freq DESC
# ─────────────────────────────────────────────────────────────────────────────
def test_show_bug_reports_section(temp_db):
    """Section 4 orders by ``CASE rank * frequency DESC``."""
    now = _now()
    _seed(temp_db["db"], [
        # Frequency-1 critical should outrank frequency-5 low
        _ins_bug("fp-c1", "reviewer", "code_fix", "critical", 1,
                 "framework-edit/lib/x", "Critical one", now),
        # High frequency-1
        _ins_bug("fp-h1", "reviewer", "code_fix", "high", 1,
                 "framework-edit/lib/y", "High one", now),
        # Low frequency-5 → rank=1 * 5 = 5 (still < critical rank)
        _ins_bug("fp-l5", "reviewer", "code_fix", "low", 5,
                 "framework-edit/lib/z", "Low many", now),
        # Medium frequency-3 → rank=2 * 3 = 6
        _ins_bug("fp-m3", "reviewer", "code_fix", "medium", 3,
                 "framework-edit/lib/a", "Med some", now),
    ])
    out = py.show("framework")
    bugs = _section(out, "## Top open bug_reports tagged at this recipe's agents")
    order = [bugs.index(t) for t in
             ("Critical one", "Med some", "Low many", "High one")]
    # critical(8) > medium*3(6) > low*5(5) > high(4-ish) per rank*freq DESC
    assert order == sorted(order), f"bug order: {bugs!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (9) conductor-history — default N=10
# ─────────────────────────────────────────────────────────────────────────────
def test_conductor_history_default(temp_db):
    """Default N=10; both empty and with 2 rows. COALESCE on NULL
    outcome/realized_score and ``printf('%+.3f')`` / ``printf('%.3f')``."""
    out = py.conductor_history()
    assert out.startswith("=== last 10 conductor_decisions ===\n")

    _seed(temp_db["db"], [
        _ins_conductor(1700000000, "epic-aaaa-bbbb-cccc-dddd",
                       "code-fix", 0.85, 25.5,
                       outcome="success", realized=0.90),
        _ins_conductor(1700001000, "epic-eeee-ffff-gggg-hhhh",
                       "bdd", 0.55, 75.0, outcome=None, realized=None),
    ])
    out = py.conductor_history()
    assert "0.850" in out and "0.900" in out       # predicted + realized
    assert "0.550" in out                          # second predicted
    assert "epic-aaaa-bbbb-cccc-dd" in out         # substr(epic_id,1,22)
    # NULL realized → COALESCE '-'
    assert "-" in out


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
    out = py.conductor_history(2)
    assert out.startswith("=== last 2 conductor_decisions ===\n")
    assert "epic-0004" in out and "epic-0003" in out
    assert "epic-0002" not in out and "epic-0000" not in out


# ─────────────────────────────────────────────────────────────────────────────
# (11) help subcommand
# ─────────────────────────────────────────────────────────────────────────────
def test_help_subcommand(temp_db):
    out = py.help_text()
    assert "summary" in out and "show" in out and "conductor-history" in out


# ─────────────────────────────────────────────────────────────────────────────
# (12) unknown subcommand — exit code 2 + stderr message
# ─────────────────────────────────────────────────────────────────────────────
def test_unknown_subcommand(temp_db):
    py_rc = py.main(["totally-bogus"])
    assert py_rc == 2, f"py rc={py_rc}"
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
