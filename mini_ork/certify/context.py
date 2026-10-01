"""Repo-context surface for the certify oracle.

The oracle judges whether a patch fixes a claimed bug. Without seeing the
code it is supposed to fix, the model falls back to its library priors and
writes a probe that RE-IMPLEMENTS the buggy function inline — so the probe
"reproduces" its own copy of the bug, not the repository's. The prompt alone
does not close this hole (measured: 1 of 1 measured attempts on a fresh repo
ignored the prompt and defined its own `def median(...)`). The structural
guard in this module — `imports_code_under_test` — is the load-bearing fix.

API summary:

  CodeContext                       frozen dataclass of (text, modules, files)
  changed_py_files(patch)           non-test .py paths from `+++ b/<p>` headers
  module_name(path)                 dotted module name, or None for invalid
  code_context(repo, base, patch)   prompt-ready block of BASE source for
                                    changed .py files, capped at max_chars
  imports_code_under_test(src, ms)  True iff `src` imports any of `ms` (or a
                                    parent) without subsequently redefining
                                    those names anywhere in the module

`module_name` returns None for paths whose components are not valid Python
identifiers (e.g. `a-b/c.py`). In that case the guard has no module to enforce
against — the probe is not rejected on that ground.
"""
from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodeContext:
    """Prompt-ready surface for the code under test.

    `text`     : "# file: <path> ... ```python\\n<source>\\n```" blocks joined.
                 Empty string when no changed files have source at base_sha
                 (e.g. patch only adds new files).
    `modules`  : importable dotted names of changed .py files, deduped in
                 first-seen order. Empty tuple when nothing is importable.
    `files`    : repo-relative paths of the changed .py files included in
                 `text`, deduped in first-seen order.
    """

    text: str
    modules: tuple[str, ...]
    files: tuple[str, ...]


# `+++ b/<path>` header regex — matches `git diff` output line-for-line.
# The path may contain spaces (rare but legal) and `+++` may also be the
# /dev/null sentinel for deletions.
_HEADER = re.compile(r"^\+\+\+ b/(.+)$", re.M)
_TEST_DIRS = ("test/", "tests/")
_TEST_FILE = re.compile(r"(^|/)(test_.+\.py$|.+_test\.py$)", re.I)


