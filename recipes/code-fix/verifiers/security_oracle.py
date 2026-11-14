#!/usr/bin/env python3
"""verifiers/security_oracle.py — paired exploit-and-functional gate.

For a security-relevant change an independently supplied exploit test must fail
on the base revision and pass on the head revision, while the functional
command stays green on both. This node adapts ``mini_ork.verify.security_oracle``
to the verifier contract: one JSON envelope on stdout, exit 0 for pass, vacuous
or abstain, and exit 1 only for a refutation.

Env: MO_TARGET_CWD?, MINI_ORK_TOUCHED_FILES?, MO_SECURITY_FIX?,
MINI_ORK_EXPLOIT_TESTS?, MINI_ORK_RUN_DIR?, MO_SECURITY_ORACLE_FUNCTIONAL_CMD?,
MINI_ORK_TEST_CMD?, MO_SECURITY_ORACLE_TIMEOUT?.
Output: single-line JSON on stdout.
Exit: 0 pass / vacuous / abstain, 1 fail.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ``parents[2]`` of this file's DIRECTORY is the repo root (verifiers → code-fix
# → recipes → root); of the file itself it would be ``recipes/``.
_HERE = Path(__file__).resolve().parent

CHECK_NAMES = [
    "exploit-fails-on-base",
    "exploit-passes-on-head",
    "functional-passes-on-base",
    "functional-passes-on-head",
]


def _load_oracle():
    try:
        from mini_ork.verify import security_oracle  # type: ignore
        return security_oracle
    except ImportError:
        pass
    engine = str(_HERE.parents[2])
    if engine not in sys.path:
        sys.path.insert(0, engine)
    try:
        from mini_ork.verify import security_oracle  # type: ignore
        return security_oracle
    except ImportError as exc:
        sys.stderr.write(
            f"[security-oracle] oracle not importable from {engine}: {exc}\n"
        )
        return None


def _load_scope():
    try:
        from mini_ork.verify import touched_files  # type: ignore
        return touched_files
    except Exception:
        sys.stderr.write("[security-oracle] touched_files could not be imported\n")
        return None


def _touched(root: str, scope) -> Optional[list]:
    explicit = os.environ.get("MINI_ORK_TOUCHED_FILES", "").strip()
    if explicit:
        return [path for path in explicit.splitlines() if path.strip()]
    if scope is None:
        return None
    try:
        if not scope.is_git_worktree(root):
            return None
        return scope.touched_files(root)
    except Exception:
        sys.stderr.write("[security-oracle] change surface could not be resolved\n")
        return None


def _resolves(root: str, ref: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", root, "rev-parse", "--verify", "--quiet",
             f"{ref}^{{commit}}"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _resolve_base(scope, root: str) -> str:
    """The base revision to measure against, or ``""`` when there is none.

    There is deliberately NO ``HEAD`` fallback: ``HEAD`` is the fix's own tree,
    so the pair would compare the fix with itself — the exploit could only ever
    "fail on base" by failing on head too, and the gate would refute every
    security change it could not measure instead of reporting it unmeasured.
    """
    if scope is not None:
        base = scope.scoped_base(root)
        if base and _resolves(root, base):
            return base
    return ""


def _envelope(status: str, checks: list, reason: str) -> dict:
    """One envelope for all four outcomes.

    ``status`` is the load-bearing field and the two contracts are different: a
    NODE verifier whose evidence says ``status: unverified`` makes the executor
    return :data:`mini_ork.verify.levels.ABSTAIN_RC`, which marks the node
    ``abstained`` and withholds the publish. So only a real abstention (the
    oracle could not measure) may carry it. A vacuous verdict — the ordinary
    case, every non-security change — must exit the node green while claiming
    nothing: it emits no ``status``, keeps ``error_summary`` (the level gate's
    declared-skip key, so nothing is minted PROVEN from it) and one NON-GATING
    check recording that no relation ran.
    """
    failed = sorted(
        check.name for check in checks if check.gating and not check.passed
    )
    payload = {
        "verifier": "security_oracle",
        "pass": status in ("pass", "vacuous"),
        "verdict": status,
        "failure_kind": "toolchain" if status == "abstain" else None,
        "evidence_path": None,
        "checks_run": [check.name for check in checks],
        "failed_checks": failed,
        "checks": [
            {
                "name": check.name,
                "pass": check.passed,
                "gating": check.gating,
                "detail": check.detail,
            }
            for check in checks
        ],
        "reasons": [reason] if reason else [],
        "checked_criteria": list(CHECK_NAMES),
        "error_summary": reason,
        "findings": [],
    }
    if status == "abstain":
        payload["status"] = "unverified"
    elif status == "vacuous":
        payload["checks"] = [{
            "name": "security-oracle",
            "pass": True,
            "gating": False,
            "detail": reason,
        }]
    else:
        payload["status"] = "verified"
    return payload


def _emit(payload: dict) -> None:
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def _abstain(reason: str) -> dict:
    return _envelope("abstain", [], reason)


def main() -> int:
    root = os.environ.get("MO_TARGET_CWD", "").strip() or os.getcwd()

    oracle = _load_oracle()
    if oracle is None:
        _emit(_abstain("security oracle module could not be loaded — not measured"))
        return 0

    scope = _load_scope()
    touched = _touched(root, scope)
    if touched is None:
        _emit(_abstain("cannot attribute changed files — not measured"))
        return 0

    run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
    exploit_files = oracle.exploit_tests(
        run_dir, os.environ.get(oracle.EXPLOIT_ENV, ""), root
    )
    functional_cmd = (
        os.environ.get(oracle.FUNCTIONAL_ENV, "").strip()
        or os.environ.get(oracle.FALLBACK_FUNCTIONAL_ENV, "").strip()
    )
    base = _resolve_base(scope, root)

    status, checks, reason = oracle.evaluate(
        root,
        base,
        touched,
        os.environ.get(oracle.SECURITY_LABEL_ENV, ""),
        exploit_files,
        functional_cmd,
    )
    _emit(_envelope(status, checks, reason))
    sys.stderr.write(f"[security-oracle] {status}: {reason or 'paired oracle passed'}\n")
    return 0 if status in ("pass", "vacuous", "abstain") else 1


if __name__ == "__main__":
    sys.exit(main())
