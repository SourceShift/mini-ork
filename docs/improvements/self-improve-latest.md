# Synthesis — Recursive Self-Improvement, iter 32

## Ranked patch plan

| Rank | Bottleneck | Category | Patch summary | Evidence | Confidence |
|---|---|---|---|---|---|
| 1 | `duration_ms` never captured at any of the 3 `llm_dispatch` sites — 160/162 traces have `duration_ms ∈ {0, NULL}`; `learning_record.id=3` open since iter=0 (32 iters stale) | perf | Capture `T0/T1` wall-clock around each `llm_dispatch` call in `bin/mini-ork-execute`, plumb `duration_ms` through `_trace_write_node_rich` into the existing payload dict; writer in `lib/trace_store.sh:77` already accepts the field | `lens-bottleneck.md:16`, `lens-perf.md` F1, `lens-correctness.md` Fix 2; `bin/mini-ork-execute:473,510,566`; `bin/mini-ork-execute:300-358`; `lib/trace_store.sh:77`; arXiv 2602.10133 (AgentTrace), 2506.11019 (telemetry-aware MCP patterns) | 0.88 |
| 2 | Recipe-template profile drift — runtime `kickoff.md` / `run_profile.json` ship empty `success_criteria`, `scope_allow`, `verification_command` every iter; iter-31 commit `6a91560` masked the symptom by disabling the gate rather than seeding the fields | correctness | Make `bin/mini-ork-self-improve` write a non-empty `run_profile.json` (and matching kickoff structured sections) seeded from `recipes/recursive-self-improve/example-kickoff.md`; bypass markdown re-parse by writing JSON directly | `lens-bottleneck.md:17`; `lens-correctness.md` Fix 1; `bin/mini-ork-self-improve:318`; `recipes/recursive-self-improve/example-kickoff.md:21-50`; arXiv 2602.19065 (Agentic Problem Frames), 2603.09049 (EPOCH) | 0.78 |
| 3 | Synthesis → `learning_record` promotion gap — iter-20 ranked patches #3/#4/#5 live as markdown only; iter-32 had to rediscover them via prose scrape | arch | Add `_self_improve_promote_synthesis_findings "$RUN_DIR/synthesis.md" "$run_id" "$ITER"` to `bin/mini-ork-self-improve`, called after `_self_improve_record_success`; parse the ranked-patch table, insert one `outcome='open'` row per non-landed rank with `category`, `severity`, `evidence_paths`, `patch_summary`; existing schema sufficient | `lens-arch.md` Refactor 1; `docs/improvements/self-improve-latest.md:88-108`; `bin/mini-ork-self-improve:178-217,481-484`; `db/migrations/0017_self_improve_learning.sql:28-45`; arXiv 2605.07242 (MemoRepair), 2511.05524 (EviBound) | 0.74 |
| 4 | Envelope leak in `*.stdout.md` artifacts — `★ Insight` blocks and `` regions before writing `${CONTEXT_FILE}.stdout.md` and `${REVIEW_FILE}.stdout.md`; optionally tee the extracted JSON to `${target}.z-insight.json` | `lens-bottleneck.md:21`; `lens-correctness.md` Fix 3; `lens-arch.md` Refactor 4; `bin/mini-ork-execute:486-489,574-583` | 0.72 |
| 5 | Run-dir accumulation — 71 dirs under `.mini-ork/runs/`, with 9 dirs for 3 logical iters (3/4/5) from retry storms; no retention/quarantine policy | arch | Land a dry-run `lib/run_retention.sh` helper that surfaces a `retention-plan.json` (keep last N runs per iter family, list candidates for archive); leave actual moves behind `MINI_ORK_RUN_RETENTION_DRY_RUN=0` opt-in | `lens-bottleneck.md:20`; `lens-arch.md` Refactor 3; `bin/mini-ork-self-improve:89-90,296-298,507-511`; arXiv 2605.27328 (Garralda-Barrio), 2605.06365 (Rosen execution lineage) | 0.62 |

## Top patch — detailed plan

### Patch 1: Capture `duration_ms` at every `llm_dispatch` call site

