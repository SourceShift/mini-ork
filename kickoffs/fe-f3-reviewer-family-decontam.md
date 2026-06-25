# F3 (P0): reviewer_model column + cross-family panel verdict for PRM

## Goal

Close the verifier-gaming vector described in arXiv 2605.12474 (*Reward Hacking
in Rubric-Based RL*): rubric/verifier-trained policies exploit the *single*
verifier, and a cross-family panel exposes that gaming. mini-ork's PRM currently
adds a flat +0.15 for any approving `reviewer_verdict` string, with no idea
*which model* produced it — so a same-family reviewer rubber-stamping its own
family's work counts as independent evidence. Fix 4 of review_id=32 had to
*remove* same-family decontamination outright because `execution_traces` has no
`reviewer_model` column (the doer's lane was a bad proxy). The PRM header note
says decontamination "awaits a schema change." This epic is that change.

Two deliverables, tightly coupled (one table, one reward term):

1. **Add `reviewer_model` to `execution_traces`** so the verdict term keys on
   the *actual reviewer* lane, not the doer's lane.
2. **Make the +0.15 verdict term require a cross-family verifier quorum** —
   wire PRM's verdict term to the `schema-judge-panel` agreement-weighted
   verdict (gated by `lib/krippendorff_alpha_gate.sh`) instead of a single
   `reviewer_verdict` string. The term counts only when the approving panel
   contains at least one judge of a *different* family than the implementer.

## Background (verified this session)

- `lib/process_reward.sh` header (lines 22-33) + inline notes (64-65, 107-112,
  151, 199) already anticipate this: "Same-family neutralization awaits a schema
  change", "Awaiting a real reviewer_model column in execution_traces."
- The +0.15 term lives in BOTH PRM copies — `prm_score_trace` (line ~111) and
  `prm_backfill` (line ~201). They are mirrored verbatim and MUST stay
  byte-identical; drift silently breaks the router.
- `lib/trace_store.sh:103-127` is the central `execution_traces` INSERT/upsert
  (`trace_write`). `reviewer_verdict` is bound at lines 106/113/127. Add
  `reviewer_model` binding alongside it.
- `recipes/schema-judge-panel/` is the existing cross-family judge panel;
  `lib/krippendorff_alpha_gate.sh` is the agreement gate. PRM should consult the
  panel's agreement-weighted verdict, not invent a new panel.
- `bin/mini-ork-execute` dispatches the reviewer; it must record the reviewer's
  lane/model into the trace's new `reviewer_model` column.

## Migration slot — IMPORTANT

A concurrent framework-edit (run-1782386506-37416, "objective-aware normalized
trace rewards", panel PASS=75) has ALREADY claimed
`db/migrations/0041_execution_traces_objective_aware_reward.sql` on its branch
(not yet merged to main). **This epic MUST use `0042`**, not 0041, to avoid a
migration-number collision. F3's migration adds a *different* column
(`reviewer_model`), so it composes cleanly with 0041's columns — but the number
must not clash. Make it idempotent: gate the `ALTER TABLE ... ADD COLUMN` behind
a `pragma_table_info` existence check (init.sh applies via `sqlite3 DB < file`,
so dot-commands / guarded SQL are valid — mirror the 0039 self-guard pattern).

## Scope (in / out)

- IN: `db/migrations/0042_execution_traces_reviewer_model.sql` (idempotent),
  `lib/trace_store.sh` (bind `reviewer_model` on insert/upsert),
  `bin/mini-ork-execute` (write the reviewer's lane into `reviewer_model` when a
  review node resolves), `lib/process_reward.sh` (move the verdict term onto the
  cross-family panel verdict; keep BOTH PRM copies byte-identical).
- IN: the cross-family rule — verdict term counts only when the approving
  reviewer's family ≠ the implementer's family (parse family from the lane id:
  opus / minimax / glm / kimi / codex). Single-judge or same-family approval →
  verdict term is 0.
- IN: config knobs, not hardcode — min panel quorum and the agreement floor read
  from config with safe defaults; cold/missing panel data degrades to "no
  verdict credit", never a crash.
- OUT: no change to W_STATUS/W_TOOL/W_FILE/W_DURATION/W_COST weights, no change
  to ACTIVITY_CAP, no GRPO math change, no new judge model. Do not touch the
  0041 reward-fields work — assume it lands separately.

## DB SAFETY

Never mutate the live `.mini-ork/state.db` in implementer nodes. Validate all
schema work on a COPY. Re-running `db/init.sh` on the copy must be idempotent
(no further changes second run).

## Definition of Done

- `0042_execution_traces_reviewer_model.sql` applies cleanly via `db/init.sh` on
  a copy of the live DB; `execution_traces.reviewer_model` exists; re-run is a
  no-op.
- `lib/trace_store.sh` binds `reviewer_model`; a review trace written through
  `trace_write` persists the reviewer's lane.
- PRM's +0.15 verdict term is gated on a cross-family approving panel quorum;
  same-family or single-judge approval yields 0 credit. `prm_score_trace` and
  `prm_backfill` remain byte-identical (diff the two weight/verdict blocks).
- Cold-DB safe: a trace with no panel / no `reviewer_model` scores with verdict
  term 0 and no traceback.
- `shellcheck` + `bash -n` clean on all touched shell.

## Success Criteria (proof)

- Unit test: seed a trace with status=success + cross-family approving panel →
  PRM includes +0.15; reseed with same-family-only approval → PRM excludes it;
  reseed with empty panel → verdict term 0, no crash.
- `cp .mini-ork/state.db /tmp/f3-proof.db && MINI_ORK_DB=/tmp/f3-proof.db bash db/init.sh`
  succeeds; `sqlite3 /tmp/f3-proof.db "SELECT COUNT(*) FROM pragma_table_info('execution_traces') WHERE name='reviewer_model';"` returns 1.
- `diff <(sed -n '/PRM weight table/,/W_COST/p' prm_score_trace) <(...prm_backfill...)` shows the copies stayed in sync.

## Validation bar (epic-sized → medium validation)

Panel review (cross-family lenses) + Krippendorff-α on the cross-family rule
design + citation_verifier_mechanical on the 2605.12474 claim + refute-or-promote
on "the verdict term can no longer be lifted by a same-family rubber stamp."
Single-lens self-review is insufficient.

## Model Preference

Cross-family decontamination rule design / judgment node → `opus`. Implementer
nodes → minimax or codex (never glm; glm analysis-only).
