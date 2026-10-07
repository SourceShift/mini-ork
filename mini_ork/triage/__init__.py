"""Failure triage — decide whether a failed run is mini-ork's bug, and (opt-in)
turn that into a queued self-edit epic.

Two layers:
  * :mod:`mini_ork.triage.blame`     — pure attribution (no I/O), testable.
  * :mod:`mini_ork.triage.failures`  — I/O driver: read a failed run's events,
    attribute the failure, emit a bug report, optionally promote it to a
    ``framework-edit`` epic that the scheduler will dispatch.
"""
from __future__ import annotations

from mini_ork.triage.blame import Blame, Evidence, NodeFailure, attribute

__all__ = ["Blame", "Evidence", "NodeFailure", "attribute"]
