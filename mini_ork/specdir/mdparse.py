"""Minimal line-oriented markdown structure for spec files (no rendering).

Spec ingestion only needs the document's skeleton: ATX headings, fenced code
blocks, and the prose lines between them. A full CommonMark parser would add a
dependency for no gain, so this module recognises exactly three things:

* a leading YAML front-matter block (``---`` … ``---``/``...``), excluded from
  prose so a ``# comment`` inside it is never taken for a heading;
* fenced code blocks (``` or ~~~, 3+ chars, closed by the same char at least as
  long; an unclosed fence runs to EOF, as in CommonMark);
* ATX headings (``#``–``######``) outside fences. Setext headings are not
  recognised — spec authors use ATX.

Line numbers are 1-based. Everything is pure and deterministic.

    parse(text) -> Document
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_INLINE_CODE_RE = re.compile(r"(`+)(.+?)\1")


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    line: int


@dataclass(frozen=True)
class Fence:
    lang: str
    body: str
    start: int
    end: int


@dataclass(frozen=True)
class Document:
    headings: tuple[Heading, ...] = ()
    fences: tuple[Fence, ...] = ()
    # (line number, text) for every line outside front matter and fences,
    # headings included.
    prose: tuple[tuple[int, str], ...] = ()

    def first_h1(self) -> str | None:
        for h in self.headings:
            if h.level == 1 and h.text:
                return h.text
        return None

    def section_lines(self, heading: Heading) -> list[tuple[int, str]]:
        """Prose lines under ``heading`` up to the next heading of the same or
        a higher level (sub-headings stay inside the section)."""
        end = None
        for h in self.headings:
            if h.line > heading.line and h.level <= heading.level:
                end = h.line
                break
        heading_lines = {h.line for h in self.headings}
        return [(n, t) for n, t in self.prose
                if n > heading.line and (end is None or n < end) and n not in heading_lines]

    def bullet_lines(self) -> list[tuple[int, str]]:
        return [(n, t) for n, t in self.prose if _BULLET_RE.match(t)]


def inline_code_spans(line: str) -> list[str]:
    """Contents of the backtick code spans on one prose line."""
    return [m.group(2).strip() for m in _INLINE_CODE_RE.finditer(line)]


def strip_inline_code(line: str) -> str:
    return _INLINE_CODE_RE.sub(" ", line)


def _front_matter_end(lines: list[str]) -> int:
    """Index of the first line after a leading YAML front-matter block, or 0."""
    if not lines or lines[0].rstrip() != "---":
        return 0
    for i in range(1, len(lines)):
        if lines[i].rstrip() in ("---", "..."):
            return i + 1
    return 0  # unterminated: not front matter, a thematic break


def parse(text: str) -> Document:
    lines = text.splitlines()
    headings: list[Heading] = []
    fences: list[Fence] = []
    prose: list[tuple[int, str]] = []
    i = _front_matter_end(lines)
    while i < len(lines):
        raw = lines[i]
        lineno = i + 1
        m = _FENCE_OPEN_RE.match(raw)
        # A backtick fence's info string may not itself contain a backtick.
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            marker = m.group(1)
            info = m.group(2).strip()
            lang = info.split()[0].lower() if info else ""
            close_re = re.compile(r"^ {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*$")
            body: list[str] = []
            j = i + 1
            while j < len(lines) and not close_re.match(lines[j]):
                body.append(lines[j])
                j += 1
            end = min(j, len(lines) - 1) + 1  # closing-fence line, or EOF when unclosed
            fences.append(Fence(lang=lang, body="\n".join(body), start=lineno, end=end))
            i = j + 1
            continue
        h = _HEADING_RE.match(raw)
        if h:
            headings.append(Heading(level=len(h.group(1)), text=(h.group(2) or "").strip(), line=lineno))
        prose.append((lineno, raw))
        i += 1
    return Document(headings=tuple(headings), fences=tuple(fences), prose=tuple(prose))
