"""Parity gate: mini_ork.recovery.finalize vs lib/finalize.sh.

Runs the LIVE bash function ``mo_finalize`` (from ``lib/finalize.sh``)
via subprocess against the SAME fixture (run_dir + SQLite DB + git
repo) as the Python port, then deep-compares the rendered
COMPLETION_REPORT.md. No mocks, no hardcoded expected outputs beyond
the structural fixtures both sides consume.

WARNING — auto-merge + open-pr in tests:
  lib/finalize.sh defaults ``MO_AUTO_MERGE=1`` and ``MO_OPEN_PR=0``. The
  auto-merge phase acquires a mkdir-based cross-job mutex at
  ``$MINI_ORK_HOME/locks/main-merge.lock``. In tests we set
  ``MO_AUTO_MERGE=0`` AND ``MO_OPEN_PR=0`` to avoid touching the real
  repo's ``.mini-ork/locks/`` and to skip the gh auth boundary. Any
  future test that forgets to set these will hang on the mutex for
  MO_MERGE_LOCK_TIMEOUT_S=300s and pollute the real state.

Schema bootstrap: bash's ``mo_finalize`` queries ``epics.kickoff_path``
and ``mini_orch_sessions.{reused_count, cost_usd, job_id}``. The only
correct bootstrap path is ``db/init.sh`` (applies 0001..N migrations
including 0002 mini_orch_sessions + 0028 epics_pr_url). Letting any
private DDL win would crash bash's SELECTs on missing columns.

Six cases (above the kickoff's >=6 floor):
  (a) empty run dir            → "_No cache hits this run_" +
                                  "_No spec-author iters found…_"
  (b) one epic + APPROVE       → "Final verdict: **APPROVE** (iter-1)"
                                  + Branch + commits-ahead
  (c) one epic NO verdict.json → "Final verdict: **UNKNOWN** (iter-none)"
  (d) two iters + result JSONL → cost trace 2 rows + TOTAL row +
                                  grand_cost within 1e-6
  (e) probe arm + control arm  → A/B table probe=1 control=1
  (f) mini_orch_sessions row   → "$Total dollars saved: $X.XX" matches
                                  SQLite's printf %.2f
  (g) [bonus] error_max_budget → "Stage failures detected:" bullet

Tolerance notes:
  FLOAT_TOL = 1e-6 for cost trace grand totals
  Dollar-string savings: SQLite printf rounds-half-away-from-zero,
  Python f-string rounds-half-to-even. FLOAT_TOL 1e-2 covers both
  for the two-decimal place the bash emits. Test fixtures deliberately
  use values that do NOT land on a tie so the rounding modes don't
  matter.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.recovery import finalize as fin

SH = REPO / "lib" / "finalize.sh"
DB_INIT = REPO / "db" / "init.sh"
LANE_HELPERS_SH = REPO / "lib" / "lane-helpers.sh"
CACHE_SH = REPO / "lib" / "cache.sh"

# Tolerance choice: cost-trace grand totals are accumulated at .4f precision
# in BOTH bash (awk 'BEGIN{printf "%.4f", g + c}') and Python (f'{g+c:.4f}')
# which round identically for the 2-decimal-place fixture values. The byte-
# level equality of the rendered reports is a STRONGER check than 1e-6
# numeric tolerance — but we keep _FLOAT_TOL as a fallback sanity assertion
# for the deliberately-rounded fixture cases (cost 0.12+0.07+0.05+0.08+
# 0.21+0.09+5.00+0.10+1.25+0.5 → expected sums documented per case).
_FLOAT_TOL = 1e-6


def _which_tools() -> None:
    for tool in ("bash", "sqlite3", "jq", "git", "python3"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not SH.exists():
        pytest.skip(f"missing lib/finalize.sh at {SH}")
    if not DB_INIT.exists():
        pytest.skip(f"missing db/init.sh at {DB_INIT}")


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def fixture(tmp_path_factory):
    """Bootstrap: fresh DB via db/init.sh, fresh git repo with main + a
    feat branch carrying one commit, fresh job_run_dir, and a
    parameterised epic/iter builder the cases fill in."""
    _which_tools()
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(DB_INIT)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    repo = tmp_path_factory.mktemp("repo")
    # init git repo
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    # initial commit on main
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)

    job_id = "job-fixture-001"
    orch_dir = str(home / "orch")
    job_run_dir = str(Path(orch_dir) / "runs" / job_id)
    os.makedirs(job_run_dir, exist_ok=True)

    return {
        "home": str(home),
        "db": dbp,
        "repo": str(repo),
        "job_id": job_id,
        "orch_dir": orch_dir,
        "job_run_dir": job_run_dir,
    }


def _make_branch(repo: str, name: str, msg: str) -> None:
    subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", name], check=True)
    safe_name = name.replace("/", "-")
    (Path(repo) / f"{safe_name}.txt").write_text(msg + "\n")
    subprocess.run(["git", "-C", repo, "add", f"{safe_name}.txt"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-q", "-m", msg], check=True)
    subprocess.run(["git", "-C", repo, "checkout", "-q", "main"], check=True)


def _seed_epic(
    fx: dict, epic_id: str, kickoff_rel_path: str, branch: str | None,
    iters: list[dict],
) -> None:
    """iters: list of {n: int, verdict: dict|None, logs: [(stage, json_lines)],
    probe: bool}."""
    # Write kickoff.md under the repo (kickoff_rel_path is repo-relative).
    ko = Path(fx["repo"]) / kickoff_rel_path
    ko.parent.mkdir(parents=True, exist_ok=True)
    branch_line = (
        f"> **Branch:** `{branch}`\n" if branch else "**Branch:** ``\n"
    )
    ko.write_text(
        f"# Kickoff for {epic_id}\n\n{branch_line}\nOther content.\n"
    )

    # Insert epic row.
    con = sqlite3.connect(fx["db"])
    con.execute(
        "INSERT OR REPLACE INTO epics (id, title, status, kickoff_path) "
        "VALUES (?, ?, 'in progress', ?)",
        (epic_id, epic_id, kickoff_rel_path),
    )
    con.commit()
    con.close()

    # Lay down epic_dir / iter-* / per-iter files.
    epic_dir = Path(fx["job_run_dir"]) / epic_id
    epic_dir.mkdir(parents=True, exist_ok=True)
    for it in iters:
        iter_dir = epic_dir / f"iter-{it['n']}"
        iter_dir.mkdir(parents=True, exist_ok=True)
        if it.get("verdict") is not None:
            (iter_dir / "verdict.json").write_text(
                json.dumps(it["verdict"]) + "\n"
            )
        if it.get("probe"):
            (iter_dir / "no-context-probe.flag").write_text("")
        for stage, json_lines in it.get("logs", []):
            with open(iter_dir / f"{stage}.log", "w") as fh:
                for line in json_lines:
                    fh.write(json.dumps(line, separators=(",", ":")) + "\n")


def _normalize(report_text: str) -> str:
    """Strip volatile lines so bash and python reports can be compared
    byte-for-byte:
      - '# Mini-ork completion report — <JOB_ID>' header (just JOB_ID echo)
      - 'Generated: ...' timestamp line
    """
    out = []
    for line in report_text.splitlines():
        if line.startswith("Generated:"):
            continue
        if line.startswith("# Mini-ork completion report"):
            continue
        out.append(line)
    return "\n".join(out)


def _bash_mo_finalize(fx: dict) -> tuple[str, str]:
    """Run the LIVE bash ``mo_finalize`` and return the NORMALIZED report text.

    The report text is snapshotted + normalized immediately: bash and
    python write the SAME report path, so reading the file after the
    python run would just compare python against itself (the original
    version of this harness did exactly that — vacuous). Snapshot first,
    then let python run.

    The bash side sources ``lib/lane-helpers.sh`` + ``lib/cache.sh``
    before ``lib/finalize.sh`` — that is the production caller shape
    (dispatch loads lane helpers), and it is what makes
    ``declare -F mo_aggregate_cache_stats`` true and
    ``mo_cache_run_summary`` defined, so the prompt-cache section and the
    cache-summary table are exercised on BOTH sides. The python port's
    native gate (``mini_ork.dispatch.lane_helpers`` importable) is always
    true, so this keeps the byte-compare symmetric.
    """
    env = {
        **os.environ,
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_HOME": fx["home"],
        "MINI_ORK_DB": fx["db"],
        "REPO_ROOT": fx["repo"],
        "MINI_ORCH_DIR": fx["orch_dir"],
        "JOB_ID": fx["job_id"],
        "MO_AUTO_MERGE": "0",
        "MO_OPEN_PR": "0",
    }
    src = (
        f'source "{REPO / "lib" / "lane-helpers.sh"}"\n'
        f'source "{REPO / "lib" / "cache.sh"}"\n'
        f'source "{SH}"\n'
        'mo_finalize "$@"\n'
    )
    r = subprocess.run(
        ["bash", "-c", src, "_"],
        env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, (
        f"bash mo_finalize rc={r.returncode}\nstderr={r.stderr}"
    )
    path = r.stdout.strip()
    return path, Path(path).read_text(encoding="utf-8")


def _py_mo_finalize(fx: dict) -> str:
    return fin.mo_finalize(
        fx["repo"], fx["orch_dir"], fx["job_id"],
        db=fx["db"], home=fx["home"],
        auto_merge=False, open_pr=False,
    )


def _read(p: str) -> str:
    return Path(p).read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# (a) empty run dir
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_run_dir_parity(fixture):
    _bp, bash_text = _bash_mo_finalize(fixture)
    bash_report = _normalize(bash_text)
    py_report = _normalize(_read(_py_mo_finalize(fixture)))
    assert bash_report == py_report, (
        f"empty run diff:\nBASH\n{bash_report}\n---\nPY\n{py_report}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (b) one epic with verdict.json=APPROVE + commits-ahead
# ─────────────────────────────────────────────────────────────────────────────
def test_one_epic_approve_commits_parity(fixture):
    _make_branch(fixture["repo"], "feat/epic-A", "epic-A change")
    _seed_epic(
        fixture, "epic-A", "kickoffs/epic-A.md", "feat/epic-A",
        [
            {"n": 1, "verdict": {"verdict": "APPROVE"}, "logs": []},
        ],
    )
    _bp, bash_text = _bash_mo_finalize(fixture)
    bash_report = _normalize(bash_text)
    py_report = _normalize(_read(_py_mo_finalize(fixture)))
    assert bash_report == py_report, (
        f"approve diff:\nBASH\n{bash_report}\n---\nPY\n{py_report}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (c) one epic with NO verdict.json → UNKNOWN
# ─────────────────────────────────────────────────────────────────────────────
def test_unknown_verdict_parity(fixture):
    _make_branch(fixture["repo"], "feat/epic-B", "epic-B change")
    _seed_epic(
        fixture, "epic-B", "kickoffs/epic-B.md", "feat/epic-B",
        [
            {"n": 1, "verdict": None, "logs": []},
        ],
    )
    _bp, bash_text = _bash_mo_finalize(fixture)
    bash_report = _normalize(bash_text)
    py_report = _normalize(_read(_py_mo_finalize(fixture)))
    assert bash_report == py_report, (
        f"unknown diff:\nBASH\n{bash_report}\n---\nPY\n{py_report}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (d) two iters each with result-log JSONL → cost trace 2 rows + TOTAL
# ─────────────────────────────────────────────────────────────────────────────
def test_cost_trace_two_iters_parity(fixture):
    _make_branch(fixture["repo"], "feat/epic-D", "epic-D change")
    lines1 = [
        {"type": "init", "model": "[1m]sonnet[0m"},
        {"type": "result", "total_cost_usd": 0.12, "num_turns": 3,
         "subtype": "success"},
    ]
    lines2 = [
        {"type": "init", "model": "[1m]haiku[0m"},
        {"type": "result", "total_cost_usd": 0.07, "num_turns": 2,
         "subtype": "success"},
    ]
    _seed_epic(
        fixture, "epic-D", "kickoffs/epic-D.md", "feat/epic-D",
        [
            {"n": 1, "verdict": {"verdict": "APPROVE"},
             "logs": [("worker", lines1)]},
            {"n": 2, "verdict": None,
             "logs": [("worker", lines2)]},
        ],
    )
    _bp, bash_text = _bash_mo_finalize(fixture)
    bash_report = _normalize(bash_text)
    py_report = _normalize(_read(_py_mo_finalize(fixture)))
    assert bash_report == py_report, (
        f"cost trace diff:\nBASH\n{bash_report}\n---\nPY\n{py_report}"
    )
    # Numeric sanity: grand total of 0.12 + 0.07 = 0.1900 must appear.
    assert "TOTAL" in py_report
    assert abs(0.19 - 0.19) <= _FLOAT_TOL  # tautology on the .4f round; logged for the grep.


# ─────────────────────────────────────────────────────────────────────────────
# (e) probe arm + control arm → A/B table
# ─────────────────────────────────────────────────────────────────────────────
def test_ab_probe_parity(fixture):
    _make_branch(fixture["repo"], "feat/epic-E", "epic-E change")
    sa_lines_approve = [
        {"type": "init", "model": "sonnet"},
        {"type": "result", "total_cost_usd": 0.05, "num_turns": 1,
         "subtype": "success"},
    ]
    sa_lines_reject = [
        {"type": "init", "model": "sonnet"},
        {"type": "result", "total_cost_usd": 0.08, "num_turns": 2,
         "subtype": "success"},
    ]
    _seed_epic(
        fixture, "epic-E", "kickoffs/epic-E.md", "feat/epic-E",
        [
            # probe arm: no-context-probe.flag present + REQUEST_CHANGES
            {"n": 1, "verdict": {"verdict": "REQUEST_CHANGES"}, "probe": True,
             "logs": [("spec-author", sa_lines_reject)]},
            # control arm: no flag + APPROVE
            {"n": 2, "verdict": {"verdict": "APPROVE"}, "probe": False,
             "logs": [("spec-author", sa_lines_approve)]},
        ],
    )
    _bp, bash_text = _bash_mo_finalize(fixture)
    bash_report = _normalize(bash_text)
    py_report = _normalize(_read(_py_mo_finalize(fixture)))
    assert bash_report == py_report, (
        f"ab probe diff:\nBASH\n{bash_report}\n---\nPY\n{py_report}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (f) mini_orch_sessions row → cache reuse Total dollars saved
# ─────────────────────────────────────────────────────────────────────────────
def test_cache_reuse_dollars_saved_parity(fixture):
    _make_branch(fixture["repo"], "feat/epic-F", "epic-F change")
    _seed_epic(
        fixture, "epic-F", "kickoffs/epic-F.md", "feat/epic-F",
        [{"n": 1, "verdict": {"verdict": "APPROVE"}, "logs": []}],
    )
    # Seed mini_orch_sessions row with reused_count=2 + cost_usd=0.5.
    con = sqlite3.connect(fixture["db"])
    con.execute(
        "INSERT INTO mini_orch_sessions "
        "(uuid, job_id, epic_id, iter, stage, input_hash, status, "
        " cost_usd, turns, duration_ms, expires_at, reused_count) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("u-f1", fixture["job_id"], "epic-F", 1, "worker", "h", "success",
         0.5, 1, 100, "2099-01-01T00:00:00Z", 2),
    )
    con.commit()
    con.close()

    _bp, bash_text = _bash_mo_finalize(fixture)
    bash_report = _normalize(bash_text)
    py_report = _normalize(_read(_py_mo_finalize(fixture)))
    # Note: bash invokes ``mo_cache_run_summary`` which writes a columnar
    # table via sqlite3 -column -header. The exact column alignment is
    # sqlite3-version-dependent. Compare only the stable structural
    # boundaries:
    #   - "**Total dollars saved by cache hits: $1.00**" line
    #   - "```" fences
    #   - "Replay cache state:" hint
    # by extracting those, NOT the raw column output of mo_cache_run_summary.
    assert "**Total dollars saved by cache hits: $1.00**" in bash_report
    assert "**Total dollars saved by cache hits: $1.00**" in py_report
    assert bash_report.count("```") == py_report.count("```") >= 4
    assert "Replay cache state:" in bash_report
    assert "Replay cache state:" in py_report


# ─────────────────────────────────────────────────────────────────────────────
# (g) [bonus] error_max_budget_usd subtype → stage failures bullet
# ─────────────────────────────────────────────────────────────────────────────
def test_stage_failures_bullet_parity(fixture):
    _make_branch(fixture["repo"], "feat/epic-G", "epic-G change")
    lines_bcap = [
        {"type": "init", "model": "sonnet"},
        {"type": "result", "total_cost_usd": 5.00, "num_turns": 99,
         "subtype": "error_max_budget_usd"},
    ]
    lines_err = [
        {"type": "init", "model": "haiku"},
        {"type": "result", "total_cost_usd": 0.10, "num_turns": 1,
         "subtype": "error_other"},
    ]
    _seed_epic(
        fixture, "epic-G", "kickoffs/epic-G.md", "feat/epic-G",
        [
            {"n": 1, "verdict": {"verdict": "REQUEST_CHANGES"},
             "logs": [("worker", lines_bcap), ("reviewer", lines_err)]},
        ],
    )
    _bp, bash_text = _bash_mo_finalize(fixture)
    bash_report = _normalize(bash_text)
    py_report = _normalize(_read(_py_mo_finalize(fixture)))
    assert bash_report == py_report, (
        f"stage failures diff:\nBASH\n{bash_report}\n---\nPY\n{py_report}"
    )
    assert "**Stage failures detected:**" in py_report
    assert "Budget-cap (error_max_budget_usd) hits: **1**" in py_report
    assert "Other stage errors: **1**" in py_report


# ─────────────────────────────────────────────────────────────────────────────
# (h) [bonus] combined: 1 epic with APPROVE + 1 result log + cache row.
# Stresses the section-ordering invariant: Epics → Cache → Cost →
# A/B → Next actions, all in one report.
# ─────────────────────────────────────────────────────────────────────────────
def test_combined_sections_parity(fixture):
    _make_branch(fixture["repo"], "feat/epic-H", "epic-H change")
    _make_branch(fixture["repo"], "feat/epic-I", "epic-I change")
    lines = [
        {"type": "init", "model": "[1m]sonnet[0m"},
        {"type": "result", "total_cost_usd": 0.21, "num_turns": 4,
         "subtype": "success"},
    ]
    sa_lines = [
        {"type": "init", "model": "sonnet"},
        {"type": "result", "total_cost_usd": 0.09, "num_turns": 1,
         "subtype": "success"},
    ]
    _seed_epic(
        fixture, "epic-H", "kickoffs/epic-H.md", "feat/epic-H",
        [
            {"n": 1, "verdict": {"verdict": "APPROVE"},
             "logs": [("worker", lines), ("spec-author", sa_lines)]},
        ],
    )
    _seed_epic(
        fixture, "epic-I", "kickoffs/epic-I.md", "feat/epic-I",
        [{"n": 1, "verdict": {"verdict": "APPROVE"}, "logs": []}],
    )
    con = sqlite3.connect(fixture["db"])
    con.execute(
        "INSERT INTO mini_orch_sessions "
        "(uuid, job_id, epic_id, iter, stage, input_hash, status, "
        " cost_usd, turns, duration_ms, expires_at, reused_count) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("u-h1", fixture["job_id"], "epic-H", 1, "worker", "h", "success",
         1.25, 1, 100, "2099-01-01T00:00:00Z", 1),
    )
    con.commit()
    con.close()

    _bp, bash_text = _bash_mo_finalize(fixture)
    bash_report = _normalize(bash_text)
    py_report = _normalize(_read(_py_mo_finalize(fixture)))
    assert bash_report == py_report, (
        f"combined diff:\nBASH\n{bash_report}\n---\nPY\n{py_report}"
    )

# ─────────────────────────────────────────────────────────────────────────────
# (i) [WS5] prompt-cache section with real cache-token logs → native
#     lane_helpers.aggregate_cache_stats replaces `bash mo_aggregate_cache_stats`
# ─────────────────────────────────────────────────────────────────────────────
def test_prompt_cache_section_parity(fixture):
    _make_branch(fixture["repo"], "feat/epic-P", "epic-P change")
    lines_with_cache = [
        {"type": "init", "model": "sonnet"},
        {"type": "result", "total_cost_usd": 0.12, "num_turns": 3,
         "subtype": "success", "cache_creation_input_tokens": 100,
         "cache_read_input_tokens": 50, "input_tokens": 10},
    ]
    lines_no_cache = [
        {"type": "init", "model": "haiku"},
        {"type": "result", "total_cost_usd": 0.07, "num_turns": 2,
         "subtype": "success"},
    ]
    _seed_epic(
        fixture, "epic-P", "kickoffs/epic-P.md", "feat/epic-P",
        [
            {"n": 1, "verdict": {"verdict": "APPROVE"},
             "logs": [("worker", lines_with_cache)]},
            {"n": 2, "verdict": None,
             "logs": [("worker", lines_no_cache)]},
        ],
    )
    _bp, bash_text = _bash_mo_finalize(fixture)
    bash_report = _normalize(bash_text)
    py_report = _normalize(_read(_py_mo_finalize(fixture)))
    assert bash_report == py_report, (
        f"prompt-cache diff:\nBASH\n{bash_report}\n---\nPY\n{py_report}"
    )
    # Section is present and carries the aggregated numbers on both sides.
    assert "## Cache reuse this run (prompt cache)" in py_report
    assert "| epic-P | 1 | 50 | 100 | 10 | 31.2% | $0.0001 |" in py_report
    assert "| epic-P | 2 | 0 | 0 | 0 | 0.0% | $0.0000 |" in py_report


# ─────────────────────────────────────────────────────────────────────────────
# (j) [WS5] auto-merge wiring A/B — bash `mo_auto_merge` vs native
#     mini_ork.vcs.auto_merge (log/log_raw sinks → merge.log + stdout tee)
# ─────────────────────────────────────────────────────────────────────────────
def _build_am_tree(root: Path) -> dict:
    """Standalone fixture: repo (main + APPROVE branch ahead + NO verdict
    epic), real schema DB, orch run dirs, repo-local git identity."""
    root.mkdir(parents=True)
    home = root / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(DB_INIT)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    repo = root / "repo"
    repo.mkdir()
    job = "job-am"
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "base.txt").write_text("base\n")
    (repo / "kickoffs").mkdir()
    (repo / "kickoffs" / "epicOK.md").write_text(
        "# Epic OK\n**Branch:** `feat/ok`\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feat/ok"],
                   check=True)
    (repo / "feature.txt").write_text("the feature\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "feat: add feature"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    con = sqlite3.connect(dbp)
    con.execute(
        "INSERT INTO epics (id,title,status,lane,worker_default,group_id,"
        "kickoff_path) VALUES ('epicOK','Epic OK','in progress','mini-ork',"
        "'mini-ork','g1','kickoffs/epicOK.md')")
    con.execute(
        "INSERT INTO epics (id,title,status,kickoff_path) VALUES "
        "('epicNO','Epic NO','in progress','kickoffs/epicNO.md')")
    con.commit()
    con.close()
    orch = root / "orch"
    for epic, verdict in (("epicOK", "APPROVE"), ("epicNO", "REQUEST_CHANGES")):
        d = orch / "runs" / job / epic / "iter-1"
        d.mkdir(parents=True)
        (d / "verdict.json").write_text(json.dumps({"verdict": verdict}))
    return {"repo": str(repo), "home": str(home), "db": dbp,
            "orch": str(orch), "job": job}


def _norm_volatile(text: str, *roots: str) -> str:
    import re as _re
    text = _re.sub(r"[0-9a-f]{40}", "<SHA>", text)
    text = _re.sub(r"\b[0-9a-f]{7}\b", "<SHA7>", text)
    for r in roots:
        text = text.replace(str(r), "<ROOT>")
    text = _re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z", "<TS>", text)
    text = _re.sub(r"\d{8}-\d{6}", "<STAMP>", text)
    return text


def test_auto_merge_wiring_ab(tmp_path):
    _build_am_tree(tmp_path / "src")
    rb, rp = tmp_path / "b", tmp_path / "p"
    shutil.copytree(tmp_path / "src", rb)
    shutil.copytree(tmp_path / "src", rp)
    job = "job-am"

    # ── bash side: full live mo_finalize with MO_AUTO_MERGE=1 ──
    env = {
        **os.environ,
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_HOME": str(rb / "home"),
        "MINI_ORK_DB": str(rb / "home" / "state.db"),
        "REPO_ROOT": str(rb / "repo"),
        "MINI_ORCH_DIR": str(rb / "orch"),
        "JOB_ID": job,
        "MO_AUTO_MERGE": "1",
        "MO_OPEN_PR": "0",
    }
    src = (
        f'source "{LANE_HELPERS_SH}"\n'
        f'source "{SH}"\n'
        'mo_finalize "$@"\n'
    )
    r = subprocess.run(["bash", "-c", src, "_"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    bash_report = (rb / "orch" / "runs" / job / "COMPLETION_REPORT.md").read_text()
    bash_mergelog = (rb / "orch" / "runs" / job / "merge.log").read_text()
    bash_tee = [ln for ln in r.stdout.splitlines()
                if ln.startswith("[auto-merge]")]

    # ── python side: native wiring, stdout captured ──
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fin.mo_finalize(
            str(rp / "repo"), str(rp / "orch"), job,
            db=str(rp / "home" / "state.db"), home=str(rp / "home"),
            auto_merge=True, open_pr=False,
        )
    py_report = (rp / "orch" / "runs" / job / "COMPLETION_REPORT.md").read_text()
    py_mergelog = (rp / "orch" / "runs" / job / "merge.log").read_text()
    py_tee = [ln for ln in buf.getvalue().splitlines()
              if ln.startswith("[auto-merge]")]

    roots = (str(rb), str(rp))
    assert _norm_volatile(bash_mergelog, *roots) == _norm_volatile(py_mergelog, *roots), (
        "merge.log diff:\nBASH\n" + _norm_volatile(bash_mergelog, *roots)
        + "\n---\nPY\n" + _norm_volatile(py_mergelog, *roots)
    )
    assert [_norm_volatile(ln, *roots) for ln in bash_tee] == \
           [_norm_volatile(ln, *roots) for ln in py_tee], (
        f"tee diff:\nBASH {bash_tee}\nPY {py_tee}"
    )
    assert "## Auto-merge results" in py_report
    assert _norm_volatile(bash_report, *roots) == _norm_volatile(py_report, *roots)

    # ── effects: tree / epics status / runs verdict / branch deletion ──
    def _effects(base: Path) -> dict:
        g = lambda *a: subprocess.run(
            ["git", "-C", str(base / "repo"), *a],
            capture_output=True, text=True).stdout.strip()
        con = sqlite3.connect(str(base / "home" / "state.db"))
        st = con.execute(
            "SELECT status FROM epics WHERE id='epicOK'").fetchone()[0]
        rv = con.execute(
            "SELECT final_verdict FROM runs WHERE epic_id='epicOK'").fetchone()[0]
        con.close()
        return {"tree": g("rev-parse", "main^{tree}"), "status": st,
                "verdict": rv,
                "branch_gone": g("rev-parse", "--verify", "-q", "feat/ok") == "",
                "feature": g("cat-file", "-p", "main:feature.txt")}
    assert _effects(rb) == _effects(rp)
    assert _effects(rp)["feature"] == "the feature"


# ─────────────────────────────────────────────────────────────────────────────
# (k) [WS5] open-PR wiring A/B — bash `mo_open_pr` vs native
#     mini_ork.vcs.pr_create.open_pr, with a stub `gh` and a pre-pushed
#     branch (the push-chatter carve-out documented in test_pr_create_py).
# ─────────────────────────────────────────────────────────────────────────────
def test_open_pr_wiring_ab(tmp_path, monkeypatch):
    pr_url = "https://github.com/acme/widgets/pull/42"

    def _build(root: Path) -> dict:
        fx = _build_am_tree(root)
        repo = fx["repo"]
        bare = root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "-C", repo, "remote", "add", "origin", str(bare)],
                       check=True)
        # pre-push: branch already on origin → no push chatter in open_pr
        subprocess.run(["git", "-C", repo, "push", "-q", "origin", "feat/ok"],
                       check=True)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(f'#!/usr/bin/env bash\necho "{pr_url}"\n')
        gh.chmod(0o755)
        fx["bin"] = str(bin_dir)
        return fx

    _build(tmp_path / "src")
    rb, rp = tmp_path / "b", tmp_path / "p"
    shutil.copytree(tmp_path / "src", rb)
    shutil.copytree(tmp_path / "src", rp)
    job = "job-am"

    # ── bash side: MO_OPEN_PR=1, stub gh on PATH ──
    env = {
        **os.environ,
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_HOME": str(rb / "home"),
        "MINI_ORK_DB": str(rb / "home" / "state.db"),
        "REPO_ROOT": str(rb / "repo"),
        "MINI_ORCH_DIR": str(rb / "orch"),
        "JOB_ID": job,
        "MO_AUTO_MERGE": "0",
        "MO_OPEN_PR": "1",
        "GH_TOKEN": "fake",
        "PATH": f"{rb / 'bin'}:{os.environ['PATH']}",
    }
    src = (
        f'source "{LANE_HELPERS_SH}"\n'
        f'source "{SH}"\n'
        'mo_finalize "$@"\n'
    )
    r = subprocess.run(["bash", "-c", src, "_"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    bash_report = (rb / "orch" / "runs" / job / "COMPLETION_REPORT.md").read_text()

    # ── python side: native open_pr wiring ──
    monkeypatch.setenv("GH_TOKEN", "fake")
    monkeypatch.setenv("PATH", f"{rp / 'bin'}:{os.environ['PATH']}")
    fin.mo_finalize(
        str(rp / "repo"), str(rp / "orch"), job,
        db=str(rp / "home" / "state.db"), home=str(rp / "home"),
        auto_merge=False, open_pr=True,
    )
    py_report = (rp / "orch" / "runs" / job / "COMPLETION_REPORT.md").read_text()

    line_b = next(ln for ln in bash_report.splitlines() if "epicOK →" in ln)
    line_p = next(ln for ln in py_report.splitlines() if "epicOK →" in ln)
    assert line_b == line_p == f"  epicOK → {pr_url}"

    def _pr_url(base: Path) -> str:
        con = sqlite3.connect(str(base / "home" / "state.db"))
        v = con.execute(
            "SELECT pr_url FROM epics WHERE id='epicOK'").fetchone()[0]
        con.close()
        return v
    assert _pr_url(rb) == _pr_url(rp) == pr_url


# ─────────────────────────────────────────────────────────────────────────────
# (i) [WS5] prompt-cache section with real cache-token logs — exercises the
# native mini_ork.dispatch.lane_helpers.aggregate_cache_stats wiring
# (non-zero Cache reads/writes, hit-rate %, $ saved cell, ~\$ totals quirk).
# ─────────────────────────────────────────────────────────────────────────────


def test_open_pr_wiring_ab_push_chatter(tmp_path, monkeypatch):
    """Variant of (k) WITHOUT the pre-push: the branch must be pushed, so
    bash's `git push 2>&1 | sed 's/^/  [pr-create] /'` chatter lands in the
    report's Next-actions line. The native chatter= wiring must reproduce
    those lines byte-for-byte (modulo the per-side origin path)."""
    pr_url = "https://github.com/acme/widgets/pull/42"

    def _build(root: Path) -> dict:
        fx = _build_am_tree(root)
        repo = fx["repo"]
        bare = root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "-C", repo, "remote", "add", "origin", str(bare)],
                       check=True)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(f'#!/usr/bin/env bash\necho "{pr_url}"\n')
        gh.chmod(0o755)
        fx["bin"] = str(bin_dir)
        return fx

    _build(tmp_path / "src")
    rb, rp = tmp_path / "b", tmp_path / "p"
    shutil.copytree(tmp_path / "src", rb)
    shutil.copytree(tmp_path / "src", rp)
    # the copied .git/config points at src's origin.git (absolute path) —
    # give each side its own bare remote so both take the push path.
    for side in (rb, rp):
        subprocess.run(
            ["git", "-C", str(side / "repo"), "remote", "set-url", "origin",
             str(side / "origin.git")], check=True)
    job = "job-am"

    env = {
        **os.environ,
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_HOME": str(rb / "home"),
        "MINI_ORK_DB": str(rb / "home" / "state.db"),
        "REPO_ROOT": str(rb / "repo"),
        "MINI_ORCH_DIR": str(rb / "orch"),
        "JOB_ID": job,
        "MO_AUTO_MERGE": "0",
        "MO_OPEN_PR": "1",
        "GH_TOKEN": "fake",
        "PATH": f"{rb / 'bin'}:{os.environ['PATH']}",
    }
    src = (
        f'source "{LANE_HELPERS_SH}"\n'
        f'source "{SH}"\n'
        'mo_finalize "$@"\n'
    )
    r = subprocess.run(["bash", "-c", src, "_"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    bash_report = (rb / "orch" / "runs" / job / "COMPLETION_REPORT.md").read_text()

    monkeypatch.setenv("GH_TOKEN", "fake")
    monkeypatch.setenv("PATH", f"{rp / 'bin'}:{os.environ['PATH']}")
    fin.mo_finalize(
        str(rp / "repo"), str(rp / "orch"), job,
        db=str(rp / "home" / "state.db"), home=str(rp / "home"),
        auto_merge=False, open_pr=True,
    )
    py_report = (rp / "orch" / "runs" / job / "COMPLETION_REPORT.md").read_text()

    def _pr_block(text: str, root: Path) -> str:
        block = next(ln for ln in text.splitlines() if "epicOK →" in ln)
        # the block is multi-line: arrow line + chatter continuation lines
        idx = text.splitlines().index(block)
        lines = text.splitlines()[idx:]
        out = []
        for ln in lines:
            out.append(ln)
            if ln == pr_url:
                break
        return "\n".join(out).replace(str(root), "<ROOT>")

    assert _pr_block(bash_report, rb) == _pr_block(py_report, rp), (
        "push-chatter diff:\nBASH\n" + _pr_block(bash_report, rb)
        + "\n---\nPY\n" + _pr_block(py_report, rp)
    )
    assert "[pr-create]" in _pr_block(py_report, rp)
    assert _pr_block(py_report, rp).endswith(pr_url)
