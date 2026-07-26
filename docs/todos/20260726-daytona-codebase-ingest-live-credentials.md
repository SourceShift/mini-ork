# Daytona codebase-ingest live credential follow-up

**Status:** blocked on service credential configuration  
**Last worked on:** 2026-07-26  
**Scope:** Researcher-managed Daytona execution of MiniOrk's codebase-ingest recipe

## What was verified

- A clean Daytona sandbox cloned the current MiniOrk repository over HTTPS,
  installed the user launcher, initialized a fresh Git project, validated it,
  and completed the code-fix dry-run.
- The Researcher service created a Daytona sandbox, uploaded the
  codebase-ingest overlay recipe, cloned the target repository, and started
  MiniOrk's planner.
- The provider-backed planner then failed with HTTP 401 before any parallel
  lenses ran. The report artifact was therefore not produced.

## Remaining work

1. Repair the Researcher service's provider configuration so the planner has
   both a valid credential and explicit model metadata.
2. Keep credential preflight enabled; it must accept the codebase-ingest
   recipe without a diagnostic bypass.
3. Re-run the recipe against https://github.com/SourceShift/mini-ork.git at
   main with the architecture, runtime, learning, and recipe facets.
4. Verify that the output artifact contains findings from every configured
   lens, identifies the checked repository revision, and records a verified
   completion status.
5. Retain the run ID and artifact path as the end-to-end acceptance evidence.

## Acceptance command shape

~~~text
Researcher runMiniOrkInSandbox(
  recipe: codebase-ingest,
  repo_url: https://github.com/SourceShift/mini-ork.git,
  ref: main,
  facets: [architecture, runtime, learning, recipes]
)
~~~

Success means a provider-backed completed run with the structured
codebase-ingest report—not merely sandbox creation, cloning, or a dry run.

