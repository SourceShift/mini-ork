---
title: mini-ork Scalability Audit — v0.1.1 → fleet-scale
feature: framework
doc_type: audit
status: active
version: 1.0
last_updated: 2026-05-30
audience: agent+human
---

# mini-ork Scalability Audit

> **Self-audit by design.** This audit's *intent* is for mini-ork to audit
> itself via `mini-ork run refactor-audit <kickoff>`. v0.1.1's real-LLM
> dispatch path has a known blocker (finding **D-007** below: `llm_dispatch`
> bare-name in `bin/mini-ork-plan|execute|invoke-prompt` does not resolve
> to `mo_llm_dispatch`). Pending that fix, this audit was **composed via
> the Agent tool with 4 model-lens stances** (GLM/Kimi/Codex/Opus); the
> outputs are captured in `/tmp/sc-{glm,kimi,codex,opus}-*.md` and
> synthesized below. The companion `recipes/refactor-audit/` recipe ships
> in this commit so the next pass *can* run via `mini-ork run`.

**Scope.** ~/ps/mini-ork at SHA `bc0811a` (v0.1.2 — post-tests+security).
145 source files / 13 sqlite migrations / 13 framework primitives /
9 bin entrypoints / 2 recipes.

**Method.** 4 parallel audits by stance, each producing a `/tmp/sc-*.md`
report; one synthesis pass (this doc) cross-ranks findings by
**(severity × leverage / effort)** and assigns each to a v0.x release
bucket.

**Top-line.** 31 findings synthesized across 4 stances. **0 blocking
production today** (v0.1.1 is right-sized for the 1K-tasks/day, single-dev
workload it ships for). **9 P1 issues** must close before 100K/day on a
single server. **17 P2 architectural shifts** unlock 1M-10M/day across a
fleet. **5 advisory items** for v1.0 polish.

---

## Severity × leverage matrix

```
                    HIGH leverage         MED leverage          LOW leverage
                  ─────────────────    ─────────────────    ─────────────────
P0 (NOW)         │       —              │       —              │       —
                 │   (no v0.1.1 blocker)
                 │
P1 (v0.2)        │  D-007 llm_dispatch  │  G-009 WAL pragma   │  G-016 budget caps
                 │   bare-name resolve   │  K-04 trace daemon   │  G-022 max_lanes
                 │  K-01 batch gradient  │  G-006 trace TTL     │  G-007 audit_log archive
                 │  D-002 batch reflect  │
                 │
P2 (v0.3 / 1.0)  │  O-R1 dialect-aware  │  K-11 ETs index +    │  K-02 auto-merge
                 │    migrations         │      archive          │      parameterize
                 │  O-R4 Go runtime      │  K-10 benchmark      │  K-05 context cache
                 │  O-R8 PG shard tenant │      parallel        │  K-12 async git blame
                 │  O-R11 ML clustering  │  D-001 prompt cache  │
                 │                       │  D-005 budget gate   │
                 │
P3 (advisory)    │       —              │  D-003 cl_opus.sh    │  G-12,13,14,15
                 │                       │      collapse           ls→find
                 │                       │  G-005 N+1 scope     │  K-07 plan-content
                 │                       │      pairs              cache
```

**Legend:** `G-*` = GLM tactical scan; `K-*` = Kimi code-level refactor;
`D-*` = Codex LLM-dispatch deep-dive; `O-R*` = Opus architectural-shape.

---

## P1 — v0.2 release blockers (close before 100K/day)

These prevent the framework from operating cleanly at 100K runs/day on a
single server. Each is bounded effort (≤2 weeks total).

