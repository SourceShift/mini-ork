#!/usr/bin/env python3
"""Collapse five decorative ``edge_type`` values into ``depends_on``.

ECO6 K14. ``schemas/workflow.schema.json`` accepts eight ``edge_type`` names,
but the compiler (``mini_ork/workflow/compiler.py``) gives runtime meaning to
only three of them:

* ``depends_on``  — ordering parent (+ optional ``from_output``/``to_input``
  artifact binding, which any edge type already carries),
* ``retries``     — the revise loop (``max_rounds``, object ``condition:``),
* ``escalates_to``— a repair edge only inside a ``recursion:`` block.

The other five — ``supplies_context_to``, ``verifies``, ``blocks``,
``human_decision_gate`` and ``verifies_user_choice`` — are already compiled
exactly like ``depends_on`` (``compiler.py`` treats every edge that is not
``escalates_to``/``retries``/``recursive: true`` as an ordering parent). This
codemod rewrites their declarations in every shipped recipe so the corpus stops
claiming semantics the runtime does not implement. The names stay accepted by
the schema for one release, and ``mini-ork validate`` warns on out-of-tree
recipes that still use them.

This is a line/regex-level edit on purpose. A YAML load/dump round-trip would
drop comments, reorder keys and requote scalars; the kickoff requires every
other byte to survive. Two physical shapes exist in the corpus — the flow form
(``- { from: a, to: b, edge_type: verifies }``) and the block form (``edge_type:
verifies`` on its own indented line) — and both put the value directly after
the ``edge_type:`` key, so one key-anchored substitution covers both. A quoted
value (``edge_type: "verifies"``) is matched including both quotes and emitted
in the bare form; leaving the trailing quote behind would corrupt the scalar.

The pattern is anchored on the *key*, never the bare word: recipe prose in
comments and descriptions contains ``verifies`` and ``supplies context to``,
and a word-level ``s/verifies/depends_on/`` would corrupt it. Comment lines are
skipped outright, which is free insurance for a future ``# edge_type: …`` note.

Usage::

    python3 scripts/codemods/eco6_collapse_edge_types.py [workflow.yaml …]

With no path arguments it rewrites every ``recipes/*/workflow.yaml`` under the
repository root. The script is idempotent.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Longest first so ``verifies_user_choice`` is not mistaken for ``verifies``;
# the ``\b`` is what keeps the two apart even if the order ever changes (``_``
# is a word character, so there is no boundary inside ``verifies_user_choice``).
_ALIAS_RE = re.compile(
    r"edge_type:[ \t]*['\"]?"
    r"(?:supplies_context_to|verifies_user_choice|human_decision_gate|verifies|blocks)"
    r"\b['\"]?"
)


def collapse_edge_types(text: str) -> str:
    """Return ``text`` with every deprecated-alias ``edge_type`` collapsed.

    Only the ``edge_type: <alias>`` value is rewritten; every other byte —
    comments, spacing, quoting, key order, other edge types — survives. Lines
    whose first non-space character is ``#`` are left untouched. Idempotent:
    ``depends_on`` is not in the alias set, so a second run is a no-op.
    """
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("#"):
            out.append(line)
            continue
        out.append(_ALIAS_RE.sub("edge_type: depends_on", line))
    return "".join(out)


def _default_targets() -> list[Path]:
    return sorted((_REPO_ROOT / "recipes").glob("*/workflow.yaml"))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    targets = [Path(arg) for arg in args] if args else _default_targets()
    changed = 0
    for path in targets:
        try:
            original = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"eco6_collapse_edge_types: {path}: {exc}", file=sys.stderr)
            return 2
        updated = collapse_edge_types(original)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed += 1
    print(f"eco6_collapse_edge_types: {changed}/{len(targets)} file(s) updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
