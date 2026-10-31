"""Tests for the harvester's host-path relativisation and ``host-path`` rejection.

A frozen framework-edit probe must be runnable on another host and in another
copy of the repo: a failed run's kickoff is written for one worktree and one
virtualenv, so ``failed_run_candidates`` makes checkout paths repo-relative,
rewrites a venv interpreter to ``python3``, and rejects a candidate that still
carries an absolute host path.

Hermetic: scratch git repo and scratch mini-ork home under ``tmp_path``, a faked
``diagnose``, no network and no provider credentials. The repo/home builders are
reused from :mod:`test_probe_harvest_git`; its autouse ``harness`` fixture does
not follow an import, so this module installs its own.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from mini_ork.learning import probe_harvest

_UNIT_DIR = Path(__file__).resolve().parent
if str(_UNIT_DIR) not in sys.path:
    sys.path.insert(0, str(_UNIT_DIR))
from test_probe_harvest_git import (  # noqa: E402
    K1_TEXT,
    _build_repo,
    _home,
    _row,
)


@pytest.fixture(autouse=True)
def _isolated_harvest(tmp_path, monkeypatch):
    """Set the scratch dir and fake ``diagnose`` for every test."""
    monkeypatch.setenv("MO_APPLY_PROBE_SCRATCH_DIR", str(tmp_path / "scratch"))

    def fake_diagnose(home, run_id, *, write=True, allow_model=False):
        assert write is False, "harvest must call diagnose with write=False"
        return {"layer": "change", "rule_id": "R5", "node": "test_verifier"}

    monkeypatch.setattr("mini_ork.recovery.diagnose.diagnose", fake_diagnose)


def _git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_out(root, *args) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _commit(root, message) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git_out(root, "rev-parse", "HEAD")


def _write_kickoff(tmp_path, text) -> str:
    path = tmp_path / "kickoffs" / "w2" / "probe.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


# ── relativize_host_paths ────────────────────────────────────────────────


def test_bare_checkout_becomes_dot():
    result = probe_harvest.relativize_host_paths(
        "Worktree: /x/wt/slug", ["/x/wt/slug"]
    )
    assert result == ("Worktree: .", [])


def test_checkout_prefixed_path_made_repo_relative():
    result = probe_harvest.relativize_host_paths(
        "/x/wt/slug/mini_ork/a.py", ["/x/wt/slug"]
    )
    assert result == ("mini_ork/a.py", [])


def test_nested_checkout_wins():
    result = probe_harvest.relativize_host_paths(
        "/x/wt/slug/inner/f.py", ["/x/wt/slug/inner", "/x/wt/slug"]
    )
    assert result == ("f.py", [])


def test_venv_python_becomes_python3():
    result = probe_harvest.relativize_host_paths(
        "/Volumes/a/.venv/bin/python3.11 -m pytest", []
    )
    assert result == ("python3 -m pytest", [])


def test_venv_rule_runs_before_checkout_stripping():
    # Once '<checkout>/' is stripped the interpreter no longer matches
    # _VENV_PYTHON (it needs a '/' before '.venv'), so only a venv-first
    # order yields 'python3 -m pytest'.
    result = probe_harvest.relativize_host_paths(
        "/x/wt/slug/.venv/bin/python3.11 -m pytest", ["/x/wt/slug"]
    )
    assert result == ("python3 -m pytest", [])


def test_other_project_home_is_a_leftover():
    result = probe_harvest.relativize_host_paths(
        "/Users/me/other-project/.mini-ork", ["/x/wt/slug"]
    )
    assert result == (
        "/Users/me/other-project/.mini-ork",
        ["/Users/me/other-project/.mini-ork"],
    )


def test_tmp_helper_is_a_leftover():
    result = probe_harvest.relativize_host_paths(
        "Run /tmp/helper.sh before the suite.", ["/x/wt/slug"]
    )
    assert result == (
        "Run /tmp/helper.sh before the suite.",
        ["/tmp/helper.sh"],
    )


def test_url_substring_is_not_a_leftover():
    result = probe_harvest.relativize_host_paths(
        "See https://example.com/Users/x for context.", ["/x/wt/slug"]
    )
    assert result == ("See https://example.com/Users/x for context.", [])


def test_path_free_text_unchanged():
    text = "Edit calc.py so the import works and keep the suite green."
    result = probe_harvest.relativize_host_paths(text, ["/x/wt/slug"])
    assert result == (text, [])


# ── repo_checkouts ───────────────────────────────────────────────────────


def test_repo_checkouts_lists_worktrees_longest_first(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, check=True)
    _git(repo, "config", "user.email", "spec@example.com")
    _git(repo, "config", "user.name", "Spec Test")
    (repo / "f.txt").write_text("base\n")
    _commit(repo, "base")
    worktree = tmp_path / "long-worktree-name"
    _git(repo, "worktree", "add", str(worktree))

    expected = sorted(
        {str(repo.resolve()), str(worktree.resolve())},
        key=len,
        reverse=True,
    )
    assert probe_harvest.repo_checkouts(str(repo)) == expected


def test_repo_checkouts_non_git_dir_falls_back_to_realpath(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert probe_harvest.repo_checkouts(str(plain)) == [str(plain.resolve())]


# ── failed_run_candidates wiring ─────────────────────────────────────────


def test_root_prefixed_kickoff_becomes_repo_relative(tmp_path):
    root, c1, c2, _ = _build_repo(tmp_path)
    repo_real = str(Path(root).resolve())
    text = (
        "Fix add.\n\n"
        f"Edit files under {repo_real}/calc.py so the import works.\n\n"
        "## Files in scope\n- calc.py\n- tests/test_calc.py (new)\n"
    )
    kickoff = _write_kickoff(tmp_path, text)
    home = _home(
        tmp_path, [_row("fe-ok-1", kickoff=kickoff, created=1, baseline_ref=c1)]
    )

    candidates, rejections = probe_harvest.failed_run_candidates(
        home, root, landing_ref="HEAD"
    )

    assert rejections == []
    assert len(candidates) == 1
    statement = candidates[0]["problem_statement"]
    assert statement == text.replace(f"{repo_real}/calc.py", "calc.py")
    assert repo_real not in statement


def test_foreign_host_path_kickoff_rejected(tmp_path):
    root, c1, _c2, _ = _build_repo(tmp_path)
    text = (
        "Fix add tests/test_calc.py.\n\n"
        "Also read /Users/someone/elsewhere/file.txt for context.\n"
    )
    kickoff = _write_kickoff(tmp_path, text)
    home = _home(
        tmp_path, [_row("fe-ok-1", kickoff=kickoff, created=1, baseline_ref=c1)]
    )

    candidates, rejections = probe_harvest.failed_run_candidates(
        home, root, landing_ref="HEAD"
    )

    assert candidates == []
    assert rejections == [{"task_id": "fe-ok-1", "reason": "host-path"}]


def test_control_kickoff_without_paths_unchanged(tmp_path):
    root, c1, c2, _ = _build_repo(tmp_path)
    kickoff = _write_kickoff(tmp_path, K1_TEXT)
    home = _home(
        tmp_path, [_row("fe-ok-1", kickoff=kickoff, created=1, baseline_ref=c1)]
    )

    candidates, rejections = probe_harvest.failed_run_candidates(
        home, root, landing_ref="HEAD"
    )

    assert rejections == []
    assert len(candidates) == 1
    assert candidates[0]["problem_statement"] == K1_TEXT
    assert candidates[0]["task_id"] == "fe-" + c2[:10]


def test_answer_pointer_still_fires_before_host_path(tmp_path):
    # A kickoff with BOTH an answer pointer and a foreign host path must be
    # rejected 'answer-pointer': the host-path rule runs after it, and the
    # first rule that fires gives the reason.
    root, c1, _c2, _ = _build_repo(tmp_path, with_kickoff=True)
    pointer_run = "lane-x-20261007160033"
    text = K1_TEXT + (
        f"\nPrior attempt in run {pointer_run}; also see"
        " /Users/someone/elsewhere/file.txt.\n"
    )
    kickoff = _write_kickoff(tmp_path, text)
    home = _home(
        tmp_path, [_row("fe-ok-1", kickoff=kickoff, created=1, baseline_ref=c1)]
    )
    answer_dir = Path(home) / "runs" / pointer_run
    answer_dir.mkdir(parents=True, exist_ok=True)
    (answer_dir / "framework-edit.diff").write_text("answer\n", encoding="utf-8")

    candidates, rejections = probe_harvest.failed_run_candidates(
        home, root, landing_ref="HEAD"
    )

    assert candidates == []
    assert rejections == [{"task_id": "fe-ok-1", "reason": "answer-pointer"}]


def test_checkouts_computed_once_per_call(tmp_path, monkeypatch):
    root, c1, _c2, _ = _build_repo(tmp_path)
    texts = [K1_TEXT, K1_TEXT.replace("Fix add.", "Rename the helper.")]
    rows = []
    for index, text in enumerate(texts, start=1):
        kickoff = _write_kickoff(tmp_path / f"k{index}", text)
        rows.append(
            _row(f"fe-ok-{index}", kickoff=kickoff, created=index, baseline_ref=c1)
        )
    home = _home(tmp_path, rows)

    calls: list[str] = []
    real_repo_checkouts = probe_harvest.repo_checkouts

    def counting(root_path: str):
        calls.append(root_path)
        return real_repo_checkouts(root_path)

    monkeypatch.setattr(probe_harvest, "repo_checkouts", counting)
    candidates, rejections = probe_harvest.failed_run_candidates(
        home, root, landing_ref="HEAD"
    )

    assert rejections == []
    assert len(candidates) == 1
    assert len(calls) == 1


def test_docstring_lists_host_path_after_answer_pointer():
    doc = probe_harvest.failed_run_candidates.__doc__ or ""
    assert "answer-pointer" in doc
    assert "host-path" in doc
    assert doc.index("answer-pointer") < doc.index("host-path")
