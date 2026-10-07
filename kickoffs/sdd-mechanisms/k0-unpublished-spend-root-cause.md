# K0 — Why does 66.9% of mini-ork spend go to runs that never publish?

Context (read first):
- `docs/plans/2026-10-07-sdd-mechanisms-for-mini-ork.md`: the SDD mechanisms
  plan. This investigation can reorder its K-sequence.
- Memory notes on cost-meter accuracy: `project_cost_meter_and_routing_experiment.md`
  and `project_audit_panel_6findings.md` in
  `/Users/admin/.claude/projects/-Volumes-docker-ssd-ps-mini-ork/memory/`.

## Goal

Find the root causes of unpublished spend. The baseline, re-measured
2026-10-07 on `.mini-ork/state.db`:

- 1338 runs, 467 published (34.9%)
- 66.9% of recorded `cost_usd` spent on runs whose status is not `published`

Split that unpublished spend into causes, measure each one in dollars and run
counts, and map each cause to the I1–I9 improvements in the plan (or to "not
SDD"). The result is evidence for prioritizing; do not change any code.

## Hypotheses to test (confirm or reject each with numbers)

- **H1 — measurement artifact.** Before 2026-10-01, `cost_usd` overstated
  non-Anthropic spend by about 7x. Before 2026-09-12 it undercounted by about
  34%. The 66.9% may be partly a meter artifact.
- **H2 — never finalized.** 385 runs are still `executing`, `classified`,
  `planned` or `reviewing` (383 of them are older than a day; about $478
  recorded). These are crashed or abandoned runs, not rejected ones.
- **H3 — no publish expected.** Some recipes never publish by design: research,
  audit, eval and smoke recipes, goal-loop children, dry runs, experiments
  such as SWE-bench and the routing experiment, and framework-edit diffs that
  were shipped by hand (framework-edit published only 6/169).
- **H4 — false rejects.** Verify passed but the run failed: code-fix 98 runs,
  framework-edit 97. 40% of run verdicts disagree with hidden tests, so good
  work may have been discarded.
- **H5 — dead before work.** Runs that died before implementing anything:
  - planner failures (87/995 run dirs have a `plan-failure-*` file)
  - `needs_answers` blocks (157/878 `run_profile.json`)
  - lane, credential or timeout failures (8.5% of LLM calls failed)
- **H6 — rollback destroyed a good edit** (goal-loop).

## Acceptance

- **AC1:** One table splits unpublished `cost_usd` into mutually exclusive
  buckets. The buckets sum to the total unpublished cost within ±0.5%.
- **AC2:** Every dollar figure appears twice: raw `cost_usd`, and corrected
  for the H1 meter eras (state the correction rule). If no correction is
  possible, say so and exclude the affected window.
- **AC3:** Each bucket is labeled with one of:
  - (a) no publish expected
  - (b) never finalized
  - (c) false reject
  - (d) real failure, naming the stage it failed in
  - (e) infra or lane failure
  - (f) other
- **AC4:** The top 3 root causes, ranked by corrected dollars. Each cites at
  least 2 run ids whose run dirs (`.mini-ork/runs/<id>/`) you actually opened
  to confirm the cause.
- **AC5:** Each root cause maps to I1–I9 or to "not SDD". If the evidence
  changes the plan, propose a K-order change with a one-line reason.
- **AC6:** Every number has the exact SQL or command that produced it, kept in
  a "Reproduce" section.

## Files in scope (touch ONLY these)

- `docs/audits/20261007-unpublished-spend-root-cause.md`: the report (new).

Do NOT modify any other file. The databases are read-only: use
`sqlite3 -readonly`. The sources are:
- `.mini-ork/state.db` (tables `task_runs`, `execution_traces`, LLM call
  tables)
- `.mini-ork/runs/*/`
- `/Volumes/docker-ssd/ps/sdd-10x-ork/home/state.db`, the SDD campaign:
  45 runs, 0 published, $617.87

## Out of scope

- Code, schema or config changes, and fixes of any kind (those are K1–K10).
- Re-running past runs, or making any paid LLM call.
- Changing run statuses in any DB.

## Verification command

```bash
test -s docs/audits/20261007-unpublished-spend-root-cause.md && \
sqlite3 -readonly .mini-ork/state.db \
  "SELECT round(sum(cost_usd),2) FROM task_runs WHERE status<>'published'"
```

The dollar total this prints must equal the "total unpublished (raw)" figure in
the report's AC1 table.
