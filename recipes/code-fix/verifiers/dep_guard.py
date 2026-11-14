#!/usr/bin/env python3
"""verifiers/dep_guard.py — the offline dependency gate, as a recipe verifier.

``mini_ork/verify/dep_guard.py`` holds the rule; this script applies it to the
run's change surface and emits the single-line verifier envelope. A *new* import
or a *declared* requirement in a touched file must resolve against the vendored
allowlist, carry an exact ``==`` pin, and not come from a URL/VCS source.

Scope is the run's own change surface (``MINI_ORK_TOUCHED_FILES`` else
``mini_ork.verify.touched_files``), exactly like ``verifiers/fallback_guard.py``:
a pre-existing bad requirement elsewhere in the tree is not this patch's
failure. Whole current file content is scanned (not a diff), and for ``.py``
files only the imports the change *adds* are checked.

Env: MO_TARGET_CWD?, MO_DEP_ALLOWLIST?, MINI_ORK_TOUCHED_FILES?
Output: one line of JSON on stdout.
Exit: 0 pass, 1 fail, 0 abstain (the envelope carries ``verdict``).
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

CHECKS_RUN = [
    "no-unpinned-dependency",
    "no-untrusted-source",
    "no-unknown-dependency",
]

#: Which name-rule findings fail which check. ``squat-suspect`` is an unknown
#: dependency that is one edit from an allowlisted one, so it rides the
#: unknown-dependency check while still appearing in ``failed_checks``.
_CHECK_RULES = {
    "no-unpinned-dependency": {"unpinned-dependency"},
    "no-untrusted-source": {"untrusted-source"},
    "no-unknown-dependency": {"unknown-dependency", "squat-suspect"},
}


def _import(name: str):
    """Load a framework module from the ENGINE, never from the scanned repo.

    Prepending the target to ``sys.path`` would let a repository under audit
    ship its own ``mini_ork/verify/dep_guard.py`` that finds nothing, or a
    ``touched_files`` that reports an empty surface — the gate would be bypassed
    by the content it exists to check. The engine is this file's own tree
    (``HERE.parents[2]``), which for a self-edit run is the run's copy of the
    framework. Deferred on purpose: an unimportable gate must abstain, not
    traceback.
    """
    try:
        return importlib.import_module(name)
    except ImportError:
        pass
    # The engine root, not the target: the module may import its own package
    # siblings (`touched_files` is loaded the same way), and a bare file load
    # gives it no package context. Inserting the ENGINE is safe in a way
    # inserting the scanned repo is not — the engine is the code doing the check.
    engine = str(HERE.parents[2])
    if engine not in sys.path:
        sys.path.insert(0, engine)
    try:
        return importlib.import_module(name)
    except ImportError:
        pass
    candidate = HERE.parents[2].joinpath(*name.split(".")).with_suffix(".py")
    if not candidate.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"_mo_{name.rsplit('.', 1)[-1]}", candidate)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:  # fallback-guard: allow: an unloadable gate must abstain, never crash the run
        sys.modules.pop(spec.name, None)
        return None
    return module


def _touched(root: str):
    """The run's change surface, or ``None`` when it cannot be attributed."""
    explicit = os.environ.get("MINI_ORK_TOUCHED_FILES", "").strip()
    if explicit:
        return [line for line in explicit.splitlines() if line.strip()]
    scope = _import("mini_ork.verify.touched_files")
    if scope is None:
        return None
    if not scope.is_git_worktree(root):
        return None
    return scope.touched_files(root)


def _allowlist_path(dep, root: str) -> Path:
    """The snapshot comes from the ENGINE, never from the scanned repo.

    A repository under audit that ships its own allowlist is a repository that
    can add itself to it — the gate would be checking a list its target wrote.
    The engine is this file's own tree, which for a self-edit run is the run's
    copy of the framework; ``MO_DEP_ALLOWLIST`` stays an explicit operator
    override (a missing value there still abstains, never passes).
    """
    override = os.environ.get("MO_DEP_ALLOWLIST", "").strip()
    if override:
        candidate = Path(override)
        return candidate if candidate.is_absolute() else Path(root) / candidate
    return HERE.parents[2] / dep.ALLOWLIST_DEFAULT


