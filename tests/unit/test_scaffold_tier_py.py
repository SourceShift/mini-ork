"""Parity gate: mini_ork.ported.scaffold_tier vs lib/scaffold_tier.sh.

Eight cases (kickoff floor: ``>=6``; 2-case buffer):

  (a) unset env (absent)             → ``harness``
  (b) ``MO_SCAFFOLD_TIER=minimal``   → ``minimal``
  (c) ``MO_SCAFFOLD_TIER=harness``   → ``harness``
  (d) ``MO_NODE_SCAFFOLD=minimal``   → ``minimal``
  (e) both UNSET to empty string     → ``harness``
  (f) global harness overrides node  → ``harness`` (conflict-mask)
  (g) unknown ``MO_SCAFFOLD_TIER``   → ``harness`` (unknown-value fall-through)
  (h) unknown global + known node    → ``minimal`` (flag-level conflict-mask:
                                        bash `case` falls through global,
                                        matches on ``MO_NODE_SCAFFOLD``)

Stdout contract: ``<value>\\n`` (bash ``printf '%s\\n'``; Python ``print()``).

Comparison strategy:
  - bash invoked via ``subprocess.run(['bash','-c', ...], env=cleared, ...)``
    with stdout captured verbatim.
  - Python port invoked in-process; stdout captured via ``capsys``.
  - Both sides ``.rstrip("\\n")`` (mirrors how bash callers already consume
    the resolver via ``$(mo_scaffold_tier)`` which strips the newline).

No stubbing, no hardcoded outputs beyond the resolver constants
``minimal`` / ``harness``. Every assertion is a live-bash-subprocess
versus-Python-port diff on identical (cleaned) environments.

The bash resolver / test script are NOT modified by this file — the
scaffolding-tier resolver is a pure env-only function, so no DB fixture
is involved.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import scaffold_tier as st  # noqa: E402

SH = REPO / "lib" / "scaffold_tier.sh"

_RESOLVER_ENV_KEYS = ("MO_SCAFFOLD_TIER", "MO_NODE_SCAFFOLD")
_BASH = "/bin/bash"
_BASH_CMD = (
    'source "{resolver}" && mo_scaffold_tier implementer code_fix'
).format(resolver=SH)


def _isolated_env(overrides: dict[str, str]) -> dict[str, str]:
    """Build a subprocess env that EXCLUDES scaffold-tier vars, then apply overrides.

    Bash ``case "${X:-}"`` treats absent and empty identically; the Python
    port reads via ``os.environ.get(X, "")``, also treating them identically.
    So an explicit ``""`` override is structurally equivalent to absent —
    but we differentiate the two in cases (a)/(e) to prove both code
    paths land on the same answer.
    """
    env = {k: v for k, v in os.environ.items() if k not in _RESOLVER_ENV_KEYS}
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    env["HOME"] = os.environ.get("HOME", "/tmp")
    for k, v in overrides.items():
        if v == "":
            env.pop(k, None)  # absent, not empty
        else:
            env[k] = v
    return env


def _bash_stripped(overrides: dict[str, str]) -> str:
    """Invoke the LIVE bash resolver with ``overrides``; return stdout minus trailing ``\\n``."""
    proc = subprocess.run(
        [_BASH, "-c", _BASH_CMD],
        env=_isolated_env(overrides),
        cwd=str(REPO),
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.rstrip("\n")


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
def test_scaffold_tier_parity(
    case_id: str, overrides: dict[str, str], expected: str,
    capsys: pytest.CaptureFixture,
) -> None:
    """Byte-identical stdout: live bash subprocess vs in-process Python port."""
    bash_out = _bash_stripped(overrides)
    py_out = _py_stripped(capsys, overrides)

    assert bash_out == expected, (
        f"[{case_id}] bash drifted: expected {expected!r}, got {bash_out!r}"
    )
    assert py_out == expected, (
        f"[{case_id}] python drifted: expected {expected!r}, got {py_out!r}"
    )
    assert bash_out == py_out, (
        f"[{case_id}] PARITY FAILED: bash={bash_out!r} python={py_out!r}"
    )


def test_scaffold_tier_bash_resolver_file_exists() -> None:
    """Bash resolver must remain at the canonical path (strangler-fig co-existence)."""
    assert SH.is_file(), f"bash resolver missing: {SH}"


def test_scaffold_tier_ergonomic_aliases() -> None:
    """`scaffold_tier` and `resolve` are thin aliases of `mo_scaffold_tier` (same fn)."""
    assert st.scaffold_tier is st.mo_scaffold_tier
    assert st.resolve is st.mo_scaffold_tier
