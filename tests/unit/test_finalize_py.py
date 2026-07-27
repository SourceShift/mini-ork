"""Unit tests: mini_ork.recovery.finalize (bash parity halves removed; formerly vs lib/finalize.sh).

Runs the Python port's ``mo_finalize`` against a fixture (run_dir + SQLite
DB + git repo) and asserts the rendered COMPLETION_REPORT.md semantically.
No mocks.

WARNING — auto-merge + open-pr in tests:
  The auto-merge phase acquires a mkdir-based cross-job mutex at
  ``$MINI_ORK_HOME/locks/main-merge.lock``. Tests that don't exercise the
  wiring pass ``auto_merge=False, open_pr=False`` to skip the mutex and
  the gh auth boundary.

Schema bootstrap: ``mo_finalize`` queries ``epics.kickoff_path`` and
``mini_orch_sessions.{reused_count, cost_usd, job_id}``. The bootstrap
path is ``db/init.sh`` (applies 0001..N migrations including 0002
mini_orch_sessions + 0028 epics_pr_url).

Cases:
  (a) empty run dir            → "_No cache hits this run_" +
                                  "_No spec-author iters found…_"
  (b) one epic + APPROVE       → "Final verdict: **APPROVE** (iter-1)"
                                  + Branch + commits-ahead
  (c) one epic NO verdict.json → "Final verdict: **UNKNOWN** (iter-none)"
  (d) two iters + result JSONL → cost trace 2 rows + TOTAL row
  (e) probe arm + control arm  → A/B table probe=1 control=1
  (f) mini_orch_sessions row   → "Total dollars saved: $X.XX"
  (g) error_max_budget         → "Stage failures detected:" bullet
  (h) combined                 → section-ordering invariant
  (i) prompt-cache section     → native lane_helpers.aggregate_cache_stats
  (j) auto-merge wiring        → merge.log + report section + git effects
  (k) open-PR wiring           → report arrow line + epics.pr_url
  (l) open-PR push chatter     → [pr-create] lines in report
"""
from __future__ import annotations

import contextlib
import io
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

DB_INIT = REPO / "db" / "init.sh"


