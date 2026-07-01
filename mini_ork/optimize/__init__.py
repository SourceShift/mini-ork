"""Mini-ork prompt optimization (Phase 1: GEPA-style reflective optimizer).

Public surface:
  ``GepaAdapter`` — Protocol scoring a candidate against a batch.
  ``optimize``    — minibatch-acceptance-gated reflective-mutation loop.
  ``MiniOrkGepaAdapter`` — concrete adapter reading execution_traces +
                           process_reward rows from the mini-ork state.db.
  ``run_suggestion``     — convenience: build a pattern_records-shaped
                           suggestion dict the reflect hook can emit.
"""

from __future__ import annotations

from .gepa import GepaAdapter, optimize
from .miniork_adapter import MiniOrkGepaAdapter, run_suggestion

__all__ = ["GepaAdapter", "optimize", "MiniOrkGepaAdapter", "run_suggestion"]
