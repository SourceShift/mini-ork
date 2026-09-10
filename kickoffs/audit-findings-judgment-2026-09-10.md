# Judge six audit findings about mini-ork run history

## Goal

Two independent judges (Anthropic claude-opus-4-8 and MiniMax-M3) verify six
audit findings against the live state DB and current source, then a
synthesizer reconciles their verdicts into one matrix.

## Deliverable (exactly one)

A verified judgment of findings F1–F6: per-finding verdict
(confirmed / partially_confirmed / refuted / unverifiable), severity,
corrected numbers where the original figures are wrong, and a reconciled
fix order. Artifacts are the two judge reports and the synthesis produced
by the audit-judge-panel recipe.

## Success criteria

1. Both judge reports (`judge-opus-audit.md`, `judge-minimax-audit.md`)
   contain a verdict block for every finding F1–F6 — verdict from
   {confirmed, partially_confirmed, refuted, unverifiable}, severity from
   {critical, high, medium, low}, and at least one evidence receipt each
   (SQL query result or file:line actually inspected).
2. `synthesis.md` has a Panel Verdict Matrix covering F1–F6 for both judges,
   plus consensus, dissents, corrected numbers, and a reconciled fix order.
3. The recipe's `panel-completeness` verifier passes on the above.

## Verification command

MINI_ORK_RUN_DIR=<run dir> .venv/bin/python \
  recipes/audit-judge-panel/verifiers/panel-completeness.py