def _which_tools() -> None:
    for tool in ("bash", "sqlite3", "git"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not DB_INIT.exists():
        pytest.skip(f"missing db/init.sh at {DB_INIT}")


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def fixture(tmp_path_factory):
    """Bootstrap: fresh DB via db/init.sh, fresh git repo with main, fresh
    job_run_dir, and a parameterised epic/iter builder the cases fill in."""
    _which_tools()
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(DB_INIT)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    repo = tmp_path_factory.mktemp("repo")
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
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
    ko = Path(fx["repo"]) / kickoff_rel_path
    ko.parent.mkdir(parents=True, exist_ok=True)
    branch_line = (
        f"> **Branch:** `{branch}`\n" if branch else "**Branch:** ``\n"
    )
    ko.write_text(
        f"# Kickoff for {epic_id}\n\n{branch_line}\nOther content.\n"
    )

    con = sqlite3.connect(fx["db"])
    con.execute(
        "INSERT OR REPLACE INTO epics (id, title, status, kickoff_path) "
        "VALUES (?, ?, 'in progress', ?)",
        (epic_id, epic_id, kickoff_rel_path),
    )
    con.commit()
    con.close()

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


def _py_mo_finalize(fx: dict) -> str:
    return fin.mo_finalize(
        fx["repo"], fx["orch_dir"], fx["job_id"],
        db=fx["db"], home=fx["home"],
        auto_merge=False, open_pr=False,
    )


def _read(p: str) -> str:
    return Path(p).read_text(encoding="utf-8")


def _report(fx: dict) -> str:
    return _read(_py_mo_finalize(fx))


# ─────────────────────────────────────────────────────────────────────────────
# (a) empty run dir
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_run_dir(fixture):
    py_report = _report(fixture)
    assert f"# Mini-ork completion report — {fixture['job_id']}" in py_report
    assert "_No cache hits this run (cold cache or first dispatch)._" in py_report
    assert "_No spec-author iters found in this run._" in py_report
    assert "TOTAL" in py_report


# ─────────────────────────────────────────────────────────────────────────────
# (b) one epic with verdict.json=APPROVE + commits-ahead
# ─────────────────────────────────────────────────────────────────────────────
def test_one_epic_approve_commits(fixture):
    _make_branch(fixture["repo"], "feat/epic-A", "epic-A change")
    _seed_epic(
        fixture, "epic-A", "kickoffs/epic-A.md", "feat/epic-A",
        [
            {"n": 1, "verdict": {"verdict": "APPROVE"}, "logs": []},
        ],
    )
    py_report = _report(fixture)
    assert "### epic-A" in py_report
    assert "Final verdict: **APPROVE** (iter-1)" in py_report
    assert "Branch: `feat/epic-A`" in py_report
    assert "Commits ahead of main:" in py_report
    assert "epic-A change" in py_report


# ─────────────────────────────────────────────────────────────────────────────
# (c) one epic with NO verdict.json → UNKNOWN
# ─────────────────────────────────────────────────────────────────────────────
def test_unknown_verdict(fixture):
    _make_branch(fixture["repo"], "feat/epic-B", "epic-B change")
    _seed_epic(
        fixture, "epic-B", "kickoffs/epic-B.md", "feat/epic-B",
        [
            {"n": 1, "verdict": None, "logs": []},
        ],
    )
    py_report = _report(fixture)
    assert "Final verdict: **UNKNOWN** (iter-none)" in py_report


# ─────────────────────────────────────────────────────────────────────────────
# (d) two iters each with result-log JSONL → cost trace 2 rows + TOTAL
# ─────────────────────────────────────────────────────────────────────────────
def test_cost_trace_two_iters(fixture):
    _make_branch(fixture["repo"], "feat/epic-D", "epic-D change")
    lines1 = [
        {"type": "init", "model": "sonnet"},
        {"type": "result", "total_cost_usd": 0.12, "num_turns": 3,
         "subtype": "success"},
    ]
    lines2 = [
        {"type": "init", "model": "haiku"},
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
    py_report = _report(fixture)
    assert "epic-D/i1/worker" in py_report and "0.1200" in py_report
    assert "epic-D/i2/worker" in py_report and "0.0700" in py_report
    # grand total of 0.12 + 0.07 = 0.1900
    total_line = next(ln for ln in py_report.splitlines() if ln.startswith("TOTAL"))
    assert total_line.rstrip().endswith("0.1900")


# ─────────────────────────────────────────────────────────────────────────────
# (e) probe arm + control arm → A/B table
# ─────────────────────────────────────────────────────────────────────────────
def test_ab_probe(fixture):
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
    py_report = _report(fixture)
    nc = next(ln for ln in py_report.splitlines() if ln.startswith("no-context"))
    ctl = next(ln for ln in py_report.splitlines() if ln.startswith("control"))
    # probe arm: 1 iter, spec-author sum 0.0800, 0 approves, 1 reject
    nc_fields = nc.split()
    assert nc_fields[1] == "1"
    assert nc_fields[2] == "0.0800"
    assert nc_fields[3] == "0" and nc_fields[4] == "1"
    # control arm: 1 iter, sum 0.0500, 1 approve, 0 rejects
    ctl_fields = ctl.split()
    assert ctl_fields[1] == "1"
    assert ctl_fields[2] == "0.0500"
    assert ctl_fields[3] == "1" and ctl_fields[4] == "0"


# ─────────────────────────────────────────────────────────────────────────────
# (f) mini_orch_sessions row → cache reuse Total dollars saved
# ─────────────────────────────────────────────────────────────────────────────
def test_cache_reuse_dollars_saved(fixture):
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

    py_report = _report(fixture)
    assert "**Total dollars saved by cache hits: $1.00**" in py_report
    assert "Replay cache state:" in py_report


# ─────────────────────────────────────────────────────────────────────────────
# (g) error_max_budget_usd subtype → stage failures bullet
# ─────────────────────────────────────────────────────────────────────────────
def test_stage_failures_bullet(fixture):
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
    py_report = _report(fixture)
    assert "**Stage failures detected:**" in py_report
    assert "Budget-cap (error_max_budget_usd) hits: **1**" in py_report
    assert "Other stage errors: **1**" in py_report


# ─────────────────────────────────────────────────────────────────────────────
# (h) combined: 1 epic with APPROVE + 1 result log + cache row.
# Stresses the section-ordering invariant: Epics → Cache → Cost →
# A/B → Next actions, all in one report.
# ─────────────────────────────────────────────────────────────────────────────
def test_combined_sections(fixture):
    _make_branch(fixture["repo"], "feat/epic-H", "epic-H change")
    _make_branch(fixture["repo"], "feat/epic-I", "epic-I change")
    lines = [
        {"type": "init", "model": "sonnet"},
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

    py_report = _report(fixture)
    # both epics present
    assert "### epic-H" in py_report and "### epic-I" in py_report
    # section ordering invariant
    i_epics = py_report.index("## Epics")
    i_cache = py_report.index("## Cache reuse this run\n")
    i_cost = py_report.index("## Cost trace")
    i_ab = py_report.index("## No-context A/B probe")
    i_next = py_report.index("## Next actions")
    assert i_epics < i_cache < i_cost < i_ab < i_next
    # cost rows + cache-savings line
    assert "0.2100" in py_report and "0.0900" in py_report
    assert "**Total dollars saved by cache hits: $1.25**" in py_report


# ─────────────────────────────────────────────────────────────────────────────
# (i) prompt-cache section with real cache-token logs → native
#     lane_helpers.aggregate_cache_stats
# ─────────────────────────────────────────────────────────────────────────────
def test_prompt_cache_section(fixture):
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
    py_report = _report(fixture)
    # Section is present and carries the aggregated numbers.
    assert "## Cache reuse this run (prompt cache)" in py_report
    assert "| epic-P | 1 | 50 | 100 | 10 | 31.2% | $0.0001 |" in py_report
    assert "| epic-P | 2 | 0 | 0 | 0 | 0.0% | $0.0000 |" in py_report


# ─────────────────────────────────────────────────────────────────────────────
# (j) auto-merge wiring — log/log_raw sinks → merge.log + stdout tee
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


def test_auto_merge_wiring(tmp_path):
    _build_am_tree(tmp_path / "p")
    rp = tmp_path / "p"
    job = "job-am"

    # ── python side: native wiring, stdout captured ──
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

    assert "[auto-merge]" in py_mergelog
    assert "merged=1 skipped=1 failed=0" in py_mergelog
    assert py_tee, "no [auto-merge] tee lines on stdout"
    assert "## Auto-merge results" in py_report

    # ── effects: tree / epics status / runs verdict / branch deletion ──
    def _g(*a):
        return subprocess.run(
            ["git", "-C", str(rp / "repo"), *a],
            capture_output=True, text=True).stdout.strip()
    con = sqlite3.connect(str(rp / "home" / "state.db"))
    st = con.execute(
        "SELECT status FROM epics WHERE id='epicOK'").fetchone()[0]
    rv = con.execute(
        "SELECT final_verdict FROM runs WHERE epic_id='epicOK'").fetchone()[0]
    con.close()
    assert st == "done"
    assert rv == "MERGED"
    assert _g("rev-parse", "--verify", "-q", "feat/ok") == ""
    assert _g("cat-file", "-p", "main:feature.txt") == "the feature"


# ─────────────────────────────────────────────────────────────────────────────
# (k) open-PR wiring — stub `gh` + a pre-pushed branch
# ─────────────────────────────────────────────────────────────────────────────
def test_open_pr_wiring(tmp_path, monkeypatch):
    pr_url = "https://github.com/acme/widgets/pull/42"

    fx = _build_am_tree(tmp_path / "p")
    rp = tmp_path / "p"
    repo = fx["repo"]
    bare = rp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "-C", repo, "remote", "add", "origin", str(bare)],
                   check=True)
    # pre-push: branch already on origin → no push chatter in open_pr
    subprocess.run(["git", "-C", repo, "push", "-q", "origin", "feat/ok"],
                   check=True)
    bin_dir = rp / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f'#!/usr/bin/env bash\necho "{pr_url}"\n')
    gh.chmod(0o755)
    job = "job-am"

    monkeypatch.setenv("GH_TOKEN", "fake")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    fin.mo_finalize(
        repo, fx["orch"], job,
        db=fx["db"], home=fx["home"],
        auto_merge=False, open_pr=True,
    )
    py_report = (rp / "orch" / "runs" / job / "COMPLETION_REPORT.md").read_text()

    line_p = next(ln for ln in py_report.splitlines() if "epicOK →" in ln)
    assert line_p == f"  epicOK → {pr_url}"

    con = sqlite3.connect(fx["db"])
    v = con.execute(
        "SELECT pr_url FROM epics WHERE id='epicOK'").fetchone()[0]
    con.close()
    assert v == pr_url