**Problem statement.** The `execution_traces` table has a ready `duration_ms` column and writer (`lib/trace_store.sh:77` calls `int(p.get("duration_ms", 0))`), but the three dispatch sites in `bin/mini-ork-execute` capture `RESULT=$(llm_dispatch …)` without bracketing the call in wall-clock measurement. The result: 160 of 162 historical traces have `duration_ms ∈ {0, NULL}` (98.8% empty rate). This is `learning_record.id=3`, open since `iter=0`, restated in iter-19 Patch 5 and iter-20 Patch 5, never landed. Every downstream perf decision in mini-ork is currently unmeasurable.

**Evidence.**

- Empty-rate measurement: `sqlite3 .mini-ork/state.db "SELECT COUNT(*) FROM execution_traces WHERE COALESCE(duration_ms,0)=0"` → 160/162. Source: `lens-bottleneck.md:16`.
- Dispatch sites without timing wrappers: `bin/mini-ork-execute:473` (researcher), `:510` (implementer), `:566` (reviewer). Source: `lens-perf.md` H1; `lens-correctness.md` row 2; `lens-bottleneck.md:16`.
- Writer is ready: `lib/trace_store.sh:77` already reads `duration_ms` from the payload dict; column is in the schema (`lib/trace_store.sh:56`). Source: `lens-perf.md` H1 counter-evidence (a)/(b).
- Payload assembler does not pass `duration_ms`: `_trace_write_node_rich` at `bin/mini-ork-execute:300-358` lists `cost_usd` but not `duration_ms` in the Python payload (`bin/mini-ork-execute:339-353`). Source: `lens-perf.md` H1, `lens-correctness.md` row 2 Files.
- Stale `learning_record` row: `id=3`, perf/medium severity, `iter=0`. Fresh grounding this iter: `2602.10133`, `2506.11019` from `lens-arxiv.md`. Source: `lens-bottleneck.md:37-40`.
- Cost-sidecar precedent (existing pattern to mirror): `lib/llm-dispatch.sh:435-438` writes `${out_file}.cost`; `bin/mini-ork-execute:302-306` reads `${MINI_ORK_RUN_DIR}/.last-llm-cost`. The analogous duration sidecar must be added. Source: `lens-perf.md` H2.
- arXiv grounding: `2602.10133` AgentTrace (structured logging at the agent dispatch boundary, capture timing/cost/status at the same point as the nondeterministic action; `lens-arxiv.md:8-13`); `2506.11019` Mind the Metrics (telemetry-aware patterns — latency should travel with the trace row, not be recovered downstream; `lens-arxiv.md:15-20`).

**Proposed change.** Mirror the existing `.last-llm-cost` sidecar with a `.last-llm-duration-ms` sidecar plus a passthrough into `_trace_write_node_rich`.

1. `lib/llm-dispatch.sh` (writer side, ≈ 8 LoC):
   - At entry of the dispatch body, capture `T0`. Use a portable shim, in this order: prefer `gdate +%s%3N` if `command -v gdate` succeeds; else fall back to `python3 -c 'import time; print(int(time.time()*1000))'`. macOS BSD `date` does not support `%N`; the codebase already requires one of these on the host.
   - On every exit path (success and failure), compute `_dur_ms=$(max 0 $((T1 - T0)))` (wrap in `max 0` to absorb NTP step-backs) and write the integer to `${MINI_ORK_RUN_DIR}/.last-llm-duration-ms`. On failure, write `0` instead of leaving the previous successful call's sidecar in place (stale-sidecar mitigation).
   - Add a `trap` or explicit unconditional sidecar write so an early `return 1` cannot leak the prior value.

2. `bin/mini-ork-execute` — `_trace_write_node_rich` at `bin/mini-ork-execute:300-358` (≈ 4 LoC):
   - Right after the existing cost read at `bin/mini-ork-execute:302-306`, read the duration sidecar:
     ```sh
     _duration_ms=0
     if [ -s "${MINI_ORK_RUN_DIR}/.last-llm-duration-ms" ]; then
       _duration_ms=$(cat "${MINI_ORK_RUN_DIR}/.last-llm-duration-ms")
     fi
     ```
   - Pass `_duration_ms` into the Python payload-assembly heredoc (currently at `bin/mini-ork-execute:339-353`) as a new argv slot, and add `"duration_ms": int(duration_ms_arg)` to the dict that gets handed to `trace_write`. The receiver `lib/trace_store.sh:77` already has `int(p.get("duration_ms", 0))`, so no schema or writer change is needed.

