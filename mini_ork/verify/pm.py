"""Node package-manager resolution for verifiers and reuse checks.

Verifiers and reuse predicates run inside the **target repo**, so the correct
package manager is a property of *that repo* — never of the engine host.
Hardcoding one is a real defect in both directions:

* ``npx eslint`` / ``npm run …`` against a repo declaring
  ``devEngines.packageManager = pnpm`` makes **npm refuse to start** — ``npm
  error code EBADDEVENGINES  Invalid name "pnpm" does not match "npm" for
  "packageManager"`` — before the tool runs at all. Observed live 2026-10-11 in
  ``recursive-validate-impl``: ``tier1_compile`` wrote ``lint_rc=1`` against
  ``tsc_rc=0``, and that **toolchain** failure was read as a **code** failure —
  cascading into 10 failed nodes and a rollback of a correct implementation.
* ``pnpm --dir …`` against an npm/yarn repo fails the mirror-image way.

Resolution order — the declaration is authoritative:

1. ``devEngines.packageManager.name`` — the repo's own statement of intent.
   ``onFail: "error"`` makes it a hard requirement, which is why nothing below
   ever substitutes a *different* manager: running npm against a pnpm-only repo
   IS the bug this module exists to prevent. If the declared manager is not
   installed, the command fails loudly rather than silently changing manager.
2. ``packageManager`` (``"pnpm@11.0.9"``).
3. The lockfile present (``pnpm-lock.yaml`` / ``yarn.lock`` / ``bun.lock*`` /
   ``package-lock.json``).
4. First of pnpm/yarn/bun/npm on ``PATH``; finally ``"npm"``.

``exec_argv`` prefers the repo's own installed ``node_modules/.bin/<tool>``.
That is the most robust choice and not a manager substitution: it is the exact
binary the repo pinned, it is what every manager's install produces, it cannot
trip a ``devEngines`` guard, and it is independent of the caller's cwd. The
same technique already backs ``recipes/code-fix/verifiers/typecheck.py``'s
``./node_modules/.bin/tsc``.

``run_argv`` and ``exec_argv`` always return an argv. A caller using either to
*detect* whether the repo has a usable node runner must gate on
``runnable_manager`` instead — otherwise a polyglot repo whose ``package.json``
exists but whose manager is not installed stops falling through to
pytest/cargo/go.

Verifiers are standalone scripts. The executor exports ``PYTHONPATH``
(``mini_ork/cli/execute.py``, ``_run_verifier_ref``) so this import resolves,
but each caller also bootstraps ``sys.path`` by walking up to the engine root —
so a verifier run by hand, or by a future caller that forgets, still imports.
"""
from __future__ import annotations

import json
import os
import shutil

MANAGERS = ("pnpm", "yarn", "bun", "npm")

# Literal argv heads a verifier must never hardcode. The four managers, plus
# `npx` — a *runner*, not a manager, which impersonates the ambient npm and
# trips the same `devEngines` guard. `npx` is the head the production defect
# actually used, so a guard over MANAGERS alone would miss its own bug.
DISALLOWED_ARGV_HEADS = (*MANAGERS, "npx", "npx.cmd")

_LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
    ("package-lock.json", "npm"),
)


