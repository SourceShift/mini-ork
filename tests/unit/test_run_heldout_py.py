"""Unit tests for scripts/run_heldout.py against a real temp git repo.

Mirrors the pattern in tests/unit/test_mine_heldout_tasks_py.py: a small
two-commit repo (base buggy, fix gold) is built per test and the runner is
exercised with deterministic ``--solver-cmd`` shell snippets — no LLM is
ever invoked from the test suite.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import run_heldout as r  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, msg: str, files: dict[str, str]) -> str:
    for rel, body in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


def _mk_minimal_manifest(tmp_path: Path, shas: dict[str, str]) -> tuple[Path, dict]:
    """Three tasks: one dev, one test-split, one weak-signal. Real shas."""
    manifest_path = tmp_path / "manifest.json"
    base_sha = shas["base"]
    fix_sha = shas["fix"]
    base_id = f"mo-{base_sha[:10]}"
    fix_id = f"mo-{fix_sha[:10]}"
    tasks = [
        {
            "id": base_id,
            "fix_sha": fix_sha,
            "base_sha": base_sha,
            "problem_statement": "fix: add subtracted instead of adding\n\nadd(2, 3) returned -1.",
            "src_files": ["calc.py"],
            "test_files": ["tests/test_calc.py"],
            "fail_to_pass": ["tests.test_calc::test_add"],
            "pass_to_pass": ["tests.test_calc::test_neg"],
            "split": "dev",
            "difficulty": "easy",
            "weak_signal": False,
        },
        {
            "id": f"{fix_id}-test",
            "fix_sha": fix_sha,
            "base_sha": base_sha,
            "problem_statement": "fix: same fix, but on the test split",
            "src_files": ["calc.py"],
            "test_files": ["tests/test_calc.py"],
            "fail_to_pass": ["tests.test_calc::test_add"],
            "pass_to_pass": ["tests.test_calc::test_neg"],
            "split": "test",
            "difficulty": "easy",
            "weak_signal": False,
        },
        {
            "id": f"{fix_id}-weak",
            "fix_sha": fix_sha,
            "base_sha": base_sha,
            "problem_statement": "fix: same fix again, weak-signal flavour",
            "src_files": ["calc.py"],
            "test_files": ["tests/test_calc.py"],
            "fail_to_pass": ["tests.test_calc::test_add"],
            "pass_to_pass": [],
            "split": "dev",
            "difficulty": "easy",
            "weak_signal": True,
        },
    ]
    manifest_path.write_text(json.dumps(tasks, indent=2))
    return manifest_path, tasks[0]


def _mk_history(tmp_path: Path) -> dict[str, str]:
    """Two commits: base (buggy calc) + fix (gold src + hidden test)."""
    repo = tmp_path / "src_repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    shas = {}
    shas["base"] = _commit(repo, "chore: base", {
        "calc.py": "def add(a, b):\n    return a - b\n\ndef neg(a):\n    return -a\n",
        "tests/test_calc.py": "from calc import neg\n\ndef test_neg():\n    assert neg(2) == -2\n",
    })
    shas["fix"] = _commit(
        repo,
        "fix: add subtracted instead of adding\n\nadd(2, 3) returned -1.",
        {
            "calc.py": "def add(a, b):\n    return a + b\n\ndef neg(a):\n    return -a\n",
            "tests/test_calc.py": (
                "from calc import add, neg\n\ndef test_neg():\n    assert neg(2) == -2\n\n"
                "def test_add():\n    assert add(2, 3) == 5\n"
            ),
        },
    )
    return {"repo": str(repo), **shas}


# ── solver-cmd stubs ──────────────────────────────────────────────────────────


def _gold_solver_str() -> str:
    """``--solver-cmd`` value: a Python one-liner that restores the gold src.

    The runner shlex-splits ``--solver-cmd``, so we shlex-quote here so the
    inner commas/brackets survive. The worktree is checked out at base, so
    the fix commit isn't reachable from HEAD via ``git log`` — ``--all``
    walks future commits so the gold sha can still be found.
    """
    code = (
        "import os, subprocess;"
        "s=os.environ['SCRATCH'];"
        "fix=subprocess.run(['git','-C',s,'log','--all','--format=%H','--','calc.py'],"
        "capture_output=True,text=True).stdout.splitlines()[0];"
        "subprocess.run(['git','-C',s,'checkout','-q',fix,'--','calc.py'],check=True)"
    )
    return " ".join(shlex.quote(tok) for tok in [sys.executable, "-c", code])


def _noop_solver_str() -> str:
    return shlex.quote("true")


def _tamper_solver_str() -> str:
    """Overwrite the test file then apply the gold src.

    Grading restores the test file from ``fix_sha`` before pytest, so the
    tamper is wiped and the task passes only because the source change is real.
    """
    code = (
        "import os, subprocess;"
        "s=os.environ['SCRATCH'];"
        "open(os.path.join(s,'tests','test_calc.py'),'w').write("
        "'from calc import neg\\ndef test_neg():\\n    assert neg(2) == -2\\n'"
        "'\\ndef test_add():\\n    assert 1 + 1 == 2\\n');"
        "fix=subprocess.run(['git','-C',s,'log','--all','--format=%H','--','calc.py'],"
        "capture_output=True,text=True).stdout.splitlines()[0];"
        "subprocess.run(['git','-C',s,'checkout','-q',fix,'--','calc.py'],check=True)"
    )
    return " ".join(shlex.quote(tok) for tok in [sys.executable, "-c", code])


def _fail_if_run_solver_str() -> str:
    """If this ever runs the second-invocation test fails — the resume code
    should skip done tasks before invoking the solver at all."""
    return shlex.quote("bash") + " " + shlex.quote("-c") + " " + shlex.quote(
        "echo solver-ran >> /tmp/should-never-write"
    )


# ── tests ────────────────────────────────────────────────────────────────────


def test_gold_solver_grades_passed(tmp_path):
    h = _mk_history(tmp_path)
    manifest_path, first_task = _mk_minimal_manifest(tmp_path, h)
    out = tmp_path / "results.json"

    rc = r.main([
        "--manifest", str(manifest_path),
        "--repo", h["repo"],
        "--out", str(out),
        "--python", sys.executable,
        "--timeout", "120",
        "--solver-cmd", _gold_solver_str(),
    ])

    assert rc == 0
    rows = json.loads(out.read_text())
    assert rows[first_task["id"]]["passed"] is True
    assert rows[first_task["id"]]["solver_rc"] == 0
    assert rows[first_task["id"]]["failed_ids"] == []


def test_noop_solver_grades_failed_and_records_fail_to_pass(tmp_path):
    h = _mk_history(tmp_path)
    manifest_path, first_task = _mk_minimal_manifest(tmp_path, h)
    out = tmp_path / "results.json"

    rc = r.main([
        "--manifest", str(manifest_path),
        "--repo", h["repo"],
        "--out", str(out),
        "--python", sys.executable,
        "--timeout", "120",
        "--solver-cmd", _noop_solver_str(),
    ])

    assert rc == 0
    rows = json.loads(out.read_text())
    assert rows[first_task["id"]]["passed"] is False
    assert "tests.test_calc::test_add" in rows[first_task["id"]]["failed_ids"]


def test_solver_tampering_with_tests_still_passes_grading(tmp_path):
    """If a solver edits the test file, grading restores tests from fix_sha
    BEFORE running pytest, so the tamper is wiped and the task passes only
    because the SOURCE change is real."""
    h = _mk_history(tmp_path)
    manifest_path, first_task = _mk_minimal_manifest(tmp_path, h)
    out = tmp_path / "results.json"

    rc = r.main([
        "--manifest", str(manifest_path),
        "--repo", h["repo"],
        "--out", str(out),
        "--python", sys.executable,
        "--timeout", "120",
        "--solver-cmd", _tamper_solver_str(),
    ])

    assert rc == 0
    rows = json.loads(out.read_text())
    assert rows[first_task["id"]]["passed"] is True


def test_kickoff_does_not_leak_test_files_paths(tmp_path):
    """The kickoff written for the solver must not mention any test_files path."""
    h = _mk_history(tmp_path)
    _, first_task = _mk_minimal_manifest(tmp_path, h)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    kickoff_text = r._render_kickoff(first_task, scratch)

    for f in first_task["test_files"]:
        assert f not in kickoff_text, f"kickoff leaked test file path: {f}"
    assert "test_add" not in kickoff_text


def test_split_test_without_allow_flag_exits_nonzero(tmp_path):
    h = _mk_history(tmp_path)
    manifest_path, _ = _mk_minimal_manifest(tmp_path, h)
    out = tmp_path / "results.json"

    rc = r.main([
        "--manifest", str(manifest_path),
        "--repo", h["repo"],
        "--split", "test",
        "--out", str(out),
        "--python", sys.executable,
        "--timeout", "60",
        "--solver-cmd", _noop_solver_str(),
    ])

    assert rc != 0


def test_split_test_with_allow_flag_runs(tmp_path):
    h = _mk_history(tmp_path)
    manifest_path, _ = _mk_minimal_manifest(tmp_path, h)
    out = tmp_path / "results.json"

    rc = r.main([
        "--manifest", str(manifest_path),
        "--repo", h["repo"],
        "--split", "test",
        "--allow-test-split",
        "--out", str(out),
        "--python", sys.executable,
        "--timeout", "60",
        "--solver-cmd", _noop_solver_str(),
    ])

    assert rc == 0
    rows = json.loads(out.read_text())
    test_task_id = f"mo-{h['fix'][:10]}-test"
    assert test_task_id in rows


def test_second_invocation_skips_done_tasks(tmp_path):
    h = _mk_history(tmp_path)
    manifest_path, first_task = _mk_minimal_manifest(tmp_path, h)
    out = tmp_path / "results.json"

    argv = [
        "--manifest", str(manifest_path),
        "--repo", h["repo"],
        "--out", str(out),
        "--python", sys.executable,
        "--timeout", "120",
        "--solver-cmd", _gold_solver_str(),
    ]
    assert r.main(argv) == 0
    snapshot = out.read_text()

    # Second run with a "this must never run" solver — the resume code must
    # skip done tasks before invoking anything.
    argv_fail = list(argv)
    argv_fail[-1] = _fail_if_run_solver_str()
    assert r.main(argv_fail) == 0

    assert out.read_text() == snapshot
    rows = json.loads(out.read_text())
    assert rows[first_task["id"]]["passed"] is True


def test_worktree_is_gone_after_each_task(tmp_path):
    h = _mk_history(tmp_path)
    manifest_path, _ = _mk_minimal_manifest(tmp_path, h)
    out = tmp_path / "results.json"

    repo = Path(h["repo"])
    before = _git(repo, "worktree", "list", "--porcelain")

    rc = r.main([
        "--manifest", str(manifest_path),
        "--repo", h["repo"],
        "--out", str(out),
        "--python", sys.executable,
        "--timeout", "120",
        "--solver-cmd", _gold_solver_str(),
    ])
    assert rc == 0

    after = _git(repo, "worktree", "list", "--porcelain")
    assert before == after, "scratch worktree leaked into the host repo's worktree list"


def test_dry_run_prints_task_ids_and_writes_nothing(tmp_path):
    h = _mk_history(tmp_path)
    manifest_path, first_task = _mk_minimal_manifest(tmp_path, h)
    out = tmp_path / "should-not-exist.json"

    captured = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "run_heldout.py"),
         "--manifest", str(manifest_path),
         "--repo", h["repo"],
         "--out", str(out),
         "--dry-run",
         "--limit", "1"],
        capture_output=True, text=True, check=True,
    )

    ids = [ln.strip() for ln in captured.stdout.splitlines() if ln.strip()]
    assert ids == [first_task["id"]]
    assert not out.exists()


def test_exclude_weak_drops_weak_signal_tasks(tmp_path):
    h = _mk_history(tmp_path)
    manifest_path, first_task = _mk_minimal_manifest(tmp_path, h)
    out = tmp_path / "results.json"

    rc = r.main([
        "--manifest", str(manifest_path),
        "--repo", h["repo"],
        "--out", str(out),
        "--exclude-weak",
        "--python", sys.executable,
        "--timeout", "120",
        "--solver-cmd", _gold_solver_str(),
    ])
    assert rc == 0

    rows = json.loads(out.read_text())
    assert first_task["id"] in rows
    weak_id = f"mo-{h['fix'][:10]}-weak"
    assert weak_id not in rows


def test_difficulty_filter_picks_only_matching_tasks(tmp_path):
    h = _mk_history(tmp_path)
    manifest_path, _ = _mk_minimal_manifest(tmp_path, h)
    out = tmp_path / "results.json"

    rc = r.main([
        "--manifest", str(manifest_path),
        "--repo", h["repo"],
        "--out", str(out),
        "--difficulty", "hard",
        "--python", sys.executable,
        "--timeout", "60",
        "--solver-cmd", _noop_solver_str(),
    ])
    assert rc == 0
    assert out.is_file()
    rows = json.loads(out.read_text())
    assert rows == {}

# ── review repairs ───────────────────────────────────────────────────────────


def _tamper_only_solver_str() -> str:
    """Rewrite the test to a tautology and change NO source: a pure cheat."""
    code = (
        "import os;s=os.environ['SCRATCH'];"
        "open(os.path.join(s,'tests','test_calc.py'),'w').write("
        "'def test_neg():\\n    assert True\\n\\ndef test_add():\\n    assert True\\n')"
    )
    return " ".join(shlex.quote(t) for t in [sys.executable, "-c", code])


def test_a_solver_that_only_rewrites_the_tests_fails(tmp_path):
    """The anti-cheat property itself. The tamper test above also applies the
    real fix, so it passes whether or not grading restores the hidden tests."""
    h = _mk_history(tmp_path)
    manifest_path, first_task = _mk_minimal_manifest(tmp_path, h)
    out = tmp_path / "results.json"

    r.main(["--manifest", str(manifest_path), "--repo", h["repo"], "--out", str(out),
            "--python", sys.executable, "--timeout", "120",
            "--solver-cmd", _tamper_only_solver_str()])

    assert json.loads(out.read_text())[first_task["id"]]["passed"] is False


def test_kickoff_and_scratch_stay_out_of_the_solver_tree_and_repo(tmp_path):
    """A kickoff inside the scratch tree is harvested as a solver-created file;
    a scratch inside the repo shows up untracked there."""
    h = _mk_history(tmp_path)
    manifest_path, first_task = _mk_minimal_manifest(tmp_path, h)
    out = tmp_path / "results.json"
    code = (
        "import os,sys;s=os.path.realpath(os.environ['SCRATCH']);"
        "k=os.path.realpath(os.environ['KICKOFF']);repo=os.path.realpath(sys.argv[1]);"
        "bad=k.startswith(s+os.sep) or s.startswith(repo+os.sep) or os.path.exists(os.path.join(s,'KICKOFF.md'));"
        "sys.exit(3 if bad else 0)"
    )
    solver = " ".join(shlex.quote(t) for t in [sys.executable, "-c", code, h["repo"]])

    r.main(["--manifest", str(manifest_path), "--repo", h["repo"], "--out", str(out),
            "--python", sys.executable, "--timeout", "120", "--solver-cmd", solver])

    assert json.loads(out.read_text())[first_task["id"]]["solver_rc"] == 0
    assert _git(Path(h["repo"]), "status", "--porcelain") == ""


def test_recipe_flag_reaches_the_default_solver(tmp_path, monkeypatch):
    h = _mk_history(tmp_path)
    manifest_path, _ = _mk_minimal_manifest(tmp_path, h)
    seen = []

    def fake_solver(scratch, kickoff_path, task_id, *, timeout, recipe="code-fix"):
        seen.append(recipe)
        return 0, 0.0

    monkeypatch.setattr(r, "_run_default_solver", fake_solver)
    r.main(["--manifest", str(manifest_path), "--repo", h["repo"],
            "--out", str(tmp_path / "results.json"), "--python", sys.executable,
            "--timeout", "120", "--recipe", "framework-edit"])

    assert seen and set(seen) == {"framework-edit"}


def test_run_cost_is_read_from_the_ledger(tmp_path, monkeypatch):
    """mini_ork_result carries no cost field, so the sink parse alone always
    reported $0 and cost-per-resolve was meaningless."""
    import sqlite3
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE execution_traces (run_id TEXT, cost_usd REAL)")
    con.executemany("INSERT INTO execution_traces VALUES (?, ?)",
                    [("heldout-t1-1", 1.25), ("heldout-t1-1", 0.5), ("other", 9.0)])
    con.commit()
    con.close()
    monkeypatch.setenv("MINI_ORK_DB", str(db))

    assert r._run_cost_usd("heldout-t1-1") == 1.75
    assert r._run_cost_usd("missing") == 0.0


def test_default_solver_runs_from_the_engine_root_not_the_scratch(tmp_path, monkeypatch):
    """The scratch is an older mini-ork checkout; with cwd=scratch, `python -m`
    imported the TASK's mini_ork and the engine crashed before planning."""
    seen = {}

    class _P:
        returncode, stdout = 0, ""

    def fake_run(cmd, **kw):
        seen.update(kw)
        return _P()

    monkeypatch.setattr(r.subprocess, "run", fake_run)
    monkeypatch.setattr(r, "_run_cost_usd", lambda run_id: 0.0)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    r._run_default_solver(scratch, tmp_path / "k.md", "t1", timeout=5, recipe="code-fix")

    assert Path(seen["cwd"]) == r.REPO
    assert seen["env"]["MO_TARGET_CWD"] == str(scratch)
    # The runner grades afterwards, so mini-ork must not roll the edit back.
    assert seen["env"]["MINI_ORK_ROLLBACK_KEEP_WORKTREE"] == "1"


def test_summary_counts_resolved_not_attempted(tmp_path, capsys):
    """A run where every task failed printed 'resolved 2 / attempted 2'."""
    h = _mk_history(tmp_path)
    manifest_path, _ = _mk_minimal_manifest(tmp_path, h)

    r.main(["--manifest", str(manifest_path), "--repo", h["repo"],
            "--out", str(tmp_path / "results.json"), "--python", sys.executable,
            "--timeout", "120", "--solver-cmd", _noop_solver_str()])

    assert "resolved 0 / attempted" in capsys.readouterr().err
