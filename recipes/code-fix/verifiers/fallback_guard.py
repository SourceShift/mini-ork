#!/usr/bin/env python3
"""verifiers/fallback_guard.py — the "no silent fallback" rule, as a machine.

``recipes/code-fix/prompts/implementer.md`` forbids "silent exception
swallowing" and "returning a default on error", and the reviewer prompt asks
the reviewer to look for it. That is a rule enforced at the strength of the
weakest reviewer lane: an approval and an oversight look identical in the
verdict. This node makes the narrow, parseable part of the rule deterministic —
``except: pass``, ``except Exception: return None`` — over exactly the files
this run touched.

Scope is the run's own change surface (``touched_files`` shared with
``verifiers/typecheck.py``): a pre-existing swallow elsewhere in the tree is
not this patch's failure and must not redden the lane. The tree-wide sweep is
``mini-ork fallbacks --all``, which is a ratchet, not a gate.

Env: MINI_ORK_RUN_DIR, MINI_ORK_HOME?, MINI_ORK_RUN_ID?, MO_TARGET_CWD?
Output: JSON on stdout, log at <run>/verifier-fallback-guard.log.
Exit: 0 pass, 1 fail (the caller reads ``pass``; both emit one envelope).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN_DIR = Path(os.environ.get("MINI_ORK_RUN_DIR", "."))
LOG_PATH = RUN_DIR / "verifier-fallback-guard.log"


def _load_guard():
    """The rule lives in the framework, not in this file.

    A recipe verifier is sometimes copied into a fixture without the rest of
    mini_ork on PYTHONPATH (same seam as ``verifiers/test.py``), so the module is
    located relative to this file first and imported second.
    """
    try:
        from mini_ork.verify import fallback_guard  # type: ignore
        return fallback_guard
    except ImportError:
        pass
    candidate = HERE.parents[2] / "mini_ork" / "verify" / "fallback_guard.py"
    if not candidate.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_mo_fallback_guard", candidate)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: `from __future__ import annotations` makes dataclass
    # field resolution look the class's module up in sys.modules (Python 3.9).
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        return None
    return module


def _load_scope():
    """The change surface is shared, not re-derived — one definition of scope."""
    try:
        from mini_ork.verify import touched_files  # type: ignore
        return touched_files
    except ImportError:
        pass
    candidate = HERE.parents[2] / "mini_ork" / "verify" / "touched_files.py"
    if not candidate.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_mo_touched_files", candidate)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        return None
    return module


def _touched(root: str) -> list | None:
    """The run's change surface, or ``None`` when it cannot be attributed."""
    explicit = os.environ.get("MINI_ORK_TOUCHED_FILES", "").strip()
    if explicit:
        return [p for p in explicit.splitlines() if p.strip()]
    scope = _load_scope()
    if scope is None:
        return None
    try:
        if not scope.is_git_worktree(root):
            return None
        return scope.touched_files(root)
    except Exception:
        return None


def _emit(*, passed: bool, reason: str, evidence: str | None, gating: bool,
          detail: str = "", findings: list | None = None) -> None:
    payload = {
        "verifier": "fallback_guard", "pass": passed,
        "verdict": "pass" if passed else "fail",
        "evidence_path": evidence,
        "checks_run": ["no-silent-fallback"],
        "failed_checks": [] if passed else ["no-silent-fallback"],
        "checks": [{"name": "no-silent-fallback", "pass": passed,
                    "gating": gating, "detail": detail or reason}],
        "reasons": [] if passed else [f"silent fallback added; see {evidence}"],
        "checked_criteria": ["no-silent-fallback"],
        "error_summary": reason,
        "findings": findings or [],
    }
    if not gating:
        # An abstention is not a pass: the engine reads ``status`` before
        # ``pass``, so an unexamined patch is recorded as unmeasured.
        payload["status"] = "unverified"
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def main() -> int:
    root = os.environ.get("MO_TARGET_CWD", "").strip() or os.getcwd()
    guard = _load_guard()
    if guard is None:
        _emit(passed=True, reason="fallback_guard module not found — not checked",
              evidence=None, gating=False)
        sys.stderr.write("[fallback-guard] module not found; abstaining\n")
        return 0

    touched = _touched(root)
    if touched is None:
        _emit(passed=True, reason="cannot attribute changed files — not checked",
              evidence=None, gating=False)
        sys.stderr.write("[fallback-guard] no change surface; abstaining\n")
        return 0

    targets = [Path(root) / p for p in touched if p.endswith(".py")]
    existing = [p for p in targets if p.is_file()]
    findings = guard.findings_for_paths(existing)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("w", encoding="utf-8") as log:
        log.write(f"root: {root}\n")
        log.write(f"touched .py files: {len(existing)} of {len(touched)}\n")
        for f in findings:
            log.write(f.render() + "\n")

    passed = not findings
    reason = ("no silent fallback in the touched files"
              if passed else f"{len(findings)} silent fallback(s)")
    _emit(passed=passed, reason=reason, evidence=str(LOG_PATH), gating=bool(existing),
          detail=f"{len(existing)} file(s) checked",
          findings=[f.as_dict() for f in findings])
    sys.stderr.write(f"[fallback-guard] {reason}\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