| ID | Title | Source | Fix sketch | Effort |
|---|---|---|---|---|
| **D-007** | `llm_dispatch` bare-name silently fails in plan/execute/invoke-prompt | Codex | One-line shim: `llm_dispatch() { mo_llm_dispatch "$@"; }` at end of `lib/llm-dispatch.sh` | 15 min |
| **K-01** | Gradient extraction = N serial LLM calls (one per trace) | Kimi | Batch all traces in one prompt; one `mo_llm_dispatch` per reflection run not N | 2 h |
| **D-002** | Reflection pipeline serial (same root cause as K-01) | Codex | Same fix as K-01 (Codex confirms ~90% cost cut on reflection step) | (covered by K-01) |
| **G-009** | `db/init.sh` never sets `PRAGMA journal_mode=WAL` | GLM | Add as first SQL in init.sh | 5 min |
| **K-04** | `trace_write` forks python3 per call (1.5M forks/day @ 100K) | Kimi | Named-pipe writer daemon batching via `executemany` | 4 h |
| **G-006** | `execution_traces` has no TTL, archive, or cleanup | GLM | `lib/archive_traces.sh` + nightly cron + `idx_et_task_class_created` | 3 h |
| **G-007** | `mo_events_archive` defined but never populated | GLM | INSERT-then-DELETE sweep trigger or cron script | 2 h |
| **G-016** | `agents.yaml` budget caps declared but never enforced | GLM + Codex | In `mo_llm_dispatch`, query `SUM(cost_usd)` per epic before each call; abort on cap | 2 h |
| **G-022** | `max_lanes: 4` config never consumed by dispatcher | GLM | Read `max_lanes` in `bin/mini-ork-execute` parallel mode; cap with semaphore | 2 h |
| **D-008** | execute reads node DAG from `plan.json.decomposition[]` instead of `workflow.yaml.nodes[]` — workflow.yaml is design-doc-only, never dispatched | dogfood-run | Make execute parse workflow.yaml directly for node-type/model-lane/prompt-ref; treat plan.json as runtime params not topology | 4 h |
| **D-008b** | Planner LLM emits `decomposition[].node_type=""` (empty string) → `.get('node_type', 'implementer')` fallback skipped because key exists → all 7 nodes log `[warn] unknown node_type=` and skip silently | dogfood-run | Strengthen planner prompt to REQUIRE explicit node_type per step + post-process validation that rejects plan if any decomposition entry has empty node_type | 1 h |
| **D-009** | `task_runs.cost_usd` never updated from `llm_dispatch` cost reports — billing visibility broken (audit run showed cost_usd=0.0 despite firing the planner LLM call) | dogfood-run | In mini-ork-plan + mini-ork-execute, after each successful llm_dispatch, UPDATE task_runs SET cost_usd = cost_usd + <call_cost> WHERE id = $MINI_ORK_RUN_ID | 1.5 h |
| **D-010** | Classifier picks first lex-matching task_class instead of best-match — when 3 classes hit on the same kickoff, lex order (alphabetical filename) wins instead of keyword-hit count. Required tactical workaround for this dogfood run: rename `refactor-audit.yaml` → `0-refactor-audit.yaml` so it sorts first | dogfood-run | Rank task_class matches by `hit_count` (number of matching keywords/regex from `matches.{keywords,regex}`); pick highest; tiebreak by filename lex | 1.5 h |

**Total v0.2 effort:** ~24 hours (4 new findings from real dogfood add
8h on top of the original 16h). Each is independent; can ship as
13 separate commits or one bundled v0.2 release.

### Dogfood signal (these 4 findings came from THIS audit's own meta-run)

A real `mini-ork run refactor-audit kickoffs/scale-refactor-mini-ork.md`
was attempted with the v0.1.2 framework + D-007 shim. Result:

- classify → ✓ (after D-010 workaround: rename to lex-first)
- plan → ✓ once (planner LLM emitted valid JSON) / ✗ once (planner LLM
  omitted `verifier_contract.checks` and got rejected — confirms LLM
  output is non-deterministic; need retry or JSON-mode enforcement)
- execute → emitted 7 `[warn] unknown node_type=` lines for 7 workflow
  nodes; all skipped (D-008 + D-008b — workflow.yaml not parsed, plan.json
  decomposition emitted empty node_types)
- verify → no artifact to verify; passthrough
- Cost: $0.00 reported in `task_runs.cost_usd` despite a real planner LLM
  call having fired (D-009 — cost not propagated)
- Net: 7 lens nodes intended; 0 actually dispatched. Audit content NOT
  produced via mini-ork dispatch; the Agent-tool composition (this doc's
  31 original findings) remains the only audit deliverable for v0.1.2.

