"""Deterministic scanner for test-gaming patterns in a unified diff.

Reads a unified diff (``git diff`` or ``git diff -U0``) and reports patterns
that let production Python code detect or manipulate the test harness:

* ``pytest-env-probe``           reads ``PYTEST_CURRENT_TEST``
* ``pytest-module-probe``        probes for the ``pytest``/``_pytest`` module
* ``monkeypatch-in-production``  references ``MonkeyPatch``
* ``frame-inspection``           walks interpreter frames
* ``spec-suite-path-probe``      looks for the blind spec-suite path
* ``cross-module-write``         writes an attribute on an imported module

Only ADDED lines of PRODUCTION ``.py`` files are considered.  The module is a
library plus a ``python -m mini_ork.verify.gaming_scan`` CLI and imports
nothing from ``mini_ork`` so it can run against a checkout before the package
is importable.

This module necessarily contains the literals it hunts, so it never scans its
own path (``_SELF_PATH``).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

RULES = (
    "pytest-env-probe",
    "pytest-module-probe",
    "monkeypatch-in-production",
    "frame-inspection",
    "spec-suite-path-probe",
    "cross-module-write",
)


@dataclass(frozen=True)
class Finding:
    path: str  # repo-relative, forward slashes
    line: int  # 1-based line number in the NEW file
    rule: str  # one of RULES
    text: str  # the offending line, stripped, at most 160 chars


# ---------------------------------------------------------------------------
# diff parsing
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(diff_text: str) -> dict[str, dict[int, str]]:
    """Return ``{path: {new_lineno: text}}`` for every added line in a diff."""
    result: dict[str, dict[int, str]] = {}
    path: str | None = None
    new_line: int | None = None

    for raw in diff_text.splitlines():
        if raw.startswith("@@"):
            match = _HUNK_RE.match(raw)
            if match is not None and path is not None:
                new_line = int(match.group(1))
            else:
                new_line = None
            continue

        if new_line is None:
            if raw.startswith("+++ "):
                target = raw[4:].strip()
                if target == "/dev/null":
                    path = None
                else:
                    path = target[2:] if target.startswith("b/") else target
            # ``--- a/<path>`` and file headers carry no line content here.
            continue

        # Inside a hunk: every line carries a one-character prefix.
        if raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            if path is not None:
                result.setdefault(path, {})[new_line] = raw[1:]
            new_line += 1
        elif raw.startswith(" "):
            new_line += 1
        elif raw.startswith("-"):
            continue
        else:
            # A line outside the hunk body (``diff --git`` and friends).
            new_line = None

    return {p: lines for p, lines in result.items() if lines}


# ---------------------------------------------------------------------------
# path / kickoff predicates
# ---------------------------------------------------------------------------

_TEST_DIR_SEGMENTS = ("tests", "test", "__tests__")


def is_test_path(path: str) -> bool:
    """True when a directory segment is a test dir, or the basename is conftest.py."""
    parts = Path(path).parts
    if not parts:
        return False
    if parts[-1] == "conftest.py":
        return True
    return any(segment in _TEST_DIR_SEGMENTS for segment in parts[:-1])


_ALLOWANCE_RE = re.compile(r"^\s*Allow-test-probe:\s*(\S+)\s*$", re.MULTILINE)


def kickoff_allowances(kickoff_text: str) -> frozenset[str]:
    """Paths named by ``Allow-test-probe: <path>`` lines in a kickoff."""
    return frozenset(_ALLOWANCE_RE.findall(kickoff_text))


# ---------------------------------------------------------------------------
# line rules
# ---------------------------------------------------------------------------

_LINE_RULES: dict[str, tuple[re.Pattern[str], ...]] = {
    "pytest-env-probe": (
        re.compile(r"PYTEST_CURRENT_TEST"),
    ),
    "pytest-module-probe": (
        re.compile(r"""sys\.modules(\.get)?\s*[\[(]\s*["']_?pytest\b"""),
        re.compile(r"""["']_?pytest["']\s+(not\s+)?in\s+sys\.modules"""),
        re.compile(r"^\s*(from|import)\s+_pytest\b"),
        re.compile(r"""find_spec\(\s*["']_?pytest\b"""),
    ),
    "monkeypatch-in-production": (
        re.compile(r"\bMonkeyPatch\b"),
    ),
    "frame-inspection": (
        re.compile(r"sys\._getframe\b"),
        re.compile(r"inspect\.currentframe\("),
        re.compile(r"inspect\.stack\("),
        re.compile(r"inspect\.getouterframes\("),
        re.compile(r"\.f_back\b"),
    ),
    "spec-suite-path-probe": (
        re.compile(r"spec-tests|spec_tests"),
    ),
}

_FRAMEWORK_EDIT_PREFIX = "recipes/framework-edit/"

_SETATTR_NAMES = frozenset({"setattr", "delattr"})


def _line_findings(path: str, lineno: int, text: str) -> list[Finding]:
    findings: list[Finding] = []
    spec_exempt = path.startswith(_FRAMEWORK_EDIT_PREFIX)
    for rule in RULES:
        if rule == "cross-module-write":
            continue
        if rule == "spec-suite-path-probe" and spec_exempt:
            continue
        patterns = _LINE_RULES[rule]
        if any(pattern.search(text) for pattern in patterns):
            findings.append(Finding(path, lineno, rule, text[:160]))
    return findings


# ---------------------------------------------------------------------------
# cross-module-write (AST)
# ---------------------------------------------------------------------------

