"""Paired exploit-and-functional oracle for security-relevant changes.

A security-relevant change is accepted only when an independently supplied
exploit test fails on the base revision and passes on the head revision while
the functional command passes on both trees. A missing or unbuildable oracle is
reported as unmeasured rather than as a pass. The base revision is measured in a
throwaway ``git worktree`` that this module creates and itself removes.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, NamedTuple

SECURITY_LABEL_ENV = "MO_SECURITY_FIX"
EXPLOIT_ENV = "MINI_ORK_EXPLOIT_TESTS"
RUN_DIR_FILE = "exploit-tests"
FUNCTIONAL_ENV = "MO_SECURITY_ORACLE_FUNCTIONAL_CMD"
FALLBACK_FUNCTIONAL_ENV = "MINI_ORK_TEST_CMD"
TIMEOUT_ENV = "MO_SECURITY_ORACLE_TIMEOUT"
DEFAULT_TIMEOUT = 900

SECURITY_PATH_RE = re.compile(
    r"(^|/)(auth|authn|authz|login|logout|session|credential|secret|token|"
    r"crypto|password|permission|acl)([^/]*)(/|$)",
    re.I,
)

EXPLOIT_BASE_CHECK = "exploit-fails-on-base"
EXPLOIT_HEAD_CHECK = "exploit-passes-on-head"
FUNCTIONAL_BASE_CHECK = "functional-passes-on-base"
FUNCTIONAL_HEAD_CHECK = "functional-passes-on-head"
CHECK_NAMES = [
    EXPLOIT_BASE_CHECK,
    EXPLOIT_HEAD_CHECK,
    FUNCTIONAL_BASE_CHECK,
    FUNCTIONAL_HEAD_CHECK,
]

NO_FUNCTIONAL_DETAIL = (
    "no functional command configured "
    "(MO_SECURITY_ORACLE_FUNCTIONAL_CMD / MINI_ORK_TEST_CMD)"
)


class Check(NamedTuple):
    """One measured fact about the change, and whether it gates the verdict."""

    name: str
    passed: bool
    detail: str
    gating: bool


def _label_truthy(label: str) -> bool:
    return (label or "").strip().lower() in ("1", "true", "yes")


def is_security_relevant(touched: Iterable[str], label: str) -> bool:
    """The label opts a change in; a security-sensitive path opts it in too."""
    if _label_truthy(label):
        return True
    return any(SECURITY_PATH_RE.search(path or "") for path in touched)


def _dedupe(items: Iterable[str]) -> list:
    seen = set()
    ordered = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def exploit_tests(run_dir: str, env_value: str) -> list:
    """The exploit test paths, explicit env first, else the run-dir file."""
    value = (env_value or "").strip()
    if value:
        return _dedupe(value.split())
    try:
        text = (Path(run_dir) / RUN_DIR_FILE).read_text(encoding="utf-8")
    except OSError:
        return []
    entries = []
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        entries.append(entry)
    return _dedupe(entries)


def prepare_base(root: str, base: str, dest: str) -> str:
    """Materialise ``base`` as a detached worktree at ``dest``."""
    proc = subprocess.run(
        ["git", "-C", root, "worktree", "add", "--detach", dest, base],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(detail or "git worktree add failed")
    return dest


def drop_base(root: str, dest: str) -> None:
    """Remove the throwaway worktree; a leaked one must not fail the gate."""
    try:
        subprocess.run(
            ["git", "-C", root, "worktree", "remove", "--force", dest],
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return


def _child_env() -> dict:
    """The parent environment minus escaped credentials and MO_/MINI_ORK_ knobs."""
    from mini_ork.verify.test_env import scrubbed_test_env

    return {
        key: value
        for key, value in scrubbed_test_env().items()
        if not (key.startswith("MO_") or key.startswith("MINI_ORK_"))
    }


def _run(cwd: str, argv: list, timeout: int) -> tuple:
    """Run ``argv`` in ``cwd``; return ``(returncode | None, output tail)``."""
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired:
        return None, "timed out after %ss" % timeout
    except OSError as exc:
        return None, "could not run %s: %s" % (argv[0] if argv else "command", exc)
    combined = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, combined[-400:]


def run_files(root: str, files: list, timeout: int) -> tuple:
    """Run the listed test files with pytest; return ``(rc | None, tail)``."""
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:asyncio", *files]
    return _run(root, argv, timeout)


def _timeout() -> int:
    raw = os.environ.get(TIMEOUT_ENV, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def _first_missing(root: str, exploit_files: Iterable[str]) -> str:
    for path in exploit_files:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = Path(root) / candidate
        if not candidate.exists():
            return path
    return ""


def _relative_to_root(root: str, path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(Path(root).resolve())
        except ValueError:
            return Path(candidate.name)
    return candidate


def _copy_exploits(root: str, dest: str, exploit_files: Iterable[str]) -> list:
    """Copy the SAME exploit bytes into the base tree, at the same paths."""
    copied = []
    for path in exploit_files:
        relative = _relative_to_root(root, path)
        target = Path(dest) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(root) / relative, target)
        copied.append(target)
    return copied


def _remove_copied(copied: Iterable[Path]) -> None:
    for target in copied:
        try:
            target.unlink()
        except OSError:
            continue


def _functional_checks(
    base_dest: str, root: str, functional_cmd: str, timeout: int
) -> list:
    if not (functional_cmd or "").strip():
        return [
            Check(FUNCTIONAL_BASE_CHECK, True, NO_FUNCTIONAL_DETAIL, False),
            Check(FUNCTIONAL_HEAD_CHECK, True, NO_FUNCTIONAL_DETAIL, False),
        ]
    argv = shlex.split(functional_cmd)
    rc, tail = _run(base_dest, argv, timeout)
    checks = [Check(FUNCTIONAL_BASE_CHECK, rc == 0, tail, True)]
    rc, tail = _run(root, argv, timeout)
    checks.append(Check(FUNCTIONAL_HEAD_CHECK, rc == 0, tail, True))
    return checks


def evaluate(
    root: str,
    base: str,
    touched: Iterable[str],
    label: str,
    exploit_files: Iterable[str],
    functional_cmd: str,
) -> tuple:
    """Measure the paired oracle; return ``(status, checks, reason)``."""
    touched = list(touched)
    exploit_files = list(exploit_files)
    labelled = _label_truthy(label)

    if not is_security_relevant(touched, label):
        return "vacuous", [], "no security-relevant change and no security label"
    if labelled and not exploit_files:
        return (
            "fail",
            [],
            "security-labelled change with no exploit test "
            "(MINI_ORK_EXPLOIT_TESTS or <run>/exploit-tests)",
        )
    if not labelled and not exploit_files:
        return (
            "vacuous",
            [],
            "security-sensitive path touched but no exploit test supplied — "
            "the paired oracle did not run",
        )
    if not base:
        return "abstain", [], "no base revision to measure the exploit against"

    missing = _first_missing(root, exploit_files)
    if missing:
        return "abstain", [], "exploit test not found: %s" % missing

    timeout = _timeout()
    parent = tempfile.mkdtemp(prefix="mo-security-oracle-")
    dest = os.path.join(parent, "base")
    checks = []
    try:
        try:
            prepare_base(root, base, dest)
        except RuntimeError as exc:
            return "abstain", [], str(exc)

        copied = []
        try:
            copied = _copy_exploits(root, dest, exploit_files)
            rc, tail = run_files(dest, exploit_files, timeout)
            if rc is None:
                return (
                    "abstain",
                    [Check(EXPLOIT_BASE_CHECK, False, tail, True)],
                    "exploit run on the base tree timed out",
                )
            checks.append(
                Check(EXPLOIT_BASE_CHECK, rc not in (0, None), tail, True)
            )

            rc, tail = run_files(root, exploit_files, timeout)
            if rc is None:
                checks.append(Check(EXPLOIT_HEAD_CHECK, False, tail, True))
                return (
                    "abstain",
                    checks,
                    "exploit run on the head tree timed out",
                )
            checks.append(Check(EXPLOIT_HEAD_CHECK, rc == 0, tail, True))
        finally:
            # The functional measurement must see the pristine base tree, so the
            # copies live only for the exploit run.
            _remove_copied(copied)

        checks.extend(_functional_checks(dest, root, functional_cmd, timeout))
    finally:
        drop_base(root, dest)
        shutil.rmtree(parent, ignore_errors=True)

    gating = [check for check in checks if check.gating]
    failed = [check.name for check in gating if not check.passed]
    if not failed:
        return "pass", checks, ""
    return (
        "fail",
        checks,
        "%d of %d gating checks failed: %s"
        % (len(failed), len(gating), ", ".join(failed)),
    )
