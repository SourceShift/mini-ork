#!/usr/bin/env python3
"""verifiers/deception.py — the deterministic deception gate for ``code-fix``.

A repair that passes the suite is not necessarily a repair. This node scans the
run's own change surface at the base revision and at head and classifies the
result: an introduced finding is ``introduction``, a still-present target is
``omission``, other surviving touched findings are ``inadequacy``, and only a
resolved-and-clean surface is ``clean``. No target named means ``unmeasured`` —
an abstention, never a pass.

``security.py`` is the shape this file follows: scope is the run's change
surface (``MINI_ORK_TOUCHED_FILES``, else the run's dirty set), output is one
JSON envelope on stdout, and anything that cannot be measured abstains.

Env: MO_TARGET_CWD?, MINI_ORK_TOUCHED_FILES?, MINI_ORK_RUN_DIR?, MO_DECEPTION_TARGET?.
Output: single-line JSON on stdout.
Exit: 0 pass / vacuous / abstain, 1 fail.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# ``parents[2]`` of this file's DIRECTORY is the repo root (verifiers → code-fix
# → recipes → root); of the file itself it would be ``recipes/``.
_HERE = Path(__file__).resolve().parent

CHECK_NAME = "scanner-cleanliness"


def _load_engine():
    """The deception module comes from the ENGINE, never from the target repo."""
    try:
        from mini_ork.verify import deception_check  # type: ignore
        return deception_check
    except ImportError:
        pass
    engine = str(_HERE.parents[2])
    if engine not in sys.path:
        sys.path.insert(0, engine)
    try:
        from mini_ork.verify import deception_check  # type: ignore
        return deception_check
    except ImportError as exc:
        sys.stderr.write(f"[deception] module not importable from {engine}: {exc}\n")
        return None


def _load_scope():
    """The change surface is shared with the other scoped verifiers."""
    try:
        from mini_ork.verify import touched_files  # type: ignore
        return touched_files
    except Exception:
        sys.stderr.write("[deception] touched_files module could not be imported\n")
        return None


def _touched(root: str, scope) -> "list | None":
    """The run's change surface, or ``None`` when it cannot be attributed."""
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
        sys.stderr.write("[deception] change surface could not be resolved\n")
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
    """The revision to measure the repair against, or ``""`` when there is none."""
    if scope is not None:
        base = scope.scoped_base(root)
        # The scope module may be an older copy (a run's engine is pinned), so
        # the ref is re-checked here: a base that does not resolve would make the
        # base checkout fail and silently degrade the gate.
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


def _envelope(status: str, report: dict) -> dict:
    """One envelope for pass / fail / vacuous / abstain.

    ``status: unverified`` on a dispatched node abstains the whole run and
    withholds the publish, so only a real abstention carries it. A vacuous
    verdict (there is no change surface to measure) exits the node green while
    claiming nothing: no ``status`` key, a non-empty ``error_summary`` and one
    NON-GATING check.
    """
    classification = report.get("classification", "")
    detail = report.get("detail") or report.get("reason") or ""
    payload = {
        "verifier": "deception",
        "pass": status in ("pass", "vacuous"),
        "verdict": status,
        "classification": classification,
        "evidence_path": None,
        "checks": [{
            "name": CHECK_NAME,
            "pass": status in ("pass", "vacuous"),
            "gating": False,
            "detail": detail,
        }],
        "reasons": [detail] if detail else [],
        "findings": [],
        "error_summary": "" if status == "pass" else detail,
    }
    if status == "abstain":
        payload["status"] = "unverified"
        payload["failure_kind"] = "toolchain"
    elif status == "vacuous":
        payload["error_summary"] = detail or "no change surface"
    return payload


def _emit(payload: dict) -> None:
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def _abstain(reason: str) -> dict:
    return _envelope("abstain", {"reason": reason})


def main() -> int:
    root = os.environ.get("MO_TARGET_CWD", "").strip() or os.getcwd()
    run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")

    engine = _load_engine()
    if engine is None:
        _emit(_abstain("deception module could not be loaded — not measured"))
        return 0

    scope = _load_scope()
    touched = _touched(root, scope)
    if touched is None:
        _emit(_abstain("cannot attribute changed files — not measured"))
        return 0

    base = _resolve_base(scope, root)
    status, report = engine.evaluate(
        root,
        base,
        touched,
        run_dir=run_dir,
        target_env=os.environ.get(engine.TARGET_ENV, ""),
    )
    # "No target named" means the check does not apply to this run, not that it
    # failed to measure. Abstaining here would fail every ordinary run and
    # withhold its publish (a dispatched node's ``status: unverified`` is a hard
    # abstention), and the node is meant to be inert until a producer names a
    # target. Only a genuine inability to measure — no base revision, an
    # unloadable engine, an unattributable change surface — stays an abstention.
    if status == "abstain" and report.get("classification") == engine.UNMEASURED:
        status = "vacuous"
    _emit(_envelope(status, report))
    detail = report.get("detail") or report.get("reason") or ""
    sys.stderr.write(f"[deception] {status}: {detail}\n")
    return 0 if status in ("pass", "vacuous", "abstain") else 1


if __name__ == "__main__":
    sys.exit(main())
