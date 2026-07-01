"""mini_ork.memory — standalone, opt-in semantic long-term memory.

Public surface (the kickoff contract's export list):

  add(...)       — reconcile a fact (or extract-then-reconcile via model) into a scope
  search(...)    — cosine-rank memories within a scope
  Embedder       — Protocol for pluggable vector embedders
  HashEmbedder   — stdlib-only default embedder (no new pip dep)

``import mini_ork.memory`` is the supported entry point. The implementation
lives in ``mini_ork.memory.semantic``; this package file just re-exports the
public surface so callers don't need to know the sub-module name.
"""

from __future__ import annotations

from .semantic import Embedder, HashEmbedder, add, search

__all__ = ["add", "search", "Embedder", "HashEmbedder"]