def _package_json(root: str = ".") -> dict:
    """The repo's parsed ``package.json``, or ``{}`` when absent/unreadable."""
    try:
        with open(os.path.join(root, "package.json"), encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def declared_manager(root: str = ".") -> str:
    """The manager the repo *declares*, or ``""`` when it declares none."""
    pkg = _package_json(root)
    dev = pkg.get("devEngines")
    if isinstance(dev, dict):
        pm = dev.get("packageManager")
        if isinstance(pm, dict):
            name = str(pm.get("name") or "").strip().lower()
            if name in MANAGERS:
                return name
    field = str(pkg.get("packageManager") or "").strip().lower()
    if field:
        name = field.split("@", 1)[0].strip()
        if name in MANAGERS:
            return name
    return ""


def lockfile_manager(root: str = ".") -> str:
    """The manager implied by a lockfile in ``root``, or ``""``."""
    for name, manager in _LOCKFILES:
        if os.path.isfile(os.path.join(root, name)):
            return manager
    return ""


def package_manager(root: str = ".") -> str:
    """Resolve the manager for ``root``: declaration, then lockfile, then PATH."""
    declared = declared_manager(root)
    if declared:
        return declared
    locked = lockfile_manager(root)
    if locked:
        return locked
    for candidate in MANAGERS:
        if shutil.which(candidate):
            return candidate
    return "npm"


def runnable_manager(root: str = ".") -> str:
    """The resolved manager **iff it is actually executable**, else ``""``.

    ``run_argv`` always returns an argv; a caller that uses it to *detect*
    whether the repo has a usable node runner must gate on this instead, or it
    will stop falling through to pytest/cargo/go on a polyglot repo whose
    ``package.json`` exists but whose manager is not installed.
    """
    manager = package_manager(root)
    return manager if shutil.which(manager) else ""


def local_bin(root: str, tool: str) -> str:
    """Absolute path to the repo's own installed ``<tool>``, or ``""``."""
    path = os.path.join(root, "node_modules", ".bin", tool)
    return path if os.access(path, os.X_OK) else ""


def exec_argv(root: str, tool: str, *extra: str) -> list[str]:
    """argv that runs an installed TOOL (eslint, jest, vitest, stryker, playwright…)."""
    binary = local_bin(root, tool)
    if binary:
        return [binary, *extra]
    manager = package_manager(root)
    if shutil.which(manager):
        if manager == "bun":
            return [manager, "x", tool, *extra]
        return [manager, "exec", tool, *extra]
    return [tool, *extra]


def run_argv(root: str, script: str, *extra: str) -> list[str]:
    """argv that runs a ``package.json`` SCRIPT via the repo's own manager."""
    manager = package_manager(root)
    if manager == "npm" and extra:
        # npm needs the separator to forward args to the script; pnpm/yarn/bun
        # forward them directly. Without it npm swallows them as its own flags.
        return [manager, "run", script, "--", *extra]
    return [manager, "run", script, *extra]


def describe(root: str = ".") -> str:
    """One-line resolution summary, for a verifier's evidence log."""
    return (f"package manager: declared={declared_manager(root) or '-'} "
            f"lockfile={lockfile_manager(root) or '-'} resolved={package_manager(root)}")


# ── spawn-failure provenance (audit 2026-10-11) ──────────────────────────────
# A shell-out verifier that fails must say WHY, or the engine reads "the tool
# could not run" as "the code is wrong": one npm refusal on a pnpm repo
# (EBADDEVENGINES) cascaded into 10 failed nodes and a rollback of code no tool
# had checked. A verifier emits the return value as `failure_kind`; `toolchain`
# turns its verdict into an abstention (UNVERIFIED) instead of a refutation.
_SPAWN_FAILURES = (
    "command not found",                                    # sh
    "No such file or directory",                            # execve ENOENT
    "ENOENT",                                               # node spawn
    "EBADDEVENGINES",                                       # npm refuses the wrong manager
    "not recognized as an internal or external command",    # cmd.exe
    "could not determine executable to run",               # npx, no local install
)


def classify_spawn(argv, rc, output):  # noqa: ARG001 — argv is part of the contract
    """``"toolchain"`` when a shell-out never started its tool, else ``""``.

    A verifier emits this as ``failure_kind``: ``toolchain`` makes its verdict an
    abstention — the checks it would have made say nothing about the code — so a
    single bad runner call stops cascading into a rollback (audit 2026-10-11).
    ``""`` is the fail-closed default: the caller keeps today's behaviour, which
    for a failed shell-out means a plain refutation.

    LOGIC HAND-OFF: ``_SPAWN_FAILURES`` is a conservative seed. Which pattern (or
    rc) separates "the tool never started" from "the tool ran and rejected the
    input" is domain knowledge about the runners — refine it here. Every
    shell-out verifier calls this one function, so a fix here is a fix everywhere.
    """
    if rc == 127:  # the shell's own "command not found"
        return "toolchain"
    # A negative rc is a tool killed by a signal before it finished: it never
    # reached a verdict on the code. m3b-activity-feed-backend-removal
    # (2026-10-15) had its type-aware eslint SIGTERMed mid-run (lint_rc -15);
    # labelled `code`, it bought an implementer revise round for code nothing
    # had rejected. As an abstention the verifier is simply re-run.
    if isinstance(rc, int) and rc < 0:
        return "toolchain"
    text = output or ""
    for needle in _SPAWN_FAILURES:
        if needle in text:
            return "toolchain"
    return ""
