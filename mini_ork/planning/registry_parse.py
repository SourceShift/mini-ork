"""Deterministic markdown-table parser for the feature registry.

The registry (``ebook-companion-feature-registry.md``) is the audit's work
list, but it is a *document*, not a data file: its tables were hand-written
across eleven sessions and never had one schema. Seven header layouts appear,
and the ``Status`` column sits at a different index in each — 4 for Clusters
A-F and J, 3 for G, 4 for H, 2 for the H-parked and I tables.

So this parser keys on **header names, never column positions**. A positional
parser reads the tables it was tested on and silently mis-maps the rest —
worse than failing, because a mangled Status reads downstream as a real
verdict. Header lookup makes a schema change fail loudly instead.

No LLM touches this: the audit's whole point is that a model re-deriving the
work list each run would give different items in a different order, and the
per-item checkpoints in ``mini_ork.orchestration.item_fanout`` key on item id.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ``### Cluster A — Onboarding & dashboard (session 9)``
_CLUSTER_RE = re.compile(r"^###\s+Cluster\s+([A-Z])\b\s*[—–-]?\s*(.*)$")
# A separator row: only pipes, colons, hyphens and spaces, and at least one run
# of hyphens. Requiring the absence of letters/digits is what keeps a real row
# like ``| — |`` (em-dash cell) from being read as a separator.
_SEP_RE = re.compile(r"^\|?[\s:|-]+\|?$")
# Which header column feeds ``title`` / ``evidence`` when several are present.
_TITLE_HEADERS = ("Feature", "Action")
_EVIDENCE_HEADERS = ("Evidence / spec", "Evidence / delta", "Evidence", "Brief")


@dataclass(frozen=True)
class RegistryItem:
    """One registry row. ``columns`` keeps the full header->value map so a
    caller that needs a variant column (``s11 #``, ``Wave``, ``Tier``) does not
    force this parser to model all seven layouts explicitly."""

    id: str
    cluster: str
    cluster_title: str
    title: str
    status: str
    evidence: str
    line: int
    columns: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_separator(line: str) -> bool:
    stripped = line.strip()
    return "-" in stripped and bool(_SEP_RE.match(stripped))


def split_row(line: str) -> list[str]:
    """Split a table row on unescaped pipes.

    Rows contain literal pipes inside code spans — ``goal=clients\\|monetize``
    (Cluster A, ONB-1) — so a plain ``str.split("|")`` yields one extra cell
    and shifts every column after it. Backslash-escaped pipes stay in-cell.
    """
    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    for char in line.strip():
        if escaped:
            buf.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(buf).strip())
            buf = []
        else:
            buf.append(char)
    cells.append("".join(buf).strip())
    # A well-formed row starts and ends with ``|``, yielding empty first/last
    # cells. Drop them; a malformed row keeps its cells intact rather than
    # losing real content to a blind strip.
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


def _clean_id(raw: str) -> str:
    """``**DASH-1**`` -> ``DASH-1``.

    Ids are bolded in some rows and plain in others; the checkpoint filename
    must not depend on which.
    """
    return raw.strip().strip("*`").strip()


def _header_index(header: list[str], name: str) -> int | None:
    for index, cell in enumerate(header):
        if cell.strip().lower() == name.lower():
            return index
    return None


def _find_prefixed(header: list[str], prefix: str) -> int | None:
    for index, cell in enumerate(header):
        if cell.strip().lower().startswith(prefix.lower()):
            return index
    return None


def _first_present(header: list[str], names: tuple[str, ...]) -> int | None:
    for name in names:
        index = _header_index(header, name)
        if index is not None:
            return index
    return None


def _cell(cells: list[str], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return cells[index]


def _table_items(
    header: list[str],
    rows: list[tuple[int, list[str]]],
    cluster: str,
    cluster_title: str,
) -> list[RegistryItem]:
    """Extract items from one table body.

    A table under a Cluster heading with an ``ID`` column but no ``Status``
    column is a hard error: silently dropping it would leave those features
    out of the audit with no signal, which is exactly the invisible-gap
    failure this parser exists to prevent. A table with no ``ID`` column at
    all is not a registry table and is skipped.
    """
    id_index = _header_index(header, "ID")
    if id_index is None:
        return []
    status_index = _find_prefixed(header, "Status")
    if status_index is None:
        raise ValueError(
            f"Cluster {cluster or '(none)'}: table has an ID column but no Status "
            f"column — header={header!r}. A registry table must carry a status."
        )
    title_index = _first_present(header, _TITLE_HEADERS)
    evidence_index = _first_present(header, _EVIDENCE_HEADERS)

    items: list[RegistryItem] = []
    for line_number, cells in rows:
        raw_id = _cell(cells, id_index)
        item_id = _clean_id(raw_id)
        if not item_id:
            continue
        columns = {
            header[i]: cells[i] for i in range(min(len(header), len(cells))) if header[i]
        }
        items.append(
            RegistryItem(
                id=item_id,
                cluster=cluster,
                cluster_title=cluster_title,
                title=_cell(cells, title_index),
                status=_cell(cells, status_index),
                evidence=_cell(cells, evidence_index),
                line=line_number,
                columns=columns,
            )
        )
    return items


def parse_registry(text: str, *, allow_duplicates: bool = False) -> list[RegistryItem]:
    """Parse every registry table under a ``### Cluster X`` heading.

    Tables elsewhere in the document (``## Spec / brief coverage``,
    ``## Sources``) are ignored because the current cluster only persists
    while a Cluster heading is in force.

    Duplicate ids raise by default: the fan-out checkpoints by id, so two rows
    sharing one would silently collapse to a single unit of work.
    """
    lines = text.splitlines()
    items: list[RegistryItem] = []
    cluster = ""
    cluster_title = ""
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]

        match = _CLUSTER_RE.match(line)
        if match:
            cluster = match.group(1)
            cluster_title = match.group(2).strip()
            index += 1
            continue

        # Any other H3 ends the cluster: a later plain table must not inherit
        # the previous cluster's identity.
        if line.startswith("###"):
            cluster = ""
            cluster_title = ""
            index += 1
            continue

        # A table header is a pipe row whose successor is a separator row.
        if (
            line.lstrip().startswith("|")
            and index + 1 < total
            and _is_separator(lines[index + 1])
        ):
            header = split_row(line)
            rows: list[tuple[int, list[str]]] = []
            cursor = index + 2
            while cursor < total and lines[cursor].lstrip().startswith("|"):
                if not _is_separator(lines[cursor]):
                    rows.append((cursor + 1, split_row(lines[cursor])))
                cursor += 1
            if cluster:
                items.extend(_table_items(header, rows, cluster, cluster_title))
            index = cursor
            continue

        index += 1

    if not allow_duplicates:
        seen: dict[str, int] = {}
        for item in items:
            if item.id in seen:
                raise ValueError(
                    f"duplicate registry id {item.id!r} at lines {seen[item.id]} and {item.line}"
                )
            seen[item.id] = item.line
    return items


def parse_registry_file(path: str | Path, *, allow_duplicates: bool = False) -> list[RegistryItem]:
    """Read ``path`` as UTF-8 and parse it."""
    text = Path(path).read_text(encoding="utf-8")
    return parse_registry(text, allow_duplicates=allow_duplicates)
