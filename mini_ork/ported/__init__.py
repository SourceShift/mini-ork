"""Mini-ork ported modules.

Faithful Python ports of the deterministic, pure-logic core of bash functions
in ``lib/``. The bash wrappers in ``lib/`` stay in place (strangler-fig
co-existence); these ports give Python callers an in-process target and give
tests a stable surface for parity verification against the live bash.

Parity is enforced by tests under ``tests/unit/test_*_parity.py``.
"""
