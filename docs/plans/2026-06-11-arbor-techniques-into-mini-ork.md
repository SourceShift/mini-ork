# Arbor → mini-ork: techniques worth borrowing

Plan written 2026-06-11 after reading https://github.com/RUC-NLPIR/Arbor and arxiv:2606.11926.

## What Arbor is, in one paragraph

Two persistent agents (Coordinator + Executor) grow an **Idea Tree** through a six-step
cycle (Observe → Ideate → Select → Dispatch → Backpropagate → Decide). Each Executor
runs its assigned hypothesis in an isolated git worktree, evaluates on a dev split,
and only merges if the change clears a configurable margin on a held-out test split.
Lessons from leaf experiments propagate upward to ancestor nodes so future ideation
starts smarter. Persistent sessions, graduated HITL modes, slash commands during
execution, YAML plugins for domain switching, and a one-screen "Research Contract"
intake round out the runtime.

## What mini-ork has today that overlaps

| Arbor concept | mini-ork's closest analog | Gap |
|---|---|---|
| Idea Tree | `self_improve_runs.parent_run_id` schema | Schema supports tree, loop uses it linearly |
| Coordinator + Executor | `bin/mini-ork` (one-shot) + `bin/mini-ork-execute` | No persistent Coordinator across runs |
| Worktree per experiment | `bin/mini-ork-self-improve` worktree per iter | Already shipping — closest overlap |
| Dev / test split + merge threshold | `verifiers/*.sh` (single artifact) | All verifiers run on one snapshot; no held-out |
| Insight backpropagation | `gradient_records` injected at planner | Global injection; no upward propagation to ancestor branches |
| Slash commands during execution | `.stop-requested` cooperative stop file | One signal, no `/tree` `/evidence` `/cost` |
| HITL modes (auto / direction / review / collaborative) | `profile_status=needs_answers` (binary) | Single gate, not graduated |
| Research Contract intake | `run_profile.json` (one-shot Q&A) | No conversational back-and-forth |
| Read-only companion agent (HITL) | WhyCard component (UI only) | No chat-with-context-isolated-agent during pause |
| YAML plugins | Recipes (heavy) | No lightweight plugin overlay on existing recipes |
| Staged budgets (smoke / pilot / full) | `budget_gate` (single cap) | Single cap, no progressive escalation |

## Ten ideas, ranked by leverage

Leverage = (operator pain it removes) × (chance it shifts brand-thesis credibility) ÷ (engineering cost).

### Top tier (ship in v0.4)

#### 1. Idea Tree for `recursive-self-improve` (highest leverage)

Replace the linear iter loop with an actual exploration tree.

- Schema: `self_improve_runs` already has `parent_run_id` and `notes`. Add
  `branch_status` enum (`pending` / `running` / `pruned` / `harvested`), `score_dev`,
  `score_test`, and `insights_json` columns. One migration.
- Loop: `bin/mini-ork-self-improve` becomes a Coordinator that maintains the tree,
  picks the most promising frontier leaves per cycle, dispatches them in parallel
  worktrees (already done), and prunes failed branches instead of abandoning.
