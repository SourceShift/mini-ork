"""Deterministic spec lint — the checkable subset of the 7 authoring errors.

Per-spec codes (from :func:`lint_text`):

* ``NO_ACCEPTANCE`` (error) — no heading like "Acceptance criteria",
  "Definition of Done", "Success criteria", or "Done when".
* ``NO_VERIFY_CMD`` (warning) — no non-empty shell code fence (``bash``,
  ``sh``, ``shell``, ``console``, ``zsh``, or no language) and no inline code
  span that looks like a command (contains whitespace, or starts with ``./``
  or ``bin/``).
* ``VAGUE_CRITERIA`` (warning) — a vague term (``MO_SDD_VAGUE_TERMS``,
  comma-separated, else the defaults below) on a line of the acceptance
  section(s), or on any bullet line when there is no such section. Matching is
  case-insensitive on word boundaries and ignores text inside code spans.
* ``OVERSIZE`` (warning) — file larger than ``MO_SDD_SPEC_MAX_BYTES``
  (default 262144; a non-positive or unparsable value falls back to it).
* ``MISSING_SECTIONS`` (warning) — no Inputs/Outputs/Errors/Edge-cases/
  Examples-style heading.

Cross-spec codes (from :func:`lint_specdir`): ``DUP_ID`` (error, one finding
per colliding file) and ``DEP_CYCLE`` (error, one finding per spec on the
cycle; the message carries the ``a -> b -> a`` path).

Headings and lines inside fenced code blocks never count. Env knobs are read
at call time. Output is sorted by ``(spec_id, code, message)``.

    lint_text(spec_id, text, size_bytes=None, *, vague_terms=None, max_bytes=None)
    lint_specdir(root, *, recursive=False, glob=None, ...) -> list[Finding]
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from mini_ork.specdir import mdparse
from mini_ork.specdir.index import find_cycles, find_duplicate_ids, format_cycle, spec_graph
from mini_ork.specdir.scan import SpecEntry, iter_spec_files, read_entry

ERROR = "error"
WARNING = "warning"
SEVERITY = {
    "DEP_CYCLE": ERROR,
    "DUP_ID": ERROR,
    "NO_ACCEPTANCE": ERROR,
    "NO_VERIFY_CMD": WARNING,
    "VAGUE_CRITERIA": WARNING,
    "OVERSIZE": WARNING,
    "MISSING_SECTIONS": WARNING,
}
CODES = tuple(SEVERITY)
DEFAULT_VAGUE_TERMS = ("should work", "properly", "correctly", "as expected", "robust", "seamless")
DEFAULT_MAX_BYTES = 262144

_ACCEPTANCE_RE = re.compile(r"acceptance|definition of done|success criteria|done when", re.I)
_SECTIONS_RE = re.compile(
    r"\b(?:inputs?|outputs?|errors?|error handling|edge[- ]cases?|examples?)\b", re.I)
_SHELL_LANGS = frozenset({"", "bash", "sh", "shell", "console", "zsh"})
_WS_RE = re.compile(r"\s")


@dataclass(frozen=True)
class Finding:
    spec_id: str
    code: str
    severity: str
    message: str

    def to_dict(self) -> dict:
        return {"spec_id": self.spec_id, "code": self.code, "severity": self.severity,
                "message": self.message}


def _finding(spec_id: str, code: str, message: str) -> Finding:
    return Finding(spec_id=spec_id, code=code, severity=SEVERITY[code], message=message)


def _sorted(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (f.spec_id, f.code, f.message))


def vague_terms_from_env() -> tuple[str, ...]:
    raw = os.environ.get("MO_SDD_VAGUE_TERMS", "")
    terms = tuple(dict.fromkeys(t.strip().lower() for t in raw.split(",") if t.strip()))
    return terms or DEFAULT_VAGUE_TERMS


def max_bytes_from_env() -> int:
    try:
        value = int(os.environ.get("MO_SDD_SPEC_MAX_BYTES", "").strip())
    except ValueError:
        return DEFAULT_MAX_BYTES
    return value if value > 0 else DEFAULT_MAX_BYTES


def has_errors(findings: Iterable[Finding]) -> bool:
    return any(f.severity == ERROR for f in findings)


def _term_re(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, term.split())) + r"(?!\w)", re.I)


def _has_verify_cmd(doc: mdparse.Document) -> bool:
    if any(f.lang in _SHELL_LANGS and f.body.strip() for f in doc.fences):
        return True
    for _, line in doc.prose:
        for span in mdparse.inline_code_spans(line):
            if _WS_RE.search(span) or span.startswith(("./", "bin/")):
                return True
    return False


def lint_text(spec_id: str, text: str, size_bytes: int | None = None, *,
              vague_terms: Sequence[str] | None = None, max_bytes: int | None = None) -> list[Finding]:
    """Per-spec findings for one spec's markdown ``text``."""
    doc = mdparse.parse(text)
    size = len(text.encode("utf-8")) if size_bytes is None else size_bytes
    limit = max_bytes if max_bytes and max_bytes > 0 else max_bytes_from_env()
    terms = tuple(vague_terms) if vague_terms is not None else vague_terms_from_env()
    out: list[Finding] = []

    acceptance = [h for h in doc.headings if _ACCEPTANCE_RE.search(h.text)]
    if not acceptance:
        out.append(_finding(spec_id, "NO_ACCEPTANCE",
                            "no acceptance-criteria section (expected a heading like 'Acceptance "
                            "criteria', 'Definition of Done', 'Success criteria' or 'Done when')"))
    if not _has_verify_cmd(doc):
        out.append(_finding(spec_id, "NO_VERIFY_CMD",
                            "no backticked verify command (a shell code fence or an inline "
                            "`command args` span)"))

    if acceptance:
        scope, note = "acceptance criteria", ""
        lines = dict(line for h in acceptance for line in doc.section_lines(h))
    else:
        scope, note = "bullet text", "; no acceptance section"
        lines = dict(doc.bullet_lines())
    for term in terms:
        rgx = _term_re(term)
        hits = sorted(n for n, line in lines.items() if rgx.search(mdparse.strip_inline_code(line)))
        if hits:
            out.append(_finding(spec_id, "VAGUE_CRITERIA",
                                f"vague term '{term}' in {scope} (line {', '.join(map(str, hits))}{note})"))

    if size > limit:
        out.append(_finding(spec_id, "OVERSIZE", f"spec is {size} bytes, over the {limit}-byte limit "
                                                 "(MO_SDD_SPEC_MAX_BYTES)"))
    if not any(_SECTIONS_RE.search(h.text) for h in doc.headings):
        out.append(_finding(spec_id, "MISSING_SECTIONS",
                            "no Inputs/Outputs/Errors/Edge-cases/Examples-style heading"))
    return _sorted(out)


