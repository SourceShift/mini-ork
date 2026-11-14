"""Offline dependency gate — existence, pinning, source, and squat distance.

An implementer that writes ``import pkg_x`` or adds a ``requirements`` line has
not verified that ``pkg_x`` exists. A hallucinated or unpinned name is exactly
the slot an attacker registers. This module makes the checkable part of that
rule deterministic: a *new* import or a *declared* requirement must resolve
against a vendored allowlist snapshot, must carry an exact ``==`` pin, and must
not come from a URL or VCS source.

Grounded in a committed snapshot, never in a model's opinion: ``tomllib`` and
``ast`` only, no network, no provider access. Where the snapshot cannot answer
(the allowlist is missing) the gate abstains rather than passing.

Two tiers share one rule set:

* **Simple / local.** ``python3 -m mini_ork.verify.dep_guard --root <dir>``
  scans a tree and prints ``{"findings": [...], "count": n}``. Stdlib only.
* **Secure / recipe.** ``recipes/code-fix/verifiers/dep_guard.py`` runs the same
  functions over the run's change surface and emits the verifier envelope.

The allowlist is generated, never hand-typed::

    python3 -m mini_ork.verify.dep_guard --write-allowlist \\
        recipes/code-fix/data/dependency-allowlist.txt
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path

ALLOWLIST_DEFAULT = "recipes/code-fix/data/dependency-allowlist.txt"

#: Distribution name -> import aliases that are not the distribution name.
IMPORT_ALIASES = {
    "pyyaml": ("yaml",),
    "pymupdf": ("fitz",),
    "python_dateutil": ("dateutil",),
    "agent_client_protocol": ("acp",),
}

#: Repository roots that are this project's own code, not third-party deps.
FIRST_PARTY_ROOTS = ("mini_ork", "tests", "recipes", "scripts", "bin", "evals")

REQUIREMENTS_RE = re.compile(r"(^|/)requirements[^/]*\.txt$")

RULES = (
    "unpinned-dependency",
    "untrusted-source",
    "unknown-dependency",
    "squat-suspect",
)

ALLOWLIST_HEADER = (
    "# Vendored dependency allowlist for the code-fix dep gate.",
    "# One normalized name per line: a distribution name or an import name.",
    "# Regenerate: python3 -m mini_ork.verify.dep_guard --write-allowlist \\",
    "#   recipes/code-fix/data/dependency-allowlist.txt",
    "# A new dependency is a deliberate, reviewable line in this file — never a",
    "# silent side effect of an implementer's import. Snapshot date: 2026-11-14.",
)

_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Finding:
    """One dependency finding at ``path:line`` (``line=0`` is file-level)."""

    path: str
    line: int
    rule: str
    name: str
    why: str

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "line": self.line,
            "rule": self.rule,
            "name": self.name,
            "why": self.why,
        }


def norm(name: str) -> str:
    """PEP 503-ish normalization: ``PyYAML`` and ``pyyaml`` are one name."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def one_edit_apart(a: str, b: str) -> bool:
    """True when the Levenshtein distance between ``a`` and ``b`` is exactly 1.

    Equal strings and distance >= 2 are False. Transpositions count as two
    edits (that is what Levenshtein says) and are therefore not "one edit".
    """
    if a == b:
        return False
    la, lb = len(a), len(b)
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    if abs(la - lb) != 1:
        return False
    shorter, longer = (a, b) if la < lb else (b, a)
    i = 0
    while i < len(shorter) and shorter[i] == longer[i]:
        i += 1
    return shorter[i:] == longer[i + 1:]


