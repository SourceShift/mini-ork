# Codex Lens — LLM Dispatch & Cost Deep-Dive
## mini-ork v0.1.1 · Audit run 2026-05-30

> **Scope:** LLM dispatch patterns, model-tier routing, prompt caching, context window
> efficiency, retry loops, and unused output tokens.
> **Files audited:** `lib/llm-dispatch.sh`, `lib/context_assembler.sh`,
> `lib/gradient_extractor.sh`, `lib/reflection_pipeline.sh`, `lib/cache.sh`,
> `lib/lane-helpers.sh`, `lib/rubric-prescreen.sh`, `lib/reflection-refiner.sh`,
> `lib/mutation-adversary.sh`, `bin/_worker-launcher.sh`, `bin/mini-ork-plan`,
> `bin/mini-ork-execute`, `config/agents.yaml`, `lib/providers/cl_opus.sh`,
> `lib/providers/cl_sonnet.sh`
>
> **Pricing assumptions:** Opus 4.7 $15/$75 per M tokens in/out; Sonnet 4.6
> $3/$15; Haiku 4.5 $0.25/$1.25. Baseline: 1 task_run = 1× planner (Opus) +
> 1× worker (Sonnet, 30-min) + 1× reviewer (Opus) + 1× gradient-extract (Sonnet).
> **Effort tiers:** S < 2h, M = half-day, L = 1–3 days.

---

## C-001 · Prompt Caching Wired to Only 4 of 9+ Dispatch Paths

**File:** `lib/lane-helpers.sh:71` (definition), `lib/reflection-refiner.sh:114`,
`lib/mutation-adversary.sh:113`, `lib/rubric-prescreen.sh:104`,
`bin/_worker-launcher.sh:336` (wired callers); `lib/llm-dispatch.sh:75-99`,
`bin/mini-ork-plan:190`, `bin/mini-ork-execute:204,219,234` (unwired)
**Effort:** S

`mo_emit_cache_flags` emits `--exclude-dynamic-system-prompt-sections` to stabilise
the system prompt for Anthropic prefix-cache hits. It is wired in reflection-refiner,
mutation-adversary, rubric-prescreen, and the worker CLI path only. Every
`mo_llm_dispatch` call (planner, researcher, implementer, reviewer, gradient-extract)
sends a unique system prompt per worktree and pays full input-token price on the
~3 KB system+tools prefix each call.

```bash
# BEFORE — lib/llm-dispatch.sh:75-99 (no cache flags in subshell)
(
  source "$cl_script"
  claude \
    --print \
    --permission-mode bypassPermissions \
    --output-format text \
    --max-turns "$max_turns" \
    "$prompt"
)

# AFTER — thread mo_emit_cache_flags into mo_llm_dispatch so every caller inherits it
(
  source "$cl_script"
  local _cache=()
  [ -f "$MINI_ORK_ROOT/lib/lane-helpers.sh" ] && \
    source "$MINI_ORK_ROOT/lib/lane-helpers.sh" 2>/dev/null && \
    mo_emit_cache_flags _cache 2>/dev/null || true
  claude \
    --print \
    "${_cache[@]}" \
    --permission-mode bypassPermissions \
    --output-format text \
    --max-turns "$max_turns" \
    "$prompt"
)
```

System+tools prefix ≈ 750 tokens. Cache-read cost 0.1× vs full. At Sonnet rates,
3 dispatches/run: saves $0.002 × 3 = **$0.006/run**. On Opus paths (planner +
reviewer): saves $0.015 × 2 = **$0.030/run**.
- **$0.036 per run · $36/1K-runs · $36,000/1M-runs**

---

## C-002 · Gradient Extractor Fires One LLM Call per Trace — O(N) Serial Calls

**File:** `lib/gradient_extractor.sh:104-106`, `lib/reflection_pipeline.sh:44-53`
**Effort:** M

`reflection_extract_gradients` loops over all trace IDs since `since_ts` and fires
a fresh Sonnet call per trace. No idempotency guard prevents re-extraction of
traces already processed.

