"""Tests for `mini-ork contribute` — argv shapes, fail-closed auth, dry-run."""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_ork.cli import contribute


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    return checkout


@pytest.fixture
def body(tmp_path: Path) -> Path:
    path = tmp_path / "body.md"
    path.write_text("# Summary\n\nFix the thing.\n", encoding="utf-8")
    return path


@pytest.fixture
def recorded(monkeypatch):
    """Capture every command contribute would run, without running it."""
    calls: list[list[str]] = []
    bodies: dict[str, str] = {}

    def fake_run(argv, *, dry_run):
        calls.append(list(argv))
        if "--body-file" in argv:
            index = argv.index("--body-file") + 1
            bodies["body"] = Path(argv[index]).read_text(encoding="utf-8")
        if argv[1:3] == ["pr", "create"] or argv[1:3] == ["issue", "create"]:
            return "https://example.test/1"
        return ""

    monkeypatch.setattr(contribute, "_run", fake_run)
    monkeypatch.setattr(contribute.shutil, "which", lambda _name: "/opt/bin/gh")
    # An env token IS the credential, so no `gh auth status` probe is interleaved.
    monkeypatch.setenv("GH_TOKEN", "test-token")
    return calls, bodies


def test_pr_pushes_fork_branch_then_opens_pr(recorded, repo, body):
    calls, bodies = recorded
    code = contribute.main([
        "pr", "--repo", str(repo), "--slug", "owner/name", "--branch", "fix/x",
        "--title", "Fix x", "--body-file", str(body), "--base", "main",
        "--remote", "fork", "--head-owner", "me",
    ])

    assert code == 0
    assert calls[0] == ["git", "-C", str(repo), "push", "--set-upstream", "fork", "fix/x"]
    create = calls[1]
    assert create[1:3] == ["pr", "create"]
    # A fork PR must name the head owner; without it gh targets the base repo.
    assert create[create.index("--head") + 1] == "me:fix/x"
    assert create[create.index("--base") + 1] == "main"
    assert "--draft" not in create
    assert bodies["body"].startswith("# Summary")


def test_pr_draft_flag_is_forwarded(recorded, repo, body):
    calls, _ = recorded
    assert contribute.main([
        "pr", "--repo", str(repo), "--slug", "owner/name", "--branch", "fix/x",
        "--title", "Fix x", "--body-file", str(body), "--draft",
    ]) == 0
    assert "--draft" in calls[1]
    # No --head-owner: head stays a bare branch name for a same-repo PR.
    assert calls[1][calls[1].index("--head") + 1] == "fix/x"


def test_issue_creates_issue_and_never_pushes(recorded, repo, body):
    calls, bodies = recorded
    assert contribute.main([
        "issue", "--repo", str(repo), "--slug", "owner/name",
        "--title", "Bug", "--body-file", str(body),
    ]) == 0
    assert len(calls) == 1
    assert calls[0][1:3] == ["issue", "create"]
    assert bodies["body"].startswith("# Summary")


def test_missing_gh_fails_closed(monkeypatch, repo, body):
    monkeypatch.setattr(contribute.shutil, "which", lambda _name: None)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    code = contribute.main([
        "issue", "--repo", str(repo), "--slug", "owner/name",
        "--title", "Bug", "--body-file", str(body),
    ])
    assert code == 2


def test_auth_probe_runs_only_without_an_env_token(monkeypatch, repo, body):
    calls: list[list[str]] = []
    monkeypatch.setattr(contribute, "_run", lambda argv, *, dry_run: calls.append(argv) or "")
    monkeypatch.setattr(contribute.shutil, "which", lambda _name: "/opt/bin/gh")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_ENTERPRISE_TOKEN", raising=False)

    contribute.main([
        "issue", "--repo", str(repo), "--slug", "owner/name",
        "--title", "Bug", "--body-file", str(body),
    ])
    assert calls[0][1:3] == ["auth", "status"]

    calls.clear()
    monkeypatch.setenv("GH_TOKEN", "test-token")
    contribute.main([
        "issue", "--repo", str(repo), "--slug", "owner/name",
        "--title", "Bug", "--body-file", str(body),
    ])
    assert all(call[1:3] != ["auth", "status"] for call in calls)


def test_unknown_flag_is_a_usage_error(repo, body, capsys):
    code = contribute.main([
        "issue", "--repo", str(repo), "--slug", "owner/name",
        "--title", "Bug", "--body-file", str(body), "--nope", "1",
    ])
    assert code == 2
    assert "unknown flag --nope" in capsys.readouterr().err


def test_empty_body_is_rejected(repo, tmp_path):
    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")
    assert contribute.main([
        "issue", "--repo", str(repo), "--slug", "owner/name",
        "--title", "Bug", "--body-file", str(empty),
    ]) == 2


def test_remote_failure_exits_3_with_its_stderr(repo, body, monkeypatch, capsys):
    import subprocess

    monkeypatch.setattr(contribute.shutil, "which", lambda _name: "/opt/bin/gh")
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setattr(
        contribute.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "HTTP 403: no rights"),
    )
    code = contribute.main([
        "issue", "--repo", str(repo), "--slug", "owner/name",
        "--title", "Bug", "--body-file", str(body),
    ])
    assert code == 3
    assert "HTTP 403: no rights" in capsys.readouterr().err