def _single_transposition(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` differ only by swapping two adjacent chars."""
    if len(a) != len(b) or a == b:
        return False
    diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    if len(diff) != 2:
        return False
    i, j = diff
    return j == i + 1 and a[i] == b[j] and a[j] == b[i]


def _squat_apart(a: str, b: str) -> bool:
    """The squat-detect rule: one edit, or one adjacent transposition.

    ``one_edit_apart`` stays pure Levenshtein-1 as specified. A transposition
    (``reqeusts`` for ``requests``) is Levenshtein-2 yet is the archetypal
    squattable typo, so the squat rule admits it without weakening the metric.
    """
    return one_edit_apart(a, b) or _single_transposition(a, b)


def load_allowlist(path: Path) -> frozenset:
    """Read the snapshot: blank lines and ``#`` comments skipped, names normalized.

    A missing or unreadable file raises (``FileNotFoundError`` / ``OSError``);
    the caller abstains rather than treating "no snapshot" as "no findings".
    """
    text = Path(path).read_text(encoding="utf-8")
    names = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        normalized = norm(stripped)
        if normalized:
            names.add(normalized)
    return frozenset(names)


def check_name(name: str, *, allowlist: frozenset, path: str, line: int) -> list:
    """The shared name rule: allowed, then squat-suspect, then unknown."""
    normalized = norm(name)
    if not normalized:
        return []
    if normalized in allowlist:
        return []
    close = sorted(entry for entry in allowlist if _squat_apart(normalized, entry))
    if close:
        # Name the distance that actually matched: a transposition is the
        # archetypal typo-squat and calling it "one edit" misstates the evidence
        # a reviewer reads before deciding whether the name was deliberate.
        distance = (
            "one adjacent transposition from"
            if _single_transposition(normalized, close[0])
            else "one edit from"
        )
        return [Finding(
            path, line, "squat-suspect", name,
            f"{distance} allowlisted '{close[0]}'",
        )]
    return [Finding(
        path, line, "unknown-dependency", name, "not in the vendored allowlist",
    )]


def _is_directive(body: str) -> bool:
    return body.startswith((
        "--index-url", "--extra-index-url", "-e ", "-e=",
    ))


def parse_requirements(text: str) -> list:
    """``(lineno, name, spec)`` for every declared requirement line, 1-based.

    Blank lines, ``#`` comments, ``-r``/``--requirement`` includes and
    ``--hash`` continuation lines are skipped. A trailing environment marker
    (``; ...``) is stripped. For a direct reference (``name @ url``) and for
    index/editable directives the full raw line is carried in ``spec`` so the
    caller can classify the source. Extras stay on the ``spec`` side.
    """
    entries = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in ("-r", "--requirement") or line.startswith(("-r ", "--requirement ")):
            continue
        if line.startswith("--hash"):
            continue
        body = line.split(";", 1)[0].strip()
        if not body:
            continue
        match = _NAME_RE.match(body)
        name = match.group(0) if match else ""
        if " @ " in body:
            entries.append((lineno, name, body))
            continue
        if _is_directive(body):
            # The directive is not a declared package name; keep the raw line in
            # `spec` so check_requirements can see the source, and do not let a
            # made-up name ("-e", "--index-url") reach the name rules.
            entries.append((lineno, "", body))
            continue
        spec = body[match.end():].strip() if match else ""
        entries.append((lineno, name, spec))
    return entries


#: A scheme that does not come from an index. ``file://`` and ``ssh://`` are as
#: reachable as ``https://`` and were missed by an earlier substring test.
_URL_SCHEME_RE = re.compile(r"\b(?:https?|ftp|file|ssh|git|hg|svn|bzr)://", re.IGNORECASE)
#: VCS pseudo-scheme: ``git+https://``, ``git+ssh://``, ``hg+file://``.
_VCS_SCHEME_RE = re.compile(r"\b(?:git|hg|svn|bzr)\+", re.IGNORECASE)


def _untrusted(spec: str) -> bool:
    """True when the requirement does not resolve from the configured index.

    A direct reference (``pkg @ url``) admits a URL with or without spaces
    around the ``@``, and pip accepts several schemes: matching one spelling of
    one of them lets ``pkg @ file:///tmp/x.whl`` through unremarked.
    """
    if _VCS_SCHEME_RE.search(spec) or _URL_SCHEME_RE.search(spec):
        return True
    if spec.startswith(("-e ", "-e=")):
        return True
    return spec.startswith(("--index-url", "--extra-index-url"))


def check_requirements(entries, *, allowlist: frozenset, path: str) -> list:
    """Apply source, pinning, and name rules to parsed requirement entries."""
    findings = []
    for lineno, name, spec in entries:
        untrusted = _untrusted(spec)
        if untrusted:
            findings.append(Finding(
                path, lineno, "untrusted-source", name,
                "direct URL, VCS, or index source",
            ))
        if "==" not in spec:
            findings.append(Finding(
                path, lineno, "unpinned-dependency", name, "no exact == pin",
            ))
        if name:
            findings.extend(check_name(
                name, allowlist=allowlist, path=path, line=lineno,
            ))
    return findings


def parse_imports(source: str) -> frozenset:
    """Root module names imported by ``source``; a syntax error raises ValueError."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"cannot parse imports: {exc}") from exc
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            if node.module:
                modules.add(node.module.split(".")[0])
    return frozenset(modules)


#: Source roots a project may keep its own code under (``src/`` layout).
_SOURCE_DIRS = ("", "src")


def is_first_party(root_dir: Path, module: str) -> bool:
    """True for this project's own modules (a known root, a file, or a package).

    Both a module file and a package directory count, because a namespace
    package has no ``__init__.py``; and both the checkout root and ``src/`` are
    searched, because a src-layout project's own code is otherwise reported as
    an unknown dependency — the gate would fail every run in such a repo.

    A directory that ships Python IS the project's code, even when its name
    matches a dependency: importing it really does run the local package. The
    gate is a dependency-existence check, not a package-name authority, so this
    is not treated as a violation — a reviewer sees the new directory in the
    diff, which a name rule cannot substitute for.
    """
    root = module.split(".")[0]
    if root in FIRST_PARTY_ROOTS:
        return True
    base = Path(root_dir)
    for prefix in _SOURCE_DIRS:
        candidate = base / prefix if prefix else base
        if (candidate / f"{root}.py").is_file():
            return True
        package = candidate / root
        # A bare directory is not evidence of first-party code by itself: an
        # empty ``requests/`` on ``sys.path`` still shadows the real dependency
        # as a namespace package, which is a way to serve a sitecustomize-style
        # hijack. It has to contain Python to be this project's own module.
        if package.is_dir() and _holds_python(package):
            return True
    return False


def _holds_python(package: Path) -> bool:
    """True when ``package`` contains Python anywhere beneath it.

    Any depth, because a namespace package nests its modules as deep as the
    project likes (``src/acme/services/billing/impl.py``) and a depth limit
    turns the project's own code into an "unknown dependency". The walk stops at
    the first hit, so the cost is one directory listing in the common case.
    """
    try:
        return next(package.rglob("*.py"), None) is not None
    except OSError:
        return False


def _is_stdlib(root: str) -> bool:
    """True when ``root`` is a standard-library module.

    ``sys.stdlib_module_names`` only exists from 3.10; on an older host the
    stdlib / frozen / built-in origin of the module spec is the same fact, so
    the gate does not fall through to "unknown dependency" for ``json``.
    """
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return root in names
    try:
        spec = importlib.util.find_spec(root)
    except (ImportError, AttributeError, TypeError, ValueError):
        return False
    if spec is None:
        return False
    origin = spec.origin or ""
    if origin in ("built-in", "frozen"):
        return True
    if not spec.has_location or not origin:
        return False
    paths = sysconfig.get_paths()
    stdlib_dir = paths.get("stdlib") or ""
    if not stdlib_dir:
        return False
    resolved = Path(origin).resolve()
    try:
        resolved.relative_to(Path(stdlib_dir).resolve())
    except (OSError, ValueError):
        return False
    # ``site-packages`` lives under the stdlib directory, so containment alone
    # calls every installed package standard library — which is the one answer
    # this function must never give for a third-party import.
    for key in ("purelib", "platlib"):
        site_dir = paths.get(key) or ""
        if not site_dir:
            continue
        try:
            resolved.relative_to(Path(site_dir).resolve())
            return False
        except (OSError, ValueError):
            continue
    return True


def check_imports(modules, *, allowlist: frozenset, root_dir: Path,
                  path: str, line: int = 0) -> list:
    """Name-check imported modules, skipping the stdlib and first-party code."""
    findings = []
    for module in sorted(modules):
        root = module.split(".")[0]
        if _is_stdlib(root):
            continue
        if is_first_party(root_dir, root):
            continue
        findings.extend(check_name(
            root, allowlist=allowlist, path=path, line=line,
        ))
    return findings


def new_imports(old: frozenset, new: frozenset) -> list:
    """Imports the change introduces, sorted."""
    return sorted(new - old)


def new_requirements(old, new) -> list:
    """Requirement entries the change introduces, compared without line numbers.

    A requirement's identity is ``(name, spec)``. Line numbers move whenever
    anything above them is edited, so comparing whole entries would report every
    requirement in a touched file as new the moment one line is inserted — and
    a pre-existing ``>=`` range elsewhere in the file would fail the run.
    """
    seen = {(name, spec) for _lineno, name, spec in old}
    return [entry for entry in new if (entry[1], entry[2]) not in seen]


# ── CLI ──────────────────────────────────────────────────────────────────────


def _toml_loads(text: str):
    """Parse TOML with ``tomllib`` (3.11+) or its backport on an older host.

    ``tomllib`` is imported lazily so the module itself still imports on a
    pre-3.11 interpreter; if neither parser is available the gate fails closed
    (``ValueError``) instead of silently treating the file as dependency-free.
    """
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib
        except ModuleNotFoundError as exc:
            raise ValueError("no TOML parser (tomllib or tomli) available") from exc
    return tomllib.loads(text)


def parse_pyproject(text: str) -> list:
    """``(0, name, spec)`` for every declared project requirement in ``text``."""
    data = _toml_loads(text)
    project = data.get("project", {}) or {}
    groups = [project.get("dependencies", []) or []]
    groups.extend((project.get("optional-dependencies", {}) or {}).values())
    entries = []
    for group in groups:
        for item in group or []:
            for _lineno, name, spec in parse_requirements(str(item) + "\n"):
                entries.append((0, name, spec))
    return entries


def _read_pyproject_entries(path: Path) -> list:
    """``(0, name, spec)`` for every declared project requirement."""
    return parse_pyproject(Path(path).read_text(encoding="utf-8"))


def _allowlist_path(root: Path) -> Path:
    override = os.environ.get("MO_DEP_ALLOWLIST", "").strip()
    if override:
        candidate = Path(override)
        return candidate if candidate.is_absolute() else Path(root) / candidate
    return Path(root) / ALLOWLIST_DEFAULT


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _scan_target(path: Path, root: Path, allowlist: frozenset) -> list:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rel = _rel(path, root)
    if REQUIREMENTS_RE.search(rel):
        return check_requirements(
            parse_requirements(text), allowlist=allowlist, path=rel,
        )
    if Path(rel).name == "pyproject.toml":
        try:
            entries = _read_pyproject_entries(path)
        except (ValueError, OSError):
            return []
        return check_requirements(entries, allowlist=allowlist, path=rel)
    if rel.endswith(".py"):
        try:
            modules = parse_imports(text)
        except ValueError:
            return []
        return check_imports(
            modules, allowlist=allowlist, root_dir=Path(root), path=rel,
        )
    return []


def _walk_targets(root: Path) -> list:
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mini-ork"}
    targets = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            if (name.endswith(".py") or REQUIREMENTS_RE.search(name)
                    or name == "pyproject.toml"):
                targets.append(Path(dirpath) / name)
    return sorted(targets)


def _repo_modules(root: Path) -> frozenset:
    """Every module name this repository defines itself.

    ``is_first_party`` answers "is this one of the known application roots".
    The generator needs the wider question, because recipe verifiers, scripts
    and fixtures import their neighbours by bare name (``_stage``, ``calc``,
    ``verifier_replay``) from directories no root list would enumerate.
    """
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mini-ork"}
    names = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for filename in filenames:
            # A package is named by its DIRECTORY, and ``__init__.py`` ends with
            # ``.py``, so the package test has to come first or the repository's
            # own packages are recorded as a module called ``__init__``.
            if filename == "__init__.py":
                names.add(Path(dirpath).name)
            elif filename.endswith(".py"):
                names.add(Path(dirpath, filename).stem)
    return frozenset(names)


def _observed_imports(root: Path) -> set:
    """Normalized third-party roots this tree actually imports."""
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mini-ork"}
    defined = _repo_modules(root)
    names = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = Path(dirpath) / filename
            try:
                modules = parse_imports(
                    path.read_text(encoding="utf-8", errors="replace")
                )
            except (OSError, ValueError):
                continue
            for module in modules:
                top = module.split(".")[0]
                if _is_stdlib(top) or is_first_party(root, top):
                    continue
                if top in defined:
                    continue
                names.add(norm(top))
    return names


def _installed_distributions() -> dict:
    """Import name -> installed distribution names, from package metadata.

    The hand-kept ``IMPORT_ALIASES`` table covers four names and will always be
    incomplete (`import PIL` installs as ``pillow``). Python 3.10+ ships
    ``packages_distributions()``, which reads the metadata that is already on
    disk — so the generator records the name ``pip install`` would take, which
    is the name a reviewer of this file needs to see. Absent metadata (or an
    older interpreter) degrades to the table alone rather than failing.
    """
    try:
        from importlib.metadata import packages_distributions  # type: ignore
    except ImportError:
        return {}
    try:
        raw = packages_distributions()
    except Exception:  # fallback-guard: allow: metadata is an optional refinement
        return {}
    return {
        norm(import_name): {norm(dist) for dist in distributions if dist}
        for import_name, distributions in raw.items()
    }


def _write_allowlist(out_path: Path) -> int:
    # The generator runs from the repository root: a hermetic test writes a
    # pyproject in a tmp cwd, and the real one is invoked at the checkout root.
    root = Path.cwd()
    names = set()

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            entries = parse_pyproject(pyproject.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            entries = []
        for _lineno, name, _spec in entries:
            normalized = norm(name)
            if normalized:
                names.add(normalized)

    # A declared dependency is not necessarily an imported one, and a dev/test
    # dependency is often declared nowhere. The snapshot has to cover both or
    # the gate fails the run that adds `import pytest` to a test file.
    names |= _observed_imports(root)
    installed = _installed_distributions()
    for name in list(names):
        for alias in IMPORT_ALIASES.get(name, ()):
            names.add(norm(alias))
        # The reverse direction too: an import proves the distribution is
        # installed, and a reader of this file should see the name they would
        # write in `pip install`, not only the alias the code uses.
        for distribution, aliases in IMPORT_ALIASES.items():
            if name in {norm(alias) for alias in aliases}:
                names.add(norm(distribution))
        names.update(installed.get(name, ()))

    # Never drop an entry a human added by hand: the snapshot is also where a
    # dependency is declared *before* it is used, and a regeneration that
    # silently forgot those would re-open the hole it was added to close.
    try:
        names |= load_allowlist(out_path)
    except OSError:
        pass

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "\n".join([*ALLOWLIST_HEADER, *sorted(names)]) + "\n", encoding="utf-8",
    )
    print(f"{len(names)} names written")
    return 0


def _scan_cli(root_arg: str, paths: list) -> int:
    root = Path(root_arg)
    allow_path = _allowlist_path(root)
    try:
        allowlist = load_allowlist(allow_path)
    except OSError as exc:
        print(json.dumps({
            "findings": [],
            "count": 0,
            "error": f"allowlist unavailable at {allow_path}: {exc}",
        }, separators=(",", ":"), ensure_ascii=False))
        return 3
    targets = [Path(p) for p in paths] if paths else _walk_targets(root)
    findings = []
    for target in targets:
        findings.extend(_scan_target(target, root, allowlist))
    print(json.dumps({
        "findings": [finding.as_dict() for finding in findings],
        "count": len(findings),
    }, separators=(",", ":"), ensure_ascii=False))
    return 1 if findings else 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--write-allowlist" in args:
        index = args.index("--write-allowlist")
        if index + 1 >= len(args):
            print("--write-allowlist requires a path", file=sys.stderr)
            return 2
        try:
            return _write_allowlist(Path(args[index + 1]))
        except (OSError, ValueError) as exc:
            print(f"--write-allowlist failed: {exc}", file=sys.stderr)
            return 3

    root = "."
    paths = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--root" and i + 1 < len(args):
            root = args[i + 1]
            i += 2
        elif arg == "--path" and i + 1 < len(args):
            paths.append(args[i + 1])
            i += 2
        else:
            i += 1
    return _scan_cli(root, paths)


if __name__ == "__main__":
    raise SystemExit(main())
