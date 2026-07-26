# recipe: refactor-audit

A multi-model audit recipe that dogfoods mini-ork to audit a codebase
(including mini-ork itself) for scalability, security, performance,
or architectural shape. Five model-lens stances run in parallel; a
deterministic transform anonymizes their reports before a synthesis pass.

## What this recipe does

Given a kickoff describing **what to audit** and **what dimensions to
audit on**, the recipe:

1. **Classify** — routes to `refactor_audit` task class
2. **Plan** — generates an audit plan with 5 lens nodes. The recipe also
   declares a deterministic `anonymize_panel` node; it is not an LLM plan step.
3. **Execute** — dispatches the 5 lenses in **parallel** (each runs
   under a different model lane per workflow.yaml node.model_lane):
   - **glm-lens** → tactical bottleneck scan (fast + broad)
   - **kimi-lens** → code-level refactor diffs (long-context)
   - **codex-lens** → LLM-dispatch / cost optimization (deep
     code-intelligence)
   - **opus-lens** → architectural shape + synthesis perspective
   - **minimax-lens** → cross-system integration and data-flow tracing
4. **Transform** — materializes an anonymous `Response A` through `Response E`
   panel bundle. The label map is system-only; synthesis receives no source
   lane, producer node, or raw artifact path.
5. **Verify** — checks all 5 lens reports, the anonymous panel bundle, and
   synthesis references exist and contain the required evidence.
6. (Out-of-band) **Reflect** — gradients written from each lens's
   trace; pattern emergence detects recurring findings across past
   audits

## Output artifacts

- `${MINI_ORK_HOME}/runs/<run_id>/lens-glm.md`
- `${MINI_ORK_HOME}/runs/<run_id>/lens-kimi.md`
- `${MINI_ORK_HOME}/runs/<run_id>/lens-codex.md`
- `${MINI_ORK_HOME}/runs/<run_id>/lens-opus.md`
- `${MINI_ORK_HOME}/runs/<run_id>/lens-minimax.md`
- `${MINI_ORK_HOME}/runs/<run_id>/panel-responses.md` — anonymous
  response bundle for the synthesizer
- `${MINI_ORK_HOME}/runs/<run_id>/workspace/system/anonymize-panel/label-map.json`
  — system-only source-label receipt
- `${MINI_ORK_HOME}/runs/<run_id>/synthesis.md` — composed final audit
- (Optional) `docs/refactor/<slug>-AUDIT.md` published by the
  publisher node

## When to use

- Before a major release — audit your own framework for what to fix in v0.x+1
- When user-reported issues cluster on a single subsystem — audit that subsystem
- Quarterly check-up — schedule via `make audit-mini-ork` cron

## When NOT to use

- For specific bug fixes — use `recipes/code-fix/` (audit is overkill)
- For shipping a feature — use `recipes/bdd-first-delivery/` (audit
  produces analysis, not patches)
- Sub-1K LOC codebases — manual audit is faster

## Cost expectation

| Scale | Estimated cost | Wall-clock |
|---|---|---|
| 1K LOC, single concern | $1-3 | 3-5 min |
| 50K LOC, full framework | $20-40 | 30-60 min |
| 500K LOC, multi-service | $200-400 | 2-4 hours |

Cost is dominated by synthesis context and the selected provider lanes.
Configure via `MO_REFACTOR_AUDIT_BUDGET_USD`.

## How to run

```bash
# 1. Write a kickoff describing the audit
cp ~/ps/mini-ork/recipes/refactor-audit/example-kickoff.md ./my-audit.md
# Edit my-audit.md — name the target dir, the dimensions, the depth.

# 2. Dispatch
mini-ork run refactor-audit ./my-audit.md

# 3. The synthesis lands under .mini-ork/runs/<run_id>/synthesis.md
```

## Dogfood note

This recipe is what produced
[`docs/refactor/SCALABILITY-AUDIT.md`](../../docs/refactor/SCALABILITY-AUDIT.md).
The first run was actually composed via the Agent tool (not via this
recipe) because v0.1.1's `llm_dispatch` bare-name issue (audit finding
D-007) blocked real-LLM dispatch. Once v0.2 lands D-007's one-line fix,
this recipe will run end-to-end via mini-ork itself.

## Customization

| Knob | Where | Effect |
|---|---|---|
| Lens count | `workflow.yaml` ports + edges | Add a lens output and bind it to `anonymize_panel.reports` |
| Models per lens | `workflow.yaml` node.model_lane | Swap glm→haiku, opus→sonnet, etc. |
| Panel transform | `workflow.yaml` `anonymize_panel` | Swap a registered transform without changing harness wiring |
| Synthesis depth | `prompts/synthesis.md` | Customize the cross-lens ranking matrix |
| Output target | `verifiers/audit-published.sh` | Publish to docs/, GitHub Issues, Slack, etc. |

## See also

- `docs/SCALABILITY-AUDIT.md` — example output produced by this recipe
- `docs/EXTENSION.md` — adding new lenses
- `docs/SAFETY.md` — bounded-autonomy ladder (audits are rung-1: tune
  prompt wording based on findings; promotion to rung-7 not allowed)
