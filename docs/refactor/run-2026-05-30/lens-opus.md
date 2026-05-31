# mini-ork Scalability Audit — Architectural Shape (Opus Lens)

> mini-ork v0.1.1 is a bash CLI orchestration framework on sqlite3. The primitives — 6-stage loop, 8 node types, 6 gate types, 8 memory namespaces — are the universal contract. Scaling to fleet throughput is a **substrate swap beneath an unchanged contract**. Recommendations R1–R22.

---

## 1. Current Shape

mini-ork runs as a single-process bash orchestrator backed by one sqlite3 file (`state.db`). Architecture at 1K/day:

- **Runtime:** 8 bash scripts in `bin/` + 22 library modules in `lib/`, sourced at invocation. No persistent daemon.
- **State:** Single `state.db` file, 13 migrations under `db/migrations/`. 15+ tables across 8 namespaces: `task_runs`, `execution_traces`, `mo_events`, `llm_calls`, `textual_gradients`, `pattern_records`, `workflow_candidates`, `audit_log`.
- **Dispatch:** `lib/llm-dispatch.sh:75–99` forks a fresh `claude` CLI subprocess per LLM call. Model tier fixed by node type; `lib/providers/cl_opus.sh` and `cl_sonnet.sh` are the only tiers. Prompt caching (`mo_emit_cache_flags` in `lib/lane-helpers.sh:71`) is wired in only 4 of 9+ dispatch paths.
- **Concurrency:** `bin/mini-ork-execute:262–306` spawns parallel nodes via `( _dispatch_node ) &` with no semaphore. `max_lanes: 4` in `config/agents.yaml:30` is declared but never read by any dispatcher.
- **Self-improvement:** `lib/reflection_pipeline.sh`, `lib/group_evolver.sh`, and `lib/promotion_gate.sh` run synchronously in the foreground after each epic. Candidate promotion writes directly to `status='promoted'`; the `status='shadow'` enum value exists at `db/migrations/0009:39` but is not implemented anywhere.
- **Observability:** `mo_events.trace_id` column exists but no OTel span is emitted. `config/agents.yaml:30–31` declares `per_epic_usd: 5.00` / `per_run_usd: 0.50` budget caps that are never read or enforced.

At 1K/day, bash fork cost (~5 ms each) and sqlite single-writer latency are invisible. This shape is correct for the current tier.

---

## 2. 100K/day Failure Modes

At ~1 run/second (100K/day), five independent failure modes converge:

**F-1: sqlite single-writer contention.** 100K runs × ~7 writes/run = ~700K writes/day (~8/s avg, 100/s burst). `db/init.sh:27–65` never sets `PRAGMA journal_mode=WAL`; the 4 python3 blocks that do set it are scattered. CLI-fork paths in `lib/memory.sh:192,408,471,530` and `lib/auto-merge.sh:170,179` operate in default journal mode — concurrent workers cause systematic `SQLITE_BUSY` stalls. No retry logic exists.

**F-2: Unbounded parallel fork.** `bin/mini-ork-execute:262–306` forks ALL plan nodes simultaneously — no cap. A 50-node plan at 100K/day: 50 concurrent bash subshells × ~500 KB copied environment each hits OS fd limits and process table ceilings. `max_lanes: 4` in `config/agents.yaml:30` is documentation only.

**F-3: Linear run-dir scan.** `bin/mini-ork-execute:90–91` runs `find … | xargs -0 ls -1t | head -1` with no `-maxdepth` to locate the latest plan. At 100K subdirs under `runs/` this is seconds of I/O per invocation.

**F-4: N+1 sqlite forks in merge loops.** `lib/auto-merge.sh:170,179,356–375` opens 4–5 separate `sqlite3` subprocesses per epic — each a fresh fork + connection. At 100K epics/day: 400K–500K unnecessary process spawns.

**F-5: ARG_MAX overflow on iter-dir globs.** `lib/auto-merge.sh:149`, `lib/finalize.sh:42`, `lib/worktree-guard.sh:37`, `lib/mo-steer.sh:98` all use `for _d in $(ls -d …iter-*/)`. Shell word-splitting hits ARG_MAX (~2 MB on Linux) at 100K run dirs; the loop silently truncates.

---

## 3. 10M/day Failure Modes

At ~115 runs/second (10M/day), three structural ceilings cannot be fixed by optimising the current substrate:

**G-1: sqlite hard ceiling.** SQLite WAL peaks at ~35K writes/sec under ideal single-host NVMe conditions. 10M runs × 7 writes/run = 70M writes/day (~810/s). Multiple concurrent worker processes serialise on the WAL lock. No batching strategy fixes a single-file single-writer architecture at this throughput.

**G-2: Synchronous reflection stalls throughput.** `lib/reflection_pipeline.sh` runs in the foreground. At 10M/day, reflection + gradient extraction + group evolution + promotion gate = 4–6 LLM calls per run. Even ignoring latency: 10M × 5 LLM calls/run = 50M LLM calls/day cannot run synchronously without a dedicated reflection fleet. The current `lib/group_evolver.sh` emits 5 candidates per invocation with no rate ceiling — a malformed proposal at fleet scale is an outage.