# ─────────────────────────────────────────────────────────────────────────────
# (l) open-PR wiring WITHOUT the pre-push — [pr-create] chatter lines
# ─────────────────────────────────────────────────────────────────────────────
def test_open_pr_wiring_push_chatter(tmp_path, monkeypatch):
    """Variant of (k) WITHOUT the pre-push: the branch must be pushed, so
    the `git push` chatter lands in the report's Next-actions line as
    ``[pr-create]``-prefixed lines ending with the PR URL."""
    pr_url = "https://github.com/acme/widgets/pull/42"

    fx = _build_am_tree(tmp_path / "p")
    rp = tmp_path / "p"
    repo = fx["repo"]
    bare = rp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "-C", repo, "remote", "add", "origin", str(bare)],
                   check=True)
    bin_dir = rp / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f'#!/usr/bin/env bash\necho "{pr_url}"\n')
    gh.chmod(0o755)
    job = "job-am"

    monkeypatch.setenv("GH_TOKEN", "fake")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    fin.mo_finalize(
        repo, fx["orch"], job,
        db=fx["db"], home=fx["home"],
        auto_merge=False, open_pr=True,
    )
    py_report = (rp / "orch" / "runs" / job / "COMPLETION_REPORT.md").read_text()

    block = next(ln for ln in py_report.splitlines() if "epicOK →" in ln)
    idx = py_report.splitlines().index(block)
    lines = py_report.splitlines()[idx:]
    out = []
    for ln in lines:
        out.append(ln)
        if ln == pr_url:
            break
    pr_block = "\n".join(out)
    assert "[pr-create]" in pr_block
    assert pr_block.endswith(pr_url)