3. Dispatch sites (`bin/mini-ork-execute:473`, `:510`, `:566`) (0 LoC): no change. The sidecar is the contract surface.

Total estimated diff: ≤ 20 LoC across 2 files.

**Regression test.** Land alongside the patch as `tests/perf/test_duration_capture.sh` (new file, ≈ 30 LoC):

```bash
#!/usr/bin/env bash
# Asserts that at least one execution_traces row with non-zero duration_ms
# is written during a single dry-run dispatch.
set -euo pipefail
DB="${MINI_ORK_DB:-/Users/admin/ps/mini-ork/.mini-ork/state.db}"
before=$(sqlite3 "$DB" "SELECT COUNT(*) FROM execution_traces WHERE COALESCE(duration_ms,0)>0")
# Invoke the smallest possible dispatch fixture (a planner stub against a no-op provider).
# Implementer must wire this to the existing fixture harness or add a thin one.
"${MINI_ORK_HOME}/bin/mini-ork-execute" --dry-run --node-type planner --fixture tests/perf/fixtures/duration_capture.json
after=$(sqlite3 "$DB" "SELECT COUNT(*) FROM execution_traces WHERE COALESCE(duration_ms,0)>0")
test "$after" -gt "$before" || { echo "FAIL: duration_ms still empty after dispatch"; exit 1; }
echo "PASS: duration_ms captured (before=$before after=$after)"
```

Assertion text the test prints: `PASS: duration_ms captured (before=N after=M)` with `M > N`. If the test runs against a host without `gdate` and without `python3`, it must hard-fail with a clear `MISSING_TIME_SHIM` message rather than silently writing `0`.

**Verification.** Existing checks that must continue to pass:

- `go vet ./...` — unrelated, should remain clean (per `verifier_contract.v1_lint`).
- `go test ./...` — Go test suite, unrelated to this shell-only patch; must remain green (per `verifier_contract.v2_tests`).
- `tests/unit/test_verifier_ref_json.sh` — existing trace-payload regression; the added `duration_ms` field must not break JSON-shape assertions.
- Existing `bin/mini-ork-execute` end-to-end smoke (cost sidecar still works): the cost path at `:302-306` must remain untouched.

Expected benchmark deltas (signs + magnitudes):

- `duration_ms` empty-rate: drops from 98.8% (160/162) to < 5% (only `status='failure'` rows that bypass the sidecar write should remain at 0). Magnitude: ≈ -94 percentage-point reduction in empty-rate after one iter of capture.
- Per-row overhead: + 1 ms per dispatch for the `T0/T1` shell-invoked timestamp shim. Negligible.
- Synthesis quality (next iter): qualitative — the perf lens for iter-33+ will be able to quote real p50/p95 numbers instead of guessing. This is the strategic payoff that justifies prioritization over the larger profile-drift patch.

**Rollback criteria.** Discard this patch if any of:

1. `tests/unit/test_verifier_ref_json.sh` fails after the change (indicates the payload-shape change broke the writer contract).
2. `lib/trace_store.sh:77` rejects the `duration_ms` key with a Python `KeyError` or schema error (would indicate the lens read of the schema was wrong).
3. Two consecutive `mini-ork-execute` runs after the patch leave `duration_ms = 0` in every new row (indicates the sidecar write path is broken end-to-end and the fix is worse than the original — empty-rate did not improve).
4. The portable-`date` shim selection produces non-monotonic or negative durations on the host (the `max(0, end - start)` guard should prevent this, but defense-in-depth: if > 1% of new rows show `0` while the dispatch is known to be slow, abort).

## Lower-ranked patches

### Patch 2: Seed `run_profile.json` (and structured kickoff sections) from `example-kickoff.md`

