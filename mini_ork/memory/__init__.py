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
    search,
)

__all__ = [
    "add",
    "search",
    "record_retrievals",
    "record_outcome",
    "Embedder",
    "HashEmbedder",
]
