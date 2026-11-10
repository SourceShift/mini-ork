"""The audit's totals table must equal what its group tables actually contain.

`docs/audits/2026-11-04-task-status-audit/SUMMARY.md` quotes per-group verdict
tallies and a grand total. Both drifted: the table claimed G2 held 101 rows /
74 DONE when the file held 97 / 70, and the paragraph explaining the four-row
gap ("rows with a struck-through id and an empty status column") described rows
that did not exist in any of the eight files. The sum was internally consistent
(571 + 105 + 61 + 118 = 855), which is exactly why nobody noticed — a table can
add up and still count rows that are not there.

So the totals are re-derived here, from the tables themselves, whenever the
suite runs. A session that edits a group file and forgets the summary gets a
red test naming the group and the column.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AUDIT = REPO / "docs" / "audits" / "2026-11-04-task-status-audit"
SUMMARY = AUDIT / "SUMMARY.md"

VERDICTS = ("DONE", "OPEN", "STALE", "UNKNOWN")
COLUMNS = ("rows", "DONE", "OPEN", "STALE", "UNKNOWN")

_VERDICT_RE = re.compile(r"[*~`]*\**\s*(DONE|OPEN|STALE|UNKNOWN)")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and set("".join(cells)) <= set("-: ")


def _group_counts(path: Path) -> tuple[int, Counter[str]]:
    """Row count and verdict tally of every `task_id … verdict` table in the file."""
    rows: list[list[str]] = []
    in_table = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            # A blank line is not the end of a table: C-runs-mo.md separates two
            # blocks of the same verdict table with one, and the second block
            # repeats no header. Prose or a heading is what ends a table.
            continue
        if not line.startswith("|"):
            in_table = False
            continue
        cells = _cells(line)
        if not in_table:
            # A verdict table starts at a header whose first cell is `task_id`
            # and that names a `verdict` column; anything else in the file
            # (an evidence list, an open-items table) is not a verdict row.
            if cells and cells[0].lower() == "task_id" and "verdict" in cells:
                in_table = True
            continue
        if _is_separator(cells) or len(cells) < 4:
            continue
        rows.append(cells)

    tally: Counter[str] = Counter()
    for cells in rows:
        match = _VERDICT_RE.match(cells[3])
        assert match is not None, (
            f"{path.name}: row {cells[0]!r} has verdict {cells[3]!r}, "
            f"which is not one of {VERDICTS}"
        )
        tally[match.group(1)] += 1
    return len(rows), tally


def _summary_table() -> tuple[dict[str, list[int]], list[int]]:
    """The Result table as `{group prefix: [rows, DONE, OPEN, STALE, UNKNOWN]}`."""
    groups: dict[str, list[int]] = {}
    total: list[int] = []
    in_table = False
    for line in SUMMARY.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if not line.startswith("|"):
            in_table = False
            continue
        cells = _cells(line)
        if not in_table:
            if cells and cells[0].lower() == "group":
                in_table = True
            continue
        if cells[0].lower() == "group":
            continue
        if _is_separator(cells) or len(cells) < 6:
            continue
        numbers = [int(re.sub(r"[^0-9]", "", c) or "0") for c in cells[1:6]]
        label = cells[0]
        if label.strip("*").lower() == "total":
            total = numbers
            continue
        prefix = label.split(" ", 1)[0].strip("*")
        assert prefix not in groups, f"SUMMARY lists group {prefix} twice"
        groups[prefix] = numbers
    assert groups, "no group rows found in SUMMARY.md's Result table"
    assert total, "no total row found in SUMMARY.md's Result table"
    return groups, total


def _group_files() -> dict[str, Path]:
    """`{prefix: path}` for every `<prefix>-*.md` group report beside SUMMARY.md."""
    files = {}
    for path in sorted(AUDIT.glob("*-*.md")):
        if path == SUMMARY:
            continue
        files[path.name.split("-", 1)[0]] = path
    return files


def test_the_summary_total_row_is_the_sum_of_its_group_rows() -> None:
    groups, total = _summary_table()
    summed = [sum(row[i] for row in groups.values()) for i in range(5)]
    assert total == summed, (
        f"SUMMARY's total row is {dict(zip(COLUMNS, total))} but its group rows "
        f"sum to {dict(zip(COLUMNS, summed))}"
    )


def test_every_group_row_matches_its_report() -> None:
    groups = _summary_table()[0]
    files = _group_files()
    assert set(groups) == set(files), (
        f"SUMMARY lists {sorted(groups)} but the directory holds {sorted(files)}"
    )
    for prefix in sorted(groups):
        rows, tally = _group_counts(files[prefix])
        actual = [rows] + [tally[v] for v in VERDICTS]
        assert groups[prefix] == actual, (
            f"{files[prefix].name}: SUMMARY says "
            f"{dict(zip(COLUMNS, groups[prefix]))}, the table holds "
            f"{dict(zip(COLUMNS, actual))}"
        )
        assert sum(actual[1:]) == rows, (
            f"{files[prefix].name}: {sum(actual[1:])} verdicts over {rows} rows"
        )


def test_the_grand_total_matches_the_files() -> None:
    total = _summary_table()[1]
    rows = 0
    tally: Counter[str] = Counter()
    for path in _group_files().values():
        n, t = _group_counts(path)
        rows += n
        tally.update(t)
    actual = [rows] + [tally[v] for v in VERDICTS]
    assert total == actual, (
        f"grand total is {dict(zip(COLUMNS, total))}, the eight files hold "
        f"{dict(zip(COLUMNS, actual))}"
    )
