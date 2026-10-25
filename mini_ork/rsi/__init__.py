"""Self-directed RSI cycle: harvest gaps, rank them, fix one, prove it.

The package is deliberately not under ``learning/``. It owns gaps in learning,
routing *and* verification, and filing it under one of the three is how the
"starved by construction" problem the 2026-10-24 audit found got started.

Layout (built incrementally; only the modules listed here exist today):

``candidates``  the unit of work — identity, signature, integer severity.
``ledger``      all reads/writes of the ``rsi_*`` tables. No policy.

Later steps add ``probes``, ``harvest``, ``queue``, ``gates``, ``worktree`` and
``cycle``. Everything except the two model edges (SUGGEST, IMPLEMENT) is pure
Python so the loop can be dry-run against a temp DB with no network.
"""
