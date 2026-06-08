# Production scenario: DB migration for run profiles

## Goal

Plan a migration that adds first-class run-profile storage for dispatcher
profile enrichment.

## Proposed tables

- `run_profiles`
- `run_profile_questions`
- `run_profile_answers`

## Requirements

- Preserve existing `task_runs`.
- Link profiles to `task_runs.id`.
- Store provider policy, scope allow/deny, budget, risk tolerance, artifact
  destination, and unresolved human questions.
- Migration must be idempotent.
- Rollback must be explicit and safe.

## Success criteria

- Integrity lens identifies foreign-key and uniqueness constraints.
- Rollback lens defines exact rollback or forward-fix strategy.
- Perf lens checks indexes for `run_id` and unresolved question lookup.
- Compat lens checks old clients still work.
- Edge-data lens covers missing answers, duplicate questions, and policy changes.

## Profile questions expected before planning

1. Should unanswered required questions block planning or create a pending inbox item?
2. What profile fields are mandatory for high-risk recipes?
3. Should profile answers be mutable after planning starts?

## Risk tolerance

High. Do not auto-apply migration in live mode without human approval.
