"""spec-index.json — build, write, read, and the graph checks behind it.

The index is the deterministic hand-off from ingestion to the SDD pipeline:
one entry per ``spec_id`` (absolute ``source_path``, ``source_hash``,
``title``, ``status``, ``depends_on``) plus ``generated_at`` (ISO-8601 UTC)
and the absolute scan ``root``. It always validates against
``schemas/spec-index.schema.json``: :func:`write_index` refuses to write an
invalid index and :func:`read_index` refuses to return one.

Duplicate ``spec_id`` values and dependency cycles are hard errors
(:class:`SpecIndexError`). Cycles are checked at two levels: spec-level
``Depends on:`` edges from the markdown, and — when SpecCards are supplied —
deliverable ``depends_on`` edges, with deliverable ids qualified as
``<spec_id>/<D-id>`` so cross-spec edges share one graph.

Writes are atomic (temp file in the target dir + ``os.replace``) with sorted
keys, so re-ingesting an unchanged tree differs only in ``generated_at``.

    build_index(entries, root, *, cards=(), generated_at=None) -> dict
    write_index(index, path) -> Path ; read_index(path) -> dict
    find_cycles(graph) -> list[list[str]]   # each cycle closed: [a, b, a]
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from mini_ork.specdir.scan import SpecEntry
from mini_ork.specdir.schema import SPEC_INDEX_SCHEMA, schema_errors
from mini_ork.specdir.spec_card import SpecCard

SCHEMA_VERSION = "1.0"
INDEX_FILENAME = "spec-index.json"


class SpecIndexError(ValueError):
    """The index cannot be built, written, or read; ``problems`` lists why."""

    def __init__(self, message: str, problems: Iterable[str] = ()):
        self.problems = list(problems)
        detail = "".join(f"\n  - {p}" for p in self.problems)
        super().__init__(f"{message}{detail}")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_duplicate_ids(entries: Iterable[SpecEntry]) -> dict[str, list[SpecEntry]]:
    """``spec_id`` -> the colliding entries (sorted by source_path), for ids
    produced by more than one file."""
    groups: dict[str, list[SpecEntry]] = {}
    for e in entries:
        groups.setdefault(e.spec_id, []).append(e)
    return {sid: sorted(group, key=lambda e: e.source_path)
            for sid, group in sorted(groups.items()) if len(group) > 1}


def _canonical(cycle: list[str]) -> tuple[str, ...]:
    pivot = cycle.index(min(cycle))
    return tuple(cycle[pivot:] + cycle[:pivot])


def find_cycles(graph: Mapping[str, Iterable[str]]) -> list[list[str]]:
    """Dependency cycles in ``graph`` (node -> nodes it depends on).

    Iterative DFS with white/grey/black colouring; every back edge yields one
    cycle. Each cycle is rotated to start at its smallest node, reported once,
    and closed (``[a, b, a]``); the list is sorted. Nodes that appear only as
    targets are leaves. A graph has a cycle iff the result is non-empty.
    """
    nodes = sorted(set(graph) | {d for deps in graph.values() for d in deps})
    adj = {n: sorted(set(graph.get(n, ()))) for n in nodes}
    white, grey, black = 0, 1, 2
    colour = dict.fromkeys(nodes, white)
    seen: set[tuple[str, ...]] = set()
    cycles: list[list[str]] = []
    for start in nodes:
        if colour[start] != white:
            continue
        colour[start] = grey
        path = [start]
        stack = [iter(adj[start])]
        while stack:
            nxt = next(stack[-1], None)
            if nxt is None:
                stack.pop()
                colour[path.pop()] = black
            elif colour[nxt] == white:
                colour[nxt] = grey
                path.append(nxt)
                stack.append(iter(adj[nxt]))
            elif colour[nxt] == grey:
                key = _canonical(path[path.index(nxt):])
                if key not in seen:
                    seen.add(key)
                    cycles.append([*key, key[0]])
    return sorted(cycles)


def spec_graph(entries: Iterable[SpecEntry]) -> dict[str, list[str]]:
    graph: dict[str, set[str]] = {}
    for e in entries:
        graph.setdefault(e.spec_id, set()).update(e.depends_on)
    return {sid: sorted(deps) for sid, deps in graph.items()}


def deliverable_graph(cards: Iterable[SpecCard | dict]) -> dict[str, list[str]]:
    """Deliverable dependency graph across cards. Nodes are
    ``<spec_id>/<D-id>``; an unqualified ``depends_on`` entry refers to the same
    card, a ``<spec_id>/<D-id>`` entry crosses specs."""
    graph: dict[str, list[str]] = {}
    for card in _as_cards(cards):
        for d in card.deliverables:
            node = f"{card.spec_id}/{d.id}"
            deps = [dep if "/" in dep else f"{card.spec_id}/{dep}" for dep in d.depends_on]
            graph[node] = sorted(set(graph.get(node, [])) | set(deps))
    return graph


def _as_cards(cards: Iterable[SpecCard | dict]) -> list[SpecCard]:
    return [c if isinstance(c, SpecCard) else SpecCard.from_dict(c) for c in cards]


def format_cycle(cycle: list[str]) -> str:
    return " -> ".join(cycle)


def graph_problems(entries: list[SpecEntry], cards: Iterable[SpecCard | dict] = ()) -> list[str]:
    """Hard-error descriptions (duplicates, spec cycles, deliverable cycles)."""
    problems = []
    for sid, group in find_duplicate_ids(entries).items():
        paths = ", ".join(e.source_path for e in group)
        problems.append(f"DUP_ID: spec_id '{sid}' produced by {paths}")
    for cycle in find_cycles(spec_graph(entries)):
        problems.append(f"DEP_CYCLE: spec dependency cycle {format_cycle(cycle)}")
    for cycle in find_cycles(deliverable_graph(cards)):
        problems.append(f"DEP_CYCLE: deliverable dependency cycle {format_cycle(cycle)}")
    return problems


def build_index(entries: Iterable[SpecEntry], root: str | os.PathLike[str], *,
                cards: Iterable[SpecCard | dict] = (), generated_at: str | None = None) -> dict:
    entries = list(entries)
    card_list = _as_cards(cards)
    problems = graph_problems(entries, card_list)
    if problems:
        raise SpecIndexError("spec index has hard errors", problems)
    status = {c.spec_id: c.status for c in card_list}
    index = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or utc_now_iso(),
        "root": str(Path(root).expanduser().resolve()),
        "specs": {
            e.spec_id: {
                "source_path": e.source_path,
                "source_hash": e.source_hash,
                "title": e.title,
                "status": status.get(e.spec_id, "draft"),
                "depends_on": list(e.depends_on),
            }
            for e in sorted(entries, key=lambda e: e.spec_id)
        },
    }
    errors = validate_index(index)
    if errors:
        raise SpecIndexError("built index violates spec-index.schema.json", errors)
    return index


def validate_index(index: object) -> list[str]:
    return schema_errors(index, SPEC_INDEX_SCHEMA)


def write_index(index: dict, path: str | os.PathLike[str]) -> Path:
    errors = validate_index(index)
    if errors:
        raise SpecIndexError("refusing to write an invalid spec index", errors)
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def read_index(path: str | os.PathLike[str]) -> dict:
    source = Path(path).expanduser()
    try:
        index = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SpecIndexError(f"{source}: not valid JSON ({exc})") from exc
    errors = validate_index(index)
    if errors:
        raise SpecIndexError(f"{source}: violates spec-index.schema.json", errors)
    return index
