"""Mini-ork prompt optimization (Phase 1: GEPA-style reflective optimizer).

Public surface: ``GepaAdapter`` (the user-supplied Protocol that scores a
candidate against a batch and surfaces reflective failures) and ``optimize``
(the minibatch-acceptance-gated reflective-mutation loop).
"""

from __future__ import annotations

from .gepa import GepaAdapter, optimize

__all__ = ["GepaAdapter", "optimize"]