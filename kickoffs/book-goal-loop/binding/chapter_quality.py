#!/usr/bin/env python3
"""objective quality anchor for the goal-loop: is chapter <n> structurally real?

Contract (mirrors ``chapter_predicate.py``): invoked as
``python3 chapter_quality.py <chapter_number>`` (argv, not shell) inside
MO_GOAL_TARGET_CWD; exit 0 == the chapter clears the objective floor; any
non-zero == it does not. The FIRST stdout line is the reason.

WHY THIS EXISTS
The goal predicate's quality half is ``rubric_status='pass'`` — the researcher's
OWN G-Eval judge flag. That is a single, self-reported, LLM-only signal, and the
code that writes it lives inside the fix child's editable tree. So the loop can
certify "highest quality" purely on a label, with nothing outside the judged
system contradicting it. This script is that outside signal: a deterministic
read of the bytes the commit path actually persisted (``book_chapter_sections``
where ``is_latest``), checked against structural floors that no judge call and
no re-roll can talk their way past.

It is a VACUITY FLOOR, not a quality judge. It cannot tell a brilliant chapter
from a competent one — only a REAL chapter from a hollow one (empty sections,
unresolved template markers, duplicated H2s, a commit whose parts disagree on
doc_version, sections the commit path never hashed). That is the honest ceiling
of a gold-free signal, and it is exactly the gap a self-reported judge flag
leaves: the judge says "pass", this says "there is something there".

A committed chapter with NO section rows fails by design: a commit whose parts
cannot be located cannot be verified as non-vacuous. Operators who would rather
observe than enforce set ``MO_GOAL_QUALITY_MODE=warn`` on the predicate side.

Connection comes from libpq env vars; no secret lives here.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

# Placeholder / vacuity markers. Calibrated against the one known-good committed
# chapter (book d0df3cdb ch1, judge-passed, 23,090 chars): zero hits across all
# patterns, so a real chapter does not trip them.
_PLACEHOLDERS: tuple[tuple[str, str], ...] = (
    ("todo", r"\bTODO\b"),
    ("tbd", r"\bTBD\b"),
    ("fixme", r"\bFIXME\b"),
    ("xxx", r"\bXXX+\b"),
    ("lorem-ipsum", r"lorem ipsum"),
    ("fill-me", r"\[(insert|fill|add|todo|tbd)\b"),
    ("unresolved-template", r"\{\{[^}]{1,60}\}\}"),
    ("placeholder", r"\bplaceholder\b"),
    ("as-an-ai", r"as an AI language model"),
    ("empty-heading", r"^#{1,6}\s*\.\.\.\s*$"),
)
_HEADING = re.compile(r"^#{1,6}\s+\S", re.M)

_LATEST = "book_uuid='{book}' AND chapter_number={chapter} AND is_latest"


def _q(sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "psql",
            "-h", os.environ.get("PGHOST", "REDACTED-INTERNAL-IP"),
            "-p", os.environ.get("PGPORT", "5932"),
            "-U", os.environ.get("PGUSER", "researcher_user"),
            "-d", os.environ.get("PGDATABASE", "researcher_db"),
            "-tA", "-F", "|", "-c", sql,
        ],
        capture_output=True,
        text=True,
    )


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except ValueError:
        return default


def _meta(book: str, chapter: str) -> list[tuple[int, str, int, str, int]] | None:
    """``[(section_index, h2_slug, length, sha256, doc_version)]``, in position
    order. ``None`` on a DB/probe error — never conflated with "no sections".

    Five small columns and no markdown: each row is exactly one line, so the
    parse is a plain ``split('|')`` with no body-rejoining heuristic to get
    wrong. The text itself is fetched separately by ``_blob``.
    """
    proc = _q(
        "SELECT section_index, coalesce(h2_slug,''), "
        "coalesce(content_length, length(content_markdown), 0), "
        "coalesce(content_sha256,''), coalesce(doc_version,0) "
        "FROM book_chapter_sections "
        f"WHERE {_LATEST.format(book=book, chapter=chapter)} "
        "ORDER BY section_index;"
    )
    if proc.returncode != 0:
        return None
    out: list[tuple[int, str, int, str, int]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 5:
            continue
        try:
            out.append((int(parts[0]), parts[1], int(parts[2]), parts[3], int(parts[4])))
        except ValueError:
            continue
    return out


def _blob(book: str, chapter: str) -> str:
    """The concatenated committed markdown. One column, one row — newlines in
    the payload are harmless because the whole stdout is the value."""
    proc = _q(
        "SELECT coalesce(string_agg(content_markdown, E'\\n' ORDER BY section_index),'') "
        "FROM book_chapter_sections "
        f"WHERE {_LATEST.format(book=book, chapter=chapter)};"
    )
    return proc.stdout if proc.returncode == 0 else ""


def _failures(
    rows: list[tuple[int, str, int, str, int]], blob: str,
) -> tuple[list[str], dict[str, object]]:
    """The floor's verdict. Returns ``(failures, facts)``; empty failures == pass."""
    min_sections = _int_env("MO_GOAL_QUALITY_MIN_SECTIONS", 3)
    min_section_chars = _int_env("MO_GOAL_QUALITY_MIN_SECTION_CHARS", 400)
    min_total_chars = _int_env("MO_GOAL_QUALITY_MIN_TOTAL_CHARS", 4000)

    lengths = [r[2] for r in rows]
    total = sum(lengths)
    doc_versions = sorted({r[4] for r in rows})
    facts: dict[str, object] = {
        "sections": len(rows),
        "total_chars": total,
        "min_section_chars": min(lengths) if lengths else 0,
        "headings": len(_HEADING.findall(blob)),
        "doc_version": doc_versions[0] if len(doc_versions) == 1 else doc_versions,
    }

    if not rows:
        return ["no-sections"], facts

    bad: list[str] = []
    if len(rows) < min_sections:
        bad.append(f"sections={len(rows)}<{min_sections}")
    if total < min_total_chars:
        bad.append(f"total={total}<{min_total_chars}")
    short = [(r[0], r[2]) for r in rows if r[2] < min_section_chars]
    if short:
        bad.append("short-section " + ",".join(f"{i}:{n}" for i, n in short[:5]))
    unhashed = [r[0] for r in rows if not r[3].strip()]
    if unhashed:
        bad.append("unhashed-section " + ",".join(str(i) for i in unhashed[:5]))
    if len(doc_versions) != 1:
        bad.append("mixed-doc-version " + ",".join(str(v) for v in doc_versions[:5]))
    slugs = [r[1] for r in rows if r[1]]
    dupes = sorted({s for s in slugs if slugs.count(s) > 1})
    if dupes:
        bad.append("dup-h2 " + ",".join(dupes[:3]))

    for name, pattern in _PLACEHOLDERS:
        hits = len(re.findall(pattern, blob, flags=re.I | re.M))
        if hits:
            bad.append(f"placeholder:{name}x{hits}")
    return bad, facts


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: chapter_quality.py <chapter_number>", file=sys.stderr)
        return 2
    chapter = argv[0].strip()
    if not re.fullmatch(r"\d+", chapter):
        print(f"bad chapter id: {chapter!r}", file=sys.stderr)
        return 2
    book = os.environ.get("BOOK_UUID", "").strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", book):
        print(f"BOOK_UUID unset/invalid: {book!r}", file=sys.stderr)
        return 2

    rows = _meta(book, chapter)
    if rows is None:
        print(f"ch{chapter} QUALITY-FAIL db-error")
        return 3

    bad, facts = _failures(rows, _blob(book, chapter))
    summary = (
        f"sections={facts['sections']} total={facts['total_chars']} "
        f"min_section={facts['min_section_chars']} "
        f"headings={facts['headings']} doc_version={facts['doc_version']}"
    )
    if bad:
        print(f"ch{chapter} QUALITY-FAIL {summary} :: {'; '.join(bad)}")
        return 1
    print(f"ch{chapter} QUALITY-OK {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
