# Synthesis — audit findings F1–F6 panel reconciliation

Composed from `judge-opus-audit.md` (claude-opus-4-8) and `judge-minimax-audit.md`
(MiniMax-M3), both judged 2026-09-10 against the live read-only DB
(`/Volumes/docker-ssd/ps/mini-ork/.mini-ork/state.db`) and the current tree at
`main` (a261cbaa). Neither judge read the other's report. Disagreement is
preserved, not averaged.

## Panel Verdict Matrix

| Finding | Opus verdict | Opus severity | Minimax verdict | Minimax severity | Agreement |
|---|---|---|---|---|---|
| F1 — GRPO reward vocabulary bug | confirmed | high | confirmed | critical | verdict-only (severity split) |
| F2 — Mixed token-ledger semantics | confirmed | high | confirmed | high | full |
| F3 — Gradient pipeline write-only ($462 → 0 applies) | partially_confirmed | high | confirmed | high | split |
| F4 — Failed calls cost/time-invisible | confirmed | medium | confirmed | high | verdict-only (severity split) |
| F5 — Learning starvation (unscored traces + dead sinks) | confirmed | high | confirmed | critical | verdict-only (severity split) |
| F6 — Context bombs, zombies, dead cost rollup | partially_confirmed | medium | confirmed | critical | split |

Agreement flag key: **full** = verdict and severity match; **verdict-only** =
verdict matches, severity differs; **split** = verdicts differ. Severity
pattern worth noting: minimax rates strictly ≥ opus on every finding and uses
`critical` three times (F1, F5, F6) where opus says high/medium/high. Opus's
lower ratings are each argued from narrowed blast radius (F1: 454/598 failure
rows bypass via non-NULL `process_reward`; F4: telemetry blind spot, not wrong
behavior; F6: zombies carry empty `agent_version_id`, excluded from writeback).

## Consensus Findings

Both judges **confirmed** F1, F2, F4, and F5, with independently identical
measurements. Strongest single receipt per finding:

- **F1** — `sqlite_master` CHECK clause for `execution_traces`:
  `CHECK (status IN ('success','failure','pending','running','vacuous'))` —
  `'failed'` is not a legal status, yet `writeback.py:254` tests
  `status not in {"success", "failed"}`. Both judges quoted both receipts
  verbatim; every failure row takes the early-return branch and the designed
  `0.15 base ± 0.10 verdict band` is unreachable for failure rows. 0
  failure+approve rows (both judges), so the 1.0-on-approve path is latent.
- **F2** — anthropic|sonnet success rows: `SUM(input_tokens)` = 2,537 vs
  `SUM(cached_input_tokens)` = 3,749,749 (both judges, exact). Cached exceeds
  input by 1,479× — impossible under include-semantics, proving the
  `input_tokens` column means different things per provider while
  `write_llm_calls_row` (llm_dispatch.py:258) applies one formula
  (`max(in − cached − cache_create, 0)`) to all. Global cache ratio 135.3%
  (opus) / 1.353 (minimax). Anthropic rows clamp to ~zero uncached cost:
  2,853/2,854 rows (opus), $0.0017 aggregate (both judges).
- **F4** — `SELECT COUNT(*), MAX(input_tokens), MAX(cost_usd), MAX(duration_ms)
  FROM llm_calls WHERE status='failed'` → 410 rows, 0 / 0.0 / 0 (both judges,
  identical). Minimax localized the writer: `llm_dispatch.py:523` hardcodes
  `duration_ms=0, cost_usd=0, input_tokens=0, output_tokens=0` on the failure
  path. Lane fail rates reproduce exactly: glm 54.8% (23/42), sonnet 26.2%
  (190/725), minimax 7.4% (43/579).
- **F5** — reward coverage among writeback-eligible traces: success 231/394,
  failure 102/169; monthly 2026-06 0/1861 (0%) → 2026-07 387/624 (62%) →
  2026-08 60/154 (39%) → 2026-09 0/6 (both judges, exact). `reward_source`
  skew rubric@v1 = 344 vs verifier@v1 = 103; all six learning sinks at 0 rows;
  `lane_region_advantage` 15 rows, every one `runs_count=1, success_count=0,
  relative_advantage=0.0` — the router never accumulated a second sample.