def cross_spec_findings(entries: Sequence[SpecEntry]) -> list[Finding]:
    """``DUP_ID`` and ``DEP_CYCLE`` across one scanned directory."""
    out: list[Finding] = []
    for sid, group in find_duplicate_ids(entries).items():
        for e in group:
            others = ", ".join(o.source_path for o in group if o is not e)
            out.append(_finding(sid, "DUP_ID",
                                f"spec_id '{sid}' from {e.source_path} collides with {others}"))
    for cycle in find_cycles(spec_graph(entries)):
        for sid in sorted(set(cycle)):
            out.append(_finding(sid, "DEP_CYCLE", f"dependency cycle {format_cycle(cycle)}"))
    return _sorted(out)


def scan_and_lint(root: str | os.PathLike[str], *, recursive: bool = False, glob: str | None = None,
                  vague_terms: Sequence[str] | None = None,
                  max_bytes: int | None = None) -> tuple[list[SpecEntry], list[Finding]]:
    """One pass over the directory: the inventory and every finding."""
    entries: list[SpecEntry] = []
    findings: list[Finding] = []
    for path in iter_spec_files(root, glob=glob, recursive=recursive):
        entry, text = read_entry(path)
        entries.append(entry)
        findings.extend(lint_text(entry.spec_id, text, entry.size_bytes,
                                  vague_terms=vague_terms, max_bytes=max_bytes))
    findings.extend(cross_spec_findings(entries))
    return entries, _sorted(findings)


def lint_specdir(root: str | os.PathLike[str], *, recursive: bool = False, glob: str | None = None,
                 vague_terms: Sequence[str] | None = None, max_bytes: int | None = None) -> list[Finding]:
    return scan_and_lint(root, recursive=recursive, glob=glob, vague_terms=vague_terms,
                         max_bytes=max_bytes)[1]
