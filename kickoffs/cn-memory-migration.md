# Kickoff — S1: memory backend seam, semantic memory on ContextNest

## Goal

Introduce a `MemoryBackend` seam under mini-ork's memory modules so that memory
needing learning, recall and self-improvement can be served by ContextNest, with
SQLite as the fallback every install has. **This kickoff is stage S1 only:**
the seam, backend selection, and `semantic_memory` + `semantic_memory_uses`
migrated to CN. Lessons, gradients, patterns and namespaced memory stay on
SQLite until S1 has proven itself in runs.

Design of record: `docs/architecture/memory-backend.md`.

## Context a run must not have to rediscover

- CN is already read at `mini_ork/context_assembler.py:1461,1475` and in
  `mini_ork/steering/context_role_packs.py:119,135`; outcomes are already posted
  to `POST /api/v1/agent/outcome` (`cn_client.py:727,779`).
- Availability probe already exists: `cn_client.available()` (cached, TTL
  `cn_client._ping_ttl()`), and `MO_DISABLE_CN=1` already forces CN off.
  Reuse both; do not write a second probe.
- The current SQLite semantic store is `mini_ork/memory/semantic.py` (retrieval,
  SimUtil-UCB ranking, `W_UTILITY`/`W_EXPLORE` at `:67-100`) plus
  `mini_ork/memory/retirement.py`. These are the only two modules S1 may move.
- CN's write surface is `POST /api/v1/tools/store` (content, session_id,
  importance, metadata; content-hash `stable_fragment_id` dedupe) and
  `POST /api/v1/agent/outcome`. There is **no** metadata-filtered list endpoint
  yet — see Constraints.

## Done when

1. `mini_ork/memory/backends.py` defines the `MemoryBackend` protocol
   (`recall`, `record_use`, `upsert`, `list_kind`, `retire`) and
   `register_backend(name, factory)`, following the registry idiom in
   `AGENTS.md` ("Python runtime registries").
2. `SqlMemoryBackend` wraps today's code with **no behavioural change**: the
   existing `tests/unit/test_memory_semantic_py.py` (and retirement tests) pass
   unmodified.
3. `CnMemoryBackend` implements the same protocol over `cn_client`, using
   `tools/store` for `upsert`, `agent/outcome` for `record_use`, and `retrieve`
   for `recall`.
4. `resolve_backend()` honours `MO_MEMORY_BACKEND=auto|cn|sql`; `auto` selects
   CN only when `cn_client.available()` and logs one line naming the chosen
   backend and, on fallback, the reason.
5. `MO_MEMORY_BACKEND=cn` with CN unreachable raises a named error from
   `recall`; it never returns `[]`.
6. Writes during fallback set `pending_cn=1` on the SQLite row (migration in
   `db/migrations/`, guarded `ALTER`), and `mini-ork memory sync` replays them
   idempotently and clears the flag.
7. The run record carries `memory_backend` (`cn|sql`), and the assembled prompt
   section states it in one line.
8. `tests/unit/test_memory_backend_contract_py.py` parametrizes the suite in
   the design doc §6 over both backends and passes; the CN case uses a recorded
   HTTP fake, not a live daemon.
9. `list_kind` is implemented for SQL now and returns a clear
   `NotImplementedError("CN listing: docs/architecture/memory-backend.md §4.1.1")`
   on the CN backend until that endpoint lands — an honest gap, not a silent `[]`.
10. `docs/architecture/memory-backend.md` §7 marks S1's row done and records
    which falsifier (F1-F4) the first measured run addresses.

## Constraints

- Do not migrate any table other than `semantic_memory` / `semantic_memory_uses`
  in this stage. A diff that touches `learning/` is out of scope.
- Do not add a new HTTP client: everything goes through `mini_ork/cn_client.py`.
- Do not change `HashEmbedder` or the SimUtil-UCB weights; the seam moves where
  data lives, not how it ranks.
- `lesson_injections` stays local by the decision in the design doc §4.
- No new dependency; stdlib `urllib` only, as `cn_client` already does.
