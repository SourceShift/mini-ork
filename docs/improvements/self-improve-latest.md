# Synthesis — Recursive Self-Improvement, iter 36

## Ranked patch plan

| Rank | Bottleneck | Category | Patch summary | Evidence | Confidence |
|---|---|---|---|---|---|
| 1 | `dedupe_check` verifier hallucinates `learning_record.signature` (3rd recurrence of planner-LLM-column-hallucination pattern) | correctness | Add a multi-table schema-truth block to the planner prompt covering every table any verifier command may reference (`learning_record`, `llm_calls`, `pattern_records`, `execution_traces`), and replace the `signature` pseudocolumn dedupe with a real `(title, category, iter)` composite key. | `lens-bottleneck.md:30`; `lens-correctness.md:15-35`; `lens-arch.md:21`; `db/migrations/0017_self_improve_learning.sql:30-52`; `bin/mini-ork-plan:144-148`; live probe `Error: no such column: signature`; arXiv `2512.22250`, `2603.23050`, `2605.00628` from `lens-arxiv.md:7-27` | 0.86 |
| 2 | Plan/classify outer-span `trace_write` rows still emit `duration_ms=0`/`cost_usd=0` (91/91 = 100% of `recursive_self_improve` rows in 24h). | perf | Extract `_trace_write_node_rich` from `bin/mini-ork-execute:301-365` into `lib/trace_rich.sh` as `trace_write_node_rich`; replace 11 outer-span call sites in `bin/mini-ork-plan` + `bin/mini-ork-classify` so sidecars (`.last-llm-cost`, `.last-llm-duration-ms`) propagate. | `lens-bottleneck.md:32`; `lens-perf.md:22-46`; `bin/mini-ork-plan:108,307,321,497,505,512,520,527,577`; `bin/mini-ork-classify:113,308`; arXiv `2604.17092`, `2502.06318` (`lens-arxiv.md:55-68`) | 0.81 |
| 3 | Synthesizer ranks 4–5 patches per iter; implementer ships 1; deferred patches evaporate (0 `rank≥1` rows in `learning_record` for any iter ≥ 18). | arch | Add `_promote_synthesis_findings` after `_self_improve_record_success` (`bin/mini-ork-self-improve:182-220`) that parses the ranked-patch table from `synthesis.md` and inserts one `learning_record` row per non-rank-1 patch with `outcome='deferred'`, keyed idempotently on `(run_id, iter, rank, title)`. Gate behind `MO_PROMOTE_SYNTHESIS_FINDINGS=1`. | `lens-bottleneck.md:33`; `lens-arch.md:97-112`; `bin/mini-ork-self-improve:182-220,481-484`; arXiv `2603.10600`, `2512.10696`, `2506.05109` (`lens-arxiv.md:78-99`) | 0.74 |
| 4 | `trace_write … 2>/dev/null \|\| true` idiom at ≥ 31 sites silently swallows schema-drift errors (D-039 recurrence vector). | correctness | Add `trace_write_or_log` wrapper in `lib/trace_store.sh` that captures stderr to `${MINI_ORK_RUN_DIR}/trace-write-errors.log` and propagates the exit code; migrate plan/classify call sites first; leave `\|\| true` only on explicitly-best-effort telemetry paths. | `lens-bottleneck.md:34`; `lens-correctness.md:73-90`; `lib/trace_store.sh:35-50` (D-039 postmortem); arXiv `2604.22028`, `2511.18528` (`lens-arxiv.md:103-116`) | 0.68 |
| 5 | `llm_calls.actor` taxonomy collapse — workflow role / model lane / run-id overload makes `provider_policy_researcher` structurally vacuous. | correctness | Pass `node_type` as `MO_WORKFLOW_ROLE` from `bin/mini-ork-execute:_dispatch_node` to `mo_llm_dispatch_universal`; in `lib/llm-dispatch.sh:519-522` write `actor = "${MO_LANE_ACTOR:-${MO_WORKFLOW_ROLE:-${node_type:-${USER:-unknown}}}}"` and add `metadata_json.workflow_role` + `metadata_json.model_lane`. Update verifier to query `json_extract(metadata_json,'$.workflow_role')='researcher'` with `actor` fallback for back-compat. | `lens-bottleneck.md:31`; `lens-correctness.md:37-71`; `lens-arch.md:75-94`; `bin/mini-ork-execute:402-404,480-483,517-520,573-576`; `lib/llm-dispatch.sh:519-522`; arXiv `2602.10133`, `2604.05119`, `2601.14567` (`lens-arxiv.md:29-51`) | 0.71 |

