"""Paired exploit-and-functional oracle for security-relevant changes.

A security-relevant change is accepted only when an independently supplied
exploit test fails on the base revision and passes on the head revision while
the functional command passes on both trees. A missing or unbuildable oracle is
reported as unmeasured rather than as a pass. The base revision is measured in a
throwaway ``git worktree`` that this module creates and itself removes.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, NamedTuple, Optional

SECURITY_LABEL_ENV = "MO_SECURITY_FIX"
EXPLOIT_ENV = "MINI_ORK_EXPLOIT_TESTS"
RUN_DIR_FILE = "exploit-tests"
FUNCTIONAL_ENV = "MO_SECURITY_ORACLE_FUNCTIONAL_CMD"
FALLBACK_FUNCTIONAL_ENV = "MINI_ORK_TEST_CMD"
TIMEOUT_ENV = "MO_SECURITY_ORACLE_TIMEOUT"
DEFAULT_TIMEOUT = 900

# A path segment that names a security concern. The lookahead after the
# keyword group stops ``authors.py`` / ``tokenizer.py`` from reading as
# ``auth`` / ``token``: only a plural ``s``, a separator, a slash or the end of
# the segment may follow. Over-matching is the safe direction (an unlabelled
# change with no exploit files is vacuous either way) but a wrong match makes
# the oracle run against files that are not security code.
SECURITY_PATH_RE = re.compile(
    r"(^|/)(authori[sz]ation|authentication|authn|authz|auth|credential|secret|"
    r"token|session|login|logout|crypto|cryptograph[a-z]*|password|passwd|"
    r"permission|privilege|acl|rbac|oauth|jwt|tls|ssl)(s)?([._-][^/]*)?(/|$)",
    re.I,
)

# pytest's exit codes. Only ``1`` means "the tests ran and some failed", which
# is the one outcome that proves an exploit reproduced the bug. ``2``/``3``/``4``
# interrupt, internal error and usage error all mean the tests never ran, and
# ``5`` means nothing was collected — reading any of them as a reproduction
# would certify a fix nothing measured.
PYTEST_TESTS_FAILED = 1
PYTEST_DID_NOT_RUN = frozenset({2, 3, 4, 5})

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


def _run_shell(cwd: str, command: str, timeout: int) -> tuple:
    """Run an operator-supplied command through the shell; ``(rc|None, tail)``.

    A shell, because ``MINI_ORK_TEST_CMD`` is a shell command everywhere else
    it is honoured (``scripts/mini_ork_worktree.py`` runs the green gate with
    ``shell=True``). Splitting it with :mod:`shlex` silently mis-runs the
    ordinary ``cd x && pytest ...`` and ``-k "a or b"`` forms and would read
    the syntax error as a functional failure.
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired:
        return None, "timed out after %ss" % timeout
    except OSError as exc:
        return None, "could not run %r: %s" % (command, exc)
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


def _relative_to_root(root: str, path: str) -> Optional[Path]:
    """``path`` as a root-relative path, or ``None`` when it escapes ``root``.

    ``None`` is a refusal, not a fallback: a path outside the tree is copied
    nowhere and measured nowhere, and it must never reach the base worktree as
    an escape (``../`` or an absolute path outside the root).
    """
    root_resolved = Path(root).resolve()
    candidate = Path(path)
    try:
        resolved = candidate.resolve() if candidate.is_absolute() else (
            root_resolved / candidate
        ).resolve()
        return resolved.relative_to(root_resolved)
    except (OSError, ValueError):
        return None


def _copy_exploits(root: str, dest: str, relatives: Iterable[Path]) -> list:
    """Copy the SAME exploit bytes into the base tree, at the same paths.

    Returns ``(target, original_bytes_or_None)`` pairs so cleanup can restore a
    file the base commit already tracked instead of deleting it — the base tree
    a later functional run measures must be the tree as committed.
    """
    copied = []
    for relative in relatives:
        source = Path(root) / relative
        target = Path(dest) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        saved = target.read_bytes() if target.exists() else None
        shutil.copy2(source, target)
        copied.append((target, saved))
    return copied


def _restore_copied(copied: Iterable[tuple]) -> None:
    """Undo :func:`_copy_exploits`: rewrite the saved bytes, else unlink."""
    for target, saved in copied:
        try:
            if saved is None:
                target.unlink()
            else:
                target.write_bytes(saved)
        except OSError:
            continue


def _functional_checks(
    base_dest: str, root: str, functional_cmd: str, timeout: int
) -> tuple:
    """``(checks, abstain_reason)`` — a timeout is unmeasured, not a refutation.

    Both trees run the SAME command; only a real exit code is a verdict. A run
    that timed out says nothing about the code, so it withholds the odour of
    judgement rather than rejecting the change on the runner's speed.
    """
    if not (functional_cmd or "").strip():
        return [
            Check(FUNCTIONAL_BASE_CHECK, True, NO_FUNCTIONAL_DETAIL, False),
            Check(FUNCTIONAL_HEAD_CHECK, True, NO_FUNCTIONAL_DETAIL, False),
        ], ""
    checks = []
    for label, cwd in (
        (FUNCTIONAL_BASE_CHECK, base_dest),
        (FUNCTIONAL_HEAD_CHECK, root),
    ):
        rc, tail = _run_shell(cwd, functional_cmd, timeout)
        if rc is None:
            checks.append(Check(label, False, tail, True))
            return checks, "functional command on the %s tree did not finish" % (
                "base" if cwd == base_dest else "head",
            )
        checks.append(Check(label, rc == 0, tail, True))
    return checks, ""


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

    relatives = []
    for path in exploit_files:
        relative = _relative_to_root(root, path)
        if relative is None:
            return "abstain", [], "exploit test outside the tree: %s" % path
        relatives.append(relative)

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
            copied = _copy_exploits(root, dest, relatives)
            # The base run names the RELATIVE paths: an absolute path would
            # point back into the head tree and measure the fixed bytes as if
            # they were the base's.
            rc, tail = run_files(dest, [str(r) for r in relatives], timeout)
            if rc is None:
                return (
                    "abstain",
                    [Check(EXPLOIT_BASE_CHECK, False, tail, True)],
                    "exploit run on the base tree timed out",
                )
            if rc in PYTEST_DID_NOT_RUN:
                return (
                    "abstain",
                    [Check(
                        EXPLOIT_BASE_CHECK, False,
                        "pytest exit %s: %s" % (rc, tail), True,
                    )],
                    "the exploit did not run on the base tree (pytest exit %s) "
                    "— a collection or usage error is not a reproduction" % rc,
                )
            # Only "tests ran and failed" counts as a reproduction; a green base
            # means the exploit does not discriminate and the fix is unproven.
            checks.append(Check(
                EXPLOIT_BASE_CHECK, rc == PYTEST_TESTS_FAILED, tail, True,
            ))

            rc, tail = run_files(root, [str(r) for r in relatives], timeout)
            if rc is None:
                checks.append(Check(EXPLOIT_HEAD_CHECK, False, tail, True))
                return (
                    "abstain",
                    checks,
                    "exploit run on the head tree timed out",
                )
            checks.append(Check(EXPLOIT_HEAD_CHECK, rc == 0, tail, True))
        finally:
            # The functional measurement must see the base tree as committed,
            # so the copies are restored (or removed) once the exploit ran.
            _restore_copied(copied)

        functional, abstain = _functional_checks(
            dest, root, functional_cmd, timeout
        )
        checks.extend(functional)
        if abstain:
            return "abstain", checks, abstain
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
