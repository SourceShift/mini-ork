"""Spec-directory scanner — the deterministic inventory step of SDD.

Walks one directory for spec markdown files and emits a :class:`SpecEntry` per
file. The glob comes from ``MO_SDD_SPEC_GLOB`` (default ``*.md``, read at call
time); the scan is non-recursive unless ``recursive=True``. Hidden files and
directories, and README/index stems (case-insensitive), are skipped. Results
are sorted by path relative to the root so two scans of the same tree are
identical regardless of filesystem order.

Per entry: ``source_path`` is absolute (resolved), ``source_hash`` is
``sha256:<hex>`` over the raw bytes, ``title`` is the first H1 outside fenced
code (else the filename stem), and ``spec_id`` is the slugified stem.
``depends_on`` collects ``Depends on: <spec-id>[, ...]`` lines. Colliding
``spec_id`` values are NOT resolved here; :mod:`mini_ork.specdir.index` and
:mod:`mini_ork.specdir.lint` report them as ``DUP_ID``.

    scan_specdir(root, *, glob=None, recursive=False) -> list[SpecEntry]
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from mini_ork.specdir import mdparse

DEFAULT_GLOB = "*.md"
_SKIP_STEMS = frozenset({"readme", "index"})
_DEPENDS_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?depends on\s*(?::\s*(?:\*\*|__)?|(?:\*\*|__)\s*:)\s*(.*)$", re.I)
_NO_DEPS = frozenset({"", "none", "n/a", "-", "nothing"})


@dataclass(frozen=True)
class SpecEntry:
    spec_id: str
    source_path: str
    source_hash: str
    title: str
    size_bytes: int
    depends_on: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "spec_id": self.spec_id,
            "source_path": self.source_path,
            "source_hash": self.source_hash,
            "title": self.title,
            "size_bytes": self.size_bytes,
            "depends_on": list(self.depends_on),
        }


def _slugify(text: str) -> str:
    """Same algorithm as ``mini_ork.cli.epics._slugify`` (kept local: library
    code does not import CLI modules); fallback id is ``spec``."""
    s = re.sub(r"[^a-z0-9-]+", "-", text.lower().strip()).strip("-")
    s = re.sub(r"-+", "-", s)
    return s or "spec"


def source_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def spec_glob() -> str:
    return os.environ.get("MO_SDD_SPEC_GLOB", "").strip() or DEFAULT_GLOB


def parse_depends_on(doc: mdparse.Document) -> tuple[str, ...]:
    deps: set[str] = set()
    for _, line in doc.prose:
        m = _DEPENDS_RE.match(line)
        if not m:
            continue
        for token in m.group(1).split(","):
            token = token.strip().strip("`*_").strip()
            if token.lower() in _NO_DEPS:
                continue
            if token.lower().endswith(".md"):
                token = token[:-3]
            deps.add(_slugify(token))
    return tuple(sorted(deps))


def _skipped(rel: Path) -> bool:
    if any(part.startswith(".") for part in rel.parts):
        return True
    return rel.stem.lower() in _SKIP_STEMS


def read_entry(path: Path) -> tuple[SpecEntry, str]:
    """Build the entry for one file; also return the decoded text for callers
    (lint) that need it without a second read."""
    data = path.read_bytes()
    text = data.decode("utf-8", errors="replace")
    doc = mdparse.parse(text)
    entry = SpecEntry(
        spec_id=_slugify(path.stem),
        source_path=str(path.resolve()),
        source_hash=source_hash(data),
        title=doc.first_h1() or path.stem,
        size_bytes=len(data),
        depends_on=parse_depends_on(doc),
    )
    return entry, text


def iter_spec_files(root: str | os.PathLike[str], *, glob: str | None = None,
                    recursive: bool = False) -> list[Path]:
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise NotADirectoryError(f"spec dir not found: {base}")
    pattern = glob or spec_glob()
    candidates = base.rglob(pattern) if recursive else base.glob(pattern)
    files = []
    for path in candidates:
        rel = path.relative_to(base)
        if path.is_file() and not _skipped(rel):
            files.append(path)
    return sorted(files, key=lambda p: p.relative_to(base).as_posix())


def scan_specdir(root: str | os.PathLike[str], *, glob: str | None = None,
                 recursive: bool = False) -> list[SpecEntry]:
    return [read_entry(p)[0] for p in iter_spec_files(root, glob=glob, recursive=recursive)]