## Top patch — detailed plan

### Patch 1: schema-truth planner prompt + composite-key dedupe

**Problem statement.** The iter-36 kickoff `plan.json` verifier `dedupe_check` queries `learning_record.signature`, a column that has never existed in the schema (`db/migrations/0017_self_improve_learning.sql:30-52`). `sqlite3` errors out, the `grep -q '^0$'` pipeline produces empty stdout (grep exits 1), and the run-harness `|| true` swallow turns the failure into a vacuous PASS. This is the third recurrence of the same fingerprint in three consecutive iterations: iter-34 invented `llm_dispatch(role)`; iter-35 invented `iteration_id`/`notes`; iter-36 invented `signature`. The planner prompt at `bin/mini-ork-plan:144-148` hard-codes a one-table schema hint for `llm_calls` only.

**Evidence.**
- Scan: `lens-bottleneck.md:30` (full row with file/line refs, live `Error: in prepare, no such column: signature` reproduction)
- Correctness lens: `lens-correctness.md:15-35` (reproduction recipe R1), `lens-correctness.md:174-201` (proposed Fix 1 shape)
- Arch lens: `lens-arch.md:21` (verifier-surface gap analysis), `lens-arch.md:172` ("avoid verifier surfaces that depend on unverified planner-invented schema")
- Schema truth: `db/migrations/0017_self_improve_learning.sql:30-52` — columns are `id, run_id, iter, rank, category, title, evidence_paths, arxiv_refs, patch_summary, outcome, severity, confidence, benchmark_delta, created_at, updated_at`
- Pattern frequency: 3 (iter-34, iter-35, iter-36) — `lens-bottleneck.md:58`
- arXiv evidence (new infra not added; prompt-only change ≠ new infra, but cited for design grounding):
  - `2512.22250` — Hallucination Detection for LLM-based Text-to-SQL Generation via Two-Stage Metamorphic Testing (`lens-arxiv.md:8-13`)
  - `2603.23050` — DBAutoDoc automated schema documentation (`lens-arxiv.md:22-27`)
  - `2605.00628` — EGREFINE execution-grounded schema refinement (`lens-arxiv.md:15-20`)

**Proposed change.**

1. **Rewrite the schema-truth block in `bin/mini-ork-plan:144-148`** to cover every table any verifier command may reference. Generate it at plan time from `PRAGMA table_info` against the live DB so it cannot drift from migrations:

   ```bash
   _mo_plan_schema_truth_block() {
     local db="${MINI_ORK_DB:-/Users/admin/ps/mini-ork/.mini-ork/state.db}"
     local tables=(learning_record llm_calls pattern_records execution_traces)
     printf '\n## Canonical schema (authoritative — every verifier_contract command MUST only reference columns listed here)\n\n'
     for t in "${tables[@]}"; do
       printf '### %s\n' "$t"
       sqlite3 "$db" "PRAGMA table_info(${t});" 2>/dev/null \
         | awk -F'|' '{printf "  - %s (%s)\n", $2, $3}'
       printf '\n'
     done
     printf '### Dedupe key contract\n'
     printf '  - learning_record has NO `signature` column. To dedupe a candidate bottleneck, key on (title, category, iter).\n'
     printf '  - To check whether an iter-N candidate is novel, use: SELECT COUNT(*) FROM learning_record WHERE iter < N AND title = ? AND category = ?;\n'
   }
   ```

   Splice the function output into the planner prompt where the current static hint lives. If `PRAGMA table_info` returns nothing (DB unreachable), fall back to a hard-coded canonical block kept in `bin/mini-ork-plan` as a literal heredoc — never silently emit an empty block.

2. **Replace the `dedupe_check` verifier command at `plan.json` generation time** so future plans use the composite key. The fix lives in the planner prompt (step 1) — the schema-truth block now explicitly prescribes the correct dedupe query — so this is enforced by example. Add one more line to the prompt directives section:

   > When generating any `verifier_contract.checks[].command` that uses `sqlite3`, you MUST reference only columns listed in the Canonical schema block above. Verifier authors who reference an unlisted column will be rejected by the schema preflight (TODO patch 6).

3. **Add a one-shot validator pass in `bin/mini-ork-plan`** *before* writing `plan.json`: for each `verifier_contract.checks[].command` that contains `sqlite3`, regex-extract referenced column tokens of the form `<table>.<col>` plus bare columns in `SELECT … FROM <t>` clauses, then assert each is present in the canonical schema block. On mismatch, log to `${RUN_DIR}/plan-schema-preflight.log` and reject the plan with a structured error. (This is the metamorphic-testing principle from `2512.22250` adapted to shell.) Keep the regex deliberately narrow — false positives here halt the run, so a permissive matcher with an explicit allowlist for `count(*)`, `iif()`, `datetime()`, etc., is safer than a strict parser.

