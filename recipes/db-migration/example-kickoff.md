# Kickoff — migration: add `user_uuid` NOT NULL to `chapter_blocks`

## Change kind

`add_column` + `backfill` + `add_constraint NOT NULL`

## Target schema

- RDBMS: PostgreSQL 16.2
- Table: `chapter_blocks`
- Current row count: ~12.5M
- Current size: ~3.2 GB
- Existing FKs: `(book_uuid) REFERENCES books(uuid)`, `(parent_block_id) REFERENCES blocks(uuid)`

## DDL summary

Add `user_uuid UUID NOT NULL REFERENCES users(uuid)` to `chapter_blocks`.
Backfill from the parent `books.user_uuid`. Use the new column in
`chapter_blocks` ACL checks instead of joining to `books` on every read.

## Deployment env

prod (<prod-host>, accessed via Tailscale `<prod-host>:5932`)

## Rollback required

YES — this is prod. Must be reversible without data loss.

## Downtime tolerance

`zero` — no read-locks acceptable; max 5s write-blocks; backfill must
be online.

## Downstream consumers

- `server/services/chapterBlocks/queryService.ts` (reads `chapter_blocks` + does ACL via books join — to be simplified post-migration)
- `server/jobs/blockSyncJob.ts` (writes chapter_blocks)
- Grafana dashboard `book_chapter_progress` (reads row count by tenant)
- ETL `nightly_user_activity_aggregate` (cron 03:00 UTC)
- Search index `qdrant://chapter_blocks` (embedding sync, daily)

## Scope boundaries

- WILL NOT cover: ACL service refactor that consumes the new column (separate epic).
- WILL NOT cover: migration of historical `chapter_blocks_archive` table.

## Why now

Q3 perf audit flagged that every ACL check joins `chapter_blocks` →
`books` → `user_uuid` adding ~40ms per read. Denormalizing `user_uuid`
into `chapter_blocks` removes that join. The migration is the precondition
for the ACL simplification.

## Audience

Senior on-call. Spell out commands but no need to over-explain pg DDL.
