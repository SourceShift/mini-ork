"""mini_ork.memory — semantic long-term memory with a utility-aware retrieval
policy.

Public surface (the kickoff contract's export list, plus the utility loop):

  add(...)                 — reconcile a fact (or extract-then-reconcile via model) into a scope
  search(...)              — rank memories within a scope by utility-aware similarity
  record_retrievals(...)   — log that memories were injected into a run's prompt
  record_outcome(...)      — resolve a run's retrievals to win/loss
  Embedder                 — Protocol for pluggable vector embedders
  HashEmbedder             — stdlib-only default embedder (no new pip dep)

``import mini_ork.memory`` is the supported entry point. The implementation
lives in ``mini_ork.memory.semantic``; this package file just re-exports the
public surface so callers don't need to know the sub-module name.
"""

from __future__ import annotations

from .semantic import (
    Embedder,
    HashEmbedder,
    add,
    record_outcome,
    record_retrievals,
    rank_with_prior,
    resolve_finished_runs,
    search,
    upsert,
)
from .retirement import (
    RETIRE_ENTER_UTILITY,
    RETIRE_EXIT_UTILITY,
    RETIRE_MIN_USES,
    candidates,
    reactivate,
    retire,
    retirement_state,
)

__all__ = [
    "add",
    "upsert",
    "search",
    "rank_with_prior",
    "record_retrievals",
    "record_outcome",
    "resolve_finished_runs",
    "retire",
    "reactivate",
    "retirement_state",
    "candidates",
    "RETIRE_ENTER_UTILITY",
    "RETIRE_EXIT_UTILITY",
    "RETIRE_MIN_USES",
    "Embedder",
    "HashEmbedder",
]
