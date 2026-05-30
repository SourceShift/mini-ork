## Codex LLM Dispatch Audit — mini-ork cost + latency scaling

Audit of: `~/ps/mini-ork/`
Files read: `lib/llm-dispatch.sh`, `lib/context_assembler.sh`, `lib/gradient_extractor.sh`,
`lib/reflection_pipeline.sh`, `lib/benchmark_suite.sh`, `lib/promotion_gate.sh`,
`bin/mini-ork-execute`, `bin/mini-ork-eval`, `bin/mini-ork-plan`, `bin/_worker-launcher.sh`,
`lib/lane-helpers.sh`, `lib/cache.sh`, `lib/providers/cl_opus.sh`, `lib/providers/cl_sonnet.sh`,
`config/agents.yaml`

---

### High-leverage cost cuts (>50% savings each)

#### finding-1: Anthropic prompt caching exists in `lane-helpers.sh` but is wired to only 3 of 8+ dispatch paths
**File**: `lib/lane-helpers.sh:71` (definition), `lib/reflection-refiner.sh:114`, `lib/mutation-adversary.sh:113`, `lib/rubric-prescreen.sh:104`, `bin/_worker-launcher.sh:336` (users)
**Cost class**: linear — every missed path pays full input-token price
**Pattern**: `mo_emit_cache_flags` (which emits `--exclude-dynamic-system-prompt-sections` to stabilise the system prompt for Anthropic prefix-cache hits) is only called in `reflection-refiner`, `mutation-adversary`, `rubric-prescreen`, and the worker CLI path. The `gradient_extractor.sh` (`mo_llm_dispatch`), `mini-ork-plan`'s planner call, every `mini-ork-execute` node dispatch (researcher/implementer/reviewer), and `mini-ork-invoke-prompt` all call `mo_llm_dispatch` or `llm_dispatch` with no cache flags — every call sends a unique system prompt and pays full price.
**Optimization**: Thread `mo_emit_cache_flags` into `mo_llm_dispatch` itself (the single call-site, `lib/llm-dispatch.sh:80`) so every caller inherits caching without per-site changes. The flag is already opt-out via `MO_PROMPT_CACHE_DISABLED`. Estimated saving: 60–70% on input tokens for the system+tools prefix (~3 KB per call) for Anthropic-billed lanes (opus, sonnet). At 25 calls/epic on a $5 budget, this is ~$1.50–$3.00 per epic recovered.

---

#### finding-2: `gradient_extract` fires one LLM call per trace — N serial calls instead of one batch
**File**: `lib/reflection_pipeline.sh:45–53`, `lib/gradient_extractor.sh:106`
**Cost class**: linear per-trace — O(N traces) calls
**Pattern**: `reflection_extract_gradients` loops over all trace IDs since `since_ts` and calls `gradient_extract "$tid"` for each inside a sequential `while read` loop. Each call is a fresh `mo_llm_dispatch` invocation (a separate `claude --print` subprocess with 120s timeout). At 100 traces/24h, this is 100 consecutive LLM calls before the reflection step completes.
**Optimization**: Batch all traces into a single prompt: `TRACE BATCH: [trace1_json, trace2_json, ...]  → extract gradients for ALL`. One call, one set of input tokens for the system prefix, N trace payloads as a single user message. The schema is already a JSON array so the model can emit `[[grads_for_t1], [grads_for_t2], ...]`. Estimated saving: ~90% of reflection LLM cost (100 calls → 1 call; only input grows linearly with trace count, which is far cheaper than N separate calls each paying the full system+prompt overhead).

---

#### finding-3: `cl_opus.sh` forces ALL model variants to Opus — sonnet workers billed at Opus rates
**File**: `lib/providers/cl_opus.sh:13–14`
**Cost class**: linear — every subshell sourcing `cl_opus.sh` pays Opus prices even for subagents
**Pattern**: `cl_opus.sh` exports:
```bash
export ANTHROPIC_DEFAULT_SONNET_MODEL=claude-opus-4-7
export ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-opus-4-7
export CLAUDE_CODE_SUBAGENT_MODEL=claude-opus-4-7
```
This means any tool call or subagent spawned INSIDE an Opus-lane worker also runs at Opus price. If a reviewer agent (opus lane) spawns a sub-task via tool use that would normally use sonnet or haiku, it pays Opus rates instead. `cl_sonnet.sh` has the same pattern in reverse (all models pinned to sonnet including `ANTHROPIC_DEFAULT_OPUS_MODEL`).
**Optimization**: Pin only `ANTHROPIC_MODEL` for the primary call. Remove the `ANTHROPIC_DEFAULT_*` and `CLAUDE_CODE_SUBAGENT_MODEL` overrides from provider scripts, or set subagent model to a cheaper tier (`CLAUDE_CODE_SUBAGENT_MODEL=claude-haiku-4-5`). Estimated saving: 30–50% on Opus sessions that internally spawn tool calls, depending on tool-call depth (Opus is ~8× Haiku price; subagents on Haiku for deterministic tool calls = massive savings).

