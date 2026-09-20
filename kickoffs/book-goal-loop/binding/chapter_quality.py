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

Most of the floor is a LOWER BOUND over the committed text, which by construction
cannot see content LOSS: a chapter that kept every word and lost every figure
trips nothing. So one term is relational instead — the figure ledger, comparing
the ``viz_image`` blocks the chapter was given against the ones still live. The
two halves disagree when a cascade soft-delete (``is_deleted`` set, ``deleted_at``
still NULL — the parent-driven trigger walking ``parent_uuid``) strips a
chapter's figures while its prose and its judge flag are untouched.

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
#
# The unresolved-template term carries a negative lookbehind for `$`. A chapter
# documenting CI legitimately quotes GitHub Actions expressions such as
# ``${{ secrets.PACT_BROKER_URL }}``; that is content, not generator residue, and
# a bare ``{{ ... }}`` still trips. Without this a chapter can never clear the
# floor on text it is correct to have written.
_PLACEHOLDERS: tuple[tuple[str, str], ...] = (
    ("todo", r"\bTODO\b"),
    ("tbd", r"\bTBD\b"),
    ("fixme", r"\bFIXME\b"),
    ("xxx", r"\bXXX+\b"),
    ("lorem-ipsum", r"lorem ipsum"),
    ("fill-me", r"\[(insert|fill|add|todo|tbd)\b"),
    ("unresolved-template", r"(?<!\$)\{\{[^}]{1,60}\}\}"),
    ("placeholder", r"\bplaceholder\b"),
    ("as-an-ai", r"as an AI language model"),
    ("empty-heading", r"^#{1,6}\s*\.\.\.\s*$"),
)
_HEADING = re.compile(r"^#{1,6}\s+\S", re.M)

_LATEST = "book_uuid='{book}' AND chapter_number={chapter} AND is_latest"

# Figures live in ``blocks`` (``node_type='viz_image'`` bound to the chapter via
# ``source_type='book_chapter'`` + ``source_id=<chapter_uuid>``), NOT in the
# section markdown this script otherwise reads. So every floor above is blind to
# a chapter that kept its prose and lost its figures.
_FIGURES = (
    "WITH ch AS ("
    "  SELECT chapter_uuid FROM book_chapter_lifecycle"
    "   WHERE book_uuid='{book}' AND chapter_number={chapter} LIMIT 1"
    ") SELECT"
    "  (SELECT count(*) FROM blocks b WHERE b.node_type='viz_image'"
    "     AND b.source_type='book_chapter' AND b.source_id=ch.chapter_uuid::text),"
    "  (SELECT count(*) FROM blocks b WHERE b.node_type='viz_image'"
    "     AND b.source_type='book_chapter' AND b.source_id=ch.chapter_uuid::text"
    "     AND b.is_deleted IS NOT TRUE AND b.deleted_at IS NULL),"
    "  (SELECT count(*) FROM blocks b WHERE b.node_type='viz_image'"
    "     AND b.source_type='book_chapter' AND b.source_id=ch.chapter_uuid::text"
    "     AND b.is_deleted IS TRUE AND b.deleted_at IS NULL),"
    "  (SELECT count(*) FROM bg_source_figure_attempt a"
    "     WHERE a.chapter_uuid=ch.chapter_uuid AND a.figure_block_uuid IS NOT NULL)"
    " FROM ch;"
)


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


def _figures(book: str, chapter: str) -> dict[str, int] | None:
    """Figure liveness for one chapter, from the two tables that disagree.

    ``attached`` = viz_image blocks the generation path bound to this chapter;
    ``live`` = how many are still reader-visible; ``cascade`` = the subset
    soft-deleted with a NULL ``deleted_at``. That last signature is the whole
    point: a parent-driven cascade fires a row-level trigger that walks
    ``parent_uuid``, so a statement-level ``node_type <> 'viz_image'`` carve-out
    does not stop it, and every figure a chapter owns can disappear while the
    chapter still reads committed + rubric-pass. ``attempts`` is the figure
    pipeline's own attachment count, carried so a reason can distinguish
    "harvested then wiped" from "never had figures".

    One row, four small columns — the same single-line ``split('|')`` parse
    discipline as ``_meta``. ``None`` on a probe error, never conflated with
    "no figures".
    """
    proc = _q(_FIGURES.format(book=book, chapter=chapter))
    if proc.returncode != 0:
        return None
    line = proc.stdout.strip()
    if not line:
        # No lifecycle row for this chapter — nothing was ever given figures.
        # Not a probe error: a chapter that cannot be located already fails
        # `no-sections` above, and conflating the two would make this term
        # fail-closed on a chapter no term can describe.
        return {"attached": 0, "live": 0, "cascade": 0, "attempts": 0}
    parts = line.split("|")
    if len(parts) < 4:
        return None
    try:
        attached, live, cascade, attempts = (int(p) for p in parts[:4])
    except ValueError:
        return None
    return {"attached": attached, "live": live, "cascade": cascade, "attempts": attempts}


def _blob(book: str, chapter: str) -> str:
    """The concatenated committed markdown. One column, one row — newlines in
    the payload are harmless because the whole stdout is the value."""
    proc = _q(
        "SELECT coalesce(string_agg(content_markdown, E'\\n' ORDER BY section_index),'') "
        "FROM book_chapter_sections "
        f"WHERE {_LATEST.format(book=book, chapter=chapter)};"
    )
    return proc.stdout if proc.returncode == 0 else ""


_UNPROBED = object()


def _failures(
    rows: list[tuple[int, str, int, str, int]], blob: str,
    figures: object = _UNPROBED,
) -> tuple[list[str], dict[str, object]]:
    """The floor's verdict. Returns ``(failures, facts)``; empty failures == pass.

    ``figures`` is ``_UNPROBED`` when the caller did not ask (the term is then
    inert, so the pure verdict logic stays independently testable), a dict from
    ``_figures``, or ``None`` for a probe error — which fails closed.
    """
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
        "figures": figures,
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

    # Figure ledger. The relational term the text floors cannot express: every
    # figure the chapter was GIVEN must still be live. A chapter whose figure
    # count dropped is not the chapter the judge passed, however good its prose.
    # `MO_GOAL_QUALITY_FIGURES=warn` records the counts without failing, for the
    # case where a drop is a deliberate regeneration rather than a cascade loss.
    figures_enforcing = (
        os.environ.get("MO_GOAL_QUALITY_FIGURES", "enforce").strip().lower() != "warn"
    )
    if figures is _UNPROBED:
        facts["figures"] = None
    elif figures is None:
        bad.append("figure-probe-error")
    elif isinstance(figures, dict) and figures["attached"] and figures["live"] < figures["attached"]:
        detail = (
            f"figure-loss attached={figures['attached']} live={figures['live']}"
            f" cascade={figures['cascade']} attempts={figures['attempts']}"
        )
        if figures_enforcing:
            bad.append(detail)
        else:
            facts["figure_warn"] = detail
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

    figures = _figures(book, chapter)
    bad, facts = _failures(rows, _blob(book, chapter), figures)
    summary = (
        f"sections={facts['sections']} total={facts['total_chars']} "
        f"min_section={facts['min_section_chars']} "
        f"headings={facts['headings']} doc_version={facts['doc_version']}"
    )
    if figures:
        summary += f" figures={figures['live']}/{figures['attached']}"
    if facts.get("figure_warn"):
        summary += f" [{facts['figure_warn']}]"
    if bad:
        print(f"ch{chapter} QUALITY-FAIL {summary} :: {'; '.join(bad)}")
        return 1
    print(f"ch{chapter} QUALITY-OK {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
