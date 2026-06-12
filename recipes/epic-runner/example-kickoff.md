# Epic: build an `epic-runner` recipe

We need a new mini-ork recipe that ingests a single multi-epic markdown document
and delivers all the epics in dependency order.

## Inputs the recipe will receive

- `MINI_ORK_EPIC_DOC` — path to the multi-epic markdown doc (e.g.
  `docs/epics/scalable-schema-migration.md`).
- `MINI_ORK_EPIC_TARGET_REPO` — target repository the child epics mutate.
- `MINI_ORK_EPIC_PUBLISH` — `true` to allow child framework-edit runs to
  publish; `false` for smoke-shape only.
- `MINI_ORK_EPIC_VERIFIER_SCRIPT` — optional operator-supplied verifier script
  path.

## Success criteria

- `${MINI_ORK_RUN_DIR}/epic-runner-plan.json` with all epics parsed, dependency
  graph built, and topological waves computed.
- `${MINI_ORK_RUN_DIR}/epic-results.json` with per-epic child framework-edit
  verdicts.
- `${MINI_ORK_RUN_DIR}/wave-aggregate.json` with wave-by-wave aggregates and
  `dependency_respected: true`.
- `${MINI_ORK_RUN_DIR}/epic-runner-delivery.json` with the final delivery
  verdict.
- No meta-recipe nodes in the generated recipe; exactly 8 nodes named
  `planner`, `epic_dispatcher`, `wave_aggregator`, `epic_verifier`,
  `final_reviewer`, `publisher`, `rollback`, `reflector`.

## Heterogeneity expectation

Use at least 3 distinct model families across the non-operational nodes:
- `epic_dispatcher` on a codex-family lane
- `wave_aggregator` on a glm-family lane
- `final_reviewer` on an opus-family lane

## Verification command (HOW success is proved)

```bash
bash recipes/epic-runner/verifiers/epic-graph-complete.sh
```

## Out of scope

- Modifying the epic markdown doc itself.
- Running child epic implementations outside of framework-edit.
- Direct Qdrant/Postgres connectivity in the verifier (offline JSON checks only).

## Output

A complete `recipes/epic-runner/` directory under the canonical repo path,
committed by the publisher under `mini-ork@local` identity.

## Canonical use case

This recipe was motivated by the researcher `scalable-schema-migration` epic
(doc in `docs/epics/` or the researcher schema-migration doc), which contains
multiple dependent framework changes that must land in a strict order across
waves.
