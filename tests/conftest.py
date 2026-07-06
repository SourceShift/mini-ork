"""Suite-wide test isolation.

Many ported ``main()`` functions mirror the bash entrypoints by exporting
process env (``os.environ["MINI_ORK_HOME"]``, ``MINI_ORK_DB``, ``MINI_ORK_RUN_ID``,
``MINI_ORK_ROOT``, …). That is correct for the real CLI (process-scoped) but, when
a test calls ``main()`` in-process, the mutation persists into the shared
``os.environ`` and leaks into later tests. A downstream bash-parity test then
spawns bash with ``{**os.environ, ...}`` and inherits a stale (often deleted)
``MINI_ORK_HOME``/``MINI_ORK_DB`` from the earlier test — so bash and the port
diverge and the parity assertion fails. Each such test passes in isolation but
fails in the full suite (the CI-only, single-process failure mode).

This autouse fixture snapshots and restores ``os.environ`` and the working
directory around every test, isolating that leakage suite-wide.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_process_state():
    env_snapshot = dict(os.environ)
    try:
        cwd_snapshot = os.getcwd()
    except OSError:
        cwd_snapshot = None
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(env_snapshot)
        if cwd_snapshot is not None:
            try:
                os.chdir(cwd_snapshot)
            except OSError:
                pass
