"""The verification commands a generated kickoff falls back to.

Two emitters mint a kickoff with no file hints to build a check from:

* :func:`mini_ork.cli.epics._synth_verify` — an epic split out of a roadmap
  body that named no paths;
* :func:`mini_ork.observability.bug_report._render_promoted_kickoff` — a bug
  promoted into an epic.

They used to carry the same pair of strings twice, and that is how they
drifted: when the file-hint path learned to say ``sys.executable`` (bare
``python3`` on this host is 3.9 and cannot even ``import mini_ork`` — dataclass
``slots`` are 3.10+), the no-hint fallbacks kept the interpreter that fails
before it tests anything. One definition means the next correction lands in
both kickoff shapes at once.

The pair must also *terminate on its own* and reference paths that exist: the
previous fallback cited ``tests/integration/test_autonomous_epic_pipeline.sh``
(deleted by the 2026-07 bash-removal, Phase 4, ``72040279``) and a
``shellcheck $(...)`` whose substitution is empty when no ``.sh`` changed — so
shellcheck read stdin and hung the verifier.
"""
from __future__ import annotations

import sys


def fallback_commands() -> list[str]:
    """The self-check a kickoff gets when its body named no files.

    Built at call time, not import time: ``sys.executable`` is the interpreter
    running THIS process, which is the only one the engine knows can import
    the package.
    """
    return [
        f"{sys.executable} -m compileall -q mini_ork",
        f"{sys.executable} -m pytest -q "
        "tests/unit/test_mini_ork_epics_py.py tests/unit/test_scheduler_py.py",
    ]