**The dogfood ITSELF is the audit's strongest signal:** real
self-dispatch surfaces 4 framework gaps that Agent-tool composition
missed. The meta-loop closes when these 4 P1s + the original 9 P1s ship
as v0.2 — at which point a second dogfood run produces audit content,
not just bug signal.

---

## P1 — security followups (separate from scale audit)

From `docs/SECURITY-AUDIT.md` v0.1.1 — re-listed here so the v0.2 bucket
is complete:

- **P3-009** Parameterize 9 SQL-interpolation sites in `lib/auto-merge.sh`
  + `lib/cache.sh` (recipe-internal; defense-in-depth)
- **P3-001** `state.db` 644 → 600 in `db/init.sh`
- **K-02** Same as P3-009 (Kimi found the same sites independently —
  consensus signal)

---

## P2 — v0.3 architectural shifts (100K → 1M/day)

The framework's substrate stops being right around 1M runs/day. The
**book's universal-loop contract survives** (this is what v0.1's redesign
earned); only substrate changes.

| ID | Title | Source | Fix sketch | Effort |
|---|---|---|---|---|
| **O-R1** | Dialect-aware migrations (sqlite ↔ postgres15) | Opus | Annotate migrations with `-- @sqlite:` / `-- @postgres15:` line directives + `mini-ork-runtime migrate --dialect <kind>` | 2 weeks |
| **O-R5** | Move `reflection_pipeline` to a separate fleet worker | Opus | Replace inline call from `bin/mini-ork-execute` with PG `LISTEN/NOTIFY` or Hatchet task; reflection becomes async | 1 week |
| **O-R6** | Partition `task_runs` + `execution_traces` by `created_at` monthly | Opus | Schema migration + cutover script | 3 days |
| **O-R7** | Promote `mo_events` to Kafka; PG = 30-day materialised view | Opus | New `events_publisher` lib + reader; cutover behind feature flag | 2 weeks |
| **K-10** | `benchmark_run` parallelize via ThreadPoolExecutor | Kimi | `concurrent.futures.ThreadPoolExecutor(max_workers=8)` over benchmark_tasks | 1 h |
| **K-11** | `execution_traces` archive + missing index | Kimi | (already covered by G-006 above — kimi confirms) | (covered) |
| **D-001** | Wire Anthropic prompt caching into `mo_llm_dispatch` central path | Codex | Hoist `mo_emit_cache_flags` call from per-stage to central; 60-70% input-cost cut | 2 h |
| **D-005** | Per-epic budget gate in `mo_llm_dispatch` | Codex | (same as G-016 above — promote from P1 if not done) | (covered) |
| **K-05** | `context_assembler` 5-minute TTL cache | Kimi | `mini_orch_cache` hash-keyed by `(task_class, node, budget)` | 1.5 h |
| **K-12** | `_mo_capture_reflection` async fire-and-forget | Kimi | Move `git blame` to background subshell; primary write completes instantly | 45 min |

**Total v0.3 effort:** ~6 weeks. This is the substrate-swap milestone.

---

## P2 — v1.0 fleet-scale (1M → 10M/day)

| ID | Title | Source | Notes |
|---|---|---|---|
| **O-R4** | Migrate `lib/` + `bin/` runtime to **Go**; recipe surface stays shell | Opus | Hybrid bash-shim + Go runtime; preserves recipe-author surface; ~14-20 eng-wks |
| **O-R8** | Shard PG by `tenant_id` (~100 tenants/DB) | Opus | New TEXT column on every namespace table; default `'local'` for back-compat |
| **O-R9** | Tier storage: hot (PG ≤30d), warm (PG archive ≤180d), cold (Parquet on S3) | Opus | DuckDB or `pg_parquet` for cold-restore |
| **O-R10** | Recipe marketplace + signing | Opus | Extends security P3-007; GitHub URLs + signature verification |
| **O-R11** | SQL pattern emergence → ML clustering | Opus | sentence-transformers/all-MiniLM-L6-v2 + HDBSCAN; nightly cron |
| **O-R23** | Async reflection workers | Opus | Already started in O-R5; this is the production version |
| **D-arch-1** | Model-tier router — classify task complexity, route haiku/sonnet/opus | Codex | Up to 60% cost cut at scale; rule-based today, ML at v1.5 |
| **D-arch-2** | Semantic cache above SHA hash cache | Codex | Embedding similarity ≥0.95 → cache hit; sqlite-vec or external vector DB |