---

#### finding-4: Session-level cache (`lib/cache.sh`) is wired to only 2 of 9 stage types
**File**: `lib/cache.sh` (full file), `lib/mutation-adversary.sh:39`, `lib/rubric-prescreen.sh:43`
**Cost class**: linear — repeated runs with identical inputs re-fire the LLM
**Pattern**: `lib/cache.sh` implements a full session-reuse cache keyed by `(epic_id, iter, stage, input_hash)` with 30-day TTL, `mo_cache_lookup` / `mo_cache_emit`, and a `mini_orch_cache_stats` view tracking `dollars_saved`. This cache is ONLY wired in `mutation-adversary.sh` and `rubric-prescreen.sh`. The 7 other stage types — `spec-author`, `spec-reviewer`, `bdd-runner`, `reflection-refiner`, `worker`, `reviewer`, `gradient_extract` — bypass the cache entirely and fire fresh LLM calls on every re-run.
**Optimization**: Add `mo_cache_lookup` / `mo_cache_emit` wrappers to each remaining stage handler. The hash bundle is cheap to compute (`mo_cache_hash_bundle kickoff_path feedback_path`). For deterministic stages (spec-reviewer, rubric, reflection-refiner), cache hit rates in re-runs will be >80%. Estimated saving: 40–60% on re-runs of partially-failed epics (the most common re-run scenario).

---

### Medium-leverage improvements (15–40% savings)

#### finding-5: `budget: per_epic_usd: 5.00` in `agents.yaml` is never checked before an LLM call
**File**: `config/agents.yaml:29–31`, `lib/llm-dispatch.sh` (no check), `bin/mini-ork-execute` (no check)
**Cost class**: unbounded — budget overruns accumulate until post-hoc review
**Pattern**: `agents.yaml` declares `budget.per_epic_usd: 5.00`, `per_run_usd: 0.50`, `daily_cap_usd: 50.00`. These values are read by nothing at call time. `mo_emit_budget_flag` in `lane-helpers.sh` uses *per-stage* env var defaults (`MO_REFLECTION_BUDGET_USD`, `MO_RUBRIC_BUDGET_USD`, etc.) that are hardcoded in each handler — the per-epic and daily caps from `agents.yaml` are load-bearing config that is silently ignored. A runaway reflection loop or benchmark run can blow past the epic budget with no circuit breaker.
**Optimization**: Add a pre-call budget check in `mo_llm_dispatch`: read `agents.yaml` (or a DB-cached version of it), sum `cost_usd` from `mini_orch_sessions` WHERE `epic_id = $epic`, and abort with exit 2 if accumulated cost exceeds `per_epic_usd`. Emit a warning at 80% of budget. Estimated benefit: prevents cost storms; at scale the primary guardrail against a looping agent spending $50 in one epic.

---

#### finding-6: `context_assemble` default budget is 64K tokens but truncation is last-item-pop (O(N²) worst case and context-bloated)
**File**: `lib/context_assembler.sh:35`, lines 171–181
**Cost class**: linear — 64K tokens in every LLM call even when 10K would suffice
**Pattern**: `MINI_ORK_CTX_BUDGET_TOKENS` defaults to 64,000 (~$0.18 per call at Opus pricing for input alone). The truncation is a pop-from-end loop: it removes one item, re-serializes the full pack, re-estimates tokens, checks budget, repeats. For a 10-item prior_runs list, this is 10 full JSON re-serializations. More importantly, there is no tiering: task_brief + verifier_contract + ALL prior_runs + ALL failure_modes ship in every context, regardless of whether the node type (researcher vs verifier) actually needs them all.
**Optimization**: (1) Drop budget default to 32K (sufficient for most nodes; overridable). (2) Make context selective per `workflow_node`: a `verifier` node needs `verifier_contract` and `known_failure_modes` but not `prior_similar_runs`; an `implementer` needs prior_runs but not failure_modes. Node-type routing is already known at assembly time. Estimated saving: 30–50% input cost on non-planner nodes.

---

