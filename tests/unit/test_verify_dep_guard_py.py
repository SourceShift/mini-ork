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
    assert squat[0].why == "one adjacent transposition from allowlisted 'requests'"


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


def _run_verifier(repo: Path, *, allowlist: Path, touched: str,
                  pythonpath=None, extra_env=None):
    env = _clean_env()
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    env.update({
        "MO_TARGET_CWD": str(repo),
        "MO_DEP_ALLOWLIST": str(allowlist),
        "MINI_ORK_TOUCHED_FILES": touched,
    })
    env.update(extra_env or {})
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


# ── the gate must measure the CHANGE, not the touched file ──────────────────


def _commit(repo: Path, message: str) -> None:
    for cmd in (["git", "-C", str(repo), "add", "."],
                ["git", "-C", str(repo), "commit", "-qm", message]):
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)


def test_new_requirements_ignores_line_numbers():
    old = dep_guard.parse_requirements("# moved down\nrequests>=2.0\n")
    new = dep_guard.parse_requirements("# moved down\n\n\nrequests>=2.0\nflask==2.0\n")
    assert dep_guard.new_requirements(old, new) == [
        (5, "flask", "==2.0"),
    ]


def test_pre_existing_unpinned_requirement_in_a_touched_file_is_not_a_finding(tmp_path):
    """Touching requirements.txt must not fail the run for lines it never wrote."""
    repo = _git_repo(tmp_path)
    (repo / "requirements.txt").write_text("requests>=2.0\n", encoding="utf-8")
    _commit(repo, "pre-existing range pin")
    (repo / "requirements.txt").write_text(
        "requests>=2.0\nflask==2.0\n", encoding="utf-8",
    )
    allow = tmp_path / "allow.txt"
    allow.write_text("requests\nflask\n", encoding="utf-8")
    proc = _run_verifier(repo, allowlist=allow, touched="requirements.txt")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["verdict"] == "pass", payload["findings"]
    assert re.search(r"1 requirements", proc.stdout)


def test_added_unpinned_requirement_is_a_finding(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "requirements.txt").write_text("flask==2.0\n", encoding="utf-8")
    _commit(repo, "pinned baseline")
    (repo / "requirements.txt").write_text(
        "flask==2.0\nrequests>=2.0\n", encoding="utf-8",
    )
    allow = tmp_path / "allow.txt"
    allow.write_text("flask\nrequests\n", encoding="utf-8")
    proc = _run_verifier(repo, allowlist=allow, touched="requirements.txt")
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert [f["rule"] for f in payload["findings"]] == ["unpinned-dependency"]
    assert payload["findings"][0]["name"] == "requests"


