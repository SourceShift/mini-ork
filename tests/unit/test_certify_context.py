"""Unit tests for ``mini_ork.certify.context`` — the repo-context surface.

Hermetic. The `code_context(...)` test stands up a real tmp two-commit git
repo so the `git show <base_sha>:<path>` call has something to read; the rest
are pure-string / regex tests.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mini_ork.certify.context import (
    changed_py_files,
    code_context,
    imports_code_under_test,
    module_name,
)


# ── helpers ────────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=check,
    )


def _init_py_repo(path: Path) -> Path:
    """Two-commit repo: at BASE, stats/__init__.py AND stats/median.py (buggy)
    both exist. HEAD adds README.md (so `changed_py_files` distinguishes the
    test fixtures from real README.md presence).

    Mirrors the shape of the kickoff's `stats.median` demo so the guard's
    intended use is exercised end-to-end. The BUG marker in median.py is
    unique enough to assert that BASE source — not HEAD source — is what
    `code_context(...)` surfaces.
    """
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "ci@local")
    _git(path, "config", "user.name", "ci")
    (path / "stats").mkdir()
    (path / "stats" / "__init__.py").write_text("")  # empty at base
    (path / "stats" / "median.py").write_text(
        "def median(xs):\n"
        "    xs = sorted(xs)\n"
        "    n = len(xs)\n"
        "    if n % 2:\n"
        "        return xs[n // 2]\n"
        "    return xs[n // 2]      # BUG: should be (n//2 - 1, n//2) averaged\n"
    )
    (path / "pyproject.toml").write_text("[project]\nname='stats'\nversion='0'\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "base")
    # head: README added — the only thing the patch under test will mention.
    (path / "README.md").write_text("# stats\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "head")
    return path


# ── 1. changed_py_files filters non-test .py ───────────────────────────────


def test_changed_py_files_filters_non_py_and_tests_and_devnull():
    patch = (
        "--- a/stats/__init__.py\n+++ b/stats/__init__.py\n@@ -1 +1 @@\n- x\n+y\n"
        "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n"
        "--- /dev/null\n+++ b/stats/new_module.py\n@@ -0,0 +1 @@\n+def f(): return 1\n"
        "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@ -1 +1 @@\n-1\n+2\n"
        "--- a/test_foo.py\n+++ b/test_foo.py\n@@ -1 +1 @@\n-a\n+b\n"
        "--- a/foo_test.py\n+++ b/foo_test.py\n@@ -1 +1 @@\n-a\n+b\n"
        "--- a/src/pkg/mod.py\n+++ b/src/pkg/mod.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    out = changed_py_files(patch)
    # stats/__init__.py, src/pkg/mod.py, AND the new stats/new_module.py
    # (added by `/dev/null` → `+++ b/stats/new_module.py`) are kept.
    # tests/test_x.py and the test_* / *_test.py files are excluded.
    # /dev/null excluded. README.md excluded.
    assert out == ["stats/__init__.py", "stats/new_module.py", "src/pkg/mod.py"]


def test_changed_py_files_dedupes_in_first_seen_order():
    patch = (
        "--- a/pkg/m.py\n+++ b/pkg/m.py\n@@ -1 +1 @@\n-x\n+y\n"
        "--- a/pkg/m.py\n+++ b/pkg/m.py\n@@ -10 +10 @@\n-a\n+b\n"
        "--- a/pkg/n.py\n+++ b/pkg/n.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    assert changed_py_files(patch) == ["pkg/m.py", "pkg/n.py"]


# ── 2. module_name mapping ─────────────────────────────────────────────────


@pytest.mark.parametrize("path,expected", [
    ("stats/__init__.py", "stats"),
    ("pkg/sub/mod.py", "pkg.sub.mod"),
    ("src/pkg/a.py", "pkg.a"),
    ("a.py", "a"),
    ("pkg/__init__.py", "pkg"),
])
def test_module_name_happy_paths(path, expected):
    assert module_name(path) == expected


@pytest.mark.parametrize("path", [
    "a-b/c.py",          # hyphen in component
    "pkg/3bad.py",       # leading digit
    ".py",               # empty
    "pkg/.py",           # empty component
])
def test_module_name_returns_none_for_invalid(path):
    assert module_name(path) is None


# ── 3. code_context — text, modules, files, truncation ─────────────────────


def test_code_context_returns_base_source_with_module_and_files(tmp_path):
    repo = _init_py_repo(tmp_path / "repo")
    base_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD~1"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # patch text only needs the `+++ b/<p>` headers for changed_py_files to
    # decide which files to look up. `git diff` produces them naturally; the
    # full diff is irrelevant to code_context.
    patch = (
        "--- a/stats/__init__.py\n+++ b/stats/__init__.py\n@@ -1 +1,2 @@\n+from .median import median\n"
        "--- a/stats/median.py\n+++ b/stats/median.py\n@@ -0,0 +1,6 @@\n+def median(xs): ...\n"
        "--- a/README.md\n+++ b/README.md\n@@ -0,0 +1 @@\n+# stats\n"
    )
    ctx = code_context(repo, base_sha, patch)
    # stats/median.py exists at base (we created it at HEAD); stats/__init__.py
    # at base is empty but still exists. README.md is non-py and excluded.
    assert "stats" in ctx.modules
    assert "stats.median" in ctx.modules
    # text contains the BASE source of median.py (the buggy `return xs[n // 2]`)
    # and the BASE empty content of __init__.py — NOT the head content.
    assert "BUG: should be" in ctx.text
    # The HEAD version of __init__.py would import median; the BASE __init__.py
    # is empty. Confirm BASE not HEAD.
    assert "from .median" not in ctx.text  # BASE __init__ is empty
    assert all(f in ctx.files for f in ("stats/median.py", "stats/__init__.py"))
    assert "README.md" not in ctx.files


def test_code_context_truncates_at_max_chars(tmp_path):
    repo = _init_py_repo(tmp_path / "repo")
    base_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD~1"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    patch = "--- a/stats/median.py\n+++ b/stats/median.py\n@@ -0,0 +1,5 @@\n+...\n"
    ctx = code_context(repo, base_sha, patch, max_chars=120)
    assert "… truncated" in ctx.text
    assert len(ctx.text) <= 200  # room for header + footer overhead


def test_code_context_skips_files_missing_at_base(tmp_path):
    """A patch that ADDS a file is silently skipped — no source at base_sha."""
    repo = _init_py_repo(tmp_path / "repo")
    base_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD~1"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    patch = (
        "--- /dev/null\n+++ b/stats/brand_new.py\n@@ -0,0 +1,3 @@\n+def f(): return 1\n"
    )
    ctx = code_context(repo, base_sha, patch)
    # brand_new.py is in changed_py_files (it's a new file with `/dev/null`
    # on the `-` side) but `git show base_sha:stats/brand_new.py` fails; the
    # module is NOT in modules, the file is NOT in files.
    assert "stats.brand_new" not in ctx.modules
    assert "stats/brand_new.py" not in ctx.files


# ── 4. imports_code_under_test — the structural guard ───────────────────────


@pytest.mark.parametrize("src", [
    "from stats import median\nimport pytest\n",
    "import stats\ndef test_x(): assert median([1,2,3]) == 2\n",
    "from stats.core import median as m\n",
    "from stats.sub import x\n",                       # stats is a parent
    "import stats.core.deep\n",
    "import stats as s\ndef test_x(): s.median([1]) == 1\n",
])
def test_imports_code_under_test_true_for_module_or_parent(src):
    assert imports_code_under_test(src, ("stats.median",)) is True


@pytest.mark.parametrize("src", [
    "def median(xs):\n    return sorted(xs)[len(xs) // 2]\n",   # copy in test file
    "import pandas as pd\n",                                    # unrelated
    "# from stats import median — not real code\n",             # comment
    "x = 'from stats import median'\n",                          # string literal
    "",                                                          # empty
])
def test_imports_code_under_test_false_when_no_real_import(src):
    assert imports_code_under_test(src, ("stats",)) is False


def test_imports_code_under_test_skips_guard_when_modules_empty():
    """SWE-bench-style callers pass CodeContext(modules=()) — guard no-ops."""
    # A probe that defines its own median is accepted when modules is empty:
    # the guard has nothing to enforce.
    src = "def median(xs): return sorted(xs)[len(xs)//2]\n"
    assert imports_code_under_test(src, ()) is True


def test_imports_code_under_test_matches_parent_package():
    """`from stats.core import x` satisfies module `stats`."""
    assert imports_code_under_test("from stats.core import x\n", ("stats",)) is True


def test_imports_code_under_test_false_for_sibling_module():
    """`from other_pkg import foo` does NOT satisfy `stats`."""
    assert imports_code_under_test("from other_pkg import foo\n", ("stats",)) is False
