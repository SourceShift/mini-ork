# T2 — Active-State Index + Grounded Rejection schema

## Problem

Two independent signals converge on the same gap in mini-ork's planning
substrate:

1. The 2026-06-15 audit (synthesis at
   `.mini-ork/runs/run-1781523329-71306/synthesis.md`) found that the
   planner reads from `failure_memory`, `task_memory`, `gradient_records`,
   and now ContextNest via PR #17 / PR #19 — but **no single block
   surfaces the actively-open state** (unresolved errors, open
   constraints, established facts, pending goals). Memory entries
   asserted facts that were stale by hours; planner trusted them
   anyway.

2. HarnessBridge (arXiv:2606.12882, June 2026) names the same gap:
   long-horizon agents waste context reconstructing "what state am I
   in" from chronological history. The paper introduces an
   **Active-State Index** placed *before* projected history that
   records "unresolved errors, open constraints, established facts,
   pending goals, remaining decision variables." Combined with a
   per-unit projection policy, it cuts token usage 42-89% while
   matching or beating specialized harnesses.

3. The same audit's Theme C finding (`docs/RSP.md:172` TW-8 region)
   identified that mini-ork's gate verdicts at lib/coalition_gate.sh:49,
   lib/krippendorff_alpha_gate.sh:56,
   lib/citation_verifier_mechanical.sh:61,
   lib/refute_or_promote_gate.sh:128, lib/honest_ci_gate.sh:66, and
   lib/honest_ci_gate.sh:182 return free-text rationale on failure.
   Reflector has to re-extract (concern, evidence, suggestion) from
   prose. HarnessBridge's Action Projection demands the structured
   tuple as a precondition.

Full HarnessBridge analysis lives at
`.mini-ork/research-notes/arxiv-2606.12882-harnessbridge-techniques-for-mini-ork.md`
(gitignored research dir).

## Definition of Done

Two coupled deliverables shipped in one PR:

**Active-State Index (HarnessBridge Technique 1).** A new lib
`lib/active_state_index.sh` exposes `mo_active_state_block` returning a
compact JSON-then-markdown block with five sections sourced from live
state.db rows:

| Section | Source query |
|---|---|
| `unresolved_errors` | `failure_memory` rows in `db/migrations/0009_memory_namespaces.sql:1` for the current `task_class`, last 7 days, NOT yet linked to a successful follow-up |
| `open_constraints` | `policy_decisions` rows in `db/migrations/0026_policy_state.sql:1` with `result IN ('enforced','blocked')` and `event_type` like `constraint_*` |
| `established_facts` | `task_runs` rows in `db/migrations/0013_task_runs.sql:1` with `status='succeeded'` for the current `task_class`, last 30 days, top 5 by artifact-quality |
| `pending_goals` | `task_runs` rows with `status IN ('in_progress','blocked')` for the current `task_class` |
| `decision_variables` | Per-lane knobs from `config/agents.yaml:1` the planner can tune at dispatch time |

The block is injected at the top of every planner prompt in
`bin/mini-ork-plan:176` MO_INJECT_LEARNINGS block, immediately after the
existing ContextNest atoms wiring restored by PR #19 at
`bin/mini-ork-plan:195`.

**Grounded Rejection schema (HarnessBridge Technique 4).** A new lib
`lib/gates_common.sh` exposes `mo_grounded_rejection` returning the
canonical 3-tuple `(concern, evidence, suggestion)` as JSON.
A new migration adds a `grounded_rejections` table to state.db storing
one row per gate failure with provenance back to `execution_traces`
via `evidence_trace_ids` JSON column. The five gate libs above are
updated to emit the tuple on `fail` / `needs_revision` verdicts.

## Scope

### Phase 1 — infrastructure (this PR)