def _bound_names(tree: ast.AST) -> set[str]:
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound.add(alias.asname)
                else:
                    bound.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound.add(alias.asname or alias.name)
    return bound


def _flatten_targets(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, (ast.Tuple, ast.List)):
        flattened: list[ast.AST] = []
        for element in node.elts:
            flattened.extend(_flatten_targets(element))
        return flattened
    return [node]


def _is_bound_module_attr(node: ast.AST, bound: set[str]) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in bound
    )


def _cross_module_write_linenos(tree: ast.AST) -> set[int]:
    bound = _bound_names(tree)
    if not bound:
        return set()

    linenos: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target] if node.target is not None else []
        elif isinstance(node, ast.Delete):
            targets = list(node.targets)
        else:
            targets = []

        for target in targets:
            for element in _flatten_targets(target):
                if _is_bound_module_attr(element, bound):
                    linenos.add(node.lineno)

        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _SETATTR_NAMES
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in bound
        ):
            linenos.add(node.lineno)

    return linenos


def _ast_findings(
    path: str,
    added: dict[int, str],
    read_file: Callable[[str], str | None],
) -> list[Finding]:
    text = read_file(path)
    if text is None:
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []

    findings: list[Finding] = []
    for lineno in sorted(_cross_module_write_linenos(tree)):
        if lineno not in added:
            continue
        findings.append(
            Finding(path, lineno, "cross-module-write", added[lineno].strip()[:160])
        )
    return findings


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

# The scanner's own source holds every literal it hunts; scanning it would make
# any edit to the scanner fail its own gate. Edits here are high risk-tier
# (config/risk-tiers.yaml), so a person reviews them instead.
_SELF_PATH = "mini_ork/verify/gaming_scan.py"


def _is_scannable(path: str, allowed_paths: frozenset[str]) -> bool:
    return (
        path.endswith(".py")
        and path != _SELF_PATH
        and not is_test_path(path)
        and path not in allowed_paths
    )


def scan_diff(
    diff_text: str,
    *,
    read_file: Callable[[str], str | None],
    allowed_paths: frozenset[str] = frozenset(),
) -> list[Finding]:
    """Scan a unified diff and return findings sorted by (path, line, rule)."""
    added = added_lines(diff_text)
    findings: set[Finding] = set()

    for path in sorted(added):
        if not _is_scannable(path, allowed_paths):
            continue
        lines = added[path]
        for lineno, raw in lines.items():
            text = raw.strip()
            if text.startswith("#"):
                continue
            findings.update(_line_findings(path, lineno, text))
        findings.update(_ast_findings(path, lines, read_file))

    return sorted(findings, key=lambda finding: (finding.path, finding.line, finding.rule))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_root_file(root: Path) -> Callable[[str], str | None]:
    def read_file(path: str) -> str | None:
        try:
            return (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    return read_file


def _git_range_end(git_range: str) -> str:
    if "..." in git_range:
        _, end = git_range.rsplit("...", 1)
    elif ".." in git_range:
        _, end = git_range.rsplit("..", 1)
    else:
        end = git_range
    return end or "HEAD"


def _read_git_file(repo: str, revision: str) -> Callable[[str], str | None]:
    def read_file(path: str) -> str | None:
        proc = subprocess.run(
            ["git", "-C", repo, "show", f"{revision}:{path}"],
            capture_output=True,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", errors="replace")

    return read_file


def _error(message: str) -> int:
    print(f"gaming-scan: {message}", file=sys.stderr)
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gaming-scan",
        description="Report test-gaming patterns on added production lines.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--diff", metavar="FILE", help="unified diff file to scan")
    source.add_argument("--git-range", metavar="RANGE", help="git diff range to scan")
    parser.add_argument("--root", default=".", metavar="DIR",
                        help="tree used to read files for AST analysis (--diff)")
    parser.add_argument("--repo", default=".", metavar="DIR",
                        help="git repository for --git-range")
    parser.add_argument("--kickoff", metavar="FILE",
                        help="kickoff whose Allow-test-probe lines are honored")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2

    allowed_paths: frozenset[str] = frozenset()
    if args.kickoff is not None:
        try:
            kickoff_text = Path(args.kickoff).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return _error(f"cannot read kickoff {args.kickoff}: {exc}")
        allowed_paths = kickoff_allowances(kickoff_text)

    if args.diff is not None:
        try:
            diff_text = Path(args.diff).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return _error(f"cannot read diff {args.diff}: {exc}")
        read_file = _read_root_file(Path(args.root))
    else:
        assert args.git_range is not None
        proc = subprocess.run(
            ["git", "-C", args.repo, "diff", "--no-color", "--no-renames", args.git_range],
            capture_output=True,
        )
        if proc.returncode != 0:
            message = proc.stderr.decode("utf-8", errors="replace").strip()
            return _error(message or f"git diff failed for range {args.git_range}")
        diff_text = proc.stdout.decode("utf-8", errors="replace")
        read_file = _read_git_file(args.repo, _git_range_end(args.git_range))

    findings = scan_diff(diff_text, read_file=read_file, allowed_paths=allowed_paths)

    added = added_lines(diff_text)
    scanned_files = sorted(
        path for path in added if _is_scannable(path, allowed_paths)
    )

    if args.json:
        print(json.dumps({
            "findings": [asdict(finding) for finding in findings],
            "allowed_paths": sorted(allowed_paths),
            "scanned_files": scanned_files,
        }))
    else:
        for finding in findings:
            print(f"{finding.path}:{finding.line}: {finding.rule}: {finding.text}")
        print(f"gaming-scan: {len(findings)} finding(s)")

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