```bash
# BEFORE — lib/gradient_extractor.sh:104-106 (one call per trace, no cache)
local model="${MINI_ORK_GRADIENT_MODEL:-sonnet}"
if ! mo_llm_dispatch "$model" "$prompt" "$tmp_out" 120 5; then

# AFTER — idempotency guard + batch extraction
gradient_extract() {
  local trace_id="$1"
  # Skip already-extracted traces
  local existing
  existing=$(python3 -c "
import sqlite3,sys
c=sqlite3.connect(sys.argv[1])
r=c.execute('SELECT COUNT(*) FROM gradient_records WHERE evidence=?',(sys.argv[2],)).fetchone()
c.close(); print(r[0])" "${MINI_ORK_DB}" "$trace_id" 2>/dev/null || echo 0)
  [ "$existing" -gt 0 ] && return 0
  # ... rest of existing dispatch ...
}
```

Long-term: batch all traces into one prompt (arch-3 in this report). Each Sonnet
call ≈ $0.015 (3K in + 200 out tokens). At 10 traces/run on average:
- Without guard: $0.15/run in gradient extraction alone
- With idempotency guard (re-runs): ~$0 on hits, $0.015 on first-time only
- **$0.135 per run saved on re-runs · ~$135/1K-runs · ~$135,000/1M-runs**

---

## C-003 · `cl_opus.sh` Forces ALL Model Variants to Opus — Sub-Agents Billed at Opus Rates

**File:** `lib/providers/cl_opus.sh:12-14`, `lib/providers/cl_sonnet.sh:12-13`
**Effort:** S

Every provider script overrides ALL three tier aliases to its own model. When
`cl_opus.sh` is sourced, any sub-tool call or sub-agent that would normally use
Sonnet or Haiku runs at Opus 4.7 prices ($15/M in vs $0.25/M for Haiku = 60×).

```bash
# BEFORE — cl_opus.sh:10-14 (all aliases forced to Opus)
export ANTHROPIC_MODEL=claude-opus-4-7
export ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-7
export ANTHROPIC_DEFAULT_SONNET_MODEL=claude-opus-4-7   # ← promotes all sub-calls
export ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-opus-4-7    # ← promotes all sub-calls
export CLAUDE_CODE_SUBAGENT_MODEL=claude-opus-4-7       # ← promotes sub-agents

# AFTER — pin only the primary alias; restore correct sub-tiers
export ANTHROPIC_MODEL=claude-opus-4-7
export ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-7
export ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-6
export ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-4-5-20251001
export CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-4-6
```

A reviewer session (Opus, 30 turns) typically spawns ~7 sub-calls (JSON checks,
file reads, structured assertions) that should be Haiku-class. Each pays Opus
rates instead: 7 × 500 tokens × ($15/M − $0.25/M) = **$0.052/session**.
- **$0.052 per run · $52/1K-runs · $52,000/1M-runs** (reviewer cascade alone)

---

## C-004 · Session-Level Memoization Cache Wired to Only 2 of 9 Stage Types

**File:** `lib/cache.sh` (full), `lib/mutation-adversary.sh:39`,
`lib/rubric-prescreen.sh:43`
**Effort:** M

`lib/cache.sh` implements a full `(epic_id, iter, stage, input_hash)` memoization
store with 30-day TTL, reuse counters, and a `dollars_saved` view. It is only
hooked in `mutation-adversary` and `rubric-prescreen`. The remaining 7 stage types
— spec-author, spec-reviewer, bdd-runner, reflection-refiner, worker, reviewer,
gradient_extract — bypass it entirely and fire fresh LLM calls on every re-run.

```bash
# AFTER — add cache lookup wrapper in reflection-refiner.sh (pattern for all stages)
mo_run_reflection_refiner() {
  local epic="$1" worktree="$2" iter="$3"
  # ... existing setup ...
  local cache_hash
  cache_hash=$(printf '%s\x1e%s\x1e%s' "$(cat "$kickoff_abs")" "$failure_summary" \
    "$(cat "$template" | mo_cache_input_hash)" | mo_cache_input_hash)
  local cached
  cached=$(mo_cache_lookup reflection-refiner "$epic" "$iter" "$cache_hash")
  if [ -n "$cached" ] && [ -f "$cached" ]; then
    cp "$cached" "$refl_path"
    mo_cache_record_hit reflection-refiner "$epic" "$iter" "$cache_hash"
    return 0
  fi
  # ... existing dispatch ...
  mo_cache_emit reflection-refiner "$epic" "$iter" "$cache_hash" "success" \
    "$refl_path" "$log_path" "$cost" "$turns" "$dur"
}
```

At 80% re-run cache hit rate on failed epics (common scenario), reviewer at
~$0.12/call saves $0.096/re-run. Reflection-refiner (GLM/free) saves $0/call but
saves 2–8 minutes of wall time.
- **$0.096 per re-run on reviewer · ~$38/1K-runs (at 40% re-run rate) · $38,000/1M-runs**

