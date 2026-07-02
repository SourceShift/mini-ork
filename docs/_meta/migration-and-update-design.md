# Design: robust DB migrations + safe update mechanism

> Status: proposed, 2026-07-02. Goal: let a consuming repo update mini-ork to
> the latest version **without ever overriding its `state.db`** — with integrity
> checks, transactional safety, and code re-vendoring built in.

## What exists today (and why it still forces DB wipes)

mini-ork already has a migration system, so this is a *hardening* job, not a
greenfield one:

- `db/migrations/NNNN_*.sql` (46 files), applied in lex order by `db/init.sh`.
- A `schema_migrations(filename, applied_at, checksum)` table (created in
  `0001_core.sql`) tracks what's applied; `init.sh` skips already-applied files.
- `bin/mini-ork-update` applies pending migrations to a project's `state.db`,
  reports config drift, and supports `--dry-run` / `--pull`.

**The real gaps that make people wipe the DB:**

1. **Placeholder checksums.** `init.sh` stores `checksum='runner-applied'`, not a
   hash. There is *no* integrity check — a shipped migration edited after release
   goes undetected, and drift can't be diagnosed.
2. **No code re-vendor for vendored consumers.** `mini-ork-update --pull` only
   works when `MINI_ORK_ROOT` is a git checkout. A repo that *vendored* mini-ork
   into `.mini-ork/` (the normal case — e.g. the researcher fleet) has no
   supported way to pull the new **framework code**, so people hand-rsync or wipe.
3. **No backup / no transaction.** A migration that fails midway leaves a
   half-migrated `state.db` with no rollback → the fastest "fix" is to delete it.
4. **Idempotency hacks.** `ensure_column(...)` + the `0018` special-case in
   `init.sh` are band-aids for migrations that weren't cleanly re-runnable —
   fragile, and they run *outside* the tracked migration flow.
5. **Conflated concerns + weak CLI.** `init.sh` mixes WAL setup, column repair,
   migrations, and views; `mini-ork-update` isn't wired as `mini-ork update` in
   the main dispatch table.

## Design principles

- **Never touch data on update.** `state.db`, `config/secrets.local.sh`,
  `config/agents.yaml`, `runs/` are sacred. Update only replaces framework code
  and applies additive migrations.
- **Apply each migration exactly once, transactionally, and verify integrity.**
- **Fail safe.** Back up before migrating; roll back on any failure.
- **Same path for fresh install, git checkout, and vendored consumer.**

## Migration layer (`lib/migrate.sh` — extracted from `init.sh`)

A dedicated module, callable as `mini-ork migrate [--status|--dry-run|--verify]`.

**`schema_migrations` v2** (additive migration; keep `filename` PK):
```
filename TEXT PRIMARY KEY
applied_at TEXT NOT NULL
checksum TEXT NOT NULL          -- real: sha256 of the file at apply time
mini_ork_version TEXT           -- version that applied it
duration_ms INTEGER
```

**Apply algorithm (`mini-ork migrate`):**
1. Ensure `schema_migrations` exists (bootstrap).
2. For each `db/migrations/*.sql` in lex order:
   - Compute `sha256(file)`.
   - If already applied: **verify** the stored checksum matches. Mismatch →
     hard error ("migration NNNN changed after being applied") unless
     `MO_MIGRATE_ALLOW_DRIFT=1`. Never re-run.
   - If pending: run inside `BEGIN; … COMMIT;` (SQLite has transactional DDL).
     On success, record `(filename, now, sha256, version, duration)`. On failure,
     `ROLLBACK`, print the offending statement, exit non-zero — the DB is
     unchanged.
3. Apply `db/views/*.sql` (idempotent `CREATE VIEW IF NOT EXISTS`) the same way.

**`mini-ork migrate --status`** prints: applied count, pending list, any
checksum-drifted files, current vs latest migration id. **`--dry-run`** lists
pending without writing. **`--verify`** checks every applied checksum (CI + a
`doctor` probe).

**Migration authoring rules** (documented in `db/README.md`):
- Additive only where possible (`ADD COLUMN`, `CREATE TABLE IF NOT EXISTS`).
- **Never edit a shipped migration** — add a new one (the checksum guard enforces
  this).
- Each file is one logical change, wrapped so it's transaction-safe.
- Retire `ensure_column`/`0018` hacks into a single idempotent
  `00xx_repair_learning_columns.sql` that the tracker records once.

## Update layer (`bin/mini-ork-update` → `mini-ork update`)

Turns "update" into a safe, one-command, data-preserving operation for a
consuming repo:

```
mini-ork update [--source <path|release>] [--dry-run] [--no-migrate]
```

**Steps (each reversible):**
1. **Resolve source** of the new framework: `--source <dir>` (a mini-ork clone),
   or a downloaded release tarball pinned by version, or `$MINI_ORK_SOURCE`.
   Record `from`→`to` version (a `.mini-ork/VERSION` file).
2. **Back up** `state.db` (+ `-wal`/`-shm`) and `config/` to
   `.mini-ork/.backups/<from-version>-<ts>/`.
3. **Re-vendor code** — `rsync -a --delete` the framework dirs
   (`bin lib recipes db schemas mini_ork .githooks`) from source into
   `.mini-ork/`, **excluding** `state.db*`, `config/secrets.local.sh`,
   `config/agents.yaml`, `runs/`, `.backups/`. (This is exactly the manual
   re-vendor we run today, made first-class.)
4. **Migrate** — `mini-ork migrate` (transactional; skips on `--no-migrate`).
5. **Verify** — `mini-ork doctor` + `mini-ork migrate --verify`.
6. **On any failure**, restore the `state.db` backup and the previous code, and
   report. `--dry-run` prints steps 1–4 without writing.

**Config drift** (shipped `config/*.example` vs local) is *reported*, never
auto-applied — the operator merges intentionally.

## Integration + CLI

- Wire `migrate` and `update` into `bin/mini-ork`'s dispatch table (today
  `mini-ork-update` is an unlisted sibling binary).
- `db/init.sh` keeps only bootstrap + WAL pragmas + delegate to `lib/migrate.sh`
  (drops the inline repair hacks once the repair migration lands).
- `mini-ork doctor` gains a "migrations: N applied, M pending, 0 drifted" line.

## Phased implementation

1. **P1 — real checksums + transactional apply** in a new `lib/migrate.sh`;
   `init.sh` delegates to it. Backward-compatible: existing
   `checksum='runner-applied'` rows are re-hashed on first `--verify`
   (or left, with drift-allow for legacy rows).
2. **P2 — `mini-ork migrate` CLI** (`--status/--dry-run/--verify`) + doctor line.
3. **P3 — re-vendor step in `mini-ork update`** (backup → rsync framework →
   migrate → verify → rollback) + `.mini-ork/VERSION`.
4. **P4 — retire the `ensure_column`/`0018` hacks** into one repair migration;
   simplify `init.sh`.

Net: `mini-ork update` becomes a safe, idempotent, data-preserving upgrade —
the DB is migrated forward transactionally, never overridden.
