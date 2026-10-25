#!/usr/bin/env python3
"""Strip the never-enforced per-node ``gates:`` key from recipe workflows.

ECO6 K13. 347 nodes in 42 recipes declared ``gates:`` in ``workflow.yaml``, but
nothing ever read it: the compiler (``WorkflowNode`` / ``_node_from_yaml``) has
no ``gates`` field, and the executor, handlers and publisher never look at the
key. Wiring it up would not help either — ``gate_registry.gate_evaluate``
resolves a gate by its registered *id*, not by the declared type name, so
passing these names through would fail every node.

This is a line/regex-level edit on purpose. A YAML load/dump round-trip would
drop comments, reorder keys and requote scalars; the kickoff requires every
other byte to survive. Only node-level ``gates`` constructs are removed. A
top-level ``gates:`` key and ``task_class.yaml``'s ``default_gates`` are never
touched (the patterns below require leading indentation / an inline-mapping
comma that neither has).

Three physical shapes exist in the corpus:

* flow-mapping tail:      ``- { …, gates: [a, b] }``
* own-line inline list:   ``    gates: [a, b]``
* true block sequence::

      gates:
        - a
        - b

Every edited file still loads with ``yaml.safe_load`` and every node dict is
unchanged except that ``gates`` is gone. Note how that shows up in
``git diff --numstat``: the two block forms disappear as whole lines (a pure
deletion), while a flow-mapping node keeps its declaration — only the
``, gates: […]`` entry goes — so that physical line is rewritten in place, and
git counts a rewritten line as one insertion plus one deletion. Deleting the
node itself would satisfy a deletions-only count but would drop a live graph
role and every edge that references it, which is not what this codemod does.

Usage::

    python3 scripts/codemods/eco6_strip_node_gates.py [workflow.yaml …]

With no path arguments it rewrites every ``recipes/*/workflow.yaml`` under the
repository root. The script is idempotent.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ``, gates: [...]`` inside a flow mapping. The leading comma is consumed with
# its whitespace; the whitespace before the closing ``}`` is not touched.
_FLOW_TAIL_RE = re.compile(r",[ \t]*gates:[ \t]*\[[^\]]*\]")
# ``{ gates: [...], `` when gates is the first key of a flow mapping.
_FLOW_HEAD_RE = re.compile(r"\{[ \t]*gates:[ \t]*\[[^\]]*\][ \t]*,[ \t]*")
# A node keyword on its own indented line with an inline list value.
_OWN_LINE_RE = re.compile(r"^[ \t]+gates:[ \t]*\[[^\]]*\][ \t]*$")
# A node keyword on its own indented line followed by a block sequence.
_BLOCK_KEY_RE = re.compile(r"^([ \t]+)gates:[ \t]*$")
_LIST_ITEM_RE = re.compile(r"^[ \t]*-[ \t]")


def _indent_width(line: str) -> int:
    """Leading-whitespace width with tabs expanded to the next 8-column stop."""
    return len(line[: len(line) - len(line.lstrip(" \t"))].expandtabs(8))


def strip_node_gates(text: str) -> str:
    """Return ``text`` with every node-level ``gates`` construct removed.

    Byte-preserving for everything else: lines that are not gate constructs are
    emitted unchanged, including comments and blank lines. Top-level keys and
    ``default_gates`` do not match and therefore survive.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    total = len(lines)
    while i < total:
        line = lines[i]

        block = _BLOCK_KEY_RE.match(line)
        if block:
            key_indent = _indent_width(block.group(1))
            j = i + 1
            # Consume only the block-sequence items belonging to this key: list
            # items indented deeper than the key. Stop at the first blank line,
            # comment or sibling key so everything else survives byte-for-byte.
            while j < total and _LIST_ITEM_RE.match(lines[j]):
                if _indent_width(lines[j]) <= key_indent:
                    break
                j += 1
            i = j
            continue

        if _OWN_LINE_RE.match(line):
            i += 1
            continue

        if "gates" in line:
            # The tail can sit on a continuation line of a multi-line flow
            # mapping, so it is matched without requiring the opening ``{``.
            line = _FLOW_HEAD_RE.sub("", line, count=1)
            line = _FLOW_TAIL_RE.sub("", line, count=1)

        out.append(line)
        i += 1
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
            print(f"eco6_strip_node_gates: {path}: {exc}", file=sys.stderr)
            return 2
        updated = strip_node_gates(original)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed += 1
    print(f"eco6_strip_node_gates: {changed}/{len(targets)} file(s) updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