**G-3: Unbounded table growth.** `execution_traces`, `mo_events`, `llm_calls`, and `audit_log` have no TTL, no archival sweep, and no rotation logic. `mo_events_archive` is defined at `db/migrations/0002` but no INSERT→DELETE sweep exists anywhere. At 10M runs, `execution_traces` accumulates 60M+ rows/day (~1.8B in 30 days). Full-table GROUP BY in `lib/pattern_store.sh:137–162` takes seconds at this cardinality.

---

## 4. State Layer Evolution

**sqlite → Postgres 15 → sharded Postgres + tiered cold storage**

The 8-namespace schema is architecturally sound; only the substrate changes.

**At 100K/day:** Add `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA cache_size=-65536;` as the first SQL in `db/init.sh:27`. Batch multi-step reads in `lib/auto-merge.sh:170–375` and `lib/memory.sh:192–538` into single queries — eliminate N+1 CLI forks. Add `runs_index(task_run_id, plan_key)` to avoid full-`find` scans on `runs/`. Implement the missing `mo_events → mo_events_archive` sweep (table defined, never wired).

**At 1M/day:** Swap sqlite for Postgres 15 behind a dialect-aware migration runner. The 13 `db/migrations/*.sql` carry sqlite-specific syntax (`RAISE(ABORT,…)`, `WITHOUT ROWID`). Annotate with `-- @sqlite:` / `-- @postgres15:` directives; the runner emits the right form per `MINI_ORK_DB_KIND`. Partition `task_runs` and `execution_traces` by `created_at` monthly. Add `tenant_id` column (default `'local'`) — invisible to single-host deployments, required for sharding. Migration tool must be idempotent using `pg_advisory_lock` (PG) or UNIQUE INSERT on `schema_migrations.filename` (sqlite).

**At 10M/day:** Shard PG by `tenant_id` (~100 tenants/DB). Three-tier storage: hot (PG ≤30d live writes), warm (PG read-only archive partitions ≤180d), cold (Parquet on S3 queryable via DuckDB). `audit_log` stays append-only per `db/migrations/0012:66–78` — partition it, never delete. Promote `mo_events` to Kafka; PG holds 30-day materialised view, not source of truth. Never archive `textual_gradients` or `pattern_records` — they are load-bearing self-evolution memory.

---

## 5. Dispatch Layer Evolution

**bash subprocesses → rate-gated async → Go runtime**

**At 100K/day — three immediate wins on the current substrate:**

Enforce `max_lanes` semaphore in `bin/mini-ork-execute:262–306`: read `config/agents.yaml:30` at dispatch time and hold at most `max_lanes` concurrent `( _dispatch_node ) &`. Wire `mo_emit_cache_flags` into `mo_llm_dispatch` at `lib/llm-dispatch.sh:75–99` so all 9+ dispatch paths inherit prefix-cache hits on the ~3 KB system prompt — estimated 60% reduction in planner and reviewer input-token cost. Move `reflection_pipeline.sh`, `mutation-adversary.sh`, and `rubric-prescreen.sh` off the foreground: fan them out with `&` + bounded `wait` — they read the same inputs and write independent outputs.

**At 1M/day — Go runtime migration:**

Migrate `lib/` primitives and `bin/` runtime to a single `mini-ork-runtime` Go binary. Bash stays as the user-facing shim (arg-parsing + `exec` into Go); recipe verifiers stay shell; `os/exec` invokes them from Go. Goroutines replace `&` + `wait`; context cancellation propagates through LLM child processes. `lib/providers/cl_opus.sh` and `cl_sonnet.sh` become Go provider implementations behind a unified interface, enabling runtime model-tier routing (Opus → Sonnet → Haiku based on node type + budget state).

**At 10M/day — fleet and safety gates:**

Reflection and group evolution run as stateless worker fleets consuming from Kafka. Shadow phase for promotions: new candidate → `status='shadow'` (enum defined at `db/migrations/0009:39`, not implemented) → serves 5% traffic for 24h → ramp to `promoted` on positive utility_delta → instant rollback + quarantine on regression. Hard ceiling: ≤10 promotion candidates per workflow per 24h in `lib/group_evolver.sh` regardless of evidence pressure. Fleet-wide rollback via PG LISTEN/NOTIFY — version-pointer flip in `version_registry_pointers` (`db/migrations/0011:104`) propagates to all workers in <60s.

---

## 6. Observability Gaps

Three gaps are acceptable at 1K/day; they become blocking at scale:

**O-1: No distributed trace.** `mo_events.trace_id` and `iters.test_trace_id` columns exist but no OTel span is emitted around `claude --print` in `lib/llm-dispatch.sh`. Every node dispatch is a black box. At 10M/day a failed run has no traceable lineage — you cannot determine which node, which model, or which prompt caused the failure without reconstructing from logs.

