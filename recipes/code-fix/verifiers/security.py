#!/usr/bin/env python3
"""verifiers/security.py — the deterministic security gate for ``code-fix``.

``fallback_guard.py`` is the shape this file follows: scope is the run's own
change surface (``MINI_ORK_TOUCHED_FILES``, else the run's dirty set), output is
one JSON envelope on stdout, and anything that cannot be measured is an
abstention — never a pass and never a refutation. ``mini_ork.verify.security_scan``
owns the rules; this node only adapts them to the verifier contract.

Env: MO_TARGET_CWD?, MINI_ORK_TOUCHED_FILES?, MINI_ORK_HOME?, MINI_ORK_RUN_ID?,
and ``MO_SECURITY_SCANNER`` (``auto`` default | ``off`` — an operator switch
that ABSTAINS, it never turns the gate into a pass).
Output: single-line JSON on stdout.
Exit: 0 pass or abstain, 1 fail.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# ``parents[2]`` of this file's DIRECTORY is the repo root (verifiers → code-fix
# → recipes → root); of the file itself it would be ``recipes/``, so the file
# fallback would look for the scanner in the wrong tree and silently abstain.
_HERE = Path(__file__).resolve().parent

CHECK_NAMES = ["no-credential-in-diff", "no-dangerous-api-in-diff"]

_CREDENTIAL_RULES = frozenset({
    "private-key",
    "aws-access-key",
    "github-token",
    "provider-api-key",
    "slack-token",
    "credential-literal",
})

_DANGEROUS_RULES = frozenset({
    "world-writable",
    "tls-verify-off",
    "shell-exec",
    "unsafe-deserialization",
})


def _load_scanner():
    """The rules come from the ENGINE, never from the repo being scanned.

    Prepending the TARGET to ``sys.path`` would let a repository under audit
    ship its own ``mini_ork/verify/security_scan.py`` that finds nothing — the
    gate would be bypassed by the very content it exists to check. The engine is
    this file's own tree (``_HERE.parents[2]``), which for a self-edit run is the
    run's copy of the framework.

    The engine goes on ``sys.path`` because a verifier runs as a bare script and
    the package is not installed, so without it nothing under ``mini_ork`` is
    importable — including ``security_scan``'s own import of
    ``gaming_scan.added_lines``. Inserting the engine is safe in the way
    inserting the target is not: the engine tree is the code doing the checking.
    """
    try:
        from mini_ork.verify import security_scan  # type: ignore
        return security_scan
    except ImportError:
        pass
    engine = str(_HERE.parents[2])
    if engine not in sys.path:
        sys.path.insert(0, engine)
    try:
        from mini_ork.verify import security_scan  # type: ignore
        return security_scan
    except ImportError as exc:
        sys.stderr.write(f"[security] scanner not importable from {engine}: {exc}\n")
        return None


def _load_scope():
    """The change surface is shared with the other scoped verifiers."""
    try:
        from mini_ork.verify import touched_files  # type: ignore
        return touched_files
    except Exception:
        sys.stderr.write("[security] touched_files module could not be imported\n")
        return None


def _touched(root: str, scope) -> list | None:
    """The run's change surface, or ``None`` when it cannot be attributed."""
    explicit = os.environ.get("MINI_ORK_TOUCHED_FILES", "").strip()
    if explicit:
        return [p for p in explicit.splitlines() if p.strip()]
    if scope is None:
        return None
    try:
        if not scope.is_git_worktree(root):
            return None
        return scope.touched_files(root)
    except Exception:
        sys.stderr.write("[security] change surface could not be resolved\n")
        return None


def _checks(passes: dict[str, bool], detail: str) -> list[dict]:
    return [
        {"name": name, "pass": passes[name], "gating": True, "detail": detail}
        for name in CHECK_NAMES
    ]


def _abstain(reason: str) -> dict:
    return {
        "verifier": "security",
        "pass": False,
        "verdict": "abstain",
        "status": "unverified",
        "failure_kind": "toolchain",
        "evidence_path": None,
        "checks_run": list(CHECK_NAMES),
        "failed_checks": [],
        "checks": [
            {"name": name, "pass": False, "gating": False, "detail": reason}
            for name in CHECK_NAMES
        ],
        "reasons": [reason],
        "checked_criteria": list(CHECK_NAMES),
        "error_summary": reason,
        "findings": [],
    }


def _finding_dict(finding) -> dict:
    return {
        "path": finding.path,
        "line": finding.line,
        "rule": finding.rule,
        "why": finding.why,
        "text": "<redacted>",
    }


