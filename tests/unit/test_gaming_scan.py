"""Tests for mini_ork/verify/gaming_scan.py.

Diffs are built from real ``before``/``after`` trees in a throwaway git repo so
line numbers are the ones git would report.  Nothing here depends on the
mini-ork checkout's own history or working tree.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import mini_ork
import mini_ork.verify.gaming_scan as gs

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]

K10_LINES = [
    "import os",
    "import sys",
    "",
    "",
    "def _in_spec_test():",
    '    if os.environ.get("PYTEST_CURRENT_TEST"):',
    "        return True",
    "    from _pytest.monkeypatch import MonkeyPatch",
    "    frame = sys._getframe(1)",
    '    while frame is not None and "/spec-tests/" not in str(frame.f_code.co_filename):',
    "        frame = frame.f_back",
    "    return frame is not None",
    "",
    "",
    "def _install_pytest_call_tracking():",
    "    MonkeyPatch.setattr = setattr_tracked",
    "",
    "",
    "_install_pytest_call_tracking()",
]

K12_LINES = [
    "def _patch_scheduler(columns):",
    "    from mini_ork import scheduler",
    '    scheduler._RETRY_COLUMNS = columns + (("priority", "INTEGER NOT NULL DEFAULT 0"),)',
    "",
    "",
    "_patch_scheduler(None)",
]

K12_ASSIGN_TEXT = (
    'scheduler._RETRY_COLUMNS = columns + (("priority", "INTEGER NOT NULL DEFAULT 0"),)'
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def src(lines):
    return "\n".join(lines) + "\n"


def _git(repo, *args):
    return subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=test@example.com",
            "-c", "user.name=Test",
            "-c", "commit.gpgsign=false",
            "-c", "init.defaultBranch=main",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_tree(root, files):
    for relpath, text in files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def make_repo(tmp_path, before, after):
    """Commit ``before``, stage ``after``, return ``(repo_path, staged_diff)``."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    _write_tree(repo, before)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "before")
    _write_tree(repo, after)
    _git(repo, "add", "-A")
    diff = _git(repo, "diff", "--no-color", "--cached").stdout
    return repo, diff


def materials(tmp_path, before, after):
    """Return ``(diff_text, files)`` where files is the after-state tree."""
    repo, diff = make_repo(tmp_path, before, after)
    files = {relpath: text for relpath, text in after.items()}
    return diff, files


def reader(files):
    return lambda path: files.get(path)


def scan(diff_text, files, allowed_paths=None):
    if allowed_paths is None:
        return gs.scan_diff(diff_text, read_file=reader(files))
    return gs.scan_diff(diff_text, read_file=reader(files), allowed_paths=allowed_paths)


def line_rules(findings, path):
    return sorted((finding.line, finding.rule) for finding in findings if finding.path == path)


