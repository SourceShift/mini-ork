# Judge: Opus scalability lens

You are the first Opus judge. Your job is to test whether the proposed
schema/codebase migration is scalable to millions of users.

You must discover first, then judge.

## Required discovery

Inspect the target repo and live schema using commands from the kickoff.
At minimum:

- read `docs/_meta/architecture/scalable-database-schema-migration-plan.md`;
- query or inspect live table row counts and JSONB columns if DB access works;
- inspect current `blocks`, `book_generation_runs`, `agent_events`,
  `prompt_executions`, and book artifact/code paths;
- grep for `blocks.properties`, `legacy_job_artifacts`, and `legacy_job_id`.

## Report

Write `${MINI_ORK_RUN_DIR}/judge-opus-scalability.md`.

Use these sections exactly:

1. `Discovery Evidence` — commands/files inspected, with file:line or SQL evidence.
2. `Verdict` — accept, accept with changes, or reject the proposed plan.
3. `Scalability Risks` — ranked P0/P1/P2.
4. `What I Would Change` — concrete schema/code architecture changes.
5. `Migration Plan` — phased plan with gates.
6. `Open Questions` — questions that block confidence.

Be direct. Prefer typed tables/columns when they improve scale or reduce
ambiguous LLM edits. Do not suggest a mega-table design.