def _fail(findings: list, detail: str) -> dict:
    failed_checks = sorted({finding.rule for finding in findings})
    credential_hit = any(f.rule in _CREDENTIAL_RULES for f in findings)
    dangerous_hit = any(f.rule in _DANGEROUS_RULES for f in findings)
    hits = "; ".join(
        f"{f.path}:{f.line} {f.rule}" for f in findings[:3]
    )
    reason = f"{len(findings)} security finding(s) in the added lines: {hits}"
    return {
        "verifier": "security",
        "pass": False,
        "verdict": "fail",
        "evidence_path": None,
        "checks_run": list(CHECK_NAMES),
        "failed_checks": failed_checks,
        "checks": _checks(
            {
                CHECK_NAMES[0]: not credential_hit,
                CHECK_NAMES[1]: not dangerous_hit,
            },
            detail,
        ),
        "reasons": [reason],
        "checked_criteria": list(CHECK_NAMES),
        "error_summary": reason,
        "findings": [_finding_dict(finding) for finding in findings],
    }


def _pass(detail: str) -> dict:
    return {
        "verifier": "security",
        "pass": True,
        "verdict": "pass",
        "evidence_path": None,
        "checks_run": list(CHECK_NAMES),
        "failed_checks": [],
        "checks": _checks(
            {CHECK_NAMES[0]: True, CHECK_NAMES[1]: True}, detail
        ),
        "reasons": [],
        "checked_criteria": list(CHECK_NAMES),
        "error_summary": "",
        "findings": [],
    }


def _emit(payload: dict) -> None:
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def _resolve_base(scope, root: str) -> str:
    """The revision the run's added lines are measured from.

    The run's own start point when there is one; otherwise ``HEAD``. That
    fallback is load-bearing: ``git diff`` with no revision compares the
    worktree to the INDEX, so a run that stages its edits (the publisher does)
    would show the gate nothing and scan as clean. With no commits at all there
    is nothing to diff against and the untracked-file harvest is the surface.
    """
    if scope is not None:
        base = scope.scoped_base(root)
        if base:
            return base
    try:
        result = subprocess.run(
            ["git", "-C", root, "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return "HEAD" if result.returncode == 0 else ""


def main() -> int:
    root = os.environ.get("MO_TARGET_CWD", "").strip() or os.getcwd()

    mode = os.environ.get("MO_SECURITY_SCANNER", "auto").strip().lower()
    if mode == "off":
        _emit(_abstain("MO_SECURITY_SCANNER=off — the gate did not run"))
        return 0

    scanner = _load_scanner()
    if scanner is None:
        _emit(_abstain("security scanner module could not be loaded — not checked"))
        return 0

    scope = _load_scope()
    touched = _touched(root, scope)
    if touched is None:
        _emit(_abstain("cannot attribute changed files — not checked"))
        return 0

    # The surface is known before the diff is read, so hand it to the collector:
    # it can skip untracked paths outright instead of reading a whole run-record
    # tree to then throw the lines away.
    allowed = set(touched)
    base = _resolve_base(scope, root)
    added, unreadable, measured = scanner.collect_added(root, base, only=allowed)
    if not measured:
        _emit(_abstain("git could not produce a diff — not checked"))
        return 0

    # The diff is the whole dirty tree; the gate's surface is what the run
    # changed. Without this filter an unrelated dirty file in the checkout
    # refutes a run that never touched it. (The collector already skipped
    # out-of-surface untracked files; this covers tracked ones.)
    added = {path: lines for path, lines in added.items() if path in allowed}
    unreadable = [path for path in unreadable if path in allowed]

    findings = scanner.scan_added(added)
    added_lines_count = sum(len(lines) for lines in added.values())
    detail = (
        "no added lines"
        if added_lines_count == 0
        else f"{added_lines_count} added lines across {len(added)} files"
    )
    if unreadable:
        detail += (
            f"; {len(unreadable)} unreadable path(s): "
            + ", ".join(unreadable[:3])
        )

    if findings:
        _emit(_fail(findings, detail))
        sys.stderr.write(f"[security] {len(findings)} finding(s)\n")
        return 1

    if unreadable:
        # A path in the run's own surface that the collector could not read was
        # never scanned. Calling that a pass claims coverage the gate does not
        # have — and a file made unreadable is a cheap way to get one.
        _emit(_abstain(
            f"{len(unreadable)} changed path(s) could not be read and were not "
            "scanned: " + ", ".join(unreadable[:3])
        ))
        return 0

    _emit(_pass(detail))
    sys.stderr.write(f"[security] {detail}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
