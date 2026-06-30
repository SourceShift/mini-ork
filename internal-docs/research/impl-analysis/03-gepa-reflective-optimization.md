# GEPA Reflective Optimization — Source Analysis and mini-ork Adoption Plan

> **Mapping:** Recommendation R4 — GEPA-style reflective prompt-evolution optimizer
> alongside the existing GRPO loop.
>
> **Repository analysed:** `/private/tmp/miniork-ref-analysis/gepa` (tag: main)
> **mini-ork reference files:** `lib/process_reward.sh`, `lib/gradient_extractor.sh`,
> `lib/lane_router.sh`, `lib/group_evolver.sh`, `lib/reflection_pipeline.sh`,
> `docs/LEARNING-LOOP-LIFECYCLE.md`

---

## 1. GEPA Algorithm — Step-by-Step with Code Anchors

GEPA (Genetic-Pareto) is a black-box optimizer for **textual parameters** (prompts,
code, agent architectures). Its key insight: instead of collapsing execution traces
into a single scalar and backpropagating through it (GRPO), GEPA feeds the **full
execution trace** to an LLM "reflection" model, which reads the trace and proposes
targeted rewrites of the textual parameters. This replaces 5,000–25,000 GRPO rollouts
with 100–500 GEPA evaluations.

### 1.1 Entrypoint / Optimizer Class

**File:** `src/gepa/core/engine.py`
**Class:** `GEPAEngine` (line 51)

```python
class GEPAEngine(Generic[DataId, DataInst, Trajectory, RolloutOutput]):
    """Orchestrates the optimization loop using pluggable candidate proposers."""
```

`GEPAEngine.run()` (line 458) is the main loop. The loop is bounded by a
`StopperProtocol` callback (typically `MaxMetricCallsStopper` at 100–500 calls).

### 1.2 The GEPA Loop — Six Phases Per Iteration

Each iteration of `GEPAEngine.run()` executes the following, delegated to
`ReflectiveMutationProposer` (`src/gepa/proposer/reflective_mutation/reflective_mutation.py`):

```
Phase 1 — Pareto-select parent candidate
Phase 2 — Sample minibatch from trainset
Phase 3 — Evaluate parent with full trace capture  (capture_traces=True)
Phase 4 — Build reflective dataset from traces
Phase 5 — LLM proposes mutated candidate text
Phase 6 — Evaluate mutation on same minibatch; accept/reject; full-valset eval if accepted
```

#### Phase 1 — Pareto-select parent (`candidate_selector.py`)

```python
# src/gepa/strategies/candidate_selector.py, class ParetoCandidateSelector
def select_candidate_idx(self, state: GEPAState) -> int:
    return select_program_candidate_from_pareto_front(
        state.get_pareto_front_mapping(),
        state.per_program_tracked_scores,
        self.rng,
    )
```

`select_program_candidate_from_pareto_front` (`gepa_utils.py`, line 90) first prunes
dominated candidates (`remove_dominated_programs`), then samples proportional to how
many validation examples each Pareto-front member "wins" on. This selects candidates
that are good on *different* subsets — avoiding premature convergence to a single
optimum. The Pareto front is over **per-example** validation scores (frontier_type =
"instance"): candidate A is non-dominated if there exists at least one validation
example where it is best.

#### Phase 2 — Minibatch sample

`batch_sampler.next_minibatch_ids()` draws a small subsample (default ~10–20
examples) from the training set. Full valset evaluation only happens when a candidate
passes the acceptance gate.

#### Phase 3 — Evaluate with trace capture

```python
# reflective_mutation.py, line 268
eval_curr = self.adapter.evaluate(ctx.minibatch, ctx.curr_prog, capture_traces=True)
```

The adapter returns an `EvaluationBatch` including:

- `outputs`: raw per-example outputs (opaque to GEPA)
- `scores`: per-example floats (higher is better)
- `trajectories`: per-example trace objects (only when `capture_traces=True`)

#### Phase 4 — Build reflective dataset

```python
# reflective_mutation.py, line 341
reflective_dataset = self.adapter.make_reflective_dataset(
    ctx.curr_prog, eval_curr, predictor_names_to_update
)
```

The adapter converts trajectories + scores + outputs into a structured
`Mapping[component_name, list[dict]]` — one record per example, with fields like:

```json
{
  "Inputs":            { "question": "...", "context": "..." },
  "Generated Outputs": "...",
  "Feedback":          "Wrong answer. Expected 42, got 7. ..."
}
```

This is the **textual gradient** — the LLM-readable signal about what went wrong.

