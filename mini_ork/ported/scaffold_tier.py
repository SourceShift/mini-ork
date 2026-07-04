"""scaffold_tier — Python port of ``lib/scaffold_tier.sh``.

Faithful port of ``mo_scaffold_tier`` (R5b scaffold-tier resolver).
Echoes the scaffold tier (``minimal`` | ``harness``) a node should use.

Resolution order (first match wins; any unknown / unset value falls back
to the v1 conservative default ``harness``):

    1. ``MO_SCAFFOLD_TIER=minimal``  → ``minimal``
    2. ``MO_SCAFFOLD_TIER=harness``  → ``harness``
    3. ``MO_NODE_SCAFFOLD=minimal``  → ``minimal``
    4. ``MO_NODE_SCAFFOLD=harness``  → ``harness``
    5. otherwise                     → ``harness``  (default; byte-identical to pre-R5b)

Argv is positional-only for forward compatibility with a future per-node
policy table; the resolver currently ignores its arguments and reads env
only — mirrors bash exactly (which also ignores ``"$@"``).

Stdout contract: one of the two literal strings with a single trailing
newline (``minimal\\n`` or ``harness\\n``), produced via ``print()`` to
exactly match bash's ``printf '%s\\n'`` byte sequence.

Strangler-fig co-existence: ``lib/scaffold_tier.sh`` is byte-identical
before and after this module exists. ``tests/unit/test_scaffold_tier_py.py``
is the parity gate that proves the port emits byte-identical stdout
against the LIVE bash subprocess for all 8 bash test cases (no mocks,
no hardcoded outputs beyond the resolver constants ``minimal``/``harness``).

Public surface:

    mo_scaffold_tier(*args) -> str   # canonical — matches the bash function name
    scaffold_tier(*args)    -> str   # thin alias for ergonomic Python callers
    resolve(*args)          -> str   # thin alias for ergonomic Python callers

All three forward ``*args`` straight through and ignore them; only env
matters. The string return contains the trailing ``\\n`` (matches bash).
"""
from __future__ import annotations

import os
from typing import Final

_VALID: Final[frozenset[str]] = frozenset({"minimal", "harness"})
_DEFAULT: Final[str] = "harness"


def mo_scaffold_tier(*args: object) -> str:
    """Return the resolved scaffold tier (``minimal`` or ``harness``) + ``\\n``.

    Reads ``MO_SCAFFOLD_TIER`` first, then ``MO_NODE_SCAFFOLD``; any
    unknown / unset value falls back to ``harness``. Positional ``*args``
    are accepted and ignored — mirrors bash's positional-only signature
    reserved for a future per-node-type policy table.
    """
    del args  # positional args reserved; ignored to match bash verbatim.

    global_val = os.environ.get("MO_SCAFFOLD_TIER", "")
    if global_val in _VALID:
        print(global_val)
        return f"{global_val}\n"

    node_val = os.environ.get("MO_NODE_SCAFFOLD", "")
    if node_val in _VALID:
        print(node_val)
        return f"{node_val}\n"

    print(_DEFAULT)
    return f"{_DEFAULT}\n"


# Ergonomic aliases — Python callers rarely need the `mo_` prefix.
scaffold_tier = mo_scaffold_tier
resolve = mo_scaffold_tier
