# mini-ork Production Scenario Plan

This is the production validation lane for mini-ork. It is intentionally
different from `tests/run-all.sh`: the entrypoint is always a real markdown
kickoff passed through `mini-ork run <kickoff.md>` or the explicit override
form `mini-ork run <recipe> <kickoff.md>`.

The default runner mode is dry-run so it is safe during provider freezes, but
the scenario design is live-run ready. A scenario only counts as production
validated when it has been executed with `MINI_ORK_DRY_RUN=0`, provider logs
captured, artifacts inspected, and the run row checked in `state.db`.

## Why This Exists

Users do not experience mini-ork as unit tests. They experience:

1. Write one `.md` file.
2. Run `mini-ork run kickoff.md`.
3. The dispatcher classifies it, enriches the profile if confidence is low,
   plans from all available mini-ork data, executes the workflow, verifies the
   artifact, and persists what happened.

The current CLI already has the `.md -> classify -> plan -> execute -> verify`
spine. The missing product behavior is the profile-enrichment step between
classification and planning.

## Run Commands

Safe smoke of the production lane:

```bash
python3 scripts/run_production_scenarios.py --mode dry-run
```

Live production validation, once provider policy allows every lane required by
the selected recipes:

```bash
python3 scripts/run_production_scenarios.py --mode live
```

Run one scenario:

```bash
python3 scripts/run_production_scenarios.py --mode dry-run refactor-audit
```

Validate the `.md`-only dispatcher path:

```bash
python3 scripts/run_production_scenarios.py --mode dry-run --md-only code-fix
```

## Acceptance Contract

For every scenario:

- The kickoff is a real markdown file, not an inline string.
- The command uses `bin/mini-ork run`, not direct bin internals.
- `classify` emits the expected task class.
- `plan` writes `.mini-ork/runs/<run-id>/plan.json`.
- `execute` dispatches the workflow nodes from `recipes/<recipe>/workflow.yaml`.
- `verify` emits a JSON verdict.
- In live mode, expected artifacts exist and the recipe verifier reads them.
- In live mode, `task_runs` records recipe, task class, status, verdict, and
  cost metadata where available.

## Scenario Matrix

| ID | Recipe / feature | Kickoff | What it proves | Required live providers |
|---|---|---|---|---|
| P01 | `code-fix` | `kickoffs/code-fix-real-bug.md` | single-patch lifecycle, scope gate, reviewer/verifier handoff | planner, implementer, reviewer |
| P02 | `docs` | `kickoffs/docs-real-edit.md` | doc-only workflow, grep/link verification, low-risk publisher path | planner, implementer |
| P03 | `bdd-first-delivery` | `kickoffs/bdd-settings-page.md` | decomposition, partitioned spec authoring, BDD runner, publisher/rollback decision | decomposer, spec_author, implementer, reviewer |
| P04 | `refactor-audit` | `kickoffs/refactor-audit-provider-roster.md` | glm/kimi/codex/opus audit panel, synthesis, lens completeness verifier | glm, kimi, codex, opus |
| P05 | `research-synthesis` | `kickoffs/research-synthesis-heterogeneous-review.md` | web/lit/code/narrative research panel, source completeness, dissent handling | glm, kimi, codex, opus |
| P06 | `blog-post` | `kickoffs/blog-post-launch.md` | five-lens drafting, audience/counterargument lenses, draft completeness | glm, kimi, codex, opus, minimax |
| P07 | `db-migration` | `kickoffs/db-migration-user-profile.md` | migration safety panel, rollback/perf/compat/edge-data review | glm, kimi, codex, opus, minimax |
| P08 | `ops-runbook` | `kickoffs/ops-runbook-symlink-hang.md` | incident runbook generation from a real observed mini-ork failure mode | glm, kimi, codex, opus, minimax |
| P09 | `ui-audit` | `kickoffs/ui-audit-readme-cli.md` | UI/UX audit panel for first-run CLI/docs user journey | glm, kimi, codex, opus, minimax |
| P10 | `.md`-only dispatcher | reuse P01 without recipe arg | `mini-ork run kickoff.md` classifies first, resolves recipe, then runs the lifecycle | none in dry-run |
| P11 | explicit recipe override | reuse P01 with low code keywords | `mini-ork run code-fix ...` honors explicit recipe over classifier ambiguity | none in dry-run |
| P12 | inferred classifier | each kickoff via `mini-ork classify` | classifier routes natural-language kickoffs to recipe classes without explicit override | none |
| P13 | profile enrichment | profile questionnaire fixture | dispatcher asks confidence-building questions before planner when kickoff lacks critical fields | none until implemented |
| P14 | provider policy | same matrix with provider allowlist | temporary no-Claude policy affects execution selection, not durable workflow topology | allowed non-Claude providers |
| P15 | trace / reflect / improve | live run after P01 or P02 | execution traces become gradients, patterns, candidates, eval/promote inputs | same as source run |
| P16 | tiny guardrails | oversized kickoff, hook dir, malformed workflow | small security/product promises remain true under CLI usage | none |