---

## C-005 · `budget: per_epic_usd` in `agents.yaml` Is Never Enforced at Call Time

**File:** `config/agents.yaml:29-31`, `lib/llm-dispatch.sh` (no check),
`bin/mini-ork-execute` (no check)
**Effort:** M

`agents.yaml` declares `budget.per_epic_usd: 5.00`, `per_run_usd: 0.50`,
`daily_cap_usd: 50.00`. These values are read by nothing at dispatch time. The
per-stage env vars (`MO_REFLECTION_BUDGET_USD=0.40`, etc.) are hardcoded in each
handler. A looping agent can bypass the daily cap entirely. The planner failure
path hardcodes `+0.05` instead of reading actual cost (`bin/mini-ork-plan:205`).

```bash
# AFTER — add pre-call budget guard in mo_llm_dispatch
_mo_check_epic_budget() {
  local epic="${MINI_ORK_EPIC_ID:-}" model="$1"
  [ -z "$epic" ] || [ -z "${MINI_ORK_DB:-}" ] && return 0
  local spent cap
  spent=$(sqlite3 "$MINI_ORK_DB" \
    "SELECT COALESCE(SUM(cost_usd),0) FROM mini_orch_sessions WHERE epic_id='$epic'")
  cap=$(awk '/per_epic_usd:/{print $2}' "${MINI_ORK_HOME}/config/agents.yaml" 2>/dev/null || echo 5.0)
  awk -v s="$spent" -v c="$cap" 'BEGIN{exit (s<c)?0:1}' || {
    echo "mo_llm_dispatch: BUDGET CAP hit (spent=$spent >= cap=$cap) for epic=$epic" >&2
    return 1
  }
}
```

Primary impact is cost-storm prevention rather than per-run savings. Without
this guard, a single runaway loop (e.g., reviewer → re-run → reviewer oscillation)
can spend $50+ before hitting the daily cap.
- **Risk: up to $50/day per runaway epic · $0 per normal run · prevents $50/1K-run outliers**

---

## C-006 · `llm_dispatch` Shim Spawns `python3` per Call for Model Resolution

**File:** `lib/llm-dispatch.sh:162-174`
**Effort:** S

Every `llm_dispatch` call spawns a `python3` subprocess to parse `agents.yaml`
and resolve the model lane for the given `node_type`. At 3 dispatches/run ×
100K runs/day = 300K `python3` YAML-load processes daily.

```bash
# BEFORE — lib/llm-dispatch.sh:162-174 (subprocess per call)
_resolved=$(python3 - "$_agents_yaml" "$node_type" <<'PY'
import sys, yaml
...
PY
)

# AFTER — awk lookup (no subprocess, same result for flat YAML)
_mo_resolve_lane() {
  local node="$1" yaml="$2"
  awk -v n="$node" '/lanes:/{in_l=1} in_l && $1 == n":"{print $2; exit}' "$yaml" \
    2>/dev/null || echo "sonnet"
}
```

Reduces 300K python3 process spawns/day → 300K awk (far lighter). Python YAML
parse adds ~50ms on a cold import; awk is <1ms.
- **$0 direct LLM savings · reduces dispatch latency by ~50ms/call**
- **At 100K runs/day: 50ms × 300K = 4.2 CPU-hours reclaimed · ~$0.20/day compute**

---

## C-007 · `max_turns` Defaults to 60 Regardless of Node Type

**File:** `lib/llm-dispatch.sh:42`
**Effort:** S

`mo_llm_dispatch` defaults `max_turns=60` for all callers. A planner node doing
JSON decomposition or a reviewer doing diff-to-spec comparison rarely needs 60
turns. Runaway sessions that use all 60 turns at Opus rates are expensive.

```bash
# BEFORE — lib/llm-dispatch.sh:42
local max_turns="${5:-60}"

# AFTER — per-node defaults; caller still overrides if needed
_mo_default_turns() {
  case "${1:-worker}" in
    planner|spec_reviewer)  echo 15 ;;
    reviewer|researcher)    echo 20 ;;
    reflector|healer|brain) echo 10 ;;
    worker|implementer)     echo 60 ;;
    *)                       echo 30 ;;
  esac
}
local max_turns="${5:-$(_mo_default_turns "${MO_CURRENT_NODE_TYPE:-worker}")}"
```

