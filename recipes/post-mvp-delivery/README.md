# Recipe: post-mvp-delivery

`post-mvp-delivery` is the discovery-first product delivery recipe.
It is for tasks where a user asks mini-ork to deliver a post-MVP product
or capability, but the implementation details are not yet obvious.

## Behavior

The recipe does not jump straight to code. It runs a staged workflow:

1. `discovery_planner` identifies unknowns and research questions.
2. Four researcher lenses run in parallel:
   - `product_lens`: user segments, workflows, value tradeoffs.
   - `architecture_lens`: implementation architecture and repo seams.
   - `integration_lens`: external services, APIs, data, migration risks.
   - `validation_lens`: tests, release gates, observability, success metrics.
3. `options_synthesizer` produces `options.md` with 2-4 options, tradeoffs,
   risks, estimated effort, confidence, and a recommended default.
4. `options_completeness` verifies that the options package is user-decision
   ready.
5. `delivery_planner` turns the selected or recommended option into a delivery
   plan.
6. `selected_option_gate` blocks implementation until the run contains
   `selected-option.md` or the kickoff explicitly preselects an option.
7. `implementer` proceeds only from that researched plan.

## User Contract

A user can start from a single markdown file:

```bash
mini-ork run post-mvp-delivery path/to/kickoff.md
```

The workflow should produce an options package before implementation. If the
kickoff does not specify a chosen option, the options package is the checkpoint
for the user to decide before continuing.

To continue after reviewing options, add a run-local `selected-option.md`
beside `options.md` with the chosen option and rationale, or include an explicit
`Selected Option:` / `Preselected Option:` line in the kickoff.

## Why This Exists

Post-MVP work usually fails when an agent treats ambiguity as permission to
guess. This recipe makes uncertainty explicit: mini-ork researches what it
does not know, presents options, and only then extends delivery.
