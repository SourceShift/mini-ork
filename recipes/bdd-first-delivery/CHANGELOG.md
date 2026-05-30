# Changelog — bdd-first-delivery recipe

## 0.1.0 — 2026-05-30 — ported from internal mini-orch, framework-API rewrite (Phase A redesign)

Initial release. Ported from a production internal mini-orch pipeline and rewritten to be domain-neutral for the framework's user-land recipe system.

### What's in this release

- `workflow.yaml` — declarative node + edge graph for the full BDD-first pipeline
- `task_class.yaml` — recipe metadata and auto-routing keywords
- `artifact_contract.yaml` — composite artifact spec (patches + specs + verdicts)
- `prompts/decomposer.md` — generic epic decomposer (N ≤ 7 sub-epics, leaf/integration/spec BDD roles)
- `prompts/spec_author.md` — generic BDD spec author for one sub-epic
- `prompts/spec_reviewer.md` — generic spec adequacy reviewer
- `prompts/implementer.md` — generic implementer working against an approved spec
- `prompts/reviewer.md` — aggregate reviewer across all sub-epic outputs
- `prompts/self_correction.md` — minimal-patch self-correction on REQUEST_CHANGES
- `prompts/mutation_adversary.md` — adversarial spec-robustness probe
- `verifiers/playwright_runner.sh` — Playwright verifier that emits `bdd-verdict.json`
- `lib/dispatch.sh` — recipe-internal parallel sub-epic dispatch helper
- `example-kickoff.md` — example multi-section UI kickoff
- `example-output.md` — expected pipeline output for the example kickoff
- `MIGRATION.md` — mapping from internal mini-orch components to recipe components
- `README.md` — overview, workflow diagram, cost and runtime estimates