**Problem.** `bin/mini-ork-self-improve:318` emits an 8-line stub kickoff. `bin/mini-ork classify` (`bin/mini-ork:176-178`) cannot recover `success_criteria`, `scope_allow`, or `verification_command` from prose that doesn't carry them. iter-31 commit `6a91560` muted the symptom (gate disabled) without seeding the fields. Currently dormant, but architecturally wrong — the gate is the canonical correctness guard and is now off by default. Source: `lens-correctness.md` Fix 1; `lens-bottleneck.md:17`.

**Change.** In `bin/mini-ork-self-improve` (target ≈ 60 LoC), after creating `$RUN_DIR/kickoff.md`, write `$RUN_DIR/run_profile.json` directly with the structured fields parsed from `recipes/recursive-self-improve/example-kickoff.md` (or seeded inline from a canonical block). Prefer option (b) from `lens-correctness.md` Open Question 1: write the JSON directly. Avoids markdown re-parse fragility.

**Regression test.** From `lens-correctness.md` Fix 1:

```bash
python3 -c "
import json, sys
p = json.load(open(sys.argv[1]))
assert p.get('success_criteria'), 'success_criteria empty'
assert p.get('scope_allow'),       'scope_allow empty'
assert p.get('verification_command'), 'verification_command empty'
" "$RUN_DIR/run_profile.json"
```

**Rollback.** Revert if the profile-gate verifier (`recipes/recursive-self-improve/verifiers/profile-gate.sh:74`) starts failing on the seeded shape, or if planner runs against the seeded profile begin emitting different orchestrations that destabilize iter-33.

**Why not #1.** Higher LoC, design choice still live (markdown splice vs JSON seed), and currently masked by gate-disable. Land in iter-33 with `MINI_ORK_PROFILE_GATE=1` re-enabled as the success signal.

### Patch 3: `_self_improve_promote_synthesis_findings` — promote synthesis ranked patches into `learning_record`

**Problem.** iter-20 ranked Patches #3/#4/#5 from `docs/improvements/self-improve-latest.md:88-108` were never inserted into `learning_record`. iter-32 had to rediscover them via prose scrape. Source: `lens-arch.md` Refactor 1; `lens-bottleneck.md:18`; arXiv 2605.07242 (MemoRepair provenance) + 2511.05524 (EviBound evidence-bound completion).

**Change.** New runner-local function `_self_improve_promote_synthesis_findings "$RUN_DIR/synthesis.md" "$run_id" "$ITER"`. Parse the `## Ranked patch plan` table, normalize titles, insert one `outcome='open'` row per non-landed rank. Idempotency on `(run_id, iter, rank, normalized_title)`. Phase 1 additive: on parse failure write `$RUN_DIR/promotion.err` and do not fail the iter. No schema change.

**Regression test.** After iter-32 lands, `SELECT COUNT(*) FROM learning_record WHERE run_id='self-improve-iter-32-20260609110333' AND outcome='open'` must equal the number of non-#1 ranked rows in this synthesis (i.e., 4 — Patches 2–5).

**Rollback.** Revert if duplicate rows accumulate across iters (idempotency check broken) or if `_self_improve_record_success` ordering changes side-effects.

**Why not #1.** Larger diff (~100 LoC including parser + idempotency); depends on the synthesis-table shape being stable, which this very synthesis fixes by sticking to the recipe's `## Ranked patch plan` heading.

### Patch 4: `_mini_ork_write_stdout_sidecar` — strip `★ Insight` + `` regions. Optionally write the extracted JSON to `${target}.z-insight.json`. Fallback: on sanitizer failure, write the raw stream to `${target}.stdout.raw.md` and a sanitized error marker to `${target}.stdout.md` so forensic access is preserved.

**Regression test.** From `lens-correctness.md` Fix 3:

```bash
count=$(find .mini-ork/runs -name '*.stdout.md' -mtime -1 -exec grep -l '<z-insight>' {} \; | wc -l)
[ "$count" -eq 0 ] || exit 1
count2=$(find .mini-ork/runs -name '*.stdout.md' -mtime -1 -exec grep -l '★ Insight' {} \; | wc -l)
[ "$count2" -eq 0 ] || exit 1
```

**Rollback.** Revert if the sanitizer strips legitimate fenced code blocks that happen to contain the literal tokens.

