---
name: wizard
description: >-
  Walk a user through launching a mini-ork run with explicit control over the
  features that shape it and what they cost. Trigger on "/wizard", "start a run
  with options", "which features should I enable", "run this but with a review
  panel / probe scorer / recursion", "how much will this run cost", or any
  request to set up a mini-ork run interactively before starting it. The wizard
  presents the controllable features (baseline always-on, premium opt-in), lets
  the user enable/disable each and tune the amount, shows the resulting plan and
  projected cost, and only then starts the run.
---

# Wizard — configure a run, see the plan, then start it

You are guiding a user through launching **one** mini-ork run. Work in four
steps, in order, and do not start the run until the user accepts at step 4. Ask
one question at a time; keep the user oriented with the step number.

The **feature catalogue is generated** from `mini_ork.features.registry` (the
single source of truth). Never hand-write the list of features — read it:

```bash
"$MINI_ORK_ROOT/bin/mini-ork" features --json          # machine form
"$MINI_ORK_ROOT/bin/mini-ork" features                 # human table
"$MINI_ORK_ROOT/bin/mini-ork" features --recipe code-fix
```

A feature that appears in the catalogue is the *only* thing the user can tune;
if the user asks for something not listed, say so and offer the closest feature.

## Step 1 — Species (recipe + target)

- Ask which **recipe** the run should use. Offer the common ones
  (`code-fix`, `framework-edit`, `bug-audit`, `recursive-self-improve`) but
  confirm against the install: `ls "$MINI_ORK_ROOT/recipes/"`.
- Ask for the **target**: the repo/worktree the lanes run in. This becomes
  `MO_TARGET_CWD` (absolute path). Never let a lane run in the framework tree —
  see the `mini-ork` skill's SAFE-USAGE CONTRACT.
- Ask for the **task** in one or two sentences (the kickoff body). Keep it to
  one deliverable.

## Step 2 — Features (enable / disable / amount)

Render the catalogue for the chosen recipe, grouped into **Baseline** (default
on, ≤1.5× cost) and **Premium** (opt-in, >1.5×). For each premium feature,
explain the multiplier in plain words (a review panel across 3 lanes ≈ 3× the
review node's cost). Let the user:

- toggle any **bool** feature on/off,
- set the **amount** for any countable feature (panel lanes, probe count,
  recursion iterations), and
- set any **cap** (e.g. a recursion budget in USD).

Premium features are **off unless the user turns them on** — that is the
policy: a feature that adds more than 1.5× is enabled by the user, never by
default. If the user enables one, say what it costs and get an explicit "yes".

Compute the projection honestly:

```bash
"$MINI_ORK_ROOT/bin/mini-ork" features --json   # read cost_multiplier / tier per feature
```

Show the running total as a **multiple of a plain run** (e.g. "≈ 1.2× baseline,
mostly from oracle gates"). Combine features by multiplying their multipliers.

## Step 3 — Plan

Show what the run will do **before** starting it:

- the recipe's nodes and their lanes (read the recipe's `workflow.yaml` under
  `"$MINI_ORK_ROOT/recipes/<recipe>/"`, or `mini-ork plan <recipe> <kickoff>`),
- which gates will run (verification stack, oracle gates, publish gate),
- the features the user chose, and the cost projection from step 2.

If the recipe's plan needs answers from the user (the Q&A gate), collect them
now — the run will otherwise block on `needs_answers`.

## Step 4 — Accept, adjust, or start

- **Adjust** → return to step 2 or 3 with the change.
- **Accept** → launch the run with the chosen features, in one step:

```bash
export MINI_ORK_ROOT="$PWD/.mini-ork" MINI_ORK_HOME="$PWD/.mini-ork"
export MO_TARGET_CWD="/abs/path/to/target"
# one export per enabled feature's knob(s) — see the catalogue's `knobs`
# premium features: also export MO_ACCEPT_PREMIUM=1 (the gate refuses them otherwise)
"$MINI_ORK_ROOT/bin/mini-ork" run <recipe> <kickoff.md>
```

Then hand off to the board / stream so the user watches it run. To relaunch with
a tweak, go back to step 2 — the previous selection is the new starting point.

---

## Feature catalogue (generated — do not edit by hand)

<!-- BEGIN GENERATED:features -->
**Baseline — default on**

| Feature | id | knob(s) | cost | default | scope |
|---|---|---|---|---|---|
| Differential assay | `assay_differential` | `MO_ASSAY_DIFFERENTIAL`<br>`MO_ASSAY_DIFFERENTIAL_N` | ×1.18 (1+0.03×executions) | 6 | all recipes |
| Metamorphic relations | `assay_relations` | `MO_ASSAY_RELATIONS`<br>`MO_ASSAY_RELATIONS_K` | ×1.21 (1+0.07×relations) | 3 | all recipes |
| GRPO learning writeback | `learning_writeback` | `MO_LEARNING_WRITEBACK` | ×1 | 1 | all recipes |
| Oracle gates (auto) | `oracle_gates` | `MO_ORACLE_GATES_AUTO` | ×1.2 | 1 | all recipes |
| Plan Q&A gate | `plan_qa_gate` | `MINI_ORK_EXECUTE_GATE` | ×1 | 1 | all recipes |
| PRM scoring | `prm_score` | `MO_PRM_SCORE` | ×1.05 | 1 | all recipes |
| Reward stamp | `reward_stamp` | `MO_REWARD_STAMP` | ×1 | 1 | all recipes |

**Premium — opt-in (>1.5×)**

| Feature | id | knob(s) | cost | default | scope |
|---|---|---|---|---|---|
| Code-arm probe scorer | `code_arm_scorer` | `MO_APPLY_SCORER`<br>`MO_APPLY_CODE_PATCH`<br>`MO_APPLY_PROBE_MAX_TASKS` | ×2 (2×probes) | 1 | all recipes |
| Recursion | `recursion` | `MO_RECURSION_BUDGET_CAP_TOTAL_USD`<br>`MO_RECURSION_MAX_ITERATIONS` | ×3 (1×iterations) | 3 | recursive-self-improve |
| Review panel | `review_panel` | `MO_REVIEW_PANEL` | ×3 (1×lanes) | codex kimi glm | all recipes |
<!-- END GENERATED:features -->

---

Run `mini-ork features render-skill` to refresh the block above from the
registry. A gate (`mini-ork features check-skill`) fails when it is stale, so a
newly registered feature cannot be silently missing here.
