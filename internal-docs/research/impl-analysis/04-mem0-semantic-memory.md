# Impl analysis 04 — Semantic / long-term agent memory (mem0)

**Gap targeted:** mini-ork's "memory" is a SQLite run-ledger (traces, gradients, failure
records, perf stats — `lib/memory.sh`, `lib/pattern_store.sh`, `state.db`). It has no
*semantic* layer: it can't extract durable facts/learnings from a run and later retrieve the
relevant ones by meaning at plan time. mem0 is the most popular universal agent-memory layer.

**Source read:** `/private/tmp/miniork-ref-analysis/mem0` — `mem0/mem0/memory/main.py`
(`class Memory`, `add()` @716, `_add_to_vector_store()` @830, `search()` @1326).

---

## A. How mem0 works (real code)

### `add(messages, *, user_id|agent_id|run_id, infer=True, ...)`  (@716)
Memory is **scoped** by one of `user_id / agent_id / run_id` (required). With `infer=True`
(default), it doesn't store raw text — it runs an **LLM extract→reconcile** pipeline; with
`infer=False` it stores raw.

### `_add_to_vector_store()` — the extract + ADD/UPDATE/DELETE reconcile (@830)
The interesting part. Per add:
1. `parsed_messages = parse_messages(messages)` and pull short-term context
   `last_messages = self.db.get_last_messages(session_scope, limit=10)`.
2. **Find related existing memories first:** `query_embedding = embedding_model.embed(parsed_messages,"search")`;
   `existing_results = vector_store.search(query=parsed_messages, filters=search_filters, ...)`.
3. **LLM fact extraction:** system `ADDITIVE_EXTRACTION_PROMPT` (+`AGENT_CONTEXT_SUFFIX`),
   user prompt from `generate_additive_extraction_prompt(new_messages, last_k_messages)` →
   `llm.generate_response(...)` returns the salient facts.
4. **Reconcile vs existing:** the model decides per fact whether it's a new `ADD`, an `UPDATE`
   of an existing memory, a `DELETE` (contradiction), or `NOOP` — returning events like
   `{"id":..., "memory":..., "event":"ADD"|"UPDATE"|"DELETE"}`.
5. **Embed + persist:** `embedding_model.embed_batch(mem_texts,"add")` then `_create_memory`
   into the vector store (raw messages also saved via `db.save_messages`).

So memory isn't an append log — it's a **deduped, self-correcting fact set** maintained by an LLM.

### `search(query, *, filters={user_id|agent_id|run_id}, top_k=20, rerank=False)` (@1326)
Embed the query → `vector_store.search` filtered by scope → ranked memories (optional rerank,
optional graph traversal when a graph store is configured). Filters MUST include a scope id.

### Storage model
Pluggable **vector store** (Qdrant/pgvector/chroma/sqlite-vec/…) for semantic recall + an
optional **graph store** (Neo4j/Kuzu via the `graphiti`-style path) for entity relations.
Everything carries `user_id/agent_id/run_id` + metadata for scoped retrieval.

---

## B. Contrast with mini-ork
- mini-ork stores *structured run telemetry* (rewards, gradients, failures) keyed by
  task_class/recipe — great for the GRPO loop, but it's **exact-match/SQL**, not semantic.
  It can't answer "what did we learn before that's *relevant* to this new kickoff?"
- mem0 stores *natural-language learnings* with embeddings + an LLM reconcile step → semantic
  recall + self-dedup. Complementary, not a replacement.

mini-ork actually already has the *human* analog: the `~/.claude/.../memory/*.md` notes with a
`description` used "to decide relevance during recall." mem0 is the automated, embedded version
of exactly that.

---

## C. Adoption plan for mini-ork (additive layer over the SQLite ledger)

### New: `lib/semantic_memory.sh` with three ops mirroring mem0
```
mo_mem_add   "<text or trace>" --scope objective_domain=<d> [--infer]   # extract+reconcile+embed
mo_mem_search "<query>" --scope objective_domain=<d> --top-k N          # semantic recall
mo_mem_gc    --scope ...                                                 # expire/dedup
```
- **Embedding/vector backend that fits a bash tool:** `sqlite-vec` (a SQLite extension) is the
  natural fit — keeps everything in the existing `state.db` family, no new service, queryable.
  Fallback: a tiny local embedding service or pgvector if a PG seam already exists (the
  shared-brain RLM already contemplates a `policy_store` PG seam).
- **Reuse mini-ork's LLM dispatch** (`lib/llm-dispatch.sh`) for the extract+reconcile call,
  with a mem0-style ADD/UPDATE/DELETE prompt, on a cheap lane (minimax/glm).

### Where it plugs into the lifecycle
1. **reflect → improve stage** (`bin/mini-ork-reflect`): after a run, `mo_mem_add` the durable
   learnings (what worked, the gotcha, the decision) scoped by `objective_domain` — the same
   partition the shared-brain RLM already uses.
2. **plan / context-assembly** (`lib/context_assembler.sh`): before planning, `mo_mem_search`
   the kickoff text and inject the top-K relevant prior learnings into the planner context —
   so the orchestrator stops repeating past mistakes.
3. Scope by `objective_domain` (not user) to match the consumer-agnostic shared brain; tag with
   recipe + run_id for provenance.

### Why this is the right shape
- Keeps the auditable SQL ledger as source of truth; semantic memory is a derived recall index.
- The LLM-reconcile (ADD/UPDATE/DELETE) prevents the memory from bloating into a noisy log —
  directly relevant since mini-ork runs produce huge trace volume.
- Closes the loop the human memory notes already prove valuable, but automatically.

**Effort:** med (sqlite-vec wiring + extract/reconcile prompt + two call sites). **Payoff:**
cross-run learning by *meaning*, not just task_class match; fewer repeated failures.