- New: `lib/active_state_index.sh` (~250 lines)
- New: `lib/gates_common.sh` (~150 lines)
- New: `db/migrations/0037_grounded_rejection.sql` (~50 lines, append-only triggers in the same shape as `db/migrations/0036_safety_events.sql:30`)
- Edit: `lib/context_assembler.sh` — add `context_active_state_md`
- Edit: `bin/mini-ork-plan:176-223` injection block — add active-state wire-up after the ContextNest sessions block restored by PR #19
- New tests: `tests/unit/test_active_state_index.sh` (9 cases), `tests/unit/test_grounded_rejection.sh` (10 cases)

### Phase 2 — gate adopters (follow-up PR)

Wire each gate to call `mo_grounded_rejection` on fail/needs_revision.
Mechanical edits; deferred because each gate has its own verdict
contract and adoption can happen incrementally:

- `lib/coalition_gate.sh:49`
- `lib/krippendorff_alpha_gate.sh:56`
- `lib/citation_verifier_mechanical.sh:61`
- `lib/refute_or_promote_gate.sh:128`
- `lib/honest_ci_gate.sh:182`

Total estimated diff Phase 1: ~700 lines net.

## Success Criteria

1. `bash lib/active_state_index.sh` self-test fixture exits 0 with
   ≥ 5/5 fixtures pass (each block section).
2. `bash lib/gates_common.sh` self-test fixture exits 0 with
   ≥ 4/4 fixtures pass (valid tuple, JSON validation, idempotency,
   reflector consumption).
3. `bash tests/unit/test_active_state_index.sh` exits 0.
4. `bash tests/unit/test_grounded_rejection.sh` exits 0.
5. `shellcheck` clean on all new + modified shell files.
6. `mo_check_citations kickoffs/t2-active-state-index-and-grounded-rejection.md`
   returns `citations_covered` with coverage ≥ 80%.
7. Migration applies cleanly via `mini-ork update`.
8. End-to-end smoke: `MINI_ORK_DRY_RUN=1 mini-ork plan kickoffs/t2-...md`
   produces a plan.json whose context-pack includes both an
   `active_state` section and any synthetic gate failures emit grounded
   tuples queryable via `sqlite3 state.db "SELECT concern,suggestion
   FROM grounded_rejections LIMIT 5"`.

## Success Command

```bash
bash lib/active_state_index.sh && \
bash lib/gates_common.sh && \
bash tests/unit/test_active_state_index.sh && \
bash tests/unit/test_grounded_rejection.sh && \
shellcheck lib/active_state_index.sh lib/gates_common.sh tests/unit/test_active_state_index.sh tests/unit/test_grounded_rejection.sh
```

Expected: all commands exit 0.

## Non-goals

- Do NOT train a learned harness policy (HarnessBridge Technique 5 —
  Tier 3 research-collab epic; tracked in
  `kickoffs/roadmap-tier3-research-frontier.md:1`).
- Do NOT implement pre-execution Action Projection in
  `bin/mini-ork-execute:1` (HarnessBridge Technique 2 — separate
  follow-up epic).
- Do NOT implement per-unit observation projection compression rules
  in `lib/context_assembler.sh:1` (HarnessBridge Technique 3 —
  separate follow-up).
- Do NOT modify `docs/RSP.md` § 9 known-gaps (the Pending entry for
  the safety_events wire-up tracked separately as the audit's P1 #1).

## Lineage

- HarnessBridge paper PDF at `.mini-ork/research-notes/arxiv-2606.12882-harnessbridge.pdf` (gitignored research-notes dir).
- HarnessBridge applicability analysis at `.mini-ork/research-notes/arxiv-2606.12882-harnessbridge-techniques-for-mini-ork.md` ranks this as the #1 immediate move.
- 2026-06-15 medium-tier audit synthesis at `.mini-ork/runs/run-1781523329-71306/synthesis.md` (gitignored runtime state) — Theme C identifies the gate-rejection schema gap; meta-loop section identifies the planner-stale-state gap.
- The Grounded Rejection schema composes with the existing safety_events table at `db/migrations/0036_safety_events.sql:10`: tripwire firings should write both a safety_events row AND a grounded_rejections row when the tripwire stems from a gate failure.
