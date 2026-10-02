"""Fixture trees are inputs to tests, not tests themselves.

``remote_e2e_repo`` is a target repo the remote-nodes E2E copies and fixes;
its own ``tests/test_calc.py`` only imports inside that copy (and fails by
design until the fix lands), so the mini-ork suite must never collect it.
"""
collect_ignore_glob = ["remote_e2e_repo/*"]