def test_verifier_abstains_without_a_base_revision(tmp_path):
    """With no revision, every import looks new — that is not a refutation."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True,
                   capture_output=True, timeout=60)
    (repo / "needs.py").write_text("import requets\n", encoding="utf-8")
    allow = tmp_path / "allow.txt"
    allow.write_text("requests\n", encoding="utf-8")
    proc = _run_verifier(repo, allowlist=allow, touched="needs.py")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["verdict"] == "abstain"
    assert payload["failure_kind"] == "toolchain"
    assert "base revision" in payload["error_summary"]


def test_verifier_ignores_a_repo_local_allowlist(tmp_path):
    """A repository under audit must not be able to allowlist its own import."""
    repo = _git_repo(tmp_path)
    local = repo / "recipes" / "code-fix" / "data"
    local.mkdir(parents=True)
    (local / "dependency-allowlist.txt").write_text("requets\n", encoding="utf-8")
    (repo / "needs.py").write_text("import requets\n", encoding="utf-8")
    proc = _run_verifier(
        repo, allowlist=tmp_path / "absent.txt", touched="needs.py",
        extra_env={"MO_DEP_ALLOWLIST": ""},
    )
    payload = json.loads(proc.stdout)
    # The repo's own allowlist says `requets` is fine. If it were consulted the
    # run would pass; the engine snapshot is consulted instead, and it does not
    # contain the name.
    assert payload["verdict"] == "fail", payload
    assert "unknown-dependency" in payload["failed_checks"]
    engine = REPO_ROOT / "recipes" / "code-fix" / "data" / "dependency-allowlist.txt"
    assert "requets" not in engine.read_text(encoding="utf-8").split()


def test_shipped_allowlist_covers_the_test_frameworks():
    """The gate must not fail its own delivery: tests import pytest."""
    engine = REPO_ROOT / "recipes" / "code-fix" / "data" / "dependency-allowlist.txt"
    names = dep_guard.load_allowlist(engine)
    assert dep_guard.norm("pytest") in names
    assert dep_guard.norm("pytest_asyncio") in names


def test_generator_covers_declared_and_observed_dependencies(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n'
        'dependencies = ["PyYAML>=6.0"]\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("import requests\nimport helper\n",
                                     encoding="utf-8")
    (tmp_path / "helper.py").write_text("", encoding="utf-8")
    out = tmp_path / "allowlist.txt"
    proc = _run_module(["--write-allowlist", str(out)], cwd=tmp_path)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    body = {
        line.strip() for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert {"pyyaml", "yaml", "requests"} <= body
    assert "helper" not in body


# ── first-party detection, sources, and honest coverage ─────────────────────


def test_first_party_covers_src_layout_and_namespace_packages(tmp_path):
    """A src-layout or namespace-package project's own code is not a dependency."""
    src = tmp_path / "src" / "myapp"
    src.mkdir(parents=True)
    (src / "core.py").write_text("", encoding="utf-8")  # no __init__.py
    (tmp_path / "src" / "flat.py").write_text("", encoding="utf-8")

    assert dep_guard.is_first_party(tmp_path, "myapp") is True
    assert dep_guard.is_first_party(tmp_path, "flat") is True
    assert dep_guard.is_first_party(tmp_path, "requests") is False


@pytest.mark.parametrize("line", [
    "pkg @ file:///tmp/p.whl",
    "pkg@https://evil.example/p.whl",
    "pkg @ ssh://git@host/p.git",
    "pkg @ git+ssh://git@host/p.git",
    "-e file:///tmp/pkg",
])
def test_untrusted_source_schemes(line):
    findings = dep_guard.check_requirements(
        dep_guard.parse_requirements(line + "\n"),
        allowlist=frozenset({"pkg"}), path="requirements.txt",
    )
    assert "untrusted-source" in {f.rule for f in findings}


def test_verifier_abstains_when_a_touched_file_cannot_be_parsed(tmp_path):
    """A file the gate could not read was not checked — that is not a pass."""
    repo = _git_repo(tmp_path)
    (repo / "broken.py").write_text("def (:\n", encoding="utf-8")
    allow = tmp_path / "allow.txt"
    allow.write_text("yaml\n", encoding="utf-8")
    proc = _run_verifier(repo, allowlist=allow, touched="broken.py")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["verdict"] == "abstain"
    assert "could not be parsed" in payload["error_summary"]


def test_write_allowlist_keeps_hand_added_entries(tmp_path):
    """Regenerating must not forget a dependency declared before it is used."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n'
        'dependencies = ["PyYAML>=6.0"]\n',
        encoding="utf-8",
    )
    out = tmp_path / "allowlist.txt"
    out.write_text("# hand-added\nhandpicked\n", encoding="utf-8")
    proc = _run_module(["--write-allowlist", str(out)], cwd=tmp_path)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    body = {
        line.strip() for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert {"handpicked", "pyyaml", "yaml"} <= body


def test_verifier_still_measures_when_the_package_is_shadowed(tmp_path):
    """The engine root goes on sys.path — never the repo being scanned.

    A shadowing module that raises ImportError stands in for any host where
    `mini_ork` cannot be imported directly: the gate must rescue itself from
    its own tree and still measure, not abstain on a diff it could check.
    """
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "mini_ork.py").write_text(
        'raise ImportError("shadowed")\n', encoding="utf-8",
    )
    repo = _git_repo(tmp_path)
    (repo / "needs.py").write_text("import requets\n", encoding="utf-8")
    allow = tmp_path / "allow.txt"
    allow.write_text("requests\n", encoding="utf-8")
    proc = _run_verifier(
        repo, allowlist=allow, touched="needs.py", pythonpath=str(shadow),
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["verdict"] == "fail"
    assert "squat-suspect" in payload["failed_checks"]


def test_first_party_rejects_an_empty_directory(tmp_path):
    """An empty ``requests/`` still shadows the dependency on ``sys.path``.

    Namespace packages make any directory importable, so accepting a bare
    directory let a repository claim a dependency's name — and with it silence
    the gate for its own import — without shipping a line of Python.
    """
    (tmp_path / "requests").mkdir()
    assert dep_guard.is_first_party(tmp_path, "requests") is False

    (tmp_path / "requests" / "core.py").write_text("", encoding="utf-8")
    assert dep_guard.is_first_party(tmp_path, "requests") is True


def test_verifier_reports_a_finding_even_when_another_file_is_unparsable(tmp_path):
    """Abstaining here would downgrade a real fail to "unmeasured".

    The gate found a violation; that is a refutation of the patch whatever else
    it could not read. Abstention would let the patch through.
    """
    repo = _git_repo(tmp_path)
    (repo / "broken.py").write_text("def (:\n", encoding="utf-8")
    (repo / "needs.py").write_text("import requets\n", encoding="utf-8")
    allow = tmp_path / "allow.txt"
    allow.write_text("requests\n", encoding="utf-8")
    proc = _run_verifier(
        repo, allowlist=allow, touched="broken.py\nneeds.py",
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["verdict"] == "fail", payload
    assert {f["rule"] for f in payload["findings"]} == {"squat-suspect"}
    # The partial coverage is still disclosed.
    assert "could not be parsed" in payload["error_summary"]


def test_verifier_ignores_an_unresolvable_base_ref(tmp_path):
    """A base that does not resolve must not turn every import into a new one.

    Falling back to ``HEAD`` (the committed tree) keeps the measurement honest:
    an import the baseline already had is not something this change added.
    """
    repo = _git_repo(tmp_path)
    (repo / "needs.py").write_text("import requests\n", encoding="utf-8")
    _commit(repo, "baseline import")
    allow = tmp_path / "allow.txt"
    allow.write_text("flask\n", encoding="utf-8")
    proc = _run_verifier(
        repo, allowlist=allow, touched="needs.py",
        extra_env={"MO_GOAL_SCOPED_BASE": "deadbeefdeadbeef"},
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["verdict"] == "pass", payload["findings"]


def test_generator_records_the_installed_distribution_for_an_alias(
    tmp_path, monkeypatch,
):
    """``import PIL`` is installed as ``pillow``; the hand table cannot list them all.

    Driven through the installed-metadata path so the assertion does not depend
    on which packages this host happens to have — the real metadata is read in
    production, a known mapping here.
    """
    (tmp_path / "app.py").write_text("import PIL\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        dep_guard, "_installed_distributions", lambda: {"pil": {"pillow"}},
    )
    out = tmp_path / "allowlist.txt"
    assert dep_guard._write_allowlist(out) == 0
    body = {
        line.strip() for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert {"pil", "pillow"} <= body


def test_verifier_flags_a_squat_added_on_top_of_a_resolvable_base(tmp_path):
    """The true-positive direction over a real base: only the NEW import counts.

    Every other verifier test either has no baseline (untracked file) or an
    empty diff, so this is the one that proves ``_base_imports`` narrows the
    check to what the change added rather than to the whole file.
    """
    repo = _git_repo(tmp_path)
    (repo / "needs.py").write_text("import requests\n", encoding="utf-8")
    _commit(repo, "baseline import")
    (repo / "needs.py").write_text(
        "import requests\nimport requets\n", encoding="utf-8",
    )
    allow = tmp_path / "allow.txt"
    allow.write_text("requests\n", encoding="utf-8")

    proc = _run_verifier(repo, allowlist=allow, touched="needs.py")
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert [f["rule"] for f in payload["findings"]] == ["squat-suspect"]
    assert payload["findings"][0]["name"] == "requets"
    assert re.search(r"1 new imports", proc.stdout), proc.stdout
