"""Native contract for the executor's optional intervention gate.

The Bash executor treated ``lib/intervention_gate.sh`` as an optional hook and
proceeded when that file was absent. The hook was never implemented or tracked,
so fork closure preserves the only production behavior that existed: proceed.

Keeping this policy behind a named function gives a future implementation a
Python-owned extension point without leaving a dangling Bash source edge.
"""
from __future__ import annotations


def intervention_gate_check(
    node_id: str,
    node_type: str,
    lane: str,
    node_desc: str,
) -> bool:
    """Return ``True`` to preserve the absent-hook fail-open contract."""
    del node_id, node_type, lane, node_desc
    return True