## Dispatcher Profile Enrichment Requirement

The dispatcher should build a run profile before planning. This is not a
separate user-facing wizard; it is part of the first run step after
classification.

Target flow:

```text
kickoff.md
  -> classify task_class + confidence + missing_profile_fields
  -> ask focused questions if confidence is below threshold
  -> write run_profile.json next to plan.json
  -> planner receives kickoff + run_profile + task_class + recipe metadata
  -> execute uses the same run profile for budget, scope, and risk gates
```

Minimum profile fields:

| Field | Why it matters |
|---|---|
| `target_repo` | Prevents agents from auditing or editing the wrong tree. |
| `user_goal` | Gives the planner the actual outcome, not only the recipe name. |
| `success_criteria` | Turns vague requests into verifier-friendly checks. |
| `scope_allow` / `scope_deny` | Feeds scope gates and reduces accidental edits. |
| `risk_tolerance` | Decides whether publisher may auto-commit or must escalate. |
| `budget_cap_usd` | Controls budget gates before provider dispatch. |
| `provider_policy` | Supports temporary constraints such as no Claude for 24 hours. |
| `artifact_destination` | Tells publisher where the final artifact belongs. |
| `verification_command` | Lets planner prefer executable proof over prose. |
| `human_questions` | Captures unresolved decisions instead of hallucinating. |

Question strategy:

- Ask at most 3 questions at a time.
- Questions must be recipe-specific and derived from missing fields.
- Use defaults when the kickoff is explicit enough.
- Stop and mark the run `blocked_profile` if a high-risk recipe lacks required
  scope, budget, or rollback answers.

Example questions for `db-migration`:

1. Which database engine and version should this migration target?
2. Is downtime allowed? If yes, what is the maximum window?
3. What exact rollback command or backup restore path should the planner assume?

Example questions for `code-fix`:

1. What command proves the bug is fixed?
2. Which files or directories are in scope?
3. Should mini-ork commit the patch if verification passes?

## Current Gaps Found While Designing This

| Gap | Impact | Suggested change |
|---|---|---|
| No run-profile artifact between classify and plan | Planner must infer missing operational context from kickoff prose. | Add `bin/mini-ork-profile` or make `classify` emit `profile_questions=` and persist answers. |
| Dry-run plan is a placeholder | Dry-run validates topology but not planner quality. | Add `MO_PROD_SCENARIO_MODE=profile-only` and `MO_PROD_SCENARIO_MODE=plan-only-live` lanes. |
| Full suite can hang in symlink security scenario | Long production runs may be blocked by a known guardrail test. | Add timeout around that security probe or explicit symlink rejection in `db/init.sh`. |
| Provider policy is not first-class profile data | Temporary constraints get confused with durable recipe topology. | Store `provider_policy` in run profile and let dispatch enforce it. |

## Promotion Rule

A scenario moves from `designed` to `validated` only when a markdown report is
added under `docs/production-validation/runs/` with:

- command
- provider policy
- run id
- artifacts created
- verifier JSON
- DB row snapshot
- human review notes
- bugs or required changes
