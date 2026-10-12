"""LangGraph parity test suite.

These tests pin the runtime semantics of mini-ork's revise loop against the
behaviour documented in the LangGraph reference implementation. They live
in their own package so the parity suite can be filtered (``-k parity``) or
sharded separately in CI without picking up the rest of ``tests/unit/``.
"""