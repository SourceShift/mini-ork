"""Language-aware scoping for the recursive-validate-impl tier verifiers.

The tier-1/2/3 verifiers were written against a TypeScript+Jest repo: tier1
looked only for ``.ts``/``.js``/``.sh`` touched files, tier2 only for
``*.test.ts``/``__tests__`` siblings, tier3 only for ``fast-check``/``stryker``.
Every other repo therefore passed all three tiers *vacuously* — the evidence
said "no TypeScript/JavaScript source files" and the node turned green without
anything having been checked (observed on the Rust voicekit repo, 2026-11-19:
three vacuous passes ahead of a tier-4 panel that had to do all the work).

This module supplies the repository-shape logic the verifiers need for the two
ecosystems whose tooling is unambiguous to detect:

- **Rust** — a touched ``.rs`` file belongs to the nearest ancestor directory
  holding a ``Cargo.toml``; that manifest names the package to scope
  ``cargo check`` / ``cargo test`` to. Property targets are ``tests/*.rs`` files
  that are named like a property suite or contain a ``proptest!``/``quickcheck!``
  macro.
- **Python** — a touched ``module.py`` maps to a sibling ``test_module.py``
  (same directory or its ``tests/`` subdirectory), and a touched ``test_*.py``
  is itself a scoped test.

Everything here is pure filesystem inspection: no subprocess, no network, so it
is cheap enough for a verifier to run on every node. Callers still decide
whether the toolchain exists (``shutil.which``); this module only answers
"what would be checked".
"""
from __future__ import annotations

import os
import re
from typing import Iterable, List, Tuple

# One `[package]` table in a Cargo.toml, and its `name = "..."` entry. A
# `[workspace]` virtual manifest has no `[package]` table and yields "".
_PACKAGE_TABLE = re.compile(r"^\s*\[package\]\s*$", re.MULTILINE)
_NAME_ENTRY = re.compile(r'^\s*name\s*=\s*"([^"]+)"', re.MULTILINE)
_PROPERTY_MACRO = re.compile(r"\b(proptest|quickcheck)\s*!")


def is_rust(path: str) -> bool:
    return path.endswith(".rs")


def is_python(path: str) -> bool:
    return path.endswith(".py")


def _package_name(manifest_path: str) -> str:
    """The ``[package] name`` of one Cargo.toml, or "" for a virtual manifest."""
    try:
        with open(manifest_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return ""
    table = _PACKAGE_TABLE.search(text)
    if not table:
        return ""
    match = _NAME_ENTRY.search(text, table.end())
    return match.group(1) if match else ""


def rust_crate_roots(paths: Iterable[str], root: str = ".") -> List[str]:
    """Nearest ``Cargo.toml`` ancestor of each touched ``.rs`` file, deduped.

    The walk stops at ``root`` — a manifest outside the target repo is never a
    scope this run may invoke. Paths with no manifest ancestor are dropped: a
    ``.rs`` file no crate owns is checked by the workspace-wide fallback, not by
    a package guess.
    """
    boundary = os.path.abspath(root)
    found: List[str] = []
    for path in paths:
        if not is_rust(path):
            continue
        current = os.path.dirname(os.path.abspath(path))
        while True:
            if os.path.isfile(os.path.join(current, "Cargo.toml")):
                if current not in found:
                    found.append(current)
                break
            if current == boundary or current == os.path.dirname(current):
                break
            if not os.path.abspath(current).startswith(boundary):
                break
            current = os.path.dirname(current)
    return found


def rust_packages(paths: Iterable[str], root: str = ".") -> List[str]:
    """Package names for the crates owning the touched ``.rs`` files."""
    names: List[str] = []
    for crate in rust_crate_roots(paths, root):
        name = _package_name(os.path.join(crate, "Cargo.toml"))
        if name and name not in names:
            names.append(name)
    return names


def rust_property_targets(paths: Iterable[str], root: str = ".") -> List[Tuple[str, str]]:
    """``(package, test_target)`` for Rust property suites in the touched crates.

    A property suite is an integration test under ``<crate>/tests/`` whose file
    name mentions a property check or whose body uses ``proptest!`` or
    ``quickcheck!``. ``cargo test --test <stem>`` then runs exactly that target.
    """
    targets: List[Tuple[str, str]] = []
    for crate in rust_crate_roots(paths, root):
        package = _package_name(os.path.join(crate, "Cargo.toml"))
        tests_dir = os.path.join(crate, "tests")
        if not package or not os.path.isdir(tests_dir):
            continue
        try:
            entries = sorted(os.listdir(tests_dir))
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(".rs"):
                continue
            stem = entry[: -len(".rs")]
            full = os.path.join(tests_dir, entry)
            named = re.search(r"prop|quickcheck", stem, re.IGNORECASE)
            macro = False
            try:
                with open(full, encoding="utf-8", errors="replace") as handle:
                    macro = bool(_PROPERTY_MACRO.search(handle.read()))
            except OSError:
                pass
            if named or macro:
                pair = (package, stem)
                if pair not in targets:
                    targets.append(pair)
    return targets


def python_test_files(paths: Iterable[str], root: str = ".") -> List[str]:
    """Scoped pytest targets for the touched Python files.

    A touched ``test_*.py`` is its own test; a touched ``module.py`` maps to
    ``test_module.py`` in the same directory or its ``tests/`` subdirectory.
    Only existing files are returned — a missing test is not a scope.
    """
    _ = root  # kept for signature symmetry with the Rust helpers
    found: List[str] = []
    for path in paths:
        if not is_python(path):
            continue
        base = os.path.basename(path)
        directory = os.path.dirname(path)
        candidates: List[str] = []
        if base.startswith("test_") or base.endswith("_test.py"):
            candidates.append(path)
        else:
            stem = base[: -len(".py")]
            candidates.append(os.path.join(directory, f"test_{stem}.py"))
            candidates.append(os.path.join(directory, "tests", f"test_{stem}.py"))
            parent_tests = os.path.join(os.path.dirname(directory), "tests")
            candidates.append(os.path.join(parent_tests, f"test_{stem}.py"))
        for candidate in candidates:
            if os.path.isfile(candidate) and candidate not in found:
                found.append(candidate)
    return found
