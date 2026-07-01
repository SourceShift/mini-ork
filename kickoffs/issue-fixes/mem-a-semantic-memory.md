# MEM-a: semantic long-term memory module (native Python, standalone, opt-in)

## Context
Grounded in `internal-docs/research/impl-analysis/04-mem0-semantic-memory.md`. mini-ork's memory
today is a SQLite run-ledger (`lib/memory.sh`, `lib/pattern_store.sh`, `state.db`) — exact-match,
not semantic. It can't answer "what did we learn before that's RELEVANT to this new kickoff?".
mem0's pattern: LLM-extract durable facts from a trace → embed → store; retrieve by relevance;
reconcile ADD/UPDATE/DELETE so it self-dedups. This phase adds that as a native module — standalone
(nothing calls it yet; wiring into reflect/context-assembly is MEM-b), opt-in, no hard new dep.

## Deliverables
1. `mini_ork/memory/semantic.py`:
   - `add(text, *, scope, infer=True) -> list[event]` — with `infer`, call `mini_ork.dispatch.dispatch_model`
     to extract 0-N durable learnings from `text`, compare against existing memories in the same
     `scope` (semantic search), and emit ADD/UPDATE/DELETE/NOOP events (mem0's reconcile). With
     `infer=False`, store `text` raw. Scope is a string (use `objective_domain` in mini-ork).
   - `search(query, *, scope, top_k=5) -> list[memory]` — embed query, cosine-rank stored memories
     in `scope`, return top_k.
   - Storage in a SQLite table `semantic_memory(id, scope, text, embedding BLOB, created_at, meta)`
     in `MINI_ORK_DB` (add a `db/migrations/00NN_semantic_memory.sql`, idempotent `IF NOT EXISTS`).
   - **Embedder abstraction** `Embedder` protocol with `embed(texts) -> list[vector]`. Ship TWO
     impls: a real one behind an env-selected provider (leave the provider call thin/pluggable),
     and a **deterministic hash-embedding fallback** (`HashEmbedder`, pure-python, no network) used
     by default in tests and when no embedding provider is configured — so the module needs NO new
     pip dependency to run or test.
2. `mini_ork/memory/__init__.py` exporting `add`, `search`, `Embedder`, `HashEmbedder`.

## Smoke / DoD (must pass)
- `tests/test_semantic_memory_py.py` (pytest, using `HashEmbedder` + monkeypatched `dispatch_model`):
  - `add` with `infer=True` extracts a learning (stub model returns one fact) and stores it;
    `search` for a related query returns it ranked above an unrelated memory.
  - Reconcile: adding a contradicting/updated fact emits an UPDATE (or DELETE+ADD), not a blind
    duplicate — assert the memory set doesn't grow unboundedly on re-add of the same fact.
  - `search` is scoped: a memory in scope A is not returned for a scope-B query.
  - Migration is idempotent (run twice, no error).
- `python -m pytest -q` green; `python -c "import mini_ork.memory"` works.

## Constraints (scope guard)
- Add ONLY `mini_ork/memory/*.py`, `db/migrations/00NN_semantic_memory.sql`, `tests/test_semantic_memory_py.py`.
- No hard new pip dependency (HashEmbedder default; real embedder pluggable/thin). No wiring into
  reflect/context-assembly yet (MEM-b). Default system behavior unchanged. Keep the SQL run-ledger
  as-is — this is an additive derived index.
