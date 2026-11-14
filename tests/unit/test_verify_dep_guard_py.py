"""SEC-G01-T03 — the offline dependency guard (``mini_ork.verify.dep_guard``).

Hermetic: ``tmp_path`` only, no network. The verifier subprocess is exercised
the way the recipe runs it — a throwaway git repo, ``MO_TARGET_CWD`` as root,
``MO_DEP_ALLOWLIST`` as the snapshot, and ``MINI_ORK_TOUCHED_FILES`` as the run's
change surface.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import mini_ork
import mini_ork.verify.dep_guard as dep_guard

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "recipes" / "code-fix" / "verifiers" / "dep_guard.py"

_SCRUBBED_ENV = (
    "MINI_ORK_HOME",
    "MINI_ORK_RUN_ID",
    "MINI_ORK_TOUCHED_FILES",
    "MO_GOAL_SCOPED_BASE",
)


def _clean_env() -> dict:
    env = dict(os.environ)
    for name in _SCRUBBED_ENV:
        env.pop(name, None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONWARNINGS"] = "ignore"
    return env


# ── norm / one_edit_apart ────────────────────────────────────────────────────


def test_norm():
    assert dep_guard.norm("PyYAML") == "pyyaml"
    assert dep_guard.norm("python-dateutil") == "python_dateutil"
    assert dep_guard.norm("uvicorn[standard]") == "uvicorn_standard"


@pytest.mark.parametrize("a,b", [
    ("requets", "requests"),
    ("reqests", "requests"),
    ("requests", "requestss"),
])
def test_one_edit_apart_true(a, b):
    assert dep_guard.one_edit_apart(a, b)


@pytest.mark.parametrize("a,b", [
    ("requests", "requests"),
    ("reqesst", "requests"),
    ("torch", "requests"),
])
def test_one_edit_apart_false(a, b):
    assert not dep_guard.one_edit_apart(a, b)


# ── allowlist ────────────────────────────────────────────────────────────────


def test_load_allowlist_skips_comments_and_blanks(tmp_path):
    path = tmp_path / "allow.txt"
    path.write_text("# comment\n\nPyYAML\npython-dateutil\n   \nrequests\n", encoding="utf-8")
    assert dep_guard.load_allowlist(path) == frozenset(
        {"pyyaml", "python_dateutil", "requests"}
    )


def test_load_allowlist_missing_raises(tmp_path):
    with pytest.raises(OSError):
        dep_guard.load_allowlist(tmp_path / "missing.txt")


# ── requirements ─────────────────────────────────────────────────────────────


def test_requirements_rules():
    allow = frozenset({"requests"})
    assert dep_guard.check_requirements(
        dep_guard.parse_requirements("requests==2.31.0\n"),
        allowlist=allow, path="requirements.txt",
    ) == []
    unpinned = dep_guard.check_requirements(
        dep_guard.parse_requirements("requests\n"), allowlist=allow,
        path="requirements.txt",
    )
    assert [(f.rule, f.why) for f in unpinned] == [
        ("unpinned-dependency", "no exact == pin")
    ]
    both = dep_guard.check_requirements(
        dep_guard.parse_requirements("evilpkg\n"), allowlist=frozenset(),
        path="requirements.txt",
    )
    assert {f.rule for f in both} == {"unpinned-dependency", "unknown-dependency"}
    squat = dep_guard.check_requirements(
        dep_guard.parse_requirements("reqeusts==1.0\n"), allowlist=allow,
        path="requirements.txt",
    )
    assert [f.rule for f in squat] == ["squat-suspect"]
    assert squat[0].why == "one edit from allowlisted 'requests'"


@pytest.mark.parametrize("line", [
    "pkg @ https://evil.example/p.whl",
    "pkg @ git+https://github.com/x/y",
    "-e git+https://github.com/x/y#egg=pkg",
    "--index-url https://evil.example.com/simple",
    "--extra-index-url https://evil.example.com/simple",
])
def test_untrusted_sources(line):
    findings = dep_guard.check_requirements(
        dep_guard.parse_requirements(line + "\n"),
        allowlist=frozenset({"pkg"}), path="requirements.txt",
    )
    assert any(f.rule == "untrusted-source" for f in findings)


def test_parse_requirements_skips_comments_and_includes():
    text = "# comment\n-r other.txt\n--requirement more.txt\n--hash=sha256:abc\n\n"
    assert dep_guard.parse_requirements(text) == []


# ── imports ──────────────────────────────────────────────────────────────────


def test_parse_imports_roots_and_relative():
    assert dep_guard.parse_imports("import os, yaml\nfrom x.y import z\n") == frozenset(
        {"os", "yaml", "x"}
    )
    assert dep_guard.parse_imports("from . import a\nfrom ..pkg import b\nimport os\n") == frozenset(
        {"os"}
    )
    with pytest.raises(ValueError):
        dep_guard.parse_imports("def broken(:\n")


def test_first_party_and_stdlib(tmp_path):
    assert dep_guard.check_imports(
        frozenset({"os", "json"}), allowlist=frozenset(), root_dir=tmp_path, path="m.py",
    ) == []
    assert dep_guard.check_imports(
        frozenset({"mini_ork"}), allowlist=frozenset(), root_dir=tmp_path, path="m.py",
    ) == []
    (tmp_path / "ownmod.py").write_text("x = 1\n", encoding="utf-8")
    assert dep_guard.check_imports(
        frozenset({"ownmod"}), allowlist=frozenset(), root_dir=tmp_path, path="m.py",
    ) == []


def test_check_imports_squat_is_file_level():
    findings = dep_guard.check_imports(
        frozenset({"requets"}), allowlist=frozenset({"requests"}),
        root_dir=Path("."), path="m.py",
    )
    assert len(findings) == 1
    assert findings[0].rule == "squat-suspect"
    assert findings[0].line == 0


def test_new_imports():
    assert dep_guard.new_imports(frozenset({"a", "b"}), frozenset({"b", "c"})) == ["c"]


# ── CLI ──────────────────────────────────────────────────────────────────────


def _run_module(args, cwd, extra_env=None):
    env = _clean_env()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "mini_ork.verify.dep_guard", *args],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120,
    )


def test_cli_write_allowlist(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "demo"\nversion = "0.1.0"\n'
        'dependencies = ["PyYAML>=6.0", "jsonschema>=4.0"]\n\n'
        "[project.optional-dependencies]\n"
        'web = ["fastapi>=0.110"]\n',
        encoding="utf-8",
    )
    out = tmp_path / "allowlist.txt"
    proc = _run_module(["--write-allowlist", str(out)], cwd=tmp_path)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "4 names written" in proc.stdout
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("#")
    body = [
        line.strip() for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]
    assert body == ["fastapi", "jsonschema", "pyyaml", "yaml"]


def test_cli_scan_reports_findings(tmp_path):
    root = tmp_path / "root"
    (root / "recipes" / "code-fix" / "data").mkdir(parents=True)
    (root / "recipes" / "code-fix" / "data" / "dependency-allowlist.txt").write_text(
        "yaml\nrequests\n", encoding="utf-8",
    )
    needs = root / "needs.py"
    needs.write_text("import requets\n", encoding="utf-8")
    proc = _run_module(["--root", str(root), "--path", str(needs)], cwd=root)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["count"] == len(payload["findings"]) >= 1
    assert {"path", "line", "rule", "name", "why"} <= set(payload["findings"][0])

    needs.write_text("import yaml\n", encoding="utf-8")
    clean = _run_module(["--root", str(root), "--path", str(needs)], cwd=root)
    assert clean.returncode == 0, (clean.stdout, clean.stderr)
    assert json.loads(clean.stdout)["findings"] == []


def test_cli_missing_allowlist_exit_3(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    needs = root / "needs.py"
    needs.write_text("import yaml\n", encoding="utf-8")
    proc = _run_module(
        ["--root", str(root), "--path", str(needs)],
        cwd=root,
        extra_env={"MO_DEP_ALLOWLIST": str(tmp_path / "absent" / "allow.txt")},
    )
    assert proc.returncode == 3, (proc.stdout, proc.stderr)


# ── verifier subprocess ──────────────────────────────────────────────────────


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    for cmd in (
        ["git", "init", "-q"],
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        ["git", "-C", str(repo), "add", "README.md"],
        ["git", "-C", str(repo), "commit", "-qm", "seed"],
    ):
        subprocess.run(cmd, cwd=str(repo), check=True, capture_output=True, timeout=60)
    return repo


def _run_verifier(repo: Path, *, allowlist: Path, touched: str, pythonpath=None):
    env = _clean_env()
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    env.update({
        "MO_TARGET_CWD": str(repo),
        "MO_DEP_ALLOWLIST": str(allowlist),
        "MINI_ORK_TOUCHED_FILES": touched,
    })
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(repo), env=env, capture_output=True, text=True, timeout=120,
    )


def test_verifier_loads_the_rule_module_by_path_when_the_package_is_not_importable(tmp_path):
    """The file-load fallback must find the ENGINE, not the scanned repo.

    A verifier runs as a bare script with no package installed, so the path
    fallback is the only thing standing between an unimportable engine and a
    silent abstention. Resolving it against the target repo instead would let a
    repository under audit supply its own rules.
    """
    repo = _git_repo(tmp_path)
    allow = tmp_path / "allow.txt"
    allow.write_text("yaml\nrequests\n", encoding="utf-8")
    (repo / "needs.py").write_text("import requets\n", encoding="utf-8")
    proc = _run_verifier(
        repo, allowlist=allow, touched="needs.py", pythonpath="",
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["verdict"] == "fail", payload
    assert "squat-suspect" in payload["failed_checks"]


def test_verifier_squat_fails(tmp_path):
    repo = _git_repo(tmp_path)
    allow = tmp_path / "allow.txt"
    allow.write_text("yaml\nrequests\n", encoding="utf-8")
    (repo / "needs.py").write_text("import requets\n", encoding="utf-8")
    proc = _run_verifier(repo, allowlist=allow, touched="needs.py")
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["verdict"] == "fail"
    assert "squat-suspect" in payload["failed_checks"]


def test_verifier_allowlisted_import_passes(tmp_path):
    repo = _git_repo(tmp_path)
    allow = tmp_path / "allow.txt"
    allow.write_text("yaml\n", encoding="utf-8")
    (repo / "needs.py").write_text("import yaml\n", encoding="utf-8")
    proc = _run_verifier(repo, allowlist=allow, touched="needs.py")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["verdict"] == "pass"
    assert payload["pass"] is True
    assert re.search(r"\d+ requirements and \d+ new imports checked", proc.stdout)


def test_verifier_missing_allowlist_abstains(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "needs.py").write_text("import yaml\n", encoding="utf-8")
    proc = _run_verifier(
        repo, allowlist=tmp_path / "missing.txt", touched="needs.py",
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["verdict"] == "abstain"
    assert payload["pass"] is False
    assert payload["status"] == "unverified"
    assert payload["failure_kind"] == "toolchain"
