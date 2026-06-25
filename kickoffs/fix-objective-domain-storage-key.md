# Fix: persist per-`objective_domain` routing (re-key the brain's policy store)

## Why (evidence)
Pre-push Layer-3 review-36 finding #193 (HIGH), confirmed in code:
`lib/lane_router.sh` groups relative-advantage samples by
`(objective_domain, task_class, node_type)` but **upserts** into
`agent_performance_memory` whose PRIMARY KEY is only
`(agent_version_id, task_class)`. The code comment at `lib/lane_router.sh:117`
admits it: *"per-domain UPSERTs collide on storage … domain isolation lives at
the slice-mean level, not the storage row."* Net effect: the v0.4 headline
feature — a shared brain that **learns per `objective_domain`** — does not
actually persist per domain. Two consumers (e.g. code-delivery vs book-gen)
overwrite each other's routing state.

## Ordering (important)
This is a **prerequisite to the researcher repo's Phase 0** (the Postgres
store-port at `rlm-phase0-pg-store-port-kickoff.md`), NOT part of it. Phase 0
explicitly forbids changing `lane_router`/`decide()` and only ports the
**existing** schema to Postgres. Therefore the canonical SQLite schema must be
re-keyed here FIRST; Phase 0 then ports the corrected schema and its
`decide(code-delivery)` vs `decide(book-gen)` slice-isolation smoke becomes a
real storage-level guarantee instead of a slice-mean artifact.

## Scope (4 touch-points — all mini-ork framework, SQLite canonical path)
1. **Migration** (new, additive, partial-apply-safe — heed review-36 #194):
   add `objective_domain TEXT NOT NULL DEFAULT ''` to `agent_performance_memory`
   and widen the key to `(agent_version_id, task_class, objective_domain)`.
   SQLite can't `ALTER … PRIMARY KEY`, so rebuild: create the new table, copy
   rows (existing rows get `objective_domain=''` = the legacy global slice),
   swap, recreate indexes. Wrap in a guarded check (`pragma_table_info`) so a
   partial/re-applied migration is a no-op, not an error.
2. **Write path** `lib/lane_router.sh:151` — include `objective_domain` in the
   INSERT column list and the `ON CONFLICT(...)` target so upserts are
   per-domain.
3. **Read path** `lane_router_preferred_lane` (`lib/lane_router.sh:174`) —
   accept an `objective_domain` argument and filter on it (fall back to the
   `''` global slice when a domain has < `MO_LEARNING_MIN_SAMPLES`).
4. **Caller** `lib/decision_service.sh:86` — thread the request's
   `objective_domain` through to `lane_router_preferred_lane`.

## Definition of Done
- New migration applies cleanly on a fresh DB AND is a no-op on an
  already-migrated/partial DB (idempotent).
- `agent_performance_memory` upserts no longer collide across domains: two
  domains writing the same `(agent_version_id, task_class)` produce two rows.
- `lane_router_preferred_lane <task_class> <node_type> <objective_domain>`
  returns the per-domain preferred lane, falling back to the global slice
  under the sample floor.
- `decide()` routes using the per-domain slice.
- SQLite default behavior otherwise unchanged; no formula changes (only
  key/storage + the domain argument plumbed through).
- Re-run the pre-push Layer-3 review → #193 resolved.

## Related cleanups to fold in (same review-36, MEDIUM)
- **#194** `db/migrations/0042_*.sql:28` — make the rlm-1 migration idempotent /
  partial-apply safe (same guarded-rebuild discipline as above).
- **#195** `lib/deadline_budget.sh:41` — sourcing the lib leaks strict shell
  options (`set -e`/`-u`/`-o pipefail`) into the caller; save/restore shell
  opts around the sourced body so consumers aren't forced into strict mode.

## Out of scope
- The Postgres backend (that is the researcher repo's Phase 0, gated on this).
- Any consumer/domain-specific content — this stays generic OSS framework code.