**Regression test.** Add `tests/unit/test_plan_schema_truth.sh` with two assertions:

- **A1 (negative).** A synthetic `verifier_contract` containing `SELECT signature FROM learning_record` must cause `bin/mini-ork-plan` to exit non-zero with stderr containing `schema-preflight: unknown column 'signature' on table 'learning_record'`.
- **A2 (positive).** A synthetic `verifier_contract` containing `SELECT COUNT(*) FROM learning_record WHERE iter < 36 AND title = ? AND category = ?` must pass preflight and be written to `plan.json` unchanged.

Test assertion text (literal): `assert plan-preflight rejects 'no such column: signature' AND accepts composite (title, category, iter) dedupe`.

**Verification.**

- `go vet ./...` — must remain green (no Go changes, but verifier requires it).
- `go test ./...` — must remain green.
- `bash tests/unit/test_plan_schema_truth.sh` — new test must pass.
- Live probe: `sqlite3 /Users/admin/ps/mini-ork/.mini-ork/state.db "PRAGMA table_info(learning_record);" | grep -q signature` must return non-zero (negative control — confirms the column still does not exist and the preflight is doing real work, not vacuously passing).
- Expected benchmark deltas:
  - Iter-37 `lens-bottleneck.md` row "planner LLM hallucinates a column" → resolved or absent. Sign: downward (frequency 3 → 0).
  - Iter-37 `plan.json` `verifier_contract.checks` for `dedupe_check` → references `title`+`category`+`iter`, not `signature`. Sign: structural change, magnitude binary.
  - No expected change to `recursive_self_improve` zero-duration rate (that is Patch 2).

**Rollback criteria.**

- If `PRAGMA table_info` injection makes the planner prompt exceed the model's context window for any tier, revert to the static heredoc fallback only (keep the preflight validator).
- If the preflight validator produces false positives on legitimate aggregate expressions (e.g. `COUNT(DISTINCT actor)`, `json_extract(metadata_json, '$.workflow_role')`), narrow the regex and ship; do not revert the schema-truth block.
- If any of `go vet ./...` or `go test ./...` regresses, revert all changes — no part of Patch 1 should touch Go code, so a regression indicates an unrelated incidental change crept in.
- Hard rollback: if iter-37 surfaces *any* new column-hallucination row, revert and escalate to Patch 6 (full pattern miner) in iter-38.

## Lower-ranked patches

### Patch 2 (perf): hoist `_trace_write_node_rich` into `lib/trace_rich.sh`

**Problem.** 91/91 (100%) of `recursive_self_improve` `execution_traces` rows in the last 24h carry `duration_ms=0`; sidecars `.last-llm-cost` and `.last-llm-duration-ms` are populated but never read by plan/classify outer spans (`lens-perf.md:24-38`).

**Change.** Lift `_trace_write_node_rich` (`bin/mini-ork-execute:301-365`) into `lib/trace_rich.sh` as `trace_write_node_rich`. Parameterize `TASK_CLASS` as a positional arg, not an env read (mitigates `lens-perf.md:120` env-propagation hazard). Replace 11 outer-span call sites in `bin/mini-ork-plan` (108, 307, 321, 497, 505, 512, 520, 527, 577) and `bin/mini-ork-classify` (113, 308). ~50–80 LOC.

**Test.** New benchmark `bench_outer_span_richness` per `lens-perf.md:82-86`: invoke `bin/mini-ork-plan` against a synthetic kickoff, assert `duration_ms > 0 AND cost_usd > 0` on the resulting trace row.

**Verification.** `go vet ./... && go test ./...` green; iter-37 zero-dur rate < 10% (sign: down, magnitude: 100% → ≤ 10%).

### Patch 3 (arch): synthesis → `learning_record` promoter

**Problem.** Synthesizer ranks 4–5 patches; only the rank-0 success stub lands. 0 `rank≥1` rows for any iter ≥ 18 (`lens-bottleneck.md:33`).

**Change.** Add `_promote_synthesis_findings` after `_self_improve_record_success` (`bin/mini-ork-self-improve:182-220`). Parse the `## Ranked patch plan` markdown table from `synthesis.md`, insert one row per rank-≥-2 patch with `outcome='deferred'`. Idempotent on `(run_id, iter, rank, title)`. Gate `MO_PROMOTE_SYNTHESIS_FINDINGS=1`.

