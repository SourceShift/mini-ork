#!/usr/bin/env python3
"""verifiers/hypothesis.py — the tiered hypothesis-then-confirm gate for ``code-fix``.

``verifiers/security.py`` is the shape this file follows: scope is the run's
own change surface (``MINI_ORK_TOUCHED_FILES``, else the run's dirty set),
the rules come from the ENGINE, probes live under ``MINI_ORK_RUN_DIR``, output
is one JSON envelope on stdout, and anything that cannot be measured is an
abstention — never a pass and never a refutation. A pattern-matched
dangerous-API line is only a hypothesis here; ``hypothesis_cascade`` reports a
finding only when an executed probe confirms it.

Env: MO_TARGET_CWD?, MINI_ORK_TOUCHED_FILES?, MINI_ORK_HOME?, MINI_ORK_RUN_ID?,
MINI_ORK_RUN_DIR (probes at ``<run_dir>/hypotheses/<probe_name>``).
Output: single-line JSON on stdout.
Exit: 0 pass, vacuous or abstain; 1 fail.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# ``parents[2]`` of this file's DIRECTORY is the repo root (verifiers → code-fix
# → recipes → root); of the file itself it would be ``recipes/``, so the file
# fallback would look for the cascade in the wrong tree and silently abstain.
_HERE = Path(__file__).resolve().parent

CHECK_NAMES = ["probe-confirms-hypothesis"]


def _load_modules():
    """The cascade and the diff collector come from the ENGINE, never the target.

    Prepending the TARGET to ``sys.path`` would let a repository under audit ship
    its own ``mini_ork/verify/hypothesis_cascade.py`` that proposes nothing — the
    gate would be bypassed by the very content it exists to check. The engine is
    this file's own tree; it goes on ``sys.path`` because a verifier runs as a
    bare script and the package is not installed.
    """
    try:
        from mini_ork.verify import hypothesis_cascade, security_scan  # type: ignore
        return security_scan, hypothesis_cascade
    except ImportError:
        pass
    engine = str(_HERE.parents[2])
    if engine not in sys.path:
        sys.path.insert(0, engine)
    try:
        from mini_ork.verify import hypothesis_cascade, security_scan  # type: ignore
        return security_scan, hypothesis_cascade
    except ImportError as exc:
        sys.stderr.write(
            f"[hypothesis] cascade not importable from {engine}: {exc}\n")
        return None, None


def _load_scope():
    """The change surface is shared with the other scoped verifiers."""
    try:
        from mini_ork.verify import touched_files  # type: ignore
        return touched_files
    except Exception:
        sys.stderr.write("[hypothesis] touched_files module could not be imported\n")
        return None


def _touched(root: str, scope) -> list | None:
    """The run's change surface, or ``None`` when it cannot be attributed."""
    explicit = os.environ.get("MINI_ORK_TOUCHED_FILES", "").strip()
    if explicit:
        return [p for p in explicit.replace(",", "\n").splitlines() if p.strip()]
    if scope is None:
        return None
    try:
        if not scope.is_git_worktree(root):
            return None
        return scope.touched_files(root)
    except Exception:
        sys.stderr.write("[hypothesis] change surface could not be resolved\n")
        return None


