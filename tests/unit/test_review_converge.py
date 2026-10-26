"""Review convergence: snapshots, the review delta, and the classic reviewer.

Kickoff rev6-r5a. The git helpers use a scratch repository; ``_snapshot_review_tree``
must never touch the real index, HEAD or refs, so the tests assert that too.
"""
import inspect
import json
import os
import re
import subprocess

import mini_ork
from mini_ork.cli import execute, execute_handlers


def _git(worktree, *args):
    env = dict(os.environ)
    env.pop("GIT_INDEX_FILE", None)
    proc = subprocess.run(["git", "-C", str(worktree), *args],
                          capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise AssertionError("git %s failed: %s" % (args, proc.stderr))
    return proc.stdout


def _repo(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _git(worktree, "init", "-q")
    (worktree / "a.txt").write_text("one\n")
    (worktree / "b.txt").write_text("bee\n")
    _git(worktree, "add", "-A")
    _git(worktree, "-c", "user.email=t@example.com", "-c", "user.name=T",
         "commit", "-q", "-m", "init")
    run = tmp_path / "run"
    run.mkdir()
    return worktree, run


def _review_dir(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    return run


def test_snapshot_returns_sha_and_leaves_repo_untouched(tmp_path):
    worktree, run = _repo(tmp_path)
    index = worktree / ".git" / "index"
    head_before = _git(worktree, "rev-parse", "HEAD").strip()
    mtime_before = os.stat(index).st_mtime_ns

    sha = execute._snapshot_review_tree(str(worktree), str(run), [])

    assert re.fullmatch(r"[0-9a-f]{40,64}", sha or "")
    assert (run / "review-tree.txt").read_text().strip() == sha
    assert os.stat(index).st_mtime_ns == mtime_before
    assert _git(worktree, "rev-parse", "HEAD").strip() == head_before
    assert _git(worktree, "--no-optional-locks", "status", "--porcelain") == ""
    assert not (run / ".review-index").exists()


def test_snapshot_failure_returns_empty(tmp_path):
    not_a_repo = tmp_path / "bare"
    not_a_repo.mkdir()
    run = _review_dir(tmp_path)
    assert execute._snapshot_review_tree(str(not_a_repo), str(run), []) == ""
    assert not (run / "review-tree.txt").exists()
    assert not (run / ".review-index").exists()


def test_delta_after_archive_contains_only_the_second_edit(tmp_path):
    worktree, run = _repo(tmp_path)
    assert execute._snapshot_review_tree(str(worktree), str(run), [])
    (worktree / "a.txt").write_text("one\nsecond\n")
    assert execute._snapshot_review_tree(str(worktree), str(run), [])
    execute._archive_revise_round(str(run), 1, [])
    assert (run / "revise" / "round-1" / "review-tree.txt").is_file()
    (worktree / "b.txt").write_text("bee\nedit2\n")
    assert execute._snapshot_review_tree(str(worktree), str(run), [])

    result = execute._review_delta(str(run), str(worktree), [])
    assert result is not None
    patch, files = result
    assert "edit2" in patch and "second" not in patch
    assert files == ["b.txt"]
    assert "edit2" in (run / "review-delta.patch").read_text()


def test_delta_none_without_both_snapshots(tmp_path):
    worktree, run = _repo(tmp_path)
    assert execute._review_delta(str(run), str(worktree), []) is None
    execute._snapshot_review_tree(str(worktree), str(run), [])
    assert execute._review_delta(str(run), str(worktree), []) is None
    execute._archive_revise_round(str(run), 1, [])
    (run / "review-tree.txt").write_text("")
    assert execute._review_delta(str(run), str(worktree), []) is None


def test_delta_git_failure_is_none(tmp_path):
    worktree, run = _repo(tmp_path)
    execute._snapshot_review_tree(str(worktree), str(run), [])
    execute._archive_revise_round(str(run), 1, [])
    (run / "review-tree.txt").write_text("f" * 40 + "\n")
    (run / "revise" / "round-1" / "review-tree.txt").write_text("0" * 40 + "\n")
    assert execute._review_delta(str(run), str(worktree), []) is None


def test_delta_scopes_to_specs(tmp_path):
    worktree, run = _repo(tmp_path)
    execute._snapshot_review_tree(str(worktree), str(run), [])
    (worktree / "a.txt").write_text("one\nsecond\n")
    execute._snapshot_review_tree(str(worktree), str(run), [])
    execute._archive_revise_round(str(run), 1, [])
    (worktree / "b.txt").write_text("bee\nedit2\n")
    (worktree / "sub").mkdir()
    (worktree / "sub" / "x.py").write_text("EDIT2MARK\n")
    execute._snapshot_review_tree(str(worktree), str(run), [])

    patch_all, files_all = execute._review_delta(str(run), str(worktree), [])
    assert files_all == ["b.txt", "sub/x.py"]
    assert "EDIT2MARK" in patch_all
    patch_scoped, files_scoped = execute._review_delta(
        str(run), str(worktree), ["b.txt"])
    assert files_scoped == ["b.txt"]
    assert "EDIT2MARK" not in patch_scoped


def test_review_worktree_and_specs_contract():
    signature = inspect.signature(execute._review_worktree_and_specs)
    assert list(signature.parameters) == ["run_dir"]
    # _assemble_reviewer_inputs shares the resolution and snapshots the tree
    source = inspect.getsource(execute._assemble_reviewer_inputs)
    assert "_review_worktree_and_specs" in source
    assert "_snapshot_review_tree" in source


def test_reviewer_envelope_contents():
    envelope = execute_handlers.reviewer_envelope("x.json")
    for needle in ('"findings"', '"resolved"', "instances",
                   "write that exact JSON object to x.json",
                   '"verdict": "pass|fail|needs_revision|escalate"',
                   '"notes": []', '"findings": []', '"resolved": []',
                   '"severity"',
                   "needs_revision and fail MUST carry at least one finding",
                   "report it ONCE and list every other location in instances"):
        assert needle in envelope, needle
    source = inspect.getsource(execute_handlers._handle_reviewer)
    assert "reviewer_envelope(" in source
    assert "MO_REVIEW_CONVERGE" in inspect.getsource(execute_handlers)


def test_revise_failure_section_renders_findings(tmp_path):
    run = _review_dir(tmp_path)
    payload = {
        "verdict": "needs_revision",
        "notes": ["check the loop"],
        "findings": [{"id": "F1", "file": "a.py", "line": 3,
                      "severity": "high", "issue": "off by one",
                      "instances": [{"file": "b.py", "line": 9}]}],
        "resolved": [],
    }
    for name in ("review-reviewer1.json", "reviewer1.json"):
        (run / name).write_text(json.dumps(payload))
    section = execute._revise_failure_section(
        str(run), ("reviewer1", "reviewer"), "error")
    assert "## reviewer1 (reviewer)" in section
    assert "findings:" in section
    assert "- [F1] high a.py:3 — off by one" in section
    assert "(also at b.py:9)" in section


def test_revise_failure_section_id_fallback_and_no_findings(tmp_path):
    run = _review_dir(tmp_path)
    (run / "review-reviewer1.json").write_text(json.dumps({
        "verdict": "fail",
        "findings": [{"file": "c.py", "line": 1, "severity": "medium",
                      "issue": "unmarked finding"}],
        "resolved": []}))
    section = execute._revise_failure_section(
        str(run), ("reviewer1", "reviewer"), "error")
    assert "- [?] medium c.py:1 — unmarked finding" in section

    (run / "review-reviewer1.json").write_text(json.dumps({
        "verdict": "needs_revision", "notes": ["n"], "resolved": []}))
    section = execute._revise_failure_section(
        str(run), ("reviewer1", "reviewer"), "error")
    assert "findings:" not in section


def test_write_revise_feedback_last_round_marker(tmp_path):
    run = _review_dir(tmp_path)
    marker = ("This is the last revision round. If a finding survived an "
              "earlier fix, do not patch the same lines again: choose a "
              "different approach for it.")
    last = execute._write_revise_feedback(str(run), 3, 3, [])
    assert marker in open(last, encoding="utf-8").read()
    mid = execute._write_revise_feedback(str(run), 1, 3, [])
    assert "This is the last revision round" not in open(mid, encoding="utf-8").read()


def test_recipe_reviewer_md_speaks_the_runtime_contract():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(mini_ork.__file__)))
    path = os.path.join(repo_root, "recipes", "framework-edit", "prompts",
                        "reviewer.md")
    content = open(path, encoding="utf-8").read()
    for needle in ("needs_revision", "resolved", "checked_criteria",
                   "artifact_ref", "leave empty for a new finding",
                   "Your earlier findings",
                   "every other location of the same defect",
                   "From round 2", "put fixed ones in `resolved`"):
        assert needle in content, needle
    assert "`approve`, `revise`, or `reject`" not in content


def test_ledger_reads_findings_from_a_prose_wrapped_review(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    review = run_dir / "review-reviewer.json"
    body = {"verdict": "needs_revision", "notes": [],
            "findings": [{"id": "", "file": "a.py", "line": 3,
                          "severity": "high", "issue": "breaks x"}]}
    review.write_text("Here is my review:\n```json\n" + json.dumps(body) + "\n```\nDone.\n")
    execute_handlers._record_review_ledger(str(run_dir), str(review), "needs_revision")
    ledger = json.loads((run_dir / "revise" / "findings.json").read_text())
    assert [f["id"] for f in ledger["findings"]] == ["F1"]
    # A prose-wrapped file is left as the reviewer wrote it.
    assert review.read_text().startswith("Here is my review:")