**arXiv grounding.** `2603.10600` (trajectory-informed memory), `2512.10696` (procedural memory lifecycle), `2506.05109` (metacognitive learning) — `lens-arxiv.md:78-99`.

**Test.** After a successful iter-36 publish with the flag set, `SELECT COUNT(*) FROM learning_record WHERE iter=36 AND rank>=2` returns ≥ 4.

### Patch 4 (correctness): `trace_write_or_log` wrapper

**Problem.** D-039 recurrence vector: ≥ 31 `2>/dev/null || true` call sites silently swallow `INSERT` failures (`lens-correctness.md:73-90`).

**Change.** Add `trace_write_or_log` in `lib/trace_store.sh` that redirects stderr to `${MINI_ORK_RUN_DIR}/trace-write-errors.log` and propagates the exit code. Migrate `bin/mini-ork-plan` and `bin/mini-ork-classify` call sites first; leave true best-effort callers explicitly `|| true`. Allow `SQLITE_BUSY` (exit 5) under a narrow `|| true` shim.

**Test.** `tests/unit/test_trace_write_failure.sh` per `lens-correctness.md:240-251`: a drifted schema must cause `trace_write_or_log` to exit non-zero.

### Patch 5 (correctness): normalize `llm_calls.actor` to workflow role

**Problem.** `provider_policy_researcher` is structurally vacuous because `actor` collapses workflow role / model lane / run-id (`lens-correctness.md:37-71`, `lens-arch.md:75-94`).

**Change.** Export `MO_WORKFLOW_ROLE="$node_type"` around dispatch in `bin/mini-ork-execute:_dispatch_node`. In `lib/llm-dispatch.sh:519-522`, prefer it for `actor`. Carry `workflow_role` and `model_lane` in `metadata_json` for back-compat. Rewrite the verifier to query `json_extract(metadata_json,'$.workflow_role')='researcher'` with `actor` fallback.

**Test.** After fix, any researcher-lane dispatch must leave at least one row where `json_extract(metadata_json,'$.workflow_role')='researcher'`.

**Note.** Patch 5 is the smallest correctness fix that unblocks the kickoff's own `provider_policy_researcher` check, but the schema-shim approach (metadata_json) is interim. The arch lens (`lens-arch.md:178`) flags first-class `workflow_role`/`model_lane` columns as the longer-term answer; this is left for iter-37+.

## Convergence assessment

**Not yet at diminishing returns.** Three signals say keep going:

1. **Pattern frequency rising, not falling.** The "planner-LLM-hallucinates-a-column" fingerprint is now at frequency 3 across consecutive iters. If Patch 1 lands cleanly, iter-37 should show frequency 0 — a sharp drop that the next-iter bottleneck scanner will register, validating that the loop *can* close a class of bugs.
2. **Structural memory deficit.** 0 `rank≥1` rows for any iter ≥ 18 means the loop has been operating without durable backlog. Patch 3 closes that gap; until it lands, every iter's deduplication is approximate (text-scrape over markdown).
3. **Verifier-surface unverifiability.** Two of the iter-36 kickoff's own verifier checks (`dedupe_check`, `provider_policy_researcher`) are demonstrably vacuous. Until the schema-preflight (Patch 1) and actor normalization (Patch 5) land, the outer loop cannot trust its own success signal.

The outer loop should continue for at least iters 37–39 and re-evaluate convergence after Patches 1, 2, and 3 have shipped. Iter-36 will only ship Patch 1.

## Provenance footer

- Lenses consumed: minimax (perf), kimi (correctness), codex (arch), arXiv research lane
- Synthesizer family: opus (Anthropic) — sole permitted Anthropic-family lane per provider policy
- arXiv papers cited: 14 (`2512.22250`, `2605.00628`, `2603.23050`, `2602.10133`, `2604.05119`, `2601.14567`, `2502.06318`, `2604.17092`, `2604.14531`, `2603.10600`, `2512.10696`, `2506.05109`, `2604.22028`, `2511.18528`) — all sourced from `lens-arxiv.md`
- Cross-iteration learnings applied: 8 rows from `learning_record` (live census in `lens-bottleneck.md:39-57`) plus iter-33/34/35 synthesis markdown text-scrape
- Lens-availability notes:
  - `lens-arch.md` content was emitted to the worktree mirror; orchestrator should promote `.mini-ork/runs/self-improve-iter-36-20260609122707/lens-arch.md` from the worktree if absent at the canonical run path
  - `lens-arxiv.md` same — full content in worktree at `lens-arxiv.md`
