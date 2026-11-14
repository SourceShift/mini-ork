#!/usr/bin/env python3
"""verifiers/security.py — the deterministic security gate for ``code-fix``.

``fallback_guard.py`` is the shape this file follows: scope is the run's own
change surface (``MINI_ORK_TOUCHED_FILES``, else the run's dirty set), output is
one JSON envelope on stdout, and anything that cannot be measured is an
abstention — never a pass and never a refutation. ``mini_ork.verify.security_scan``
owns the rules; this node only adapts them to the verifier contract.

Env: MO_TARGET_CWD?, MINI_ORK_TOUCHED_FILES?, MINI_ORK_HOME?, MINI_ORK_RUN_ID?
Output: single-line JSON on stdout.
Exit: 0 pass or abstain, 1 fail.
"""

from __future__ import annotations

import json
import os
import sys

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


def _load_scanner(root: str):
    """The rules live in the framework, imported after ``root`` joins the path."""
    if root and root not in sys.path:
        sys.path.insert(0, root)
    try:
        from mini_ork.verify import security_scan  # type: ignore
        return security_scan
    except Exception:
        sys.stderr.write("[security] scanner module could not be imported\n")
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


def main() -> int:
    root = os.environ.get("MO_TARGET_CWD", "").strip() or os.getcwd()

    scanner = _load_scanner(root)
    if scanner is None:
        _emit(_abstain("security scanner module could not be imported — not checked"))
        return 0

    scope = _load_scope()
    touched = _touched(root, scope)
    if touched is None:
        _emit(_abstain("cannot attribute changed files — not checked"))
        return 0

    base = scope.scoped_base(root) if scope is not None else ""
    added, unreadable, measured = scanner.collect_added(root, base)
    if not measured:
        _emit(_abstain("git could not produce a diff — not checked"))
        return 0

    # The diff is the whole dirty tree; the gate's surface is what the run
    # changed. Without this filter an unrelated dirty file in the checkout
    # refutes a run that never touched it.
    allowed = set(touched)
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

    _emit(_pass(detail))
    sys.stderr.write(f"[security] {detail}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