A planner session that runs 60 turns instead of 15 on Opus at ~500 tokens/turn
= 45 wasted turns × 500 tokens × $75/M output = **$0.0017/wasted-turn × 45 = $0.075**
extra per runaway planner. At 10% runaway rate:
- **$0.0075 per run · $7.50/1K-runs · $7,500/1M-runs**

---

## C-008 · No Haiku Tier for Short-Output Structured Tasks

**File:** `config/agents.yaml` (no haiku assignments)
**Effort:** M

The entire `agents.yaml` has no Haiku-class assignments. Tasks that emit <500
tokens of structured JSON (rubric scoring, gradient summarization, pattern
deduplication) use Sonnet at $3/M when Haiku at $0.25/M is sufficient — a
12× price gap.

```yaml
# AFTER — add haiku tier for short-output tasks
lanes:
  # ... existing ...
  rubric_scorer:      haiku    # 8 yes/no → JSON; $0.0001/call vs $0.0012 Sonnet
  gradient_extractor: haiku    # JSON array extraction; $0.0002/call vs $0.0015 Sonnet
  cache_validator:    haiku    # binary pass/fail; $0.0001/call
  pattern_summarizer: haiku    # cluster grouping; $0.0002/call
```

Set `MINI_ORK_GRADIENT_MODEL=haiku` and `MO_RUBRIC_LANE=haiku` as defaults.

Gradient extraction: Sonnet $0.015/call → Haiku $0.0012/call = saves $0.0138/call.
At 10 gradient calls/run:
- **$0.138 per run · $138/1K-runs · $138,000/1M-runs**

---

## C-009 · Context Pack Budget Always 64K Tokens Regardless of Node Complexity

**File:** `lib/context_assembler.sh:35`, lines 171-181
**Effort:** M

