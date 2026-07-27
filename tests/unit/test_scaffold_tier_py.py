"""Unit tests: mini_ork.orchestration.scaffold_tier (bash parity halves removed; formerly vs lib/scaffold_tier.sh).

Eight cases:

  (a) unset env (absent)             → ``harness``
  (b) ``MO_SCAFFOLD_TIER=minimal``   → ``minimal``
  (c) ``MO_SCAFFOLD_TIER=harness``   → ``harness``
  (d) ``MO_NODE_SCAFFOLD=minimal``   → ``minimal``
  (e) both UNSET to empty string     → ``harness``
  (f) global harness overrides node  → ``harness`` (conflict-mask)
  (g) unknown ``MO_SCAFFOLD_TIER``   → ``harness`` (unknown-value fall-through)
  (h) unknown global + known node    → ``minimal`` (flag-level conflict-mask:
                                        the resolver falls through the global,
                                        matches on ``MO_NODE_SCAFFOLD``)

Stdout contract: ``<value>\\n`` (``print()``).

The scaffolding-tier resolver is a pure env-only function, so no DB fixture
is involved.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.orchestration import scaffold_tier as st

_RESOLVER_ENV_KEYS = ("MO_SCAFFOLD_TIER", "MO_NODE_SCAFFOLD")


def _py_stripped(capsys: pytest.CaptureFixture, overrides: dict[str, str]) -> str:
    """Invoke the Python port with ``overrides``; return captured stdout minus trailing ``\\n``."""
    saved = {k: os.environ.get(k) for k in _RESOLVER_ENV_KEYS}
    try:
        for k in _RESOLVER_ENV_KEYS:
            os.environ.pop(k, None)
        for k, v in overrides.items():
            if v == "":
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        st.mo_scaffold_tier("implementer", "code_fix")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    captured = capsys.readouterr()
    return captured.out.rstrip("\n")


_CASES: list[tuple[str, dict[str, str], str]] = [
    ("a_both_absent",            {},                                                                    "harness"),
    ("b_global_minimal",         {"MO_SCAFFOLD_TIER": "minimal"},                                       "minimal"),
    ("c_global_harness",         {"MO_SCAFFOLD_TIER": "harness"},                                       "harness"),
    ("d_node_minimal",           {"MO_NODE_SCAFFOLD": "minimal"},                                       "minimal"),
    ("e_both_empty_string",      {"MO_SCAFFOLD_TIER": "", "MO_NODE_SCAFFOLD": ""},                       "harness"),
    ("f_global_harness_wins",    {"MO_SCAFFOLD_TIER": "harness", "MO_NODE_SCAFFOLD": "minimal"},        "harness"),
    ("g_global_unknown",         {"MO_SCAFFOLD_TIER": "mini"},                                          "harness"),
    ("h_unknown_global_node_ok", {"MO_SCAFFOLD_TIER": "mini", "MO_NODE_SCAFFOLD": "minimal"},           "minimal"),
]


@pytest.mark.parametrize(
    "case_id,overrides,expected",
    _CASES,
    ids=[c[0] for c in _CASES],
)
def test_scaffold_tier(
    case_id: str, overrides: dict[str, str], expected: str,
    capsys: pytest.CaptureFixture,
) -> None:
    """The resolver maps each env combination to the documented tier."""
    py_out = _py_stripped(capsys, overrides)
    assert py_out == expected, (
        f"[{case_id}] resolver drifted: expected {expected!r}, got {py_out!r}"
    )


def test_scaffold_tier_ergonomic_aliases() -> None:
    """`scaffold_tier` and `resolve` are thin aliases of `mo_scaffold_tier` (same fn)."""
    assert st.scaffold_tier is st.mo_scaffold_tier
    assert st.resolve is st.mo_scaffold_tier
