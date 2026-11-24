"""Unit tests for the ``contributor`` node (mini_ork.cli.contributor).

Nothing here touches the network: ``open_pr`` is replaced by a recorder, so the
tests assert what the node *would* push and post, and — just as importantly —
what it refuses to.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from mini_ork.cli import contributor


@pytest.fixture()
def target_repo(tmp_path):
    """A checkout with one commit on a feature branch (a pushable branch)."""
    repo = tmp_path / "target"
    repo.mkdir()
    run = subprocess.run
    run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "file.txt").write_text("base\n")
    run(["git", "-C", str(repo), "add", "file.txt"], check=True)
    run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    run(["git", "-C", str(repo), "checkout", "-q", "-b", "fix/thing"], check=True)
    return repo


@pytest.fixture()
def run_dir(tmp_path):
    path = tmp_path / "run"
    path.mkdir()
    return path


@pytest.fixture()
def recorded(monkeypatch):
    calls = []

    def fake_open_pr(request, *, dry_run=False):
        calls.append((request, dry_run))
        return "https://example.invalid/pr/1"

    monkeypatch.setattr(contributor, "open_pr", fake_open_pr)
    return calls


def _env(monkeypatch, **values):
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_no_slug_means_skipped_and_nothing_posted(monkeypatch, run_dir, recorded):
    _env(monkeypatch, MO_CONTRIBUTE_SLUG=None)

    rc, reason = contributor.contributor_node("/", str(run_dir), None, "run-1")

    assert (rc, reason) == (0, "skipped")
    assert recorded == []


def test_security_run_is_refused_and_records_why(monkeypatch, run_dir, recorded, target_repo):
    _env(
        monkeypatch,
        MO_CONTRIBUTE_SLUG="owner/repo",
        MO_CONTRIBUTE_TITLE="fix: leak",
        MO_SECURITY_FIX="1",
        MO_CONTRIBUTE_REPO=str(target_repo),
    )

    rc, reason = contributor.contributor_node("/", str(run_dir), None, "run-1")

    assert (rc, reason) == (1, "security_channel_required")
    assert recorded == [], "a security fix must never reach a public PR"
    record = json.loads((run_dir / "contribution.json").read_text())
    assert record["status"] == "refused"
    assert record["reason"] == "security_fix"


def test_publisher_abstention_skips_the_contribution(monkeypatch, run_dir, recorded, target_repo):
    (run_dir / "publish-abstain.json").write_text(json.dumps({"reason": "levels_unverified"}))
    _env(
        monkeypatch,
        MO_CONTRIBUTE_SLUG="owner/repo",
        MO_CONTRIBUTE_TITLE="fix: thing",
        MO_CONTRIBUTE_REPO=str(target_repo),
    )

    rc, reason = contributor.contributor_node("/", str(run_dir), None, "run-1")

    assert (rc, reason) == (0, "publish_abstained")
    assert recorded == []


def test_pr_is_posted_from_the_run_branch_with_its_body(
    monkeypatch, run_dir, recorded, target_repo
):
    body = "Fixes #3607\n\nBody written by the run.\n"
    (run_dir / "pr-body.md").write_text(body)
    _env(
        monkeypatch,
        MO_CONTRIBUTE_SLUG="acme/widget",
        MO_CONTRIBUTE_TITLE="fix(extensions): honest timeout",
        MO_CONTRIBUTE_REPO=str(target_repo),
        MO_CONTRIBUTE_REMOTE="fork",
        MO_CONTRIBUTE_HEAD_OWNER="contributor",
        MO_CONTRIBUTE_ISSUE="3607",
    )

    rc, reason = contributor.contributor_node("/", str(run_dir), None, "run-1")

    assert (rc, reason) == (0, "done")
    request, dry_run = recorded[0]
    assert dry_run is False
    assert request.branch == "fix/thing", "the checkout's own branch is the default"
    assert request.base == "main"
    assert request.remote == "fork"
    assert request.head_owner == "contributor"
    assert request.slug == "acme/widget"
    assert request.body == body
    record = json.loads((run_dir / "contribution.json").read_text())
    assert record["status"] == "posted"
    assert record["url"] == "https://example.invalid/pr/1"


def test_missing_title_fails_closed_without_posting(monkeypatch, run_dir, recorded, target_repo):
    _env(
        monkeypatch,
        MO_CONTRIBUTE_SLUG="owner/repo",
        MO_CONTRIBUTE_TITLE=None,
        MO_CONTRIBUTE_REPO=str(target_repo),
    )

    rc, reason = contributor.contributor_node("/", str(run_dir), None, "run-1")

    assert (rc, reason) == (0, "contribution_failed"), "non-strict: a PR failure must not roll back the patch"
    assert recorded == []
    assert json.loads((run_dir / "contribution.json").read_text())["status"] == "failed"


def test_strict_mode_turns_a_failure_into_a_failing_node(
    monkeypatch, run_dir, recorded, target_repo
):
    _env(
        monkeypatch,
        MO_CONTRIBUTE_SLUG="owner/repo",
        MO_CONTRIBUTE_TITLE=None,
        MO_CONTRIBUTE_STRICT="1",
        MO_CONTRIBUTE_REPO=str(target_repo),
    )

    rc, reason = contributor.contributor_node("/", str(run_dir), None, "run-1")

    assert (rc, reason) == (1, "contribute_failed")


def test_body_is_derived_from_the_issue_when_no_file_exists(
    monkeypatch, run_dir, recorded, target_repo
):
    _env(
        monkeypatch,
        MO_CONTRIBUTE_SLUG="owner/repo",
        MO_CONTRIBUTE_TITLE="fix: thing",
        MO_CONTRIBUTE_ISSUE="#42",
        MO_CONTRIBUTE_REPO=str(target_repo),
    )

    rc, _reason = contributor.contributor_node("/", str(run_dir), None, "run-1")

    assert rc == 0
    assert recorded[0][0].body.startswith("Fixes #42\n")


def test_default_branch_is_refused(monkeypatch, run_dir, recorded, target_repo):
    subprocess.run(["git", "-C", str(target_repo), "checkout", "-q", "main"], check=True)
    _env(
        monkeypatch,
        MO_CONTRIBUTE_SLUG="owner/repo",
        MO_CONTRIBUTE_TITLE="fix: thing",
        MO_CONTRIBUTE_REPO=str(target_repo),
        MO_CONTRIBUTE_ISSUE="1",
    )

    rc, reason = contributor.contributor_node("/", str(run_dir), None, "run-1")

    assert (rc, reason) == (0, "contribution_failed")
    assert recorded == []


def test_dry_run_posts_nothing_and_says_so(monkeypatch, run_dir, recorded, target_repo):
    _env(
        monkeypatch,
        MO_CONTRIBUTE_SLUG="owner/repo",
        MO_CONTRIBUTE_TITLE="fix: thing",
        MO_CONTRIBUTE_ISSUE="7",
        MO_CONTRIBUTE_REPO=str(target_repo),
        MO_CONTRIBUTE_DRY_RUN="1",
    )

    rc, _reason = contributor.contributor_node("/", str(run_dir), None, "run-1")

    assert rc == 0
    assert recorded[0][1] is True
    assert json.loads((run_dir / "contribution.json").read_text())["status"] == "dry_run"


def test_code_fix_recipe_runs_the_contributor_after_the_publisher():
    """The node is inert without the edge: without publisher→contributor it
    would never execute, and the wiring is the whole point of the recipe change."""
    import yaml

    from mini_ork.workflow.compiler import compile_workflow

    recipe = contributor.__file__.rsplit("/mini_ork/", 1)[0] + "/recipes/code-fix/workflow.yaml"
    document = yaml.safe_load(open(recipe, encoding="utf-8"))
    types = {node["name"]: node["type"] for node in document["nodes"]}
    assert types["contributor"] == "contributor"
    assert types["publisher"] == "publisher"
    edges = {(edge["from"], edge["to"], edge["edge_type"]) for edge in document["edges"]}
    assert ("publisher", "contributor", "depends_on") in edges

    compiled = compile_workflow(recipe)
    assert "contributor" in compiled.nodes