#### finding-7: `mini-ork-execute` calls an undefined `llm_dispatch` (no `mo_` prefix) — silent fallback to PATH binary
**File**: `bin/mini-ork-execute:170,185,200`, `bin/mini-ork-plan:190`, `bin/mini-ork-invoke-prompt:67`
**Cost class**: correctness bug disguised as a cost issue
**Pattern**: `_require_lib llm-dispatch` sources `lib/llm-dispatch.sh`, which defines `mo_llm_dispatch`. But the callers in `execute`, `plan`, and `invoke-prompt` call the bare name `llm_dispatch` (no `mo_` prefix). No alias or wrapper maps `llm_dispatch → mo_llm_dispatch`. In a shell with no `llm_dispatch` binary in PATH, these calls silently fail (`command not found` captured into `RESULT`) or fall through to whatever PATH provides.
**Optimization**: Add `llm_dispatch() { mo_llm_dispatch "$@"; }` at the bottom of `lib/llm-dispatch.sh`, OR rename all call sites to `mo_llm_dispatch`. Either is a one-line fix. Without this fix, `mini-ork-plan` and `mini-ork-execute` are effectively no-ops when invoked via the public API rather than through `_worker-launcher.sh`. (Worker launcher does NOT use `llm_dispatch` — it calls `claude` directly, which is why it works today.)

---

#### finding-8: `speculative` dispatch mode fires ALL nodes and waits for all — no early exit on first success
**File**: `bin/mini-ork-execute:295–306`
**Cost class**: wasteful — pays for N parallel calls when 1 would suffice
**Pattern**: The `speculative` case comment says "first success stops remaining" but the implementation just runs all in parallel and waits for all (`wait "$pid" || true`). There is no kill-on-first-success logic. All N node calls complete (and are billed) regardless of which one finishes first.
**Optimization**: After each `wait "$pid"`, check exit code; on first success, `kill "${remaining_pids[@]}" 2>/dev/null; wait` to reclaim the un-needed calls. Estimated saving: (N-1)/N of speculative LLM cost when the first candidate succeeds (often the common case).

---

#### finding-9: Gradient extraction prompt embeds the full trace JSON inline — no size gate
**File**: `lib/gradient_extractor.sh:101`
**Cost class**: linear — input cost scales with trace size, unbounded
**Pattern**: `prompt="${_GRADIENT_EXTRACTOR_PROMPT_TEMPLATE/<<<TRACE_JSON>>>/${trace_json}}"` — the full trace JSON is spliced directly into the prompt string. A long trace from a multi-hour worker run can be hundreds of KB. There is no truncation of `trace_json` before embedding, and no check against the 200K-token context limit. Beyond cost, this risks silent truncation mid-JSON which produces parse failures (the extractor has a fallback that emits `[]` silently).
**Optimization**: Pre-truncate `trace_json` to a summary before embedding: extract only `{status, task_class, duration_ms, cost_usd, final_artifact_ref, last_N_tool_calls}`. A 500-token summary is enough for gradient extraction; the full trace adds noise. Estimated saving: 70–90% input cost per gradient call on long traces.

---

#### finding-10: No provider fallback — `cl_deepseek.sh` silently redirects to GLM but failure modes are unhandled
**File**: `bin/_worker-launcher.sh:46`
**Cost class**: availability risk (latency spike on provider outage becomes full session failure)
**Pattern**: `deepseek` agent is mapped to `$_DS_FALLBACK` (default `glm`) in `_worker-launcher.sh`. That is one hop. If `glm` is also unavailable (network issue, API quota, bad credentials), the worker exits with FATAL — no second-tier fallback to `sonnet` or `minimax`. The `mo_llm_dispatch` function itself has no retry or fallback logic: on any non-zero exit from `claude --print`, it returns the error code and the caller propagates failure.
**Optimization**: Add a two-tier fallback in `mo_llm_dispatch`: on non-zero exit, retry once with exponential backoff (5s, 10s), then attempt an alternative model from the same "free" tier (glm → kimi → minimax) before escalating to paid lanes. For paid lanes (opus, sonnet): one retry with backoff is enough. This prevents transient API failures from burning the full epic timeout.

---

### Architectural changes for 10x scale

#### arch-1: Model-tier router based on task complexity

The `agents.yaml` lanes are statically assigned by node-type (planner=opus, researcher=sonnet). At 10M tasks/day, every `researcher` call at sonnet pricing regardless of task complexity is wasteful. Add a lightweight pre-call classifier (a single haiku call or rule-based on task_class + context_pack size) that downgrades simple tasks:

```
task_complexity_score → [low | medium | high | critical]
low     → haiku  (classification, dedup, simple lookup)
medium  → sonnet (standard implementation, research)
high    → sonnet + extended budget
critical → opus  (conflict resolution, final arbitration only)
```

The complexity signal is already partially available in `context_pack.known_failure_modes` (count) and `context_pack.prior_similar_runs` (success rate). A rule-based router adds zero LLM cost while cutting Opus usage by 60–80% on large-scale runs. Sonnet is 5–8× cheaper than Opus per token.

---

#### arch-2: Semantic cache layer above the hash cache

`lib/cache.sh` uses exact SHA-256 hash matching (`mo_cache_hash_bundle`). Two kickoff files that differ by only a comment, a date in the header, or a whitespace change will produce different hashes and miss the cache. At scale, this means near-identical tasks (common in batch feature development) pay full LLM cost every time.

Add a semantic similarity layer: before firing `mo_llm_dispatch`, embed the context_pack with a cheap embedding call (or use a local hash of the task_class + normalized brief text stripping timestamps). Look up top-K nearest cache entries. If cosine similarity > 0.95, return the cached output directly. If 0.85–0.95, return cached output with a "verify-before-use" flag. This requires adding `embedding_hash` to `mini_orch_sessions` and a sqlite FTS or vector extension — or an external Redis + pgvector store at 10M scale.

Estimated saving at 10M tasks: 30–50% cache hit rate on similar tasks, translating to $X × 0.40 in avoided LLM calls (where X is total spend).

---

#### arch-3: Batch reflection — collect N traces, emit one LLM call

As noted in finding-2, `reflection_extract_gradients` is the primary reflection cost driver. The architectural fix is to change the abstraction: instead of `gradient_extract(trace_id) → gradients`, add a `gradient_extract_batch(trace_ids[]) → {trace_id: gradients[]}` path that packs all traces into a single user-turn message and parses the structured response.

The gradient schema is already typed (`target`, `signal`, `suggested_change`, `confidence`) and the model can handle multi-trace batches cleanly if the prompt structure is clear:

```
TRACE BATCH (3 traces):
--- TRACE trace-abc (task_class: code-fix, status: failure) ---
{ ... }
--- TRACE trace-def (task_class: code-fix, status: success) ---
{ ... }

For EACH trace, output a JSON object:
{"trace_id": "...", "gradients": [{target, signal, suggested_change, confidence}, ...]}
Output a JSON array of these objects. No prose.
```

At 100 traces/24h → 1 call instead of 100. At 1M tasks/day with 1% failure rate → 10K traces → still tractable in ~100 batches of 100. The only tradeoff is that a single bad parse kills all gradients in a batch (mitigate with per-trace extraction as fallback when `batch_size > 1`).

---

### What's already right

**Prompt cache flag infrastructure exists and works** (`lib/lane-helpers.sh:mo_emit_cache_flags`). The `--exclude-dynamic-system-prompt-sections` flag is the correct mechanism for stabilising the Claude Code CLI system prompt. Where it IS wired (worker CLI path, reflection-refiner, mutation-adversary, rubric-prescreen), it works correctly. The cache-stats aggregator (`mo_aggregate_cache_stats`) provides per-iter visibility into hit rates and estimated savings — good operational instrumentation.

**Session-level memoization schema is solid** (`lib/cache.sh`). The `mini_orch_sessions` table with `(epic_id, iter, stage, input_hash)` composite key, 30-day TTL, GC, reuse counter, and `dollars_saved` view is well-designed. It needs broader adoption (finding-4) but requires no schema changes.

**Free-lane detection is correct** (`lib/lane-helpers.sh:mo_lane_is_free`). Suppressing `--max-budget-usd` for glm/kimi/minimax prevents "budget exceeded" errors on lanes that don't expose Anthropic billing — a real edge case that would otherwise break dispatches silently.

**Model-tier assignment in `agents.yaml` is mostly correct**. Opus is reserved for planner, reviewer, reflector (arbitration roles). Sonnet handles researcher, implementer, verifier, publisher (bulk roles). DeepSeek/GLM handle decomposer (cheap structured output). The tier separation is architecturally sound — the problem is the override in `cl_opus.sh` that collapses the tiers at runtime (finding-3).

**`budget_flag` per stage is sound** (`mo_emit_budget_flag`). Hard per-call USD caps via `--max-budget-usd` prevent runaway single-call cost. The individual stage env vars (`MO_REFLECTION_BUDGET_USD=0.40`, `MO_RUBRIC_BUDGET_USD=0.60`, `MO_MUTATION_BUDGET_USD=1.20`) are reasonable defaults and are overridable. This is better than no cap at all.