**Why not #1.** Medium severity (forensics-only files; primary `$CONTEXT_FILE` already clean). Smaller blast radius than #1 but lower leverage on the loop.

### Patch 5: Dry-run run-dir retention policy (`lib/run_retention.sh`)

**Problem.** `.mini-ork/runs/` has 71 dirs, including 9 dirs for 3 logical iters (3/4/5) from retry storms. Bottleneck scans pay growing I/O cost. Source: `lens-bottleneck.md:20`; `lens-arch.md` Refactor 3; arXiv 2605.27328 (Garralda-Barrio lifecycle governance), 2605.06365 (Rosen execution lineage retention).

**Change.** Add `lib/run_retention.sh`. Emit `$MINI_ORK_HOME/runs/retention-plan.json` listing candidate archive targets (keep last N runs per iter-family, preserve runs with non-empty `patches/` and `synthesis.md`). Default `MINI_ORK_RUN_RETENTION_DRY_RUN=1` — no actual moves this iter.

**Regression test.** Plan file is valid JSON with non-zero `candidates` array given the current 71-dir baseline. No filesystem moves during dry-run mode.

**Rollback.** Revert if any candidate marked for archive contains an active worktree reference or unflushed patches.

**Why not #1.** Medium severity, low active blast — scans still complete. Defer to iter-33 once retention shape is reviewed.

## Convergence assessment

**Not yet converged.** Three signals argue against terminating the outer loop after iter-32:

1. `learning_record.id=3` (duration_ms) has been open across 32 iters with two prior synthesis prescriptions (iter-19 Patch 5, iter-20 Patch 5) and zero landings. The current iteration is the first to converge all four research lenses on the same minimal fix, indicating the loop is finally producing actionable specificity rather than diminishing returns.
2. The arch lens identifies three independent structural gaps (synthesis→ledger promotion, targeted outcome promotion, run-dir retention) that the current schema can already host without new infra. None of these has been attempted; ranking them as Patches 3 / 5 with explicit phase-1-additive plans keeps the loop in a productive regime.
3. The correctness lens surfaced a previously-masked root-cause (profile drift, papered over by iter-31's gate-disable). The fact that iter-32 caught this — and that iter-31 only suppressed it — shows the lens stack is still uncovering novel work, not re-cycling closed items. (Dedup against landed commits `0a3bf1c`, `300fe48`, `b7acaf9`, `6a91560` confirmed in `lens-bottleneck.md:6-7`.)

Diminishing returns will be observable when (a) the bottleneck scan produces fewer than 3 unresolved items above medium severity for two consecutive iters, and (b) per-iter `duration_ms` p95 deltas (now measurable after Patch 1) stabilize within ± 5%. Neither condition holds in iter-32. Continue the loop.

## Provenance footer

- Lenses consumed: bottleneck (codex), perf (minimax), correctness (kimi), arch (codex), arxiv (codex). Note: the recipe template names `lens-perf.md` minimax and `lens-correctness.md` kimi; this iter's dispatcher honored that mapping per `run_profile.json.provider_policy.lanes`.
- Synthesizer family: opus.
- arXiv papers cited: 9 distinct IDs across the ranked patches, all sourced from `lens-arxiv.md` — `2602.10133`, `2506.11019` (Patch 1); `2602.19065`, `2603.09049` (Patch 2); `2605.07242`, `2511.05524` (Patch 3); none (Patch 4 — correctness sanitizer requires no infra justification); `2605.27328`, `2605.06365` (Patch 5). Additional cross-reference: `2506.02539` (VerificAgent) supports Patch 3's outcome-promotion safety story but is not load-bearing.
- Cross-iteration learnings applied: 5 `learning_record` open rows consulted (`id ∈ {2, 3, 10, 11, 12}` per `lens-bottleneck.md:31-49`); iter-19 Patches 2/5/6 carry-forward dispositions confirmed (Patch 2 resolved by `300fe48`; Patches 5 and 6 restated as iter-32 Patches 1 and 3 respectively); iter-20 synthesis `docs/improvements/self-improve-latest.md` Patches 1-5 reviewed (only Patch 1 landed via `300fe48`; Patches 2-5 still markdown-only and feed Patch 3 of this synthesis).
