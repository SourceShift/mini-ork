"""The framework-edit verifier's *inline* frozen list must match the engine floor.

``recipes/framework-edit/verifiers/static-check.py`` imports the guard from the
engine when it can, but a stale or partial engine falls back to a copy that
lives in the script itself. That copy was 14 entries with
``parse_frozen_paths`` returning ``[]``: it checked 14 paths and silently
ignored every entry of ``config/kernel-frozen.yaml`` — including the eight
self-driving policy inputs the 2026-10-27 kernel freeze added. A stale engine
still refused (that part worked); it just refused less than the floor says.

These tests hold the copy and the floor byte-equal (list-for-list, in order) and
exercise the inline parsers, so the two cannot drift apart again. The engine
module is the authority; the copy is derived from it, not re-invented.

The script runs its whole verifier at import time (module-level ``sys.exit``),
so it is never imported here: the fallback names are lifted out of its AST and
executed on their own.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "framework-edit" / "verifiers" / "static-check.py"
CONFIG = REPO / "config" / "kernel-frozen.yaml"

#: The names the ``except`` branch defines. All are side-effect-free defs/values.
_WANTED = frozenset({
    "_INLINE_FROZEN_PATHS",
    "_inline_extract_paths",
    "_inline_minimal_parse",
    "_inline_parse_frozen_paths",
    "_inline_load_frozen_paths",
    "_inline_frozen_hits",
})


def _fallback() -> dict[str, Any]:
    """The verifier's fallback names, executed without running the verifier."""
    tree = ast.parse(VERIFIER.read_text(encoding="utf-8"), str(VERIFIER))
    nodes: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(getattr(t, "id", "") in _WANTED for t in node.targets):
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in _WANTED:
            nodes.append(node)
    ns: dict[str, Any] = {"os": os}
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, str(VERIFIER), "exec"), ns)  # noqa: S102 — repo source, not input
    return ns


# ── the copy and the floor are the same list ────────────────────────────────

def test_the_inline_copy_equals_the_engine_floor() -> None:
    """Order included: a reordered copy is a copy someone edited by hand."""
    from mini_ork.vcs.repo_integrity_guard import FROZEN_PATHS_DEFAULT

    assert _fallback()["_INLINE_FROZEN_PATHS"] == FROZEN_PATHS_DEFAULT


def test_the_copy_carries_the_triage_input() -> None:
    """The 2026-10-29 review's first required fix: escalation.py reads
    ``file_diagnosis`` from triage/failures.py, so that file is an input."""
    assert "mini_ork/triage/failures.py" in _fallback()["_INLINE_FROZEN_PATHS"]


# ── the inline reader really reads ──────────────────────────────────────────

SAMPLE = """\
version: 1
# a comment must not become a path
paths:
  - config/kernel-frozen.yaml
  - 'mini_ork/quoted.py'
  - recipes/code-fix/probes/fixtures/**
  # interleaved comment
  - mini_ork/plain.py
other_key:
  - not/a/frozen/path.py
"""


def test_inline_minimal_parse_reads_the_paths_block_only() -> None:
    assert _fallback()["_inline_minimal_parse"](SAMPLE) == [
        "config/kernel-frozen.yaml",
        "mini_ork/quoted.py",
        "recipes/code-fix/probes/fixtures/**",
        "mini_ork/plain.py",
    ]


def test_inline_parse_agrees_with_the_engine_parser() -> None:
    """The committed config, read by both readers, must give the same list."""
    from mini_ork.vcs.repo_integrity_guard import parse_frozen_paths

    text = CONFIG.read_text(encoding="utf-8")
    inline = _fallback()["_inline_parse_frozen_paths"](text)
    assert inline == parse_frozen_paths(text)
    assert "mini_ork/triage/failures.py" in inline


def test_inline_load_reads_the_checkout_config(tmp_path: Path) -> None:
    root = tmp_path / "engine"
    (root / "config").mkdir(parents=True)
    (root / "config" / "kernel-frozen.yaml").write_text(SAMPLE, encoding="utf-8")

    got = _fallback()["_inline_load_frozen_paths"](str(root))
    assert got == ["config/kernel-frozen.yaml", "mini_ork/quoted.py",
                   "recipes/code-fix/probes/fixtures/**", "mini_ork/plain.py"]


@pytest.mark.parametrize("body", [None, "", "   \n# only comments\n"])
def test_inline_load_fails_closed_to_the_copy(tmp_path: Path, body: str | None) -> None:
    """A missing or unparsable config must never read as "nothing is frozen"."""
    fb = _fallback()
    root = tmp_path / "engine"
    (root / "config").mkdir(parents=True)
    if body is not None:
        (root / "config" / "kernel-frozen.yaml").write_text(body, encoding="utf-8")

    assert fb["_inline_load_frozen_paths"](str(root)) == fb["_INLINE_FROZEN_PATHS"]


def test_inline_frozen_hits_matches_globs() -> None:
    hits = _fallback()["_inline_frozen_hits"](
        ["recipes/code-fix/probes/fixtures/a/b.json", "mini_ork/other.py",
         "mini_ork/triage/failures.py"],
        ["recipes/code-fix/probes/fixtures/**", "mini_ork/triage/failures.py"])
    assert hits == ["recipes/code-fix/probes/fixtures/a/b.json",
                    "mini_ork/triage/failures.py"]