#### Phase 5 — Propose mutated candidate text

```python
# reflective_mutation.py, line 369
new_texts, prompts, raw_lm_outputs = self.propose_new_texts(
    ctx.curr_prog, reflective_dataset, predictor_names_to_update
)
```

`propose_new_texts` calls `InstructionProposalSignature.run_with_metadata()`
(`src/gepa/strategies/instruction_proposal.py`, line 13) with this default prompt
template:

```
I provided an assistant with the following instructions to perform a task for me:
```<curr_param>```

The following are examples of different task inputs provided to the assistant
along with the assistant's response for each of them, and some feedback on how
the assistant's response could be better:
```<side_info>```

Your task is to write a new instruction for the assistant.
... (reflect on niche strategies, include domain-specific facts)
Provide the new instructions within ``` blocks.
```

The reflection LM reads the current prompt text, the failures with
per-example feedback, and produces a revised instruction. This is the core
"textual gradient descent" — the LM reasons about *why* specific examples
failed and edits the instruction to prevent those failures.

#### Phase 6 — Minibatch-gate then full-valset eval

```python
# reflective_mutation.py, line 420
eval_after = self.adapter.evaluate(ctx.minibatch, new_candidate, capture_traces=True)
```

The same minibatch is re-evaluated with the mutated candidate. Acceptance test
(`StrictImprovementAcceptance`, `acceptance.py`, line 39):

```python
def should_accept(self, proposal, state) -> bool:
    old_sum = sum(proposal.subsample_scores_before or [])
    new_sum  = sum(proposal.subsample_scores_after  or [])
    return new_sum > old_sum
```

If accepted, the candidate is evaluated on the **full valset** and added to the
Pareto front. If rejected, no full-valset rollout occurs — the minibatch guard is
the primary cost-savings mechanism.

### 1.3 Textual Feedback vs Scalar Reward

| Dimension | GRPO | GEPA |
|---|---|---|
| Signal type | Scalar reward per rollout | Full trace + per-example text feedback |
| How improvement is proposed | Policy gradient update on weights | LLM reads failures, edits prompt text |
| What is "learned" | Weight tensors | Prompt/instruction text |
| Rollouts to converge | 5,000–25,000 | 100–500 |
| What "gradient" means | Numerical gradient ∂L/∂θ | `make_reflective_dataset` → LLM diff |

GEPA uses the scalar reward only as a **filter** (minibatch acceptance gate).
The actual mutation is driven by the LLM reading the full trajectory — error
messages, reasoning logs, wrong outputs — and producing a targeted rewrite.

---

## 2. The Adapter Interface — What mini-ork Would Implement

**File:** `src/gepa/core/adapter.py`
**Protocol:** `GEPAAdapter[DataInst, Trajectory, RolloutOutput]` (line 59)

A GEPA adapter implements two required methods and one optional:

### 2.1 `evaluate(batch, candidate, capture_traces) -> EvaluationBatch`

Runs the system with the given `candidate` (a `dict[str, str]` mapping component
name to text, e.g. `{"system_prompt": "..."}`) on a batch of inputs, and returns
per-example scores and (when `capture_traces=True`) trace objects.

For mini-ork: a "candidate" would be a recipe's textual parameters, a "batch
instance" would be a task spec, and a "score" would be derived from `process_reward`
or an outcome metric. "Trajectories" would be execution trace rows from
`execution_traces`.

### 2.2 `make_reflective_dataset(candidate, eval_batch, components_to_update) -> Mapping[str, list[dict]]`

Converts trace objects and scores into per-component feedback records for the
reflection LLM. Each record should include inputs, outputs, and natural-language
feedback about what went wrong and what the correct answer would have been.

For mini-ork: this would serialize execution trace fields (node outputs, tool calls,
reviewer verdicts, error messages) into structured feedback dicts keyed by
recipe component (e.g. `"planner_prompt"`, `"synthesizer_prompt"`).

### 2.3 Optional `propose_new_texts(candidate, reflective_dataset, components) -> dict[str, str]`

If implemented, bypasses GEPA's default `InstructionProposalSignature` prompt and
calls whatever LLM/logic the user prefers. mini-ork could use its existing
`llm-dispatch.sh` here.

---

## 3. Why GEPA Needs Far Fewer Rollouts Than GRPO

Three concrete mechanisms explain the 35x rollout reduction:

### 3.1 Minibatch-gated acceptance (the primary savings mechanism)

GRPO evaluates each rollout on the full dataset to compute group statistics and
update the policy. GEPA evaluates each *candidate mutation* on a tiny subsample
(~10–20 examples). Only mutations that improve on the subsample proceed to full
valset evaluation. Because most random mutations are rejected, the vast majority
of expensive full-valset runs are avoided.

```
GRPO:  N_rollouts × full_dataset evaluations
GEPA:  N_mutations × subsample evaluations  +  N_accepted × full_valset evaluations
         (N_accepted << N_mutations)