`MINI_ORK_CTX_BUDGET_TOKENS` defaults to 64,000 tokens (~$0.18 per call at Opus
input pricing). Every node type receives all prior_runs + all failure_modes even
when they are irrelevant (a verifier doesn't need prior_similar_runs; a reflector
doesn't need verifier_contract). Truncation is a pop-from-end loop that re-serialises
the entire pack each iteration — O(N²) for large prior_runs lists.

```bash
# BEFORE — context_assembler.sh:35
local budget="${MINI_ORK_CTX_BUDGET_TOKENS:-64000}"

# AFTER — per-node-type budget + selective field inclusion
_mo_ctx_budget() {
  case "${1:-}" in
    planner)     echo 16000 ;;
    researcher)  echo 8000  ;;
    worker)      echo 32000 ;;
    reviewer)    echo 12000 ;;
    reflector)   echo 4000  ;;
    *)           echo 24000 ;;
  esac
}
local budget
budget=$(_mo_ctx_budget "$workflow_node")
```

Dropping budget from 64K → 24K on average: saves ~40K tokens × $3/M (Sonnet)
= **$0.12/run** on input cost, **$0.96/run** on Opus calls.
- **$0.12–$0.96 per run · $120–$960/1K-runs · $120,000–$960,000/1M-runs**

---

## C-010 · `mini-ork-execute` Dispatches Reviewer Even on Empty Implementer Output

**File:** `bin/mini-ork-execute:228-242`
**Effort:** S

The reviewer node is dispatched regardless of whether the implementer produced
any output. If implementer output is empty or contains "dispatch failed", the
reviewer call is pure overhead — it reads an error and emits a JSON verdict
that downstream ignores.

```bash
# BEFORE — bin/mini-ork-execute:228-242 (unconditional reviewer dispatch)
RESULT=$(llm_dispatch \
  --task-class "$TASK_CLASS" \
  --node-type "reviewer" \
  --prompt-text "$PROMPT_CONTENT" 2>&1) || { ... }

# AFTER — guard on non-empty implementer output
if [ -s "$IMPL_LOG" ] && ! grep -q "dispatch failed" "$IMPL_LOG"; then
  RESULT=$(llm_dispatch --node-type "reviewer" ...)
else
  echo '{"verdict":"skip","notes":["implementer produced no output"]}' > "$REVIEW_FILE"
  echo "  [skip] reviewer: implementer output empty"
fi
```

At Opus rates ($0.12/call) with ~5% failure rate:
- **$0.006 per run · $6/1K-runs · $6,000/1M-runs**

---

## C-011 · Gradient Extraction Prompt Embeds Full Trace JSON — No Size Gate

**File:** `lib/gradient_extractor.sh:101`
**Effort:** S

The full trace JSON is spliced directly into the prompt string with no truncation.
A multi-hour worker run produces traces of 100KB+. Beyond cost, this risks silent
context-window truncation mid-JSON, which the extractor handles by emitting `[]`
silently (all gradients lost).

```bash
# BEFORE — lib/gradient_extractor.sh:101
local prompt="${_GRADIENT_EXTRACTOR_PROMPT_TEMPLATE/<<<TRACE_JSON>>>/${trace_json}}"

# AFTER — summarise trace before embedding
_summarise_trace() {
  echo "$1" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(json.dumps({
  'trace_id': d.get('trace_id'), 'status': d.get('status'),
  'task_class': d.get('task_class'), 'cost_usd': d.get('cost_usd'),
  'duration_ms': d.get('duration_ms'),
  'final_artifact_ref': d.get('final_artifact_ref'),
}))" 2>/dev/null || echo "$1" | head -c 2000
}
local prompt="${_GRADIENT_EXTRACTOR_PROMPT_TEMPLATE/<<<TRACE_JSON>>>/$(_summarise_trace "$trace_json")}"
```

A long trace (100KB ≈ 25K tokens at Sonnet) vs a 500-token summary saves
24,500 tokens × $3/M = **$0.074 per long trace**. At 10% long-trace rate:
- **$0.007 per run · $7/1K-runs · $7,000/1M-runs**

---

## C-012 · Reflection Pipeline Has No Batch-Size Guard

**File:** `lib/reflection_pipeline.sh:31-53`
**Effort:** S

`reflection_extract_gradients` queries ALL traces since `since_ts` with no LIMIT.
At 100K runs/day, a single hourly reflection call attempts to process ~4,167 traces.
The loop is synchronous — one gradient_extract at a time — blocking for hours.

```sql
-- BEFORE — lib/reflection_pipeline.sh:36-41 (no limit, no processed guard)
SELECT trace_id FROM execution_traces WHERE created_at >= ? ORDER BY created_at

-- AFTER — batch-size cap + processed marker
SELECT trace_id FROM execution_traces
WHERE created_at >= ? AND gradient_extracted = 0
ORDER BY created_at
LIMIT ${MINI_ORK_REFLECTION_BATCH_SIZE:-200}
```

At 4K traces/batch × $0.015/call (Sonnet) = $60/batch without guard.
With 200-trace cap: $3/batch. Runs 24 batches/day at 100K scale:
- **$57/batch × 24 = $1,368/day savings at 100K scale**
- **$1.37/1K-runs · $1,370/1M-runs**

---

## C-013 · No Per-Run Token Telemetry Aggregation

**File:** `lib/cache.sh:166-177`
**Effort:** M

`mo_cache_costline_from_log` captures `total_cost_usd` per stage but emits no
per-tier token breakdown. Without `input_tokens`, `output_tokens`,
`cache_read_input_tokens` aggregated per node-type, there is no way to:
(a) verify whether `--exclude-dynamic-system-prompt-sections` is achieving
the expected 70% prefix cache-hit ratio; (b) detect model-tier misrouting.

```bash
# AFTER — extend mo_cache_costline_from_log to emit token breakdown
mo_cache_costline_from_log() {
  local log_path="$1"
  grep '"type":"result"' "$log_path" 2>/dev/null | tail -1 | jq -r '
    [(.total_cost_usd // 0), (.num_turns // 0), (.duration_ms // 0),
     (.usage.input_tokens // 0), (.usage.output_tokens // 0),
     (.usage.cache_read_input_tokens // 0),
     (.usage.cache_creation_input_tokens // 0)] | @tsv' | tr '\t' ' '
}
```

Indirect enabler: once token telemetry is visible, operators can cut 10–15%
of spend by tuning per-node budgets based on actual usage.
- **Indirect: unlocks $10–$150/1K-runs in targeted cuts once visible**

---

## C-014 · `--include-partial-messages` Always On — 3–5× Log Volume Inflation

**File:** `bin/_worker-launcher.sh:347-349`
**Effort:** S

`--include-partial-messages` emits every token chunk as a partial JSON event,
inflating log files 3–5× versus final-only output. At 100K runs/day × 500KB
average final log × 4× partial factor = 200GB/day of log write overhead.

```bash
# BEFORE — _worker-launcher.sh:347 (always on)
--include-partial-messages \

# AFTER — enable only for debug mode
local _partial=()
[ "${MO_DEBUG:-0}" = "1" ] && _partial=(--include-partial-messages)
claude -p \
  --output-format stream-json \
  "${_partial[@]}" \
```

No LLM cost savings; reduces disk I/O and storage cost.
- **$0 LLM savings · ~$200/day storage at 100K scale ($0.02/GB) per 200GB/day reduction**

---

## Summary — Ranked by (Savings × Ease) / Effort

| ID | Finding | $/1K-runs saved | $/1M-runs saved | Effort |
|----|---------|----------------|----------------|--------|
| C-009 | Context pack 64K budget uncapped | $120–$960 | $120K–$960K | M |
| C-008 | No Haiku tier for short-output tasks | $138 | $138,000 | M |
| C-002 | Gradient O(N) calls, no idempotency | $135 | $135,000 | M |
| C-003 | Cascading model override in providers | $52 | $52,000 | S |
| C-012 | Reflection: no batch-size guard | $1.37 | $1,370/day at scale | S |
| C-001 | Prompt caching not in mo_llm_dispatch | $36 | $36,000 | S |
| C-004 | Session cache wired to only 2 stages | $38 | $38,000 | M |
| C-007 | max_turns=60 for all node types | $7.50 | $7,500 | S |
| C-011 | Full trace JSON in gradient prompt | $7 | $7,000 | S |
| C-010 | Reviewer on empty implementer output | $6 | $6,000 | S |
| C-006 | python3 per model-resolution call | $0 direct | $500 compute | S |
| C-005 | Budget cap in agents.yaml not enforced | $0 normal | Runaway risk | M |
| C-013 | No per-run token telemetry | indirect | $10–150K unlocked | M |
| C-014 | --include-partial-messages always on | $0 LLM | $200/day storage | S |

### Quick-Win Stack (all S-effort, total ~8h work)

Apply C-003 + C-001 + C-007 + C-010 + C-011 + C-012 in one PR:
- **Combined saving: ~$240/1K-runs with no architectural changes**

### Architectural Findings (for 10M/day scale)

**arch-1 — Model-Tier Router:** Replace the static `agents.yaml` lane assignments
with a lightweight pre-call complexity classifier (rule-based or single Haiku call).
Route `low` complexity → Haiku, `medium` → Sonnet, `high`/`critical` → Opus.
The complexity signal is available in `context_pack.known_failure_modes.length`
and `context_pack.prior_similar_runs[*].status`. Estimated 60–80% reduction in
Opus usage. **Saves ~$0.80 per run at full Opus-reviewer baseline · $800/1K-runs.**

**arch-2 — Semantic Cache Layer:** `lib/cache.sh` uses exact SHA-256 hash matching.
Two kickoffs differing only by comment or whitespace produce a cache miss. At 10M
scale, near-identical tasks (batch feature development) pay full LLM cost on every
variant. Add a semantic similarity layer (embedding hash + cosine threshold > 0.95
= cache hit). Requires `embedding_hash` column + local vector store or Redis.
**Estimated 30–50% hit rate on similar tasks at 10M scale.**

**arch-3 — Batch Gradient Extraction:** Change `gradient_extract(trace_id)` →
`gradient_extract_batch(trace_ids[])`. Pack N trace summaries into one user turn;
parse `{trace_id: gradients[]}` response. 100 traces → 1 call.
**Saves ~90% of reflection LLM cost above 1K-runs scale.**

---

### What Is Already Correct

- **`mo_emit_cache_flags` infrastructure** (`lib/lane-helpers.sh:71-78`) is the
  right mechanism; it just needs broader adoption (C-001).
- **`mini_orch_sessions` schema** (`lib/cache.sh`) is well-designed with TTL,
  GC, reuse counter, and `dollars_saved` view. No schema changes needed.
- **`mo_lane_is_free`** (`lib/lane-helpers.sh:17-23`) correctly suppresses
  `--max-budget-usd` on free-tier lanes (glm/kimi/minimax) — prevents "budget
  exceeded" errors on non-Anthropic providers.
- **Per-stage budget caps** (`MO_REFLECTION_BUDGET_USD=0.40`,
  `MO_RUBRIC_BUDGET_USD=0.60`, `MO_MUTATION_BUDGET_USD=1.20`) are sensible
  defaults with env-var overrides — better than no cap at all.
- **`deepseek → glm` alias** (`_worker-launcher.sh:46`) provides one-hop
  fallback. Extending to two hops (glm → kimi → minimax on quota/auth failure)
  would harden availability without changing cost.
