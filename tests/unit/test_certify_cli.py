"""Unit tests for ``mini-ork certify`` (slice C2).

Hermetic by design. A tmp git repo with two commits stands in for the real
``--repo``; the ``Crucible`` context manager and ``judge`` are monkeypatched
out so no docker, no network, no model is ever invoked.

Each test pins ONE clause of the kickoff's behaviour; a regression in any of
the three short-circuit paths (empty patch, unsupported project, no docker)
or in the certificate format (exact key set, digest recompute, cost source)
turns one assertion red and stays red.
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path

import pytest

from mini_ork.certify import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    Verdict,
)
from mini_ork.certify import image as cert_image
import mini_ork.cli.certify as cli_certify


# ── helpers ────────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=check,
    )


def _init_py_repo(path: Path) -> Path:
    """A two-commit python repo: pyproject.toml + a one-line source change."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "ci@local")
    _git(path, "config", "user.name", "ci")
    (path / "pyproject.toml").write_text("[project]\nname='p'\nversion='0'\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "base")
    (path / "pkg").mkdir()
    (path / "pkg" / "m.py").write_text("def x():\n    return 0\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "head")
    return path


def _init_bare_repo(path: Path) -> Path:
    """A non-python repo (no pyproject/setup/requirements) with two commits."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "ci@local")
    _git(path, "config", "user.name", "ci")
    (path / "README.md").write_text("# nothing\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "base")
    # second commit so HEAD~1 is valid (the CLI default --base is HEAD~1)
    (path / "README.md").write_text("# nothing more\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "head")
    return path


# A factory the tests use to stub out judge — the CLI never imports the real one.
def _fake_judge_factory(verdict: str, reason: str):
    def _fake(*args, **kwargs):
        return Verdict(
            verdict=verdict, reason=reason,
            poc_plus=None, mr_pass_rate=0.75, mr_n=4, detail={"invariants": []},
        )
    return _fake


# ── 1. PROVEN → exit 0 ────────────────────────────────────────────────────


def test_proven_exits_zero_and_writes_certificate(tmp_path, monkeypatch):
    repo = _init_py_repo(tmp_path / "repo")
    monkeypatch.setattr(cli_certify, "judge", _fake_judge_factory(PROVEN, "fixed"))
    monkeypatch.setattr(cli_certify.cert_llm, "spent", lambda: {"usd": 0.05, "calls": 3})
    monkeypatch.setattr(cli_certify.cert_llm, "reset_spend", lambda: None)
    # Skip the image build — pretend docker is on PATH but the tag already exists.
    monkeypatch.setattr(cli_certify.cert_image, "is_python_project", lambda r: True)
    monkeypatch.setattr(cli_certify.cert_image, "build_image", lambda *a, **kw: "img:tag")
    # Make `Crucible` a no-op context manager.
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        up = True
    monkeypatch.setattr(cli_certify, "Crucible", lambda spec: _C())

    out = tmp_path / "cert.json"
    stdout = io.StringIO()
    rc = cli_certify.main([
        "--repo", str(repo), "--base", "HEAD~1", "--head", "HEAD",
        "--issue", "fix the bug", "--out", str(out),
    ], stdout=stdout)
    assert rc == 0
    assert out.exists()
    cert = json.loads(out.read_text())
    assert cert["verdict"] == PROVEN
    assert cert["reason"] == "fixed"
    # Cost comes from certify.llm.spent() — the only legal source.
    assert cert["cost"]["usd"] == 0.05
    assert cert["cost"]["llm_calls"] == 3


# ── 1b. REFUTED → exit 1, UNVERIFIED → exit 2 ─────────────────────────────


@pytest.mark.parametrize("verdict,expected_rc", [
    (REFUTED, 1),
    (UNVERIFIED, 2),
])
def test_verdict_to_exit_code(tmp_path, monkeypatch, verdict, expected_rc):
    repo = _init_py_repo(tmp_path / "repo")
    monkeypatch.setattr(cli_certify, "judge", _fake_judge_factory(verdict, f"reason-{verdict}"))
    monkeypatch.setattr(cli_certify.cert_llm, "spent", lambda: {"usd": 0.0, "calls": 0})
    monkeypatch.setattr(cli_certify.cert_llm, "reset_spend", lambda: None)
    monkeypatch.setattr(cli_certify.cert_image, "is_python_project", lambda r: True)
    monkeypatch.setattr(cli_certify.cert_image, "build_image", lambda *a, **kw: "img:tag")
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        up = True
    monkeypatch.setattr(cli_certify, "Crucible", lambda spec: _C())

    out = tmp_path / "cert.json"
    rc = cli_certify.main([
        "--repo", str(repo), "--issue", "x", "--out", str(out),
    ])
    assert rc == expected_rc
    cert = json.loads(out.read_text())
    assert cert["verdict"] == verdict


# ── 2. certificate has the exact schema, digest recomputes ───────────────


def test_certificate_schema_exact_and_digest_recomputes(tmp_path, monkeypatch):
    repo = _init_py_repo(tmp_path / "repo")
    monkeypatch.setattr(cli_certify, "judge", _fake_judge_factory(PROVEN, "ok"))
    monkeypatch.setattr(cli_certify.cert_llm, "spent", lambda: {"usd": 0.01, "calls": 1})
    monkeypatch.setattr(cli_certify.cert_llm, "reset_spend", lambda: None)
    monkeypatch.setattr(cli_certify.cert_image, "is_python_project", lambda r: True)
    monkeypatch.setattr(cli_certify.cert_image, "build_image", lambda *a, **kw: "img:tag")
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        up = True
    monkeypatch.setattr(cli_certify, "Crucible", lambda spec: _C())

    out = tmp_path / "cert.json"
    cli_certify.main([
        "--repo", str(repo), "--issue", "x", "--out", str(out),
    ])
    cert = json.loads(out.read_text())
    expected_keys = {
        "schema", "id", "issued_at", "verdict", "reason",
        "repo", "change", "claim", "method", "evidence",
        "cost", "duration_s", "digest",
    }
    assert set(cert) == expected_keys
    assert cert["schema"] == "mini-ork.certificate/v1"
    # digest = sha256(canonical JSON of every other key).
    body = {k: v for k, v in cert.items() if k != "digest"}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canon.encode()).hexdigest() == cert["digest"]


# ── 3. --diff FILE used instead of git diff; empty patch → exit 2 ─────────


def test_diff_file_used_when_supplied(tmp_path, monkeypatch):
    repo = _init_py_repo(tmp_path / "repo")
    judge_called = {"n": 0}

    def _judge(issue, patch, **kw):
        judge_called["n"] += 1
        return Verdict(PROVEN, "ok", mr_n=4, mr_pass_rate=1.0, detail={"invariants": []})

    monkeypatch.setattr(cli_certify, "judge", _judge)
    monkeypatch.setattr(cli_certify.cert_llm, "spent", lambda: {"usd": 0.0, "calls": 0})
    monkeypatch.setattr(cli_certify.cert_llm, "reset_spend", lambda: None)
    monkeypatch.setattr(cli_certify.cert_image, "is_python_project", lambda r: True)
    monkeypatch.setattr(cli_certify.cert_image, "build_image", lambda *a, **kw: "img:tag")
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        up = True
    monkeypatch.setattr(cli_certify, "Crucible", lambda spec: _C())

    patch_file = tmp_path / "p.patch"
    patch_file.write_text("--- a/foo\n+++ b/foo\n@@ -1 +1 @@\n-x\n+y\n")
    out = tmp_path / "cert.json"
    rc = cli_certify.main([
        "--repo", str(repo), "--diff", str(patch_file),
        "--issue", "x", "--out", str(out),
    ])
    assert rc == 0
    cert = json.loads(out.read_text())
    assert cert["change"]["files"] == ["foo"]
    assert judge_called["n"] == 1


def test_empty_patch_short_circuits_to_unverified_without_judge(tmp_path, monkeypatch):
    repo = _init_py_repo(tmp_path / "repo")
    judge_called = {"n": 0}

    def _judge(issue, patch, **kw):
        judge_called["n"] += 1
        return Verdict(PROVEN, "ok")

    monkeypatch.setattr(cli_certify, "judge", _judge)
    monkeypatch.setattr(cli_certify.cert_image, "build_image", lambda *a, **kw: "img:tag")

    patch_file = tmp_path / "empty.patch"
    patch_file.write_text("")
    out = tmp_path / "cert.json"
    rc = cli_certify.main([
        "--repo", str(repo), "--diff", str(patch_file),
        "--issue", "x", "--out", str(out),
    ])
    assert rc == 2
    cert = json.loads(out.read_text())
    assert cert["verdict"] == UNVERIFIED
    assert cert["reason"] == "no patch"
    assert judge_called["n"] == 0


# ── 4. missing --issue → exit 64 ──────────────────────────────────────────


def test_missing_issue_exits_64(tmp_path):
    # argparse calls `parser.error()` on missing required args; our override
    # raises SystemExit(64). The test asserts the exit code via the exception.
    with pytest.raises(SystemExit) as e:
        cli_certify.main(["--repo", str(tmp_path)])
    assert e.value.code == 64


# ── 5. non-python repo, no --image → exit 2, mentions --image, judge NOT called


def test_non_python_repo_unsupported(tmp_path, monkeypatch):
    repo = _init_bare_repo(tmp_path / "bare")
    judge_called = {"n": 0}

    def _judge(issue, patch, **kw):
        judge_called["n"] += 1
        return Verdict(PROVEN, "ok")

    monkeypatch.setattr(cli_certify, "judge", _judge)

    out = tmp_path / "cert.json"
    rc = cli_certify.main([
        "--repo", str(repo), "--issue", "x", "--out", str(out),
    ])
    assert rc == 2
    cert = json.loads(out.read_text())
    assert cert["verdict"] == UNVERIFIED
    assert "--image" in cert["reason"]
    assert judge_called["n"] == 0


# ── 6. docker absent, no --image → exit 2, "runtime unavailable" ──────────


def test_docker_absent_runtime_unavailable(tmp_path, monkeypatch):
    repo = _init_py_repo(tmp_path / "repo")
    judge_called = {"n": 0}

    def _judge(issue, patch, **kw):
        judge_called["n"] += 1
        return Verdict(PROVEN, "ok")

    monkeypatch.setattr(cli_certify, "judge", _judge)
    monkeypatch.setattr(cli_certify.cert_image, "is_python_project", lambda r: True)
    # Pretend build_image raises (the real path does so when shutil.which('docker') is None).
    def _raise(*a, **kw):
        raise RuntimeError("docker not on PATH")
    monkeypatch.setattr(cli_certify.cert_image, "build_image", _raise)

    out = tmp_path / "cert.json"
    rc = cli_certify.main([
        "--repo", str(repo), "--issue", "x", "--out", str(out),
    ])
    assert rc == 2
    cert = json.loads(out.read_text())
    assert "runtime unavailable" in cert["reason"]
    assert judge_called["n"] == 0


# ── 7. image.py Dockerfile text contains the load-bearing strings ─────────


def test_render_dockerfile_contains_load_bearing_strings():
    text = cert_image.render_dockerfile("/testbed", req_files=[])
    # The build context is materialised by `git archive`; we copy it whole and
    # then init a fresh git repo because Crucible resets with `git checkout -- .`.
    assert "/testbed" in text
    assert "git init" in text
    assert "git config" in text
    assert "git commit" in text
    assert "pip install" in text
    # The test runner needs pytest — Crucible shells out to it.
    assert "pytest" in text
    # Editable install chain so [test] / [tests] / bare all work.
    assert "-e ." in text


# ── 8. --json prints JSON equal to the written certificate ───────────────


def test_json_flag_prints_certificate_to_stdout(tmp_path, monkeypatch):
    repo = _init_py_repo(tmp_path / "repo")
    monkeypatch.setattr(cli_certify, "judge", _fake_judge_factory(PROVEN, "ok"))
    monkeypatch.setattr(cli_certify.cert_llm, "spent", lambda: {"usd": 0.0, "calls": 0})
    monkeypatch.setattr(cli_certify.cert_llm, "reset_spend", lambda: None)
    monkeypatch.setattr(cli_certify.cert_image, "is_python_project", lambda r: True)
    monkeypatch.setattr(cli_certify.cert_image, "build_image", lambda *a, **kw: "img:tag")
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        up = True
    monkeypatch.setattr(cli_certify, "Crucible", lambda spec: _C())

    out = tmp_path / "cert.json"
    stdout = io.StringIO()
    rc = cli_certify.main([
        "--repo", str(repo), "--issue", "x", "--out", str(out), "--json",
    ], stdout=stdout)
    assert rc == 0
    written = json.loads(out.read_text())
    printed = json.loads(stdout.getvalue())
    assert printed == written


# ── 9. python repo detection: false for bare, true for each marker ────────


@pytest.mark.parametrize("filename", ["pyproject.toml", "setup.py", "requirements.txt"])
def test_is_python_project_true_for_each_marker(tmp_path, filename):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / filename).write_text("# marker\n")
    assert cert_image.is_python_project(repo) is True


def test_is_python_project_false_for_bare_repo(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    (repo / "README.md").write_text("# readme\n")
    assert cert_image.is_python_project(repo) is False


# ── 10. main entrypoint registration: dict + expected set stay in lockstep ─


def test_certify_in_native_module_subs():
    from mini_ork.cli.main import _NATIVE_MODULE_SUBS
    assert _NATIVE_MODULE_SUBS["certify"] == "mini_ork.cli.certify"


# ── 11. --help prints the flag table and exits 0 ──────────────────────────


def test_help_flag_prints_usage_and_exits_zero():
    out = io.StringIO()
    err = io.StringIO()
    with pytest.raises(SystemExit) as e:
        cli_certify.main(["--help"], stdout=out, stderr=err)
    assert e.value.code == 0
    # argparse writes --help to stdout by default; we accept either channel.
    text = out.getvalue() + err.getvalue()
    assert "--repo" in text
    assert "--base" in text
    assert "--head" in text
    assert "--diff" in text
    assert "--issue" in text
    assert "--image" in text
    assert "--mr-n" in text
    assert "--out" in text
    assert "--json" in text


# ── 10. Dockerfile: requirements installed, commit AFTER install ──────────


def test_render_dockerfile_installs_requirements_then_commits():
    text = cert_image.render_dockerfile(
        "/testbed", req_files=[Path("requirements.txt"), Path("requirements-dev.txt")])
    lines = text.splitlines()
    idx = {k: next(i for i, ln in enumerate(lines) if k in ln)
           for k in ("-r requirements.txt", "-r requirements-dev.txt", "-e .", "git init")}
    # Crucible's `git clean -fd` must not delete install output, so the baseline
    # commit comes after every install step.
    assert idx["git init"] > max(idx["-r requirements.txt"], idx["-r requirements-dev.txt"], idx["-e ."])


def test_render_dockerfile_requirements_only_project_skips_editable_install():
    text = cert_image.render_dockerfile(
        "/testbed", req_files=[Path("requirements.txt")], installable=False)
    assert "-r requirements.txt" in text
    assert "-e ." not in text


def test_render_dockerfile_honours_workdir():
    assert "COPY . /src/" in cert_image.render_dockerfile("/src", req_files=[])


def test_is_installable_needs_pyproject_or_setup(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests\n")
    assert cert_image.is_python_project(tmp_path)
    assert not cert_image.is_installable(tmp_path)
    (tmp_path / "setup.py").write_text("")
    assert cert_image.is_installable(tmp_path)


# ── 11. a crash inside judge still writes an UNVERIFIED certificate ───────


def test_judge_crash_writes_unverified_certificate(tmp_path, monkeypatch):
    repo = _init_py_repo(tmp_path / "repo")

    def _boom(*a, **kw):
        raise RuntimeError("container died")

    monkeypatch.setattr(cli_certify, "judge", _boom)
    monkeypatch.setattr(cli_certify.cert_llm, "spent", lambda: {"usd": 0.02, "calls": 1})
    monkeypatch.setattr(cli_certify.cert_llm, "reset_spend", lambda: None)
    monkeypatch.setattr(cli_certify.cert_image, "build_image", lambda *a, **kw: "img:tag")

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        up = True
    monkeypatch.setattr(cli_certify, "Crucible", lambda spec: _C())

    out = tmp_path / "cert.json"
    rc = cli_certify.main(["--repo", str(repo), "--issue", "x", "--out", str(out)],
                          stdout=io.StringIO())
    assert rc == 2
    cert = json.loads(out.read_text())
    assert cert["verdict"] == UNVERIFIED
    assert "container died" in cert["reason"]
    assert cert["cost"] == {"usd": 0.02, "llm_calls": 1}