```

### 3.2 Pareto-front candidate selection avoids wasted exploration

GRPO explores by sampling from a policy distribution — many samples cluster near
already-discovered optima. GEPA selects candidates proportional to how many
validation examples they "win" on (Pareto domination). This naturally focuses
exploration on under-optimized examples, not re-exploring already-solved ones.

### 3.3 LLM mutation targets failures directly

GRPO produces a gradient signal that is diffuse — it nudges policy weights toward
better-rewarded trajectories without explaining *why* specific examples failed.
GEPA's reflection step has the LLM read error messages and wrong outputs, then
make targeted edits to the instruction. This means fewer "random walks" before
finding an improvement: each proposal is causally informed by failure reasons.

The `InstructionProposalSignature.default_prompt_template` explicitly asks the
reflection LM to "Identify all niche and domain-specific factual information" from
the feedback and include it in the new instruction — a targeted, theory-of-change
edit rather than a gradient step.

---

## 4. Adoption Plan for mini-ork (GEPA Alongside Existing GRPO)

### 4.1 What GEPA Would Optimize in mini-ork

In GEPA's vocabulary, the "textual parameters" are the strings that encode
system behavior. In mini-ork's architecture, these are:

| GEPA concept | mini-ork equivalent |
|---|---|
| `candidate: dict[str, str]` | Recipe prompt fields: `{"planner_prompt": "...", "synthesizer_prompt": "...", "verifier_contract": "..."}` |
| `DataInst` (training example) | A task spec (kickoff JSON) from `epics` or a test task |
| `Trajectory` | An `execution_traces` row (or bundle of rows for a run) |
| `RolloutOutput` | `{status, process_reward, reviewer_verdict, files_written, cost_usd}` |
| `score: float` | `process_reward` or a composite metric derived from the PRM |
| `reflective_dataset record` | Serialized trace fields + LLM node outputs + failure summary |

### 4.2 New File: `lib/gepa_optimizer.sh`

This would be the A/B-selectable entry point for the reflective prompt evolution
loop. It would expose:

```bash
# Run one GEPA optimization iteration on a named recipe's prompt parameters
# MO_OPTIMIZER=gepa enables this path; MO_OPTIMIZER=grpo (default) keeps existing behavior
gepa_run_iteration <recipe_name> <task_class>
    # 1. Read candidate from prompt_win_rates / recipe prompts store
    # 2. Sample a minibatch of recent task runs
    # 3. Evaluate candidate on minibatch (re-runs or uses cached traces)
    # 4. Build reflective dataset from execution_traces
    # 5. Call reflection LM to propose new prompt text
    # 6. Evaluate mutation on same minibatch
    # 7. Accept/reject; if accepted, run full valset and update prompt_win_rates
```

The `MO_OPTIMIZER=gepa` env var acts as the A/B flag — existing GRPO writeback in
`bin/mini-ork-execute` lines 218 and 252 runs unchanged; GEPA is a second writer
targeting recipe prompts rather than lane routing.

### 4.3 New File: `lib/gepa_adapter.sh`

This implements the three GEPAAdapter methods in bash/Python, wiring to existing
mini-ork infrastructure:

**`gepa_evaluate()`** — wraps `bin/mini-ork run` or replays cached
`execution_traces` rows, returns `process_reward` as the per-example score.
Captures trace rows as the trajectory object.

```bash
gepa_evaluate() {
  local recipe="$1" candidate_json="$2" minibatch_task_ids="$3"
  # For each task_id in minibatch:
  #   - if trace exists in execution_traces: use cached process_reward
  #   - else: dispatch run and record trace
  # Output: JSON array of {score, trace_id}
}
```

**`gepa_make_reflective_dataset()`** — converts execution traces into feedback
records. Uses the existing fields from `execution_traces`:

```bash
gepa_make_reflective_dataset() {
  # For each trace_id, fetch:
  #   - node_type, status, tool_calls, files_written, reviewer_verdict
  #   - LLM output text (from llm_calls table if available)
  #   - process_reward
  # Format as:
  # { "Inputs": {task_description}, "Generated Outputs": {node_output},
  #   "Feedback": "process_reward=0.2, reviewer_verdict=REJECT, error=..." }
}
```

This reuses the data already written by `prm_score_trace` and `gradient_extract` —
no new trace infrastructure needed.

**`gepa_propose_new_texts()`** — calls mini-ork's existing `llm-dispatch.sh`
with the reflection prompt:

```bash
gepa_propose_new_texts() {
  local component="$1" current_prompt="$2" reflective_dataset_json="$3"
  # Use the InstructionProposalSignature template verbatim:
  # Substitute <curr_param> = current_prompt
  # Substitute <side_info> = reflective_dataset_json formatted as markdown
  # Dispatch through: llm-dispatch.sh with reflection_lm (e.g., opus)
  # Parse result: extract text between ``` blocks
}
```

### 4.4 Integration into `lib/reflection_pipeline.sh`

The existing `reflection_run` function in `lib/reflection_pipeline.sh` already
orchestrates a six-step pipeline (extract gradients → deduplicate → link failures
→ detect stale → summarize patterns → suggest promotions). GEPA would be wired
in as a parallel track after step 1, when `MO_OPTIMIZER=gepa`:

```bash
# In reflection_run():
reflection_extract_gradients "$since_ts"      # existing step 1

