"""Every panel verifier with a strict cross-lane key-parity check must ignore
the harness provenance keys.

The researcher handler stamps ``served_lane`` / ``requested_lane`` /
``served_family`` into a judge's artifact only when its dispatch left a lane
record, so judges in one panel can differ by exactly those keys. A verifier
that compares raw key sets would then fail a healthy panel (review 151 caught
audit-judge-panel-3l; review 152 the two siblings). This pins every current
and future ``recipes/*/verifiers/panel-completeness.py`` that does a
top-level parity check.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HARNESS = {"served_lane", "requested_lane", "served_family"}


def _verifiers_with_parity() -> list[Path]:
    return sorted(
        p for p in REPO.glob("recipes/*/verifiers/panel-completeness.py")
        if "top_level_keys_differ" in p.read_text(encoding="utf-8")
    )


def _harness_keys(path: Path) -> set[str] | None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_HARNESS_KEYS" for t in node.targets):
            if isinstance(node.value, ast.Set):
                return {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    return None


def test_the_parity_verifiers_are_found():
    names = {p.parts[-3] for p in _verifiers_with_parity()}
    assert {"audit-judge-panel", "audit-judge-panel-3l", "session-task-judge"} <= names, names


def test_every_parity_verifier_excludes_the_harness_keys():
    for path in _verifiers_with_parity():
        keys = _harness_keys(path)
        assert keys == HARNESS, f"{path.relative_to(REPO)}: _HARNESS_KEYS={keys}"
        text = path.read_text(encoding="utf-8")
        assert "set(a) != set(b)" not in text, (
            f"{path.relative_to(REPO)} still compares raw key sets")