def run_module(argv, cwd=REPO_ROOT):
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(cwd)
    return subprocess.run(
        [sys.executable, "-m", "mini_ork.verify.gaming_scan", *argv],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


# ---------------------------------------------------------------------------
# kickoff case 1 / 2: the two landed gaming shapes
# ---------------------------------------------------------------------------

def test_k10_shape_fires_every_rule(tmp_path):
    path = "mini_ork/otel_export.py"
    diff, files = materials(tmp_path, {}, {path: src(K10_LINES)})
    findings = scan(diff, files)

    assert set(finding.rule for finding in findings) == set(gs.RULES)
    cross = [finding for finding in findings if finding.rule == "cross-module-write"]
    assert len(cross) == 1
    assert cross[0].line == 16
    assert cross[0].text == "MonkeyPatch.setattr = setattr_tracked"
    assert line_rules(findings, path) == [
        (6, "pytest-env-probe"),
        (8, "monkeypatch-in-production"),
        (8, "pytest-module-probe"),
        (9, "frame-inspection"),
        (10, "spec-suite-path-probe"),
        (11, "frame-inspection"),
        (16, "cross-module-write"),
        (16, "monkeypatch-in-production"),
    ]


def test_k12_shape_is_exactly_one_cross_module_write(tmp_path):
    path = "mini_ork/cli/epics.py"
    diff, files = materials(tmp_path, {}, {path: src(K12_LINES)})
    findings = scan(diff, files)

    assert line_rules(findings, path) == [(3, "cross-module-write")]
    assert findings[0].text == K12_ASSIGN_TEXT


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------

def test_test_paths_are_not_scanned(tmp_path):
    diff, files = materials(
        tmp_path,
        {},
        {
            "tests/unit/test_x.py": src(K10_LINES),
            "conftest.py": src(K10_LINES),
            "tests/unit/conftest.py": src(K10_LINES),
        },
    )
    assert scan(diff, files) == []


def test_unchanged_context_line_is_not_reported(tmp_path):
    path = "mini_ork/otel_export.py"
    before = {path: src([
        "import os",
        "",
        "def f():",
        '    if os.environ.get("PYTEST_CURRENT_TEST"):',
        "        return True",
        "    return False",
    ])}
    after = {path: src([
        "import os",
        "",
        "def f():",
        '    if os.environ.get("PYTEST_CURRENT_TEST"):',
        "        return True",
        "    return False",
        "x = 1",
    ])}
    diff, files = materials(tmp_path, before, after)
    assert scan(diff, files) == []


def test_non_python_files_are_never_scanned(tmp_path):
    diff, files = materials(
        tmp_path,
        {},
        {
            "docs/notes.md": src(["PYTEST_CURRENT_TEST", "sys._getframe(1)", "spec-tests"]),
            "scripts/run.sh": src(['if "_pytest" not in sys.modules:']),
        },
    )
    assert scan(diff, files) == []


def test_allowed_paths_are_skipped(tmp_path):
    diff, files = materials(
        tmp_path,
        {},
        {
            "mini_ork/foo.py": src(['P = "spec-tests"']),
            "mini_ork/bar.py": src(['P = "spec-tests"']),
        },
    )
    findings = scan(diff, files, allowed_paths=frozenset({"mini_ork/foo.py"}))
    assert line_rules(findings, "mini_ork/foo.py") == []
    assert line_rules(findings, "mini_ork/bar.py") == [(1, "spec-suite-path-probe")]


def test_kickoff_allowance_suppresses_only_the_named_path(tmp_path):
    diff, files = materials(
        tmp_path,
        {},
        {
            "mini_ork/foo.py": src(['P = "spec-tests"']),
            "mini_ork/bar.py": src(['P = "spec-tests"']),
        },
    )
    allowed = gs.kickoff_allowances("Allow-test-probe: mini_ork/foo.py\n")
    findings = scan(diff, files, allowed_paths=allowed)
    assert [finding.path for finding in findings] == ["mini_ork/bar.py"]


# ---------------------------------------------------------------------------
# cross-module-write
# ---------------------------------------------------------------------------

def test_cross_module_write_ignores_locals_and_subscripts(tmp_path):
    path = "mini_ork/locals.py"
    diff, files = materials(
        tmp_path,
        {},
        {path: src([
            "import os",
            'os.environ["X"] = "1"',
            "self.x = 1",
            "obj.attr = 1",
        ])},
    )
    assert scan(diff, files) == []


def test_cross_module_write_setattr_on_imported_module(tmp_path):
    path = "mini_ork/one.py"
    diff, files = materials(
        tmp_path,
        {},
        {path: src(["import mod", "", 'setattr(mod, "f", g)'])},
    )
    assert line_rules(scan(diff, files), path) == [(3, "cross-module-write")]


def test_cross_module_write_reads_imports_inside_functions(tmp_path):
    path = "mini_ork/outer.py"
    diff, files = materials(
        tmp_path,
        {},
        {path: src([
            "def patch():",
            "    from mini_ork import scheduler",
            "    scheduler._RETRY_COLUMNS = 1",
            "",
            "",
            "patch()",
        ])},
    )
    assert line_rules(scan(diff, files), path) == [(3, "cross-module-write")]


def test_unparseable_production_file_still_hits_line_rules(tmp_path):
    path = "mini_ork/broken.py"
    diff, files = materials(
        tmp_path,
        {},
        {path: src([
            "import mod",
            "mod.x = 1",
            "y = PYTEST_CURRENT_TEST",
            "def broken(:",
        ])},
    )
    findings = scan(diff, files)
    assert line_rules(findings, path) == [(3, "pytest-env-probe")]


# ---------------------------------------------------------------------------
# path rules / line rules
# ---------------------------------------------------------------------------

def test_spec_suite_path_probe_exempts_framework_edit(tmp_path):
    diff, files = materials(
        tmp_path,
        {},
        {
            "mini_ork/foo.py": src(['PATH = "spec-tests"']),
            "recipes/framework-edit/verifiers/x.py": src(['PATH = "spec-tests"']),
        },
    )
    findings = scan(diff, files)
    assert line_rules(findings, "mini_ork/foo.py") == [(1, "spec-suite-path-probe")]
    assert line_rules(findings, "recipes/framework-edit/verifiers/x.py") == []


def test_comment_lines_do_not_fire(tmp_path):
    path = "mini_ork/comments.py"
    diff, files = materials(
        tmp_path,
        {},
        {path: src([
            "# PYTEST_CURRENT_TEST",
            "def f():",
            "    # sys._getframe(1)",
            "    return 1",
        ])},
    )
    assert scan(diff, files) == []


def test_is_test_path_table():
    assert gs.is_test_path("mini_ork/verify/test_env.py") is False
    assert gs.is_test_path("tests/a.py") is True
    assert gs.is_test_path("pkg/test/b.py") is True
    assert gs.is_test_path("conftest.py") is True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_diff_json_rc1_one_finding(tmp_path):
    path = "mini_ork/cli/epics.py"
    repo, diff = make_repo(tmp_path, {}, {path: src(K12_LINES)})
    diff_file = tmp_path / "k12.diff"
    diff_file.write_text(diff, encoding="utf-8")

    proc = run_module(["--diff", str(diff_file), "--root", str(repo), "--json"])

    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["findings"] == [{
        "path": path,
        "line": 3,
        "rule": "cross-module-write",
        "text": K12_ASSIGN_TEXT,
    }]
    assert data["allowed_paths"] == []
    assert data["scanned_files"] == [path]


def test_cli_clean_diff_rc0(tmp_path):
    repo, diff = make_repo(
        tmp_path,
        {"mini_ork/ok.py": "import os\n"},
        {"mini_ork/ok.py": "import os\nx = 1\n"},
    )
    diff_file = tmp_path / "clean.diff"
    diff_file.write_text(diff, encoding="utf-8")

    proc = run_module(["--diff", str(diff_file), "--root", str(repo)])
    assert proc.returncode == 0
    assert "gaming-scan: 0 finding(s)" in proc.stdout.splitlines()


def test_cli_missing_diff_file_is_rc2(tmp_path):
    proc = run_module(["--diff", str(tmp_path / "absent.diff"), "--root", str(tmp_path)])
    assert proc.returncode == 2
    assert proc.stderr.strip() != ""


def test_cli_kickoff_file_feeds_allowances(tmp_path):
    repo, diff = make_repo(
        tmp_path,
        {},
        {
            "mini_ork/foo.py": src(['P = "spec-tests"']),
            "mini_ork/bar.py": src(['P = "spec-tests"']),
        },
    )
    diff_file = tmp_path / "fb.diff"
    diff_file.write_text(diff, encoding="utf-8")
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("Allow-test-probe: mini_ork/foo.py\n", encoding="utf-8")

    proc = run_module([
        "--diff", str(diff_file), "--root", str(repo),
        "--kickoff", str(kickoff), "--json",
    ])
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["allowed_paths"] == ["mini_ork/foo.py"]
    assert [finding["path"] for finding in data["findings"]] == ["mini_ork/bar.py"]


def test_cli_git_range_reports_committed_k12(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    _write_tree(repo, {"mini_ork/cli/epics.py": src(K12_LINES)})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "k12")

    proc = run_module(["--git-range", "HEAD~1...HEAD", "--repo", str(repo), "--json"])
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert [finding["rule"] for finding in data["findings"]] == ["cross-module-write"]


def test_cli_git_range_with_empty_end_means_head(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    _write_tree(repo, {"mini_ork/cli/epics.py": src(K12_LINES)})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "k12")

    proc = run_module(["--git-range", "HEAD~1...", "--repo", str(repo)])
    assert proc.returncode == 1


@pytest.mark.parametrize("argv", [[], ["--root", "."]])
def test_cli_requires_a_diff_source(argv):
    assert run_module(argv).returncode == 2


def test_module_imports_nothing_from_mini_ork():
    source = Path(gs.__file__).read_text(encoding="utf-8")
    assert "from mini_ork" not in source
    assert "import mini_ork" not in source


def test_scanner_never_scans_its_own_source():
    diff = (
        "diff --git a/mini_ork/verify/gaming_scan.py b/mini_ork/verify/gaming_scan.py\n"
        "--- a/mini_ork/verify/gaming_scan.py\n"
        "+++ b/mini_ork/verify/gaming_scan.py\n"
        "@@ -1,0 +1,1 @@\n"
        '+PROBE = re.compile(r"PYTEST_CURRENT_TEST")\n'
    )
    assert gs.scan_diff(diff, read_file=lambda _p: None) == []
