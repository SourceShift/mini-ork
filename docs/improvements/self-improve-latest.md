# Synthesis — Recursive Self-Improvement, iter 1

## Input status (degraded)

This synthesis was produced under degraded inputs and the ranking
explicitly accounts for it:

- `bottleneck-scan.md` was never written to the run directory. The
  perf and arch lenses each performed their own scoped scan and the
  arXiv lane fail-closed (correct behavior per its prompt contract).
- `lens-correctness.md` is absent. The kimi correctness lane failed
  with `api_error_status: 401` (see
  `llm-failures/1780984277-kimi.out`). Submodularity is reduced to
  2-of-3 voters (codex arch + minimax perf), so this iteration is
  effectively a 2-lens synthesis rather than the intended 3-lens
  Rajan-2025 panel.
- `arxiv-refs.md` is a stub recording the missing scan; no papers
  were cited by the lane. **Consequence under the recipe's hard
  constraints: no patch in this synthesis may propose new infra
  (new DB, new wrapper, new table, new MCP tool).** All ranked
  patches below are in-place refactors of code that already exists.
- `learning_record` returned zero rows. No cross-iteration dedupe
  was possible. Every bottleneck below is therefore first-occurrence
  and must be logged into `learning_record` by the reflector so
  iter 2+ can dedupe against it.

## Ranked patch plan

| Rank | Bottleneck | Category | Patch summary | Evidence | Confidence |
|---|---|---|---|---|---|
| 1 | Generic verifier_ref handler ignores JSON `pass: false` and only gates on shell exit code | correctness-of-orchestration | Add `_run_verifier_ref` adapter in `bin/mini-ork-execute` that captures stdout, parses JSON when present, and treats `.pass == false` as failure even on exit 0; fall back to legacy exit-code contract when stdout is non-JSON | arch lens §3 + `bin/mini-ork-execute:542-584`, `bin/mini-ork-self-improve:229-260`, `recipes/recursive-self-improve/verifiers/bottlenecks-found.sh:9`, `recipes/recursive-self-improve/verifiers/self-tests-pass.sh:10`, `recipes/recursive-self-improve/verifiers/no-regression.sh:11` | 0.85 |
| 2 | Cost-circuit budget check forks `python3` on every llm_dispatch call | perf | Hoist `task_runs.cost_usd` aggregate into env-var-backed cache mirroring `_MO_LANE_<UPPER>` pattern at `lib/llm-dispatch.sh:373-414`; refresh every 30s or on TTL boundary | perf lens F1 + `lib/llm-dispatch.sh:348-368`, `lib/llm-dispatch.sh:373-414`, `lib/llm-dispatch.sh:382-384` | 0.80 |
| 3 | `llm_dispatch` returns 42 on cost-circuit-open but execute handlers treat it as generic dispatch failure | correctness-of-budget | Differentiate exit 42 from generic 1 in each `_dispatch_node` caller in `bin/mini-ork-execute`; halt cleanly with `_d021_set_status "halted"` and clear log line | perf lens F6 + `lib/llm-dispatch.sh:366`, `bin/mini-ork-execute:431` | 0.85 |
| 4 | `bin/mini-ork-self-improve` copies `config/agents.recursive-self-improve.yaml` over `$MINI_ORK_HOME/config/agents.yaml`, mutating shared provider-policy state | arch / fault-isolation | Add `MINI_ORK_AGENTS_FILE` env var override; lane resolution reads it when set, falls back to `$MINI_ORK_HOME/config/agents.yaml` otherwise. Outer runner exports the env var instead of overwriting the file | arch lens §4 + `bin/mini-ork-self-improve:90-98`, `bin/mini-ork-self-improve:183-196`, `config/agents.recursive-self-improve.yaml:1-6` | 0.70 |
| 5 | Substring-match synthesis routing — reviewer becomes synthesizer only because its `node_id` contains `synth` | arch / dispatcher-contract | Add explicit `artifact_role: synthesis` or `output_ref: source_artifact` field on reviewer nodes in workflow schema; executor routes on the field; keep `[[ $node_id == *synth* ]]` as warning-emitting back-compat shim | arch lens §1 + `bin/mini-ork-execute:476-501`, `bin/mini-ork-execute:482-499`, `recipes/recursive-self-improve/workflow.yaml:24` | 0.60 |

Patches 2 and 5 from the perf lens (F2 parallel-lens dispatch; F3
single-python3 trace+cost merge) are **not ranked** this iteration
because:

- F2 is >150 LOC, medium-risk for SQLite write contention under
  concurrent `_trace_write_node_rich`, and explicitly should be
  sequenced *after* F1 (perf lens open-question #4). It is queued
  for iter 2 once F1 lands.
- F3 saves ~9.6s/iter but the same merge is partially achieved if
  patch 1 (verifier adapter) reuses the stream-json post-processor
  python3 — leave F3 until that overlap is measured.

## Top patch — detailed plan

### Patch 1: `_run_verifier_ref` JSON-aware adapter

**Problem statement.** The generic workflow executor at
`bin/mini-ork-execute:542-584` treats `verifier_ref` script success
as "process exited zero". Recursive-self-improve's own verifiers
deliberately exit 0 always and encode pass/fail in stdout JSON
(`recipes/recursive-self-improve/verifiers/bottlenecks-found.sh:9`,
`recipes/recursive-self-improve/verifiers/self-tests-pass.sh:10`,
`recipes/recursive-self-improve/verifiers/no-regression.sh:11`).
The current code therefore reports false passes for every JSON-emitting
verifier, and the outer runner has had to compensate manually at
`bin/mini-ork-self-improve:229-260` by re-reading
`verifier-result-*.json` after `mini-ork-execute` returns. This is
a leaky-boundary smell that any future recipe using JSON verifiers
would silently inherit, and it is the kind of false-pass surface
the correctness lens (had it run) would have flagged first.

**Evidence.**
- `bin/mini-ork-execute:542-584` — `_dispatch_verifier_ref` only
  checks `$?` after the shell command.
- `bin/mini-ork-self-improve:229-260` — manual `jq '.pass'` workaround
  that proves the gap.
- `recipes/recursive-self-improve/verifiers/bottlenecks-found.sh:9`,
  `recipes/recursive-self-improve/verifiers/self-tests-pass.sh:10`,
  `recipes/recursive-self-improve/verifiers/no-regression.sh:11` —
  three verifier scripts that say "Exit 0 always" in comments and
  put pass/fail in stdout JSON.
- arch lens §3 documents the smell as "verifier surface gap".
- arXiv evidence: none required — this is an in-place adapter, no
  new infra. The recipe's "new infra requires arXiv evidence" rule
  does not apply.

**Proposed change.** Add a helper inside `bin/mini-ork-execute`
(reference line numbers are pre-patch from the current main):

```bash
_run_verifier_ref() {
  local script="$1"; shift
  local stdout_file
  stdout_file="$(mktemp -t mo-verifier-XXXXXX)"
  local exit_code=0
  bash "$script" "$@" >"$stdout_file" 2>&1 || exit_code=$?

  # If stdout parses as JSON with a .pass field, that field wins.
  if jq -e 'type == "object" and has("pass")' "$stdout_file" \
        >/dev/null 2>&1; then
    local pass
    pass=$(jq -r '.pass' "$stdout_file")
    if [[ "$pass" != "true" ]]; then
      cat "$stdout_file" >&2
      rm -f "$stdout_file"
      return 1
    fi
    cat "$stdout_file"
    rm -f "$stdout_file"
    return 0
  fi

  # Legacy contract: exit code is authoritative when stdout is
  # not JSON-with-.pass.
  cat "$stdout_file"
  rm -f "$stdout_file"
  return $exit_code
}
```

Then replace every direct `bash "$verifier_ref"` invocation in
`_dispatch_verifier_ref` and any other call site inside
`bin/mini-ork-execute` (current grep target: `verifier_ref`) with
`_run_verifier_ref "$verifier_ref"`.

Once the adapter is live, remove the manual `jq '.pass'` workaround
at `bin/mini-ork-self-improve:229-260` in a follow-up commit (do
NOT bundle the removal into patch 1 — keep the rollback surface
small).

**Regression test.** Add a new test file
`tests/unit/test_verifier_ref_json.sh` that asserts:

1. `_run_verifier_ref` returns non-zero when the script exits 0 and
   stdout is `'{"pass": false, "reason": "synthetic"}'`.
   Assertion: `[ "$status" -ne 0 ]` and the captured stderr contains
   `"pass": false`.
2. `_run_verifier_ref` returns zero when the script exits 0 and
   stdout is `'{"pass": true}'`.
   Assertion: `[ "$status" -eq 0 ]`.
3. `_run_verifier_ref` preserves the legacy contract: when the
   script exits non-zero and stdout is plain text "broken", the
   adapter returns the same non-zero exit and stdout reaches the
   caller. Assertion: `[ "$status" -eq 7 ]` and captured stdout
   contains `broken`.
4. `_run_verifier_ref` preserves the legacy contract on the
   happy path: exit 0 + non-JSON stdout returns 0.
   Assertion: `[ "$status" -eq 0 ]`.

The four assertion strings above are the exact lines the test must
emit; they are the regression-detection signal, not freeform
narration.

**Verification.** Existing tests that must continue to pass:
`tests/unit/test_benchmark_suite.sh`, `tests/e2e/test_e2e_benchmark_run.sh`,
and any test under `tests/` that touches verifier dispatch
(grep for `verifier_ref` and `_dispatch_verifier_ref`). After the
patch lands, a clean `mini-ork run recursive-self-improve` should
produce identical verifier verdicts to the outer-runner's manual
JSON read at `bin/mini-ork-self-improve:229-260` — that equivalence
is the easiest empirical check. Expected benchmark deltas: zero on
latency (the adapter adds one `jq -e` per verifier call, ~5ms);
zero on cost; correctness on JSON-pass-false cases changes from
false-pass to true-fail.

**Rollback criteria.** Discard the patch if any of the following:

- Any pre-existing verifier under `recipes/*/verifiers/` that
  previously passed now fails (suggests stdout contains incidental
  JSON we mis-classify).
- `tests/unit/test_verifier_ref_json.sh` cannot be made to pass
  without weakening its assertions.
- The added `jq -e` invocation adds >50ms per verifier in
  observed wall-time (would indicate `jq` cold-start dominating;
  unlikely but observable on systems without `jq` on PATH).

## Lower-ranked patches

### Patch 2: Memoize cost-circuit check (perf F1)

**Problem.** `lib/llm-dispatch.sh:348-368` forks `python3` on every
dispatch to read `task_runs.cost_usd` and compare to
`MO_DAILY_BUDGET_USD`. The same fork-per-call anti-pattern was
already fixed for lane resolution at `lib/llm-dispatch.sh:373-414`
via env-var cache; the cost-check missed that treatment.

**Proposed change.** Mirror the `_MO_LANE_<UPPER>` env-var cache
pattern: cache `task_runs.cost_usd` aggregate in
`_MO_COST_CIRCUIT_CACHE` with a `_MO_COST_CIRCUIT_CACHED_AT`
timestamp; on each call, recompute only if `now - cached_at > 30`.
Bound TTL at 30s so a runaway run can overshoot the daily cap by
at most one TTL window.

**Regression test.** New `tests/unit/test_cost_circuit_cache.sh`:
assert that 100 sequential `llm_dispatch` calls within a 5-second
window result in exactly one `python3` invocation against
`task_runs` (instrument via a stub python3 in PATH that increments
a counter file). Assert that after 35s of wall time, a second
`python3` invocation appears.

**Verification.** No existing test should regress. Expected delta:
~50-80ms saved per dispatch (perf lens F1 estimate). Rollback if
the test fails or if the daily cap is overshot by more than 5% in
synthetic load tests.

**Why ranked below patch 1.** Correctness > perf when severity is
comparable. Patch 1 fixes a known false-pass; patch 2 fixes a
known wall-time inefficiency.

### Patch 3: Differentiate cost-circuit exit 42 in execute handlers (perf F6)

**Problem.** `lib/llm-dispatch.sh:366` returns 42 when budget is
exhausted, but the per-node-type handlers in `bin/mini-ork-execute`
(e.g. `bin/mini-ork-execute:431`) collapse the failure with `||
{ echo "researcher dispatch failed" >&2; return 1; }`. Budget
breaches are invisible in logs and the run continues consuming
time on verifier/publisher nodes.

**Proposed change.** Wrap each `_dispatch_node` call in a `case
$?` block: `42)` triggers `_d021_set_status "halted"` (or the
existing equivalent), logs `cost_circuit_open` with the offending
node_id, and returns early. `0)` is success; everything else falls
back to current generic failure handling.

**Regression test.** `tests/unit/test_cost_circuit_halt.sh`:
set `MO_DAILY_BUDGET_USD=0.01`, invoke a researcher dispatch, and
assert the run status is `halted` and the log contains
`cost_circuit_open`.

**Verification.** Zero latency impact. The change is mechanical
and confined to three handler functions. Rollback if any existing
test that intentionally sends exit 42 (none currently) breaks.

### Patch 4: `MINI_ORK_AGENTS_FILE` env var instead of mutating shared agents.yaml (arch §4)

**Problem.** `bin/mini-ork-self-improve:90-98` copies
`config/agents.recursive-self-improve.yaml` into
`$MINI_ORK_HOME/config/agents.yaml`, mutating the same file other
recipes read. In a shared `.mini-ork` home, a self-improve run can
leave subsequent unrelated runs using Codex-only canonical lanes.

**Proposed change.** Add `MINI_ORK_AGENTS_FILE` env var read by
lane resolution (`lib/llm-dispatch.sh:373-414` is the
authoritative resolution point). When set, that path wins; when
unset, fall back to the current `$MINI_ORK_HOME/config/agents.yaml`.
The outer runner `bin/mini-ork-self-improve` exports
`MINI_ORK_AGENTS_FILE="$MINI_ORK_ROOT/config/agents.recursive-self-improve.yaml"`
instead of running the copy. Keep the copy path behind a feature
flag `MO_SELF_IMPROVE_LEGACY_COPY=1` for one release.

**Regression test.** `tests/unit/test_agents_file_override.sh`:
set `MINI_ORK_AGENTS_FILE=/tmp/fake-policy.yaml`, run lane
resolution for `node_type=researcher`, assert the resolved lane
matches `/tmp/fake-policy.yaml`'s mapping and that
`$MINI_ORK_HOME/config/agents.yaml` is unmodified after the run
(compare file checksum pre/post).

**Verification.** Run a recursive-self-improve iteration with the
new env var and assert: (a) provider lanes match the override; (b)
`$MINI_ORK_HOME/config/agents.yaml` is byte-identical before and
after. Rollback if either assertion fails.

### Patch 5: Explicit synthesis routing field on reviewer nodes (arch §1)

**Problem.** `bin/mini-ork-execute:482-499` decides whether a
reviewer node is a synthesizer purely by `[[ "$node_id" == *synth*
]]`. Recipe evolution becomes fragile — renaming `opus_synthesizer`
to `final_ranker` would silently change the executor's output
contract.

**Proposed change.** Extend the workflow parser to read an optional
`artifact_role` field per node (e.g. `artifact_role: synthesis`).
Executor branches on the field. Keep `[[ $node_id == *synth* ]]`
as a warning-emitting back-compat fallback for one release.

**Regression test.** `tests/unit/test_synthesis_routing.sh`:
build a synthetic workflow with a reviewer node named
`final_ranker` carrying `artifact_role: synthesis`; assert the
executor routes it through the synthesis path (writes
`source_artifact` markdown, not `review-*.json`). Build a second
workflow with a reviewer node named `synthesis_reviewer` and NO
`artifact_role` field; assert the legacy substring path triggers
AND emits a deprecation warning on stderr.

**Verification.** All existing recipes (recursive-self-improve,
research-synthesis, blog-post, ops-runbook, post-mvp-delivery)
must continue to produce the same artifacts. Migration: add
`artifact_role: synthesis` to each of those recipes' synthesizer
nodes in a follow-up commit, not in patch 5 itself.

**Why ranked lowest.** Largest blast radius (touches workflow
schema across multiple recipes) and the smell does not currently
cause any user-visible failure — the existing recipes happen to
follow the `*synth*` naming convention. Defer until iter 3+ when
the correctness gains from patches 1 and 3 have stabilized.

## Convergence assessment

Mini-ork is NOT approaching diminishing returns. This is iter 1
and the synthesis surfaced five distinct, high-confidence patches
even with two of three lenses degraded and zero arXiv evidence
available. The shape of the findings is telling:

- Three of five patches (1, 3, 4) target known leaky boundaries
  the codebase has already been working around inside
  `bin/mini-ork-self-improve`. That is exactly the smell that the
  recursive-self-improvement loop is designed to surface and
  promote into the generic primitive layer.
- Two of five (patches 2, 5) are pure inefficiency / fragility
  patches with no current production impact but high payoff if
  the project keeps scaling lens count or recipe count.

The outer loop should continue. Recommended sequencing for iter 2:

1. Land patch 1 (highest correctness payoff, smallest blast).
2. Repair the kimi correctness lane (401 auth) before iter 2 so
   the panel is back to 3-of-3 voters.
3. Regenerate `bottleneck-scan.md` upstream of the lenses so the
   arXiv lane can actually cite papers and so this synthesis no
   longer has to fail-closed on the "new infra requires arXiv"
   constraint.

## Provenance footer

- Lenses consumed: codex (arch, present), minimax (perf, present),
  kimi (correctness, absent — 401 auth failure at
  `llm-failures/1780984277-kimi.out`).
- Synthesizer family: opus (this artifact).
- arXiv papers cited: 0 (arXiv lane fail-closed on missing
  `bottleneck-scan.md`; see `context-arxiv_research.json`).
- Cross-iteration learnings applied: 0 rows from `learning_record`
  (table exists, zero rows; first iteration).
- Hard-constraint compliance: no patch in this synthesis proposes
  new infra; all five are in-place refactors of existing modules,
  so the "new infra → arXiv required" rule is not breached.
- Lens family diversity: 2 of 3 (codex + minimax). Submodularity
  is reduced for this iteration; the reflector must log the kimi
  401 incident so the runner gates iter 2 on lane availability.