→ JSON with `"pass": true` (this is the recipe's verifier node).

## Scope

- **Read-only.** No edits to any repo source file. Judges write only their
  own report artifacts under the run dir.
- **Live DB:** `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/state.db`
  (NOT the `.bak-*` / `.pre-*` backups, NOT `.mini-ork/state/state.db`).
  Use `sqlite3 -readonly`.
- **Source files in scope:**
  - `/Volumes/docker-ssd/ps/mini-ork/mini_ork/learning/writeback.py`
  - `/Volumes/docker-ssd/ps/mini-ork/mini_ork/dispatch/llm_dispatch.py`
  - `/Volumes/docker-ssd/ps/mini-ork/mini_ork/dispatch/codex_transport.py`
- Judges may run any read-only `sqlite3` query against the live DB and read
  any file in the repo.

## The six findings

Each finding lists the auditor's claim, the key receipt, and a reproduction
path. Judges must re-derive the numbers themselves.

### F1 — GRPO reward vocabulary bug (writeback.py)

Claim: `write_grpo_advantages`'s inline `reward()` (writeback.py:252–258)
tests `status not in {"success", "failed"}`, but `execution_traces.status`
CHECK constraint allows only `('success','failure','pending','running','vacuous')`
— there is no `"failed"`. Every `failure` row takes the early-return branch,
so the designed `0.15 base ± 0.10 verdict band` is dead code: all failures
score flat 0.0, verdict deltas (`needs_revision`, `fail`, …) never apply, and
a `failure` row with an approve verdict would score 1.0 (0 such rows today).

Reproduce:
- Read writeback.py lines 23–45, 139–141, 248–262.
- `sqlite3 -readonly .mini-ork/state.db "SELECT COUNT(*) FROM execution_traces WHERE status='failure'"` → 596
- `... "SELECT COUNT(*) FROM execution_traces WHERE status='failure' AND LOWER(COALESCE(reviewer_verdict,'')) IN ('approve','approved','pass','passed','ok')"` → 0

### F2 — Mixed token-ledger semantics (llm_dispatch.py)

Claim: `write_llm_calls_row` (llm_dispatch.py:248) computes
`uncached_in = max(in_tok - cached_in - cache_create, 0)`, assuming
`input_tokens` INCLUDES cached tokens (true for the codex transport,
codex_transport.py:220). Anthropic/gateway transports report
`input_tokens` as uncached-only, so the subtraction clamps to 0 →
`cost_input_uncached_usd = 0` for those lanes, and the stored
`input_tokens` column has different meanings per lane. Aggregate proof:
global cache ratio 135% (cached > input), anthropic/sonnet success rows sum
input=2,537 vs cached=3,749,749.

Reproduce:
- Read llm_dispatch.py lines 248–310; codex_transport.py lines 211–260.
- `... "SELECT provider, model_id, SUM(input_tokens), SUM(cached_input_tokens) FROM llm_calls WHERE status='success' GROUP BY 1,2"`

### F3 — Gradient pipeline is write-only ($462 → 0 applies)

Claim: feature `mini-ork:gradient-extract` burned $461.93 across 2,458 calls
(37% of all-time $1,232 spend; 34.06M input tokens), producing 6,747
`gradient_records` — but `textual_gradients` = 0 (no code in the tree writes
that table) and `apply_attempts` = 0. 212 extractions failed, 190 with empty
`error_message`. Also 2,044/6,747 rows (30%) share a first-25-char opener
(267 near-duplicate clusters).

Reproduce:
- `... "SELECT COUNT(*), ROUND(SUM(cost_usd),2) FROM llm_calls WHERE feature_name='mini-ork:gradient-extract' GROUP BY status"` (per status)
- `... "SELECT COUNT(*) FROM textual_gradients"` → 0; same for `apply_attempts`
- `grep -rln "INSERT INTO textual_gradients" mini_ork/ bin/ scripts/` → (no results)

### F4 — Failed calls are cost- and time-invisible

Claim: all 405 `status='failed'` llm_calls rows record `input_tokens=0`,
`output_tokens=0`, `cost_usd=0`, `duration_ms=0`. Real provider failures
(401 hangs, usage-limit cut-offs mid-stream, timeouts) consume input tokens
and minutes of wall-clock, but the ledger prices them at zero — cost
governance cannot see failure burn. Lane fail rates: glm 54.8% (23/42),
sonnet 26.2% (190/725), minimax 7.4%.

Reproduce:
- `... "SELECT COUNT(*), MAX(input_tokens), MAX(cost_usd), MAX(duration_ms) FROM llm_calls WHERE status='failed'"`
- `... "SELECT provider, model_id, SUM(status='failed'), COUNT(*) FROM llm_calls GROUP BY 1,2"`

### F5 — Learning starvation: unscored traces + dead sinks

Claim: among writeback-eligible traces (non-empty `agent_version_id`),
reward coverage is 231/394 successes and 102/169 failures; by month the
coverage DEGRADED (June 0%, July 62%, August 39%). The scored corpus is
dominated by `reward_source='rubric@v1'` (344) over `'verifier@v1'` (103) —
inverted versus the execution-anchored design. Sinks never written:
`lessons_bank` 0, `reflection_log` 0, `verifier_results` 0,
`gauntlet_failures` 0, `defect_attributions` 0, `semantic_memory` 0.
`lane_region_advantage` holds 15 rows, every one `runs_count=1,
success_count=0, relative_advantage=0.0` — the router's regional learning
never accumulated a second sample.

Reproduce:
- `... "SELECT status, COUNT(*), SUM(reward_value IS NOT NULL) FROM execution_traces WHERE agent_version_id IS NOT NULL AND agent_version_id<>'' GROUP BY 1"`
- `... "SELECT substr(created_at,1,7), COUNT(*), SUM(reward_value IS NOT NULL) FROM execution_traces GROUP BY 1"`
- `... "SELECT reward_source, COUNT(*) FROM execution_traces WHERE reward_value IS NOT NULL GROUP BY 1"`
- `... "SELECT COUNT(*) FROM lane_region_advantage"` and inspect rows

### F6 — Measurement/recovery gaps: context bombs, zombies, dead cost rollup

Claim: (a) `mini-ork:codex_lens` success calls average 1.06M input tokens,
max single call 14,768,026 input tokens (llm_calls id=4317, 2026-07-10,
output 27,557, cost $2.89 — metadata `cache_read_input_tokens` 14,445,312);
27 codex_lens successes recorded zero output tokens. (b) 94
`execution_traces` are stuck `status='running'` (77 from June, all with
empty `agent_version_id`) — runs never finalized, never reflected. (c)
`runs.cost_usd` is populated for exactly 1 row ($0.00) — run-level cost
rollup is dead; all cost lives only in `llm_calls`.

Reproduce:
- `... "SELECT id, input_tokens, output_tokens, cost_usd, metadata_json FROM llm_calls WHERE id=4317"`
- `... "SELECT COUNT(*), CAST(AVG(input_tokens) AS INT), MAX(input_tokens) FROM llm_calls WHERE feature_name='mini-ork:codex_lens' AND status='success'"`
- `... "SELECT substr(created_at,1,7), COUNT(*) FROM execution_traces WHERE status='running' GROUP BY 1"`
- `... "SELECT COUNT(*), SUM(cost_usd IS NOT NULL) FROM runs"`

## Judge instructions

1. Re-run every reproduction command yourself; read the named source lines.
2. For each finding, decide: does the bug exist in the CURRENT tree, did it
   only ever explain historical rows, or is the claim wrong/overstated?
3. Dispute any number you cannot reproduce — write your measured value.
4. Severity is about ongoing damage, not history.
