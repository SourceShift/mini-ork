"""mini_ork.specdir — deterministic spec-directory ingestion (spec-driven-delivery K1).

Zero-LLM layer that turns a directory of markdown spec files into a validated
``spec-index.json`` and a lint report, and defines the SpecCard contract the
LLM contract compiler fills in later. Design:
docs/plans/2026-10-03-spec-driven-delivery.md. CLI: ``mini-ork specs``
(:mod:`mini_ork.cli.specs`).

    from mini_ork.specdir import scan_specdir, build_index, write_index, lint_specdir

Modules: :mod:`.scan` (inventory), :mod:`.spec_card` (SpecCard + schema
validation), :mod:`.index` (spec-index.json, duplicate/cycle checks),
:mod:`.lint` (authoring-error findings), :mod:`.mdparse` (markdown skeleton),
:mod:`.schema` (schema loading). Pure stdlib + jsonschema: no network, no
child processes, no model calls.
"""
from __future__ import annotations

from mini_ork.specdir.index import (
    INDEX_FILENAME,
    SpecIndexError,
    build_index,
    deliverable_graph,
    find_cycles,
    find_duplicate_ids,
    read_index,
    write_index,
)
from mini_ork.specdir.lint import Finding, has_errors, lint_specdir, lint_text, scan_and_lint
from mini_ork.specdir.scan import SpecEntry, scan_specdir
from mini_ork.specdir.spec_card import SpecCard, SpecCardError, validate_card

__all__ = [
    "INDEX_FILENAME",
    "Finding",
    "SpecCard",
    "SpecCardError",
    "SpecEntry",
    "SpecIndexError",
    "build_index",
    "deliverable_graph",
    "find_cycles",
    "find_duplicate_ids",
    "has_errors",
    "lint_specdir",
    "lint_text",
    "read_index",
    "scan_and_lint",
    "scan_specdir",
    "validate_card",
    "write_index",
]