if [ "${MO_OPTIMIZER:-grpo}" = "gepa" ]; then
  # GEPA parallel track: reflective prompt evolution
  gepa_run_iteration "$recipe_name" "$task_class"
fi

# GRPO track: existing steps 2-6
reflection_deduplicate ...
reflection_suggest_promotions ...
```

The GEPA track writes improved prompt texts to a new column `prompt_candidates`
in the existing `gradient_records` table (or a new `gepa_candidates` table),
analogous to how `gradient_records` stores suggested changes for human/LLM review.

### 4.5 Recipe Prompts as "Textual Parameters"

Mini-ork stores recipe node prompts in YAML files under `recipes/` and in
`prompts/` templates. For GEPA evolution, each evolving component maps to:

| Component name | Source |
|---|---|
| `"planner_prompt"` | `prompts/planner.md` template |
| `"synthesizer_prompt"` | `prompts/synthesizer.md` |
| `"verifier_contract"` | `prompts/verifier.md` or inline in recipe |
| `"kickoff_framing"` | Kickoff template for a given task class |

These texts are the `candidate: dict[str, str]` GEPA optimizes. Each GEPA
iteration reads the current version, evaluates it against recent task traces,
and proposes an improved version. The improved version is written back to the
prompts store (initially as a candidate, promoted to default only after passing
acceptance and validation gates).

### 4.6 Wiring to Existing Reward Signals

mini-ork already produces all the reward signals GEPA needs:

- **Per-example score:** `execution_traces.process_reward` (produced by
  `lib/process_reward.sh::prm_score_trace` after every node dispatch).
  No change needed — this becomes GEPA's `scores: list[float]`.

- **Trajectory / trace:** `execution_traces` rows for a run bundle. The
  `gepa_make_reflective_dataset` function serializes these into feedback records.
  Existing `gradient_extractor.sh::gradient_extract` already LLM-reads traces
  and extracts change signals — GEPA's reflective dataset is a structured version
  of the same information.

- **Reflection LM:** Already used in `gradient_extractor.sh` and `reflection-refiner.sh`.
  GEPA's reflection LM call uses the same dispatch path (`llm-dispatch.sh`), just
  with the `InstructionProposalSignature` template.

### 4.7 Pareto Candidate Pool in mini-ork

GEPA's Pareto-front selection requires tracking multiple candidate prompt versions
against multiple validation examples. In mini-ork this maps to:

- **Candidate pool:** A new table `gepa_prompt_candidates (candidate_id, recipe, component, text, valset_scores_json, created_at)`. Each accepted GEPA mutation adds one row.
- **Pareto front:** Computed at selection time — for each validation task, find which candidate scored best; a candidate is on the Pareto front if it is not dominated on all tasks.
- **Selection:** Sample from Pareto-front candidates proportional to number of tasks where they are best — same algorithm as `select_program_candidate_from_pareto_front` in `gepa_utils.py`.

This is a clean addition: it does not touch the existing `agent_performance_memory`
or `prompt_win_rates` tables (which remain the GRPO track).

### 4.8 A/B Gate and Rollout Budget

The key cost-control parameters mirror GEPA's:

```bash
MO_OPTIMIZER=gepa                  # default: grpo (existing behavior)
MO_GEPA_MINIBATCH_SIZE=10          # examples per subsample (GEPA default ~10-20)
MO_GEPA_MAX_METRIC_CALLS=150       # total evaluations budget per recipe per run
MO_GEPA_REFLECTION_LM=opus         # model for reflective mutation step
MO_GEPA_CANDIDATE_COMPONENTS=planner_prompt,synthesizer_prompt  # which components to evolve
```

The minibatch guard (Phase 6, acceptance test on subsample before full-valset) is
the primary cost mechanism — it means only ~10–20% of mutation attempts incur a
full-valset evaluation.

### 4.9 Rollout-to-Existing-Trace Reuse

Mini-ork already has a dense historical trace store. GEPA's minibatch evaluation
step (Phase 3) can be **fully cache-served** from `execution_traces` for historical
task runs — no new LLM dispatch needed. Only novel tasks (not yet in
`execution_traces`) require live dispatch. This further reduces GEPA's rollout cost
for mini-ork below the baseline 100–500 GEPA evaluations, because most "evaluations"
are SQL reads against the existing trace store.

The `optimize_anything_adapter.py` in GEPA already implements this pattern
(`_call_evaluator` with `cache_mode="memory"` or `"disk"`); mini-ork's version
uses `execution_traces` as the persistent cache.

### 4.10 Migration Path (Three Phases)

**Phase 0 (scaffolding, no behavior change):**
- Add `lib/gepa_adapter.sh` with `gepa_evaluate`, `gepa_make_reflective_dataset`,
  `gepa_propose_new_texts` (backed by existing `execution_traces` and `llm-dispatch.sh`).
- Add migration for `gepa_prompt_candidates` table.
- Wire `MO_OPTIMIZER` env gate — no-op when `grpo`.

**Phase 1 (shadow mode):**
- Enable `MO_OPTIMIZER=gepa` in a test environment.
- Run GEPA iterations on historical traces (no live task dispatches — fully cache-served).
- Log proposed mutations to `gepa_prompt_candidates` table; do not promote to default.
- Compare GEPA-proposed prompts to human-reviewed `gradient_records.suggested_change` —
  measure overlap as a sanity check.

**Phase 2 (A/B production):**
- Enable `MO_OPTIMIZER=gepa` for one recipe per task class.
- Let GEPA evolve that recipe's prompts alongside GRPO's lane routing.
- Measure: (a) process_reward improvement on the GEPA-evolved recipe vs control;
  (b) total evaluation cost vs the equivalent GRPO update cycle.
- Promote winning candidates to `prompts/` on gate-pass.

---

## 5. Key Differences Between GEPA's Design and mini-ork's Existing Loop

| Axis | GEPA | mini-ork GRPO |
|---|---|---|
| What is optimized | Textual prompt parameters | Lane routing weights (`relative_advantage`) |
| Optimization target per iteration | Recipe prompt text | Which model lane to use for a node |
| Signal used for mutation | Full trace text → LLM reflection | Scalar reward → group z-score |
| Candidate pool | Multi-member Pareto front of prompt versions | Single "best lane" per (task_class, node_type) |
| Cost-control mechanism | Minibatch acceptance gate | Sample size threshold (≥3) |
| Convergence | ~100–500 evaluations | Requires dense per-lane trace history |

GEPA and GRPO are **complementary**, not competing:
- GRPO selects *which model* to run a node on (lane routing).
- GEPA selects *what the node is told to do* (prompt text evolution).

Both consume `execution_traces` and `process_reward`; neither interferes with the
other's write path.

---

## 6. Files Changed / Added Summary

| File | Action | Purpose |
|---|---|---|
| `lib/gepa_adapter.sh` | Add | GEPAAdapter implementation: evaluate, make_reflective_dataset, propose_new_texts |
| `lib/gepa_optimizer.sh` | Add | Main GEPA loop: Pareto select → minibatch → reflect → mutate → accept/reject |
| `lib/reflection_pipeline.sh` | Edit (add branch) | Wire GEPA track after step 1 when `MO_OPTIMIZER=gepa` |
| `migrations/NNNN_gepa_candidates.sql` | Add | `gepa_prompt_candidates` table |
| `bin/mini-ork` | Edit (add flag) | Document `MO_OPTIMIZER` env var in `--help` |

No existing files are modified except `reflection_pipeline.sh` (additive branch)
and `bin/mini-ork` (help text). The GRPO path in `bin/mini-ork-execute` is unchanged.

---

*Generated 2026-06-30. Sources: GEPA repo at commit `main`; mini-ork `lib/` and `docs/`.*