def changed_py_files(patch: str) -> list[str]:
    """Return repo-relative .py paths touched by `patch`, deduped in first-seen order.

    Filters out:
      * the /dev/null deletion sentinel,
      * non-`.py` files,
      * anything under `test/` or `tests/`,
      * any file named `test_*.py` or `*_test.py` at any depth.

    The exclusion is mechanical — there is no list of "test paths" to maintain.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in _HEADER.findall(patch):
        path = raw.strip()
        if not path or path == "/dev/null":
            continue
        if not path.endswith(".py"):
            continue
        if path.startswith(_TEST_DIRS) or _TEST_FILE.search(path):
            continue
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def module_name(path: str) -> str | None:
    """Convert a repo-relative `.py` path into its importable dotted name.

    * strips the `.py` suffix,
    * drops a leading `src/` segment (src-layout convention),
    * turns `pkg/__init__.py` into `pkg`,
    * turns `pkg/sub/mod.py` into `pkg.sub.mod`.

    Returns None when any path component is not a valid Python identifier
    (e.g. hyphens, dots). Callers that need to reject the path further
    should branch on None.
    """
    p = path[:-3] if path.endswith(".py") else path
    parts = p.split("/")
    if parts and parts[0] == "src":
        parts = parts[1:]
    if not parts:
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
        if not parts:
            return None
    for seg in parts:
        if not _IDENT.match(seg):
            return None
    return ".".join(parts)


def code_context(
    repo: Path,
    base_sha: str,
    patch: str,
    max_chars: int = 8000,
) -> CodeContext:
    """Build a `CodeContext` for the changed .py files at `base_sha`.

    For each path returned by `changed_py_files(patch)` that EXISTS at
    `base_sha` (a patch that ADDS a new file is silently skipped), the BASE
    source is fetched via `git -C repo show <base_sha>:<path>` and prepended
    with an `# file:` header. Output is truncated by `max_chars`; the last
    partial block carries a `# … truncated` line so the model knows it is
    seeing a prefix.

    The source shown is the BASE (buggy) version only — never the patch or
    the head version. The guard's whole point is to anchor the probe to the
    code it is meant to test, not to a snapshot of the fix.
    """
    files = changed_py_files(patch)
    modules: list[str] = []
    blocks: list[str] = []
    kept_files: list[str] = []
    used = 0

    for path in files:
        if used >= max_chars:
            break
        try:
            r = subprocess.run(
                ["git", "-C", str(repo), "show", f"{base_sha}:{path}"],
                capture_output=True, text=True, check=True,
            )
        except subprocess.CalledProcessError:
            # File does not exist at base (patch adds a new file) — skip.
            continue
        mod = module_name(path)
        # The module may still be in `modules` even when the file is truncated
        # or absent — `imports_code_under_test` keys on `modules` regardless.
        if mod is not None and mod not in modules:
            modules.append(mod)
        if mod is None:
            # Not importable; do not waste context on it, but record the path
            # so the certificate still says "we looked at this file".
            if path not in kept_files:
                kept_files.append(path)
            continue
        header = f"# file: {path}  (import as: {mod})\n```python\n"
        footer = "\n```\n"
        budget = max_chars - used
        body = r.stdout
        if len(header) + len(body) + len(footer) > budget:
            body = body[: max(0, budget - len(header) - len(footer))]
            body += "\n# … truncated\n"
        blocks.append(header + body + footer)
        used += len(header) + len(body) + len(footer)
        if path not in kept_files:
            kept_files.append(path)

    return CodeContext(
        text="".join(blocks),
        modules=tuple(modules),
        files=tuple(kept_files),
    )


# ── structural guard ─────────────────────────────────────────────────────────
# A probe that does not import the code under test proves nothing about the
# repository — it tests its own re-implementation. The guard rejects any
# candidate whose source has no `import` / `from … import …` line for the
# target module OR a parent package of it. After that, it also rejects any
# candidate that subsequently REDEFINES one of those imported names (or
# monkeypatches them onto a bound module alias) — a probe that does
# `from stats import median\ndef median(xs): …` tests its own copy, not the
# repository's, and must not pass.

# Match `import <name>` / `import <name>.<rest>` (name is an identifier).
_IMPORT_RE = re.compile(
    r"^\s*import\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)",
    re.M,
)
# Match `from <name>(.<rest>)? import …` — the leading module path before
# the first `import` keyword. The `from X import a, b` form is what
# generators use most often; `from X import a as b` is also covered by
# this regex because we only look at the head module.
_FROM_RE = re.compile(
    r"^\s*from\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s+import\b",
    re.M,
)


def _ancestors(module: str) -> tuple[str, ...]:
    """Return `(module, parent, grandparent, …)` — every prefix of the dotted path."""
    parts = module.split(".")
    return tuple(".".join(parts[: i]) for i in range(len(parts), 0, -1))


def _regex_fallback(src: str, targets: set[str]) -> bool:
    """Pre-AST regex check, unchanged. Used when `ast.parse(src)` raises SyntaxError."""
    for raw in _IMPORT_RE.findall(src):
        if any(a in targets for a in _ancestors(raw)):
            return True
    for raw in _FROM_RE.findall(src):
        if any(a in targets for a in _ancestors(raw)):
            return True
    return False


def _walk_qualifying_imports(
    tree: ast.AST, targets: set[str]
) -> tuple[set[str], bool]:
    """Walk `tree`; return ``(bound names from qualifying imports, any qualifying)``.

    Qualifying = the imported module (or any ancestor, per ``_ancestors``) is
    in ``targets``. Relative imports (``level > 0``) are skipped — they refer
    to code in the SAME package the probe lives in, not external code under
    test. ``from X import *`` records nothing but still flips the second
    element to ``True``: a wildcard import satisfies the guard, but its
    individual names cannot be rebound-checked.
    """
    bound: set[str] = set()
    any_qual = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(a in targets for a in _ancestors(alias.name)):
                    bound.add(alias.asname or alias.name.split(".")[0])
                    any_qual = True
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            module = node.module or ""
            if not any(a in targets for a in _ancestors(module)):
                continue
            any_qual = True
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound.add(alias.asname or alias.name)
    return bound, any_qual


def _collect_name_targets(t: ast.AST, into: set[str]) -> None:
    """Recurse into Tuple/List/Starred targets to collect every bare ``Name`` id."""
    if isinstance(t, ast.Name):
        into.add(t.id)
    elif isinstance(t, (ast.Tuple, ast.List)):
        for elt in t.elts:
            _collect_name_targets(elt, into)
    elif isinstance(t, ast.Starred):
        _collect_name_targets(t.value, into)


def _walk_rebindings(tree: ast.AST) -> set[str]:
    """Walk ``tree``; collect every name rebound in any scope, any depth.

    Strict per the kickoff: a false reject costs one probe retry; a false
    accept certifies a copy. ``ast.arg`` is included — a function parameter
    that shadows an imported name makes the probe test its local default,
    not the import, so it counts as a redefinition. ``ast.walk`` recurses
    through FunctionDef / AsyncFunctionDef / ClassDef bodies — nested
    rebindings are visible.
    """
    rebinds: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            rebinds.add(node.name)
        elif isinstance(node, ast.arg):
            rebinds.add(node.arg)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                _collect_name_targets(t, rebinds)
        elif isinstance(node, ast.AnnAssign):
            if node.target is not None:
                _collect_name_targets(node.target, rebinds)
        elif isinstance(node, ast.AugAssign):
            _collect_name_targets(node.target, rebinds)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _collect_name_targets(node.target, rebinds)
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                _collect_name_targets(node.optional_vars, rebinds)
        elif isinstance(node, ast.NamedExpr):
            if isinstance(node.target, ast.Name):
                rebinds.add(node.target.id)
        elif isinstance(node, ast.Global):
            rebinds.update(node.names)
    return rebinds


def _has_module_alias_monkeypatch(tree: ast.AST, bound: set[str]) -> bool:
    """True iff any node rebinds an attribute on a bound module alias.

    ``X.attr = …`` (Assign / AugAssign) and ``setattr(X, …)`` where ``X`` is
    a bound module alias from step 3 monkeypatch the code under test in
    place — the probe no longer exercises the repository's definition.
    """
    if not bound:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if _is_alias_attr_assign(tgt, bound):
                    return True
        elif isinstance(node, ast.AugAssign):
            if _is_alias_attr_assign(node.target, bound):
                return True
        elif isinstance(node, ast.Call):
            if _is_setattr_alias_call(node, bound):
                return True
    return False


def _is_alias_attr_assign(t: ast.AST, bound: set[str]) -> bool:
    return (
        isinstance(t, ast.Attribute)
        and isinstance(t.value, ast.Name)
        and t.value.id in bound
    )


def _is_setattr_alias_call(node: ast.Call, bound: set[str]) -> bool:
    if not (isinstance(node.func, ast.Name) and node.func.id == "setattr"):
        return False
    if not node.args:
        return False
    first = node.args[0]
    return isinstance(first, ast.Name) and first.id in bound


def imports_code_under_test(src: str, modules: tuple[str, ...]) -> bool:
    """True iff ``src`` imports any module in ``modules`` (or any parent) AND
    does not subsequently redefine any of those imported names anywhere in
    the module.

    A parent package counts: a probe that does ``from stats import median``
    satisfies module ``stats``, and a probe that does ``from stats.core
    import median`` also satisfies module ``stats`` (the guard is permissive
    on the high end — it never blocks a probe that touches the code, only
    one that does not touch it at all).

    A redefinition counts anywhere in the module — a nested ``def
    median(xs)``, a module-level ``median = lambda xs: 0``, a class body
    rebinding the name, a ``with … as median``, a ``for median in …``, a
    walrus ``median := …``, ``global median``, a function parameter ``def
    test_x(median)``, an attribute assignment ``stats.median = …``, or a
    ``setattr(stats, "median", …)``. Each is a structural hole: the probe
    tests its own copy, not the repository's.

    When ``modules`` is empty the guard is skipped — ``True`` keeps today's
    behaviour for SWE-bench-style callers that pass ``CodeContext(modules=())``.
    Source that fails to parse falls back to the original regex check (a
    probe that does not parse fails later when run; the guard must not
    crash).
    """
    if not modules:
        return True
    targets: set[str] = set()
    for m in modules:
        targets.update(_ancestors(m))
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return _regex_fallback(src, targets)
    bound, any_qual = _walk_qualifying_imports(tree, targets)
    if not any_qual:
        return False
    rebinds = _walk_rebindings(tree)
    if bound & rebinds:
        return False
    if _has_module_alias_monkeypatch(tree, bound):
        return False
    return True


__all__ = [
    "CodeContext",
    "changed_py_files",
    "module_name",
    "code_context",
    "imports_code_under_test",
]