def _read(path: Path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _pyproject_entries(dep, text: str) -> list:
    # The parser lives in the rule module: it already carries the tomllib /
    # tomli fallback, and a second bare `import tomllib` here would crash the
    # verifier on a host without one instead of abstaining.
    return dep.parse_pyproject(text)


def _base_text(root: str, base: str, rel: str):
    """The base version of ``rel``; ``None`` when it did not exist then."""
    try:
        result = subprocess.run(
            ["git", "-C", root, "show", f"{base}:{rel}"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _base_imports(dep, root: str, base: str, rel: str) -> frozenset:
    """Imports in the base version of ``rel``; empty when there is no base."""
    text = _base_text(root, base, rel)
    if text is None:
        return frozenset()
    try:
        return dep.parse_imports(text)
    except ValueError:
        return frozenset()


def _scan(dep, root: str, touched: list, allowlist: frozenset, base: str):
    """Check the entries the change ADDS, never the whole of a touched file.

    Whole-file scanning reddens a run for a requirement or requirement-range it
    never wrote: editing anything in ``requirements.txt`` would re-fail every
    pre-existing ``>=`` in it. The base version of each file supplies what was
    already there, exactly as it does for imports.
    """
    findings = []
    unparsed = []
    requirements_checked = 0
    imports_checked = 0
    root_path = Path(root)
    for rel in touched:
        rel_posix = rel.replace(os.sep, "/")
        path = root_path / rel
        if not path.is_file():
            continue
        text = _read(path)
        if text is None:
            unparsed.append(rel_posix)
            continue
        base_text = _base_text(root, base, rel_posix)
        if dep.REQUIREMENTS_RE.search(rel_posix):
            try:
                added = dep.new_requirements(
                    dep.parse_requirements(base_text or ""),
                    dep.parse_requirements(text),
                )
            except ValueError:
                unparsed.append(rel_posix)
                continue
            requirements_checked += len(added)
            findings.extend(dep.check_requirements(
                added, allowlist=allowlist, path=rel_posix,
            ))
        elif Path(rel_posix).name == "pyproject.toml":
            try:
                added = dep.new_requirements(
                    _pyproject_entries(dep, base_text) if base_text else [],
                    _pyproject_entries(dep, text),
                )
            except (ValueError, OSError):
                unparsed.append(rel_posix)
                continue
            requirements_checked += len(added)
            findings.extend(dep.check_requirements(
                added, allowlist=allowlist, path=rel_posix,
            ))
        elif rel_posix.endswith(".py"):
            old = _base_imports(dep, root, base, rel_posix)
            try:
                new = dep.parse_imports(text)
            except ValueError:
                unparsed.append(rel_posix)
                continue
            added = dep.new_imports(old, new)
            imports_checked += len(added)
            findings.extend(dep.check_imports(
                added, allowlist=allowlist, root_dir=root_path, path=rel_posix,
            ))
    return findings, unparsed, requirements_checked, imports_checked


def _checks(findings, gating: bool) -> list:
    failed = {finding.rule for finding in findings}
    checks = []
    for name in CHECKS_RUN:
        ok = not (_CHECK_RULES[name] & failed)
        checks.append({
            "name": name,
            "pass": ok,
            "gating": gating,
            "detail": "clear" if ok else "see findings",
        })
    return checks


def _emit(payload: dict) -> None:
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def _emit_abstain(reason: str) -> int:
    _emit({
        "verifier": "dep_guard",
        "pass": False,
        "verdict": "abstain",
        "status": "unverified",
        "failure_kind": "toolchain",
        "evidence_path": None,
        "checks_run": list(CHECKS_RUN),
        "failed_checks": [],
        "checks": [
            {"name": name, "pass": False, "gating": False, "detail": reason}
            for name in CHECKS_RUN
        ],
        "reasons": [reason],
        "checked_criteria": list(CHECKS_RUN),
        "error_summary": reason,
        "findings": [],
        "unparsed": [],
    })
    return 0


def _emit_result(findings, unparsed, requirements_checked, imports_checked) -> int:
    passed = not findings
    failed_checks = sorted({finding.rule for finding in findings})
    coverage = (
        f"{requirements_checked} requirements and "
        f"{imports_checked} new imports checked"
    )
    if passed:
        summary = coverage
        reasons = []
    else:
        summary = f"{len(findings)} dependency finding(s)"
        reasons = [f"{len(findings)} dependency finding(s); see findings"]
    if unparsed:
        # The fail stands, but a reader must know the check was partial.
        uncovered = (
            f"{len(unparsed)} touched file(s) could not be parsed and were not "
            "checked: " + ", ".join(unparsed[:3])
        )
        summary = f"{summary}; {uncovered}"
        reasons.append(uncovered)
    _emit({
        "verifier": "dep_guard",
        "pass": passed,
        "verdict": "pass" if passed else "fail",
        "evidence_path": None,
        "checks_run": list(CHECKS_RUN),
        "failed_checks": failed_checks,
        "checks": _checks(findings, gating=True),
        "reasons": reasons,
        "checked_criteria": list(CHECKS_RUN),
        "error_summary": summary,
        "findings": [finding.as_dict() for finding in findings],
        "unparsed": unparsed,
    })
    return 0 if passed else 1


def _resolve_base(scope, root: str) -> str:
    """The revision the run's change is measured against.

    The run's own start point first; otherwise ``HEAD``, because ``git diff``
    with no revision compares the worktree to the INDEX and cannot see a staged
    edit — a run that stages its work before the gate runs would otherwise
    measure as having changed nothing.
    """
    if scope is not None:
        base = scope.scoped_base(root)
        # ``scoped_base`` already refuses a ref it cannot resolve, but the
        # module may be an older copy (a run's engine is pinned), so the check
        # that matters is made here too: a base that does not resolve makes
        # every pre-existing import look newly added.
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


def _resolves(root: str, ref: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", root, "rev-parse", "--verify", "--quiet",
             f"{ref}^{{commit}}"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def main() -> int:
    root = os.environ.get("MO_TARGET_CWD", "").strip() or os.getcwd()
    dep = _import("mini_ork.verify.dep_guard")
    if dep is None:
        return _emit_abstain("dependency gate module is not importable")

    allow_path = _allowlist_path(dep, root)
    try:
        allowlist = dep.load_allowlist(allow_path)
    except OSError as exc:
        return _emit_abstain(
            f"allowlist snapshot unavailable at {allow_path}: {exc}"
        )

    touched = _touched(root)
    if touched is None:
        return _emit_abstain("cannot attribute the run's change surface")

    scope = _import("mini_ork.verify.touched_files")
    base = _resolve_base(scope, root)
    if not base:
        # Without a revision every import and requirement in a touched file is
        # indistinguishable from one the change added, so the gate would report
        # pre-existing dependencies as new ones. That is not a refutation.
        return _emit_abstain("no base revision to measure the change against")
    findings, unparsed, requirements_checked, imports_checked = _scan(
        dep, root, touched, allowlist, base,
    )
    if unparsed and not findings:
        # A file the gate could not read or parse was not checked. Reporting the
        # run as passing would claim coverage it does not have.
        return _emit_abstain(
            f"{len(unparsed)} touched file(s) could not be parsed: "
            + ", ".join(unparsed[:3])
        )
    # A violation the gate DID find is a refutation whatever else was unreadable:
    # abstaining here would downgrade a real fail to "unmeasured" and let the
    # patch through, which is the fail-open this gate exists to prevent.
    return _emit_result(findings, unparsed, requirements_checked, imports_checked)


if __name__ == "__main__":
    raise SystemExit(main())