Both judges also converge on a cross-cutting diagnosis: the learning loop's
machinery exists end-to-end in the current tree but is operationally idle —
extraction funded ($461.93), reward coverage ~41% and shrinking, six sinks
empty, apply gate shipped OFF. Opus adds that F1/F2/F4 are one bug class
(code assuming vocabularies/semantics the schema never guarantees — the same
class writeback.py's own docstring records from the conductor incident).

## Dissents

### D1 — F3 verdict: partially_confirmed (opus) vs confirmed (minimax)

The economics are undisputed: $461.93 / 2,458 calls (2,246 success + 212
failed) / 34.06M input tokens / 37.5% of all-time spend; `textual_gradients` =
0 and `apply_attempts` = 0; no `INSERT INTO textual_gradients` anywhere in the
tree (both judges ran the search). The dissent is about framing and two
sub-claims:

- **Opus (partial)**: "write-only" overstates. `gradient_records` has 15+
  readers (reflect.py, reflection_pipeline.py, role_evolver.py,
  context_assembler.py, similarity.py, apply.py, web routes), so gradients DO
  feed prompt/context composition. `apply_attempts` HAS a writer
  (apply.py:550) behind `MO_APPLY_ENABLED`, which defaults OFF — root cause is
  "shipped gated, never exercised," not "no code path." Additionally, the
  auditor's near-duplicate figures (2,044/6,747 rows in 267 clusters) do not
  reproduce on ANY column opus probed.
- **Minimax (confirmed)**: treats the finding as stated — `textual_gradients`
  has no producer; the apply loop is wired (apply.py:550) but receives zero
  input because `textual_gradients` is empty; 190/212 (89.6%) failures carry
  empty `error_message`. Minimax did not test the dup-cluster claim.
- **Conflicting numbers**: auditor 2,044 rows / 267 clusters vs opus measured
  first-25-char-opener clusters: `signal` 840/3,383, `suggested_change`
  858/3,625, `target` 421/6,237 (target: 6,237/6,747 rows sit in dup clusters
  = 92%, so duplication itself is real; the auditor's specific figures are
  not). Minimax: no measurement.
- **Mechanism divergence on apply_attempts=0** (not mutually exclusive): opus
  attributes it to the master gate defaulting OFF; minimax attributes it to
  `textual_gradients` being empty (apply's input). Both may be true
  simultaneously — the gate is off AND its input is empty.

### D2 — F6 verdict: partially_confirmed (opus) vs confirmed (minimax)

Sub-claims (a) and (b) are confirmed to the digit by both judges, including
the single-call receipt llm_calls id=4317 (input 14,768,026 / output 27,557 /
$2.888 / cache-read 14,445,312; minimax adds duration 973,879ms), codex_lens
136 successes averaging 1,061,128 input with 27 zero-output successes, and 94
zombie `running` traces (June 77 / July 13 / Aug 4, all with empty
`agent_version_id`). The dissent is sub-claim (c), run-level cost rollup:

- **Opus (refutes c in substance)**: the auditor queried the wrong table. The
  `runs` table IS vestigial (1 row, $0.00) — but run-level cost rollup lives
  in `task_runs.cost_usd`, populated 478/478 rows summing $807.60, and that
  column is the live input to the daily cost circuit (`cost_circuit_open`,
  llm_dispatch.py:216–219). Residual real gap: task_runs' $807.60 covers only
  65% of llm_calls' $1,232.60 (plausibly F4 zeroing + parentless calls).
- **Minimax (confirms c)**: `runs.cost_usd` populated for exactly 1 row at
  $0.00; "$1,232.60 total, only visible via `llm_calls`, 0% surfaced at run
  level"; per-run cost reporting and run-level budget enforcement unreachable.
- **Conflicting numbers**: run-level surfaced spend = **0%** (minimax) vs
  **65% / $807.60** (opus). Minimax never queried `task_runs`; opus never
  disputed minimax's `runs`-table measurement (both measured runs = 1 row,
  $0.00 — the literal numbers agree; the inference "rollup is dead" is what
  opus counters with a receipt minimax lacks). This is the panel's sharpest
  unresolved conflict — see Questions For Human Decision.

### D3 — Severity calibration (F1, F4, F5, F6)

Not verdict-affecting, but the splits are systematic. Minimax: F1 critical
(flat 0.0 reward → GRPO advantage has zero variance on the failure axis, 598
rows), F5 critical (learning loop is the claimed moat), F6 critical. Opus: F1
high (454/598 failures bypass via `process_reward`; only 73/169
writeback-eligible failures traverse the buggy branch, plus 15 zombie
`running` rows leaking in), F4 medium (telemetry gap, not misbehavior), F5
high, F6 medium (zombies excluded from writeback; rollup not dead per D2).
The synthesis records both scales rather than picking one.

## Corrected Numbers

| # | Figure | Auditor | Opus measured | Minimax measured | Status |
|---|---|---|---|---|---|
| 1 | F1 failure rows | 596 | **598** | **598** | both agree; live-DB drift (DB still receiving writes) |
| 2 | F4 failed llm_calls rows | 405 | **410** | **410** | both agree; drift |
| 3 | F3 near-duplicate clusters | 2,044/6,747 rows, 267 clusters | does not reproduce on any column: signal 840/3,383; suggested_change 858/3,625; target 421/6,237 | not tested | **disputed/unreproduced** — duplication is real (target column: 92% of rows in dup clusters) but the auditor's figures match no column |
| 4 | F6c run-level cost surfaced | "all cost lives only in llm_calls" (= $0 at run level) | task_runs.cost_usd **478/478 populated, $807.60** (65% of $1,232.60); cost circuit reads it | **0%** surfaced; runs = 1 row, $0.00 | **direct conflict** (D2) |
| 5 | F2 global cache ratio | 135% | 135.3% | 1.353 | confirmed by both |
| 6 | F2 anthropic/sonnet input vs cached | 2,537 vs 3,749,749 | same, exact | same, exact | confirmed |
| 7 | F2 anthropic uncached-cost damage | cost_input_uncached_usd = 0 | 2,853/2,854 rows zero; $0.0017 aggregate; **cost_usd itself correct** (envelope-trusted) | $0.0017 aggregate; adds: hardcoded Anthropic rates ($15/$1.5/$18.75 per MTok) applied to ALL providers; codex uncached col sums $1,016.45 | confirmed, with additive second defect (rate hardcode) from minimax |
| 8 | F3 economics | $461.93 / 2,458 calls / 37% of $1,232 / 34.06M tok | $461.93 / 2,458 (2,246+212) / 37.5% / 34.06M | $461.93 / 2,458 | confirmed to the cent by both |
| 9 | F3 gradient_records consumers | (implied: nothing consumes output) | 15+ readers exist | not assessed | opus correction stands uncontested |
| 10 | F5 coverage figures | 231/394, 102/169; Jun 0%, Jul 62%, Aug 39%; rubric 344 vs verifier 103; LRA 15×(1,0,0.0) | all exact | all exact | confirmed |
| 11 | F6a context bomb | avg 1.06M, max 14,768,026 (id=4317, $2.89), 27 zero-output | exact ($2.888019) | exact (+973,879ms duration) | confirmed |
| 12 | F6b zombies | 94 (77 June), empty agent_version_id | 94 (77/13/4), all empty | 94 (77/13/4), all empty | confirmed |
| 13 | F1 blast radius | all failure rows affected | 454/598 bypass via process_reward; 73/169 eligible traverse the bug | not measured | opus refinement, uncontested |

## Recommended Fix Order

Reconciled ranking (severity × cheapness, consensus items first; both judges'
orders were consulted, neither copied):

1. **F1** — one-line fix (`{"success","failure"}` at writeback.py:254) plus a
   vocabulary test pinned to the actual CHECK constraint; trivial, zero
   dissent, un-corrupts the core reward signal every future writeback pass
   writes.
2. **F6b** — sweep the 94 zombies to a terminal status and add a finalize hook
   stamping `vacuous` on timeout; one UPDATE + one hook, undisputed, stops
   running-state pollution of every cohort query.
3. **F4** — pass real `duration_ms` (always knowable) and provider-reported
   usage/cost through the failure write at llm_dispatch.py:523; cheap, makes
   failure burn visible to the cost circuit and lane fuses, and un-blinds the
   pricing of every other fix here.
4. **F5** — make reward writeback fire on every completed trace (writers
   already exist in trace_store/eval_judge/execute) and fix
   `lane_region_advantage` UPSERT so `runs_count` can exceed 1; multi-touch
   but the largest structural lever — the learning loop is the product's
   claimed moat and its inputs are 41%-covered and shrinking.
5. **F2** — normalize token semantics at the transport boundary (each parser
   reports canonical uncached input + cached separately) and replace the
   hardcoded Anthropic rates with per-provider rates (minimax's additive
   defect); prerequisite for trusting any ledger-derived metric; needs a
   backfill.
6. **F3** — blocked on the enable-or-defund decision below; until it is made,
   gradient-extract must not resume burning (37.5% of all-time spend for 0
   recorded applies, and the burn window only ended ~2 weeks ago).
7. **F6a** — cap lens context per call (context-assembler budget); real but
   rare (one 14.77M-token outlier vs 1.06M average).
8. **F6c (conditional, pending D2 tie-break)** — if opus's refutation holds:
   drop or alias the vestigial `runs.cost_usd` column and investigate the
   $807.60-vs-$1,232.60 undercount; if minimax holds: add the run-finalize
   rollup UPDATE. Cheap either way, but which fix is correct depends on the
   human decision.

## Questions For Human Decision

1. **F6c tie-break (D2).** Is run-level cost rollup dead? Opus holds a direct
   counter-receipt (`task_runs.cost_usd` 478/478 = $807.60, read by
   `cost_circuit_open` at llm_dispatch.py:216–219); minimax never queried
   `task_runs` and its "0% surfaced" number cannot be reconciled with opus's
   65%. One query settles it: re-measure `task_runs.cost_usd` population and
   the circuit's read path. The answer decides fix #8's shape and whether F6's
   verdict reverts to confirmed.
2. **F3 apply gate.** Enable `mini-ork-apply` (`MO_APPLY_ENABLED=1`) against
   the 6,747 harvested gradients, or defund/retire gradient-extract? This is a
   product call only the operator can make; it gates fix #6 and determines
   whether the $461.93 is recoverable or sunk.
3. **Auditor's dup-cluster figure.** Nobody reproduced 2,044 rows / 267
   clusters on any column. Obtain the auditor's exact query, or strike the
   sub-claim from the record (the qualitative duplication claim survives via
   opus's 92% target-column measurement).