- The hardest part is **child ideation**. Currently each iter gets the same kickoff;
  child generation needs a small LLM call per cycle ("given parent's failure mode,
  propose 1–3 child variations"). One LLM call per cycle is cheap.
- Concrete first-step: visualize the existing 33-iter history AS a tree in the UI
  Trajectory page. We have the data; the SelfImproveDetailPage already shows
  `parent_run_id` and `children` per row. Just draw it.

#### 2. Dev / held-out test split discipline

Right now `verifiers/*.sh` run on one artifact snapshot. Add a two-split convention:

- Recipe authors declare `artifact_contract.yaml:test_split` — a script that
  partitions the work into "dev slice" (iterate) and "held-out test slice" (gate).
- Self-improve: dev slice = "the 5 most-recent failures the loop knows about",
  test slice = "5 random failures it hasn't seen this loop". A patch only promotes
  if test slice metric also improves by `merge_threshold` (configurable, default 5%).
- Audit recipes: dev slice = 80% of target files for iteration, held-out 20% for
  the final verifier. Closes the "we tuned the lens until it passed" failure mode.
- Schema: add `task_runs.score_dev` and `task_runs.score_test`; promotion gate
  reads both.

#### 3. Insight backpropagation up the tree

Currently `gradient_records.failure_mode` is injected into ALL future planner
prompts for the same task_class. Arbor's pattern is more targeted:

- A gradient extracted from leaf node N propagates **upward** to ancestor nodes.
  Ancestor's other children inherit the lesson.
- For mini-ork: add `gradient_records.scope_node_id` (leaf node it was extracted
  from) and `gradient_records.propagated_to[]` (ancestor nodes it's been hoisted
  to). High-confidence gradients (e.g. `confidence>0.9`) auto-hoist.
- Injection at dispatch reads the node-specific scope, not the global pool. Reduces
  prompt bloat and improves attribution.

### Middle tier (ship in v0.5)

#### 4. Slash commands during execution

`/tree`, `/evidence`, `/cost`, `/pause`, `/resume`, `/abort` while a run is dispatched.

- We already have `.stop-requested` for cooperative stop (`bin/mini-ork-execute:476`).
  Generalize: a `.signals/` dir per run dir that the executor polls between nodes.
- New `bin/mini-ork signal <run_id> <signal>` writes the file. Coordinator-side
  branch in `_dispatch_node` reads it.
- UI gets buttons: "Pause", "Show tree", "Show cost rollup" — wires to the same
  signal mechanism via a new `POST /api/v1/task-runs/:id/signal` endpoint (this
  would be the first writable API surface — controlled scope, single-action).

#### 5. Staged budget runs (smoke / pilot / full)

Recipe-creator's example-kickoff already wastes $5 if the recipe shape is broken
on dispatch. Smoke-first prevents this.

- Add `--stage smoke|pilot|full` flag to `bin/mini-ork run`. Default `full`.
- Smoke: dispatches one researcher node + the validator (skips drafters, arbiter,
  publisher). Cost cap $0.50, wall-clock 60s.
- Pilot: full DAG but on a one-file slice of the target. Cost cap $5, wall-clock
  5 min.
- Full: today's behavior.
- Recipe authors declare `task_class.yaml:stages.{smoke,pilot,full}` with per-stage
  cost + wall-time caps + node filter.

#### 6. Graduated HITL modes

Replace binary `profile_status=needs_answers` with four modes:

- `auto` (default): no human pause anywhere.
- `direction`: pause before planner, let operator nudge intent.
- `review`: pause before publisher, let operator approve/abort.
- `collaborative`: both.
- New env: `MINI_ORK_INTERACTION_MODE`. UI exposes mode picker on dispatch.
- Pause points use the slash-command signal mechanism from (4) — same plumbing.

### Lower tier (ship in v0.6+ or defer)

#### 7. Conversational intake → Run Contract

Replace one-shot Q&A in `bin/mini-ork-classify` with a short back-and-forth that
ends in a printed contract:

```
  Recipe:     silent-catch-audit
  Family map: codex (planner), glm/kimi/codex (lenses), opus (arbiter)
  Budget:     $25 max / $5 smoke
  Scope:      apps/server/**/*.ts (allow), node_modules/** (deny)
  Verifier:   bash recipes/silent-catch-audit/verifiers/audit-shape.sh
  Press Enter to dispatch, Ctrl-C to abort
```

- Useful but cosmetic. Defer until (4) and (6) ship — the signal plumbing makes
  this easy and the HITL modes give it somewhere to plug.

#### 8. Read-only companion agent for HITL pause

When operator pauses on `review` mode, spin up a separate agent process that
has read-only access to the run dir + `state.db` and can answer questions about
the dispatched run without polluting the Coordinator's context.

- This is genuinely novel and would be the strongest UX differentiator.
- Implementation: a new `mini-ork inspect <run_id>` subcommand that launches a
  Claude/Codex CLI with the run dir pre-loaded as context and a "you are a
  read-only forensics assistant" system prompt.
- Cheap to build once (4) and (6) are in.

#### 9. Lightweight YAML plugins

Recipes are heavy (workflow.yaml + prompts/ + verifiers/ + contract). Plugins
would be a single yaml that overlays:

- Family policy overrides for this run
- Per-node timeout overrides
- Stricter verifier thresholds
- Additional verifiers without forking the recipe

```yaml
# plugins/prod-strict.yaml
recipe: silent-catch-audit
family_overrides:
  arbiter: opus     # force opus instead of mixed
verifier_thresholds:
  critical_max: 0
extra_verifiers:
  - verifiers/no-network-calls.sh
```

Useful but solves a problem we don't have yet (no operator has asked for
plugin-style overlays). Defer until someone forks a recipe just to tweak one
threshold.

#### 10. Persistent Coordinator (the most ambitious)

Replace `bin/mini-ork-execute`-as-one-shot with a long-running Coordinator process
that drives multiple task_runs, maintains the tree across them, and decides what
to dispatch next based on running results.

- This is the biggest architectural shift. Would require a daemon (`mini-ork
  daemon`) with health checks, restart-after-crash semantics, and a job queue
  that lives in `state.db`.
- Recipe-creator's arbiter pattern is the natural seed: the arbiter could become
  persistent and drive multiple recipe-creator runs in sequence with the tree
  spanning them.
- **Defer**. The current one-shot model has a lot of operational virtues
  (predictable, debuggable, no daemon to manage). Don't take this on until
  the tree-of-iterations from (1) hits a wall.

## What I would ship first if I had a week

1. **Day 1–2:** (1a) the tree visualization in the UI for existing self_improve_runs.
   Pure read-side change. Validates the data model can hold a tree shape.
2. **Day 3:** (2) dev/test split convention added to the verifier contract.
   `merge_threshold` becomes a recipe-config parameter.
3. **Day 4:** (4) signal mechanism + the `/tree` and `/cost` slash commands.
   First writable API surface; lays groundwork for (6) and (8).
4. **Day 5:** (3) gradient propagation with `scope_node_id` + `propagated_to`.
   Small schema migration, large quality lift on planner prompt context.

(5), (6), and the harder half of (1) come in v0.5. (7), (8), (9) in v0.6.
(10) only when the tree pattern outgrows the one-shot executor.

## Open questions for you

1. Do you want the tree of iterations as `self_improve_runs.children`
   (today's schema) or as a separate `idea_tree_nodes` table that recipes other
   than `recursive-self-improve` can use? My instinct is the latter — making the
   tree a generic primitive lets `recipe-creator` use it too (each drafter's
   output could become a child branch).
2. The held-out test split discipline (item 2) needs operator buy-in. Are you
   willing to accept that "patch passes 5 dev cases but fails the held-out 5"
   STOPS promotion even when current `_d021_set_status` would have promoted?
3. The signal mechanism in (4) is the first writable API in `mini_ork/web/`. Do
   you want it gated behind auth (the §14 question from the earlier UI/UX brief)
   or fine with loopback-only for v0.4?

## What this DOES NOT change about mini-ork's thesis

Worth saying explicitly. Arbor uses a single CLI dispatcher (claude/codex through
LiteLLM) — they don't enforce family heterogeneity the way mini-ork does. Borrowing
their tree, dev/test discipline, and backpropagation **strengthens** mini-ork's
heterogeneity claim because it lets us measure improvement on held-out work, not
just dev work. The opposite of homogenizing.
