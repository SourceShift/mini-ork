# Feature inventory of mini-ork as a cloud agent orchestration platform

## Problem

mini-ork has grown into a full **cloud agent orchestration system with
self-improvement and learning**, but there is no single authoritative
catalog of everything it provides. Newcomers, investors, and integrators
cannot see the whole surface at a glance. We need a comprehensive,
code-grounded feature inventory that reads the actual implementation (not
marketing) and enumerates every capability, organized under the umbrella
of a cloud agent orchestration platform.

## Definition of Done

Produce a feature inventory that enumerates mini-ork's capabilities grouped
under these pillars (add pillars if the code reveals more):

1. **Orchestration core** — the task loop (classify → plan → execute →
   verify → reflect → improve), recipes, workflow DAGs, node types,
   artifact-graph contracts, gates.
2. **Heterogeneous model dispatch** — provider lanes (codex, minimax, glm,
   kimi, opus, sonnet, deepseek…), routing policies, cost governance,
   budget gates.
3. **Runtime reliability** — durable-DAG resume (step/turn), leases &
   idempotency, tool receipts, publisher-commit, throttle/retry, rollback.
4. **Verification & gates** — verifier nodes, gate registry, execution-
   anchored rewards, eval-in-run-flow node, metamorphic/jury plans.
5. **Self-improvement & learning** — the GRPO / bandit router
   (lane_domain_advantage, lane_region_advantage), GEPA prompt evolution
   (prompt_win_rates, promotion_records), reflection, apply-loop.
6. **Observability surface** — `mini-ork serve` HTTP/SSE API, the runs
   list, run detail/DAG, learning endpoints, trajectory, fingerprint.
7. **Operator & dev ergonomics** — `bin/mini-ork` subcommands, worktree
   dev loop, init/scaffold, validate/garden, doctor, providers config.

For each feature: a one-line description + at least one `file:line` anchor
proving it exists in the code. Mark features that are **shipped** vs
**specced/roadmap** distinctly (do not present roadmap items as shipped).

## Files in scope (read-only — this is an inventory, not a code change)

- `mini_ork/` — the entire Python runtime (cli/, dispatch/, gates/,
  learning/, memory/, web/, context.py, types.py)
- `bin/mini-ork` — subcommand entrypoint
- `recipes/` — every recipe's task_class.yaml + workflow.yaml
- `schemas/` — task_class / workflow / artifact_contract schemas
- `config/providers.yaml`, `config/agents.yaml`
- `docs/architecture/`, `docs/operator/` — for cross-checking claims
- `CLAUDE.md` — the canonical context map

## Scope

- Target repo: this repo (mini-ork). ~1 synthesis + 2 anchor lenses.
- Anchor model lenses: **codex** and **minimax** (as requested). Any other
  live lenses (glm/kimi) are supplementary coverage only.
- Depth: exhaustive over `mini_ork/` + `recipes/` + `bin/mini-ork`.
- Output: read-only doc. No source files under `mini_ork/`, `bin/`,
  `recipes/`, `schemas/` may be modified.

## Success criteria

- Every lens report exists, is non-empty, and cites ≥1 `file:line` each.
- The synthesis covers all 7 pillars above with ≥3 features per pillar,
  each with a code anchor, and a shipped-vs-roadmap marker.
- The synthesis publishes to `docs/reference/FEATURE-INVENTORY.md`.
- No feature is asserted without a code anchor (no marketing-only claims).

## Proof of success (verification command)

The run has succeeded when the published inventory exists, is non-trivial,
covers all seven pillars, and every asserted feature carries a code anchor.
This command must exit 0:

```bash
test -s docs/reference/FEATURE-INVENTORY.md \
  && test "$(wc -l < docs/reference/FEATURE-INVENTORY.md)" -ge 120 \
  && grep -qi "Orchestration core"        docs/reference/FEATURE-INVENTORY.md \
  && grep -qi "model dispatch"            docs/reference/FEATURE-INVENTORY.md \
  && grep -qi "reliability"               docs/reference/FEATURE-INVENTORY.md \
  && grep -qi "Verification"              docs/reference/FEATURE-INVENTORY.md \
  && grep -qi "self-improvement"          docs/reference/FEATURE-INVENTORY.md \
  && grep -qi "Observability"             docs/reference/FEATURE-INVENTORY.md \
  && grep -qEi "\.py:[0-9]+|:[0-9]+"      docs/reference/FEATURE-INVENTORY.md
```

## Non-goals

- Do NOT modify any source under `mini_ork/`, `bin/`, `recipes/`,
  `schemas/` (read-only inventory).
- Do NOT invent capabilities that aren't in the code; absence of evidence
  → mark as "not found" rather than assuming it exists.
- Do NOT audit for bugs/perf/security — this is a capability catalog, not
  a refactor audit.