**O-2: No cost attribution or enforcement.** `llm_calls` records `cost_usd`, `provider`, `model_id` but not `tenant_id`. No materialised view aggregates spend. `config/agents.yaml:30–31` declares `per_epic_usd: 5.00` / `per_run_usd: 0.50` — both are **never read or enforced** anywhere in `bin/` or `lib/`. At 10M/day, an unbudgeted runaway task class can silently exhaust daily LLM budget.

**O-3: WAL mode is applied inconsistently.** `PRAGMA journal_mode=WAL` appears in 4 separate python3 blocks but not in `db/init.sh:27–65`. `lib/memory.sh` and `lib/auto-merge.sh` open connections in default journal mode. Under concurrent workers this creates a mixed-mode database — behaviour is undefined when a WAL-mode reader and a rollback-journal writer share the same file.

---

## 7. Numbered Recommendations

Each recommendation tagged: **severity** (P0 = blocks scale tier / P1 = degrades / P2 = future hardening) × **leverage** (H/M/L) / **effort** (S < 2h, M = half-day, L = 1–3d, XL = sprint+).

| ID | Finding | Sev × Lev / Effort | Tier |
|---|---|---|---|
| R1 | Add WAL pragma to `db/init.sh:27` | P0 × H / S | 100K |
| R2 | Enforce `max_lanes` semaphore in `bin/mini-ork-execute:262` | P0 × H / S | 100K |
| R3 | Replace `find … \| xargs ls` with depth-capped lookup in `bin/mini-ork-execute:90` | P0 × H / S | 100K |
| R4 | Batch sqlite reads in `lib/auto-merge.sh:170–375` (eliminate N+1 forks) | P0 × H / M | 100K |
| R5 | Replace `ls -d …iter-*/` glob with `find \| while read` in 4 callers | P1 × M / S | 100K |
| R6 | Wire `mo_emit_cache_flags` into `mo_llm_dispatch` at `lib/llm-dispatch.sh:75` | P0 × H / S | 100K |
| R7 | Move reflection pipeline to async background (`&` + bounded wait) | P1 × H / M | 100K |
| R8 | Implement `mo_events → mo_events_archive` sweep (defined in `db/migrations/0002`, never wired) | P1 × M / S | 100K |
| R9 | Enforce `per_epic_usd` / `per_run_usd` via `llm_calls` SUM in `lib/llm-dispatch.sh` | P1 × H / M | 100K |
| R10 | Dialect-aware migration runner (`-- @sqlite:` / `-- @postgres15:` annotations) | P0 × H / L | 1M |
| R11 | Migrate `lib/` + `bin/` to Go `mini-ork-runtime` binary; bash stays as user shim | P0 × H / XL | 1M |
| R12 | Partition `task_runs` + `execution_traces` by `created_at` monthly in Postgres | P0 × H / L | 1M |
| R13 | Move `mo_events` to Kafka; PG holds 30d materialised view | P1 × H / L | 1M |
| R14 | Add OTel span per node dispatch in `lib/llm-dispatch.sh` (W3C traceparent) | P1 × H / M | 1M |
| R15 | Add `tenant_id` to all namespace tables (default `'local'`); shard PG by tenant | P0 × H / L | 10M |
| R16 | Three-tier storage: hot PG / warm PG archive / cold Parquet on S3 (DuckDB query) | P1 × H / L | 10M |
| R17 | Implement shadow promotion phase (`status='shadow'` at `db/migrations/0009:39`) | P0 × H / M | 10M |
| R18 | Rate-limit `lib/group_evolver.sh` proposals: ≤10 candidates/workflow/24h | P0 × H / S | 10M |
| R19 | Fleet-wide rollback via PG LISTEN/NOTIFY on `version_registry_pointers` (`db/migrations/0011:104`) | P1 × H / M | 10M |
| R20 | Per-tenant cost view `v_tenant_cost_24h`; enforce budget gate before dispatch | P1 × H / M | 10M |
| R21 | Recipe marketplace: `add-recipe github.com/<org>/<name>` + cosign signing + seccomp sandbox | P1 × M / XL | 10M |
| R22 | Gate autonomous self-improvement at rung 6 (human review) until Goodhart-drift defense is designed; do not ship rung-7 auto-promote at fleet scale | P0 × H / research | 10M |

**Sequencing:** R1–R9 unblock 100K and do not conflict — ship as a batch. R10–R13 require R1–R9 stable. R14 is orthogonal; ship at any tier. R15–R22 build on R10–R13. The recipe-author contract (`workflow.yaml`, `prompts/*.md`, verifier shell scripts) changes at none of these steps — that is the architectural dividend of the v0.1 design.

The single largest risk is not in R1–R22. The self-improvement loop (`lib/group_evolver.sh` + `lib/promotion_gate.sh`) optimises against a fixed benchmark suite. At 10M/day with no human reviewer in the loop, Goodhart drift — workflows that maximise benchmark utility at the cost of long-tail real-task quality — is inevitable without an explicit defense. R22 holds that gate until the team has a concrete answer.