def _resolves(root: str, ref: str) -> bool:
    """True when ``ref`` names a commit in the repository at ``root``."""
    try:
        result = subprocess.run(
            ["git", "-C", root, "rev-parse", "--verify", "--quiet",
             f"{ref}^{{commit}}"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _resolve_base(scope, root: str) -> str:
    """The revision the run's added lines are measured from.

    The run's own start point when there is one; otherwise ``HEAD`` — ``git diff``
    with no revision compares the worktree to the INDEX, so a run that stages its
    edits would show the gate nothing.
    """
    if scope is not None:
        base = scope.scoped_base(root)
        # The scope module may be an older copy (a run's engine is pinned), so
        # the ref is re-checked here: a base that does not resolve would make the
        # diff fail silently and the gate measure nothing.
        if base and _resolves(root, base):
            return base
    try:
        result = subprocess.run(
            ["git", "-C", root, "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return "HEAD" if result.returncode == 0 else ""


def _emit(payload: dict) -> None:
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def _abstain(reason: str) -> dict:
    """A real inability to measure: it abstains the run, it never passes it."""
    return {
        "verifier": "hypothesis",
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


def _vacuous(reason: str) -> dict:
    """Nothing to confirm: green, but claiming no coverage at all."""
    return {
        "verifier": "hypothesis",
        "pass": True,
        "verdict": "vacuous",
        "evidence_path": None,
        "checks_run": list(CHECK_NAMES),
        "failed_checks": [],
        "checks": [
            {"name": name, "pass": True, "gating": False, "detail": reason}
            for name in CHECK_NAMES
        ],
        "reasons": [],
        "checked_criteria": list(CHECK_NAMES),
        "error_summary": reason,
        "findings": [],
    }


def _finding(entry: dict, gating: bool) -> dict:
    return {
        "rule": entry.get("rule", ""),
        "path": entry.get("path", ""),
        "line": entry.get("line", 0),
        "severity": entry.get("severity"),
        "score": entry.get("score"),
        "detail": entry.get("reason", ""),
        "gating": gating,
    }


def _pass(abstained: list, detail: str) -> dict:
    return {
        "verifier": "hypothesis",
        "pass": True,
        "verdict": "pass",
        "evidence_path": None,
        "checks_run": list(CHECK_NAMES),
        "failed_checks": [],
        "checks": [
            {"name": name, "pass": True, "gating": True, "detail": detail}
            for name in CHECK_NAMES
        ],
        "reasons": [],
        "checked_criteria": list(CHECK_NAMES),
        "error_summary": "",
        # An unconfirmed hypothesis is a review item, never a silent pass.
        "findings": [_finding(entry, False) for entry in abstained],
    }


def _fail(confirmed: list, detail: str) -> dict:
    hits = "; ".join(
        "%s:%s %s" % (entry.get("path"), entry.get("line"), entry.get("rule"))
        for entry in confirmed[:3]
    )
    reason = (
        "%d hypothesis(es) confirmed by an executed probe: %s"
        % (len(confirmed), hits)
    )
    return {
        "verifier": "hypothesis",
        "pass": False,
        "verdict": "fail",
        "evidence_path": None,
        "checks_run": list(CHECK_NAMES),
        "failed_checks": sorted({entry.get("rule", "") for entry in confirmed}),
        "checks": [
            {"name": name, "pass": False, "gating": True, "detail": detail}
            for name in CHECK_NAMES
        ],
        "reasons": [reason],
        "checked_criteria": list(CHECK_NAMES),
        "error_summary": reason,
        "findings": [_finding(entry, True) for entry in confirmed],
    }


def main() -> int:
    root = os.environ.get("MO_TARGET_CWD", "").strip() or os.getcwd()
    run_dir = os.environ.get("MINI_ORK_RUN_DIR", "").strip()

    scanner, cascade = _load_modules()
    if scanner is None or cascade is None:
        _emit(_abstain("hypothesis cascade could not be loaded — not checked"))
        return 0

    scope = _load_scope()
    touched = _touched(root, scope)
    if touched is None:
        _emit(_abstain("cannot attribute changed files — not checked"))
        return 0

    # The surface is known before the diff is read, so the collector can skip
    # untracked paths outright instead of reading a whole run-record tree.
    allowed = set(touched)
    base = _resolve_base(scope, root)
    added, _unreadable, measured = scanner.collect_added(
        root, base, only=allowed)
    if not measured:
        _emit(_abstain("git could not produce a diff — not checked"))
        return 0

    # The diff is the whole dirty tree; the gate's surface is what the run
    # changed. An unrelated dirty file must not make this node fail.
    added = {path: lines for path, lines in added.items() if path in allowed}

    status, report = cascade.evaluate(root, added, run_dir)
    entries = report.get("hypotheses", [])
    added_lines_count = sum(len(lines) for lines in added.values())
    detail = (
        "no added lines"
        if added_lines_count == 0
        else f"{added_lines_count} added lines across {len(added)} files"
    )

    if status == "fail":
        confirmed = [
            entry for entry in entries
            if entry.get("outcome") == cascade.CONFIRMED
        ]
        _emit(_fail(confirmed, detail))
        sys.stderr.write(
            f"[hypothesis] {len(confirmed)} confirmed hypothesis(es)\n")
        return 1

    if status == "vacuous":
        _emit(_vacuous(
            report.get("reason", "no dangerous-API line on the change surface")
        ))
        sys.stderr.write("[hypothesis] vacuous: no dangerous-API line\n")
        return 0

    if status == "abstain":
        _emit(_abstain(
            report.get("reason", "the change surface could not be measured")
        ))
        return 0

    abstained = [
        entry for entry in entries
        if entry.get("outcome") == cascade.ABSTAINED
    ]
    _emit(_pass(abstained, detail))
    sys.stderr.write(
        f"[hypothesis] {len(abstained)} unconfirmed hypothesis(es), "
        "review items only\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