**Total v1.0 effort:** ~30-40 eng-wks (book Ch 32 phasing applies —
phase N+1 not until phase N produces stable signal).

---

## P3 — Advisory (defense-in-depth + polish)

| ID | Title | Source | Notes |
|---|---|---|---|
| **G-012,13,14,15** | `ls -d iter-*/` patterns hit `ARG_MAX` at scale | GLM | Replace with `find ... -maxdepth N -name 'iter-*' \| sort` |
| **G-005** | O(N²) scope-overlap pairwise check | GLM | Cache `git ls-files` outside inner loop |
| **D-003** | `cl_opus.sh` forces ALL model slots to Opus (sub-agents billed Opus) | Codex | Pin only `ANTHROPIC_MODEL`; let subagent tier default to haiku |
| **K-07** | Redundant `cat $PLAN_PATH` reads per subshell node | Kimi | Read once, export `PLAN_CONTENT_CACHED` |
| **D-008** | Speculative dispatch waits all PIDs instead of kill-on-first-success | Codex | After each `wait`, kill remaining on first success |

---

## The "hardest open question" (Opus §7)

**Goodhart's law on the promotion gate.** As `lib/utility_function.sh`
becomes the optimization target at fleet scale, candidates will be
proposed that maximize `U` without actually improving downstream task
outcomes — Goodhart-style. Three mitigations sketched in
`/tmp/sc-opus-architecture.md` §7:

1. **Adversarial benchmark generation** — periodically mutate
   `benchmark_tasks` with constraint-violating variants that should not
   pass; candidates that score well on the originals AND on adversarials
   are stronger
2. **Shadow-traffic-as-verdict** — a candidate routes 5% of real
   production traffic before promotion; real user outcomes (not just
   verifier pass-rate) determine promotion
3. **Conservative drift detection** — alert if `U` rises by >10% per
   evolution cycle and the underlying error rate did not fall in real
   traffic

**Recommendation: do NOT auto-promote at autonomy ladder rung 7 until
this question is resolved with literature review + chosen mitigation.**
The PromotionGate must require human approval until then. This is the
load-bearing safety axiom that turns v0.3 → v1.0 not into a black box.

---

## How to actually run this audit again (the dogfood path)

**Once v0.2 ships finding D-007:**

```bash
cd ~/ps/mini-ork
# 1. The kickoff that started this audit:
cat kickoffs/scale-refactor-mini-ork.md

# 2. Dispatch via mini-ork itself:
mini-ork run refactor-audit kickoffs/scale-refactor-mini-ork.md

# 3. The output lands at docs/refactor/SCALABILITY-AUDIT-<run-id>.md
#    plus task_runs row, plus execution_traces fanout, plus gradients in
#    textual_gradients for the next reflection cycle.
```

This is the meta-payoff of the framework. The audit becomes a recurring
artifact rather than a one-shot doc.

---

## References

- 4 model-lens audit reports:
  - `/tmp/sc-glm-findings.md` — 25 tactical bottlenecks (GLM stance)
  - `/tmp/sc-kimi-refactors.md` — 12 code-level refactors with diffs (Kimi)
  - `/tmp/sc-codex-llm.md` — 10 LLM-dispatch cost cuts (Codex)
  - `/tmp/sc-opus-architecture.md` — 27 numbered architectural recs (Opus)
- `recipes/refactor-audit/` — the recipe for next-time self-dispatch
- `kickoffs/scale-refactor-mini-ork.md` — the canonical kickoff that
  reproduces this audit
- `docs/SECURITY-AUDIT.md` — companion security audit (v0.1.1)
- `ideal-mini-orch-self-evolving-system-book.md` — architectural
  source-of-truth referenced by Opus stance

## Lineage

- v0.1.1 (2026-05-30): initial audit; 31 findings synthesized;
  audit-as-recipe (`recipes/refactor-audit/`) shipped for future
  self-dispatch
- Next audit: schedule via `recipes/refactor-audit/` on every v0.x
  release tag, OR on-demand when the rate-of-change of `audit_log`
  exceeds threshold (signal that complexity has grown)
