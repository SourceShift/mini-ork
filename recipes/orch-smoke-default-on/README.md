# orch-smoke-default-on

Engagement harness proving the per-run orchestrator is **default-on**.
Wraps the `orch_smoke` declaration (the failing-verifier smoke leg)
verbatim and adds a pure-python `engagement_check` verifier that
asserts the orchestrator fired at the `fail_probe` wave boundary.

> **The run is EXPECTED to FAIL.** That failure is the engagement
> signal, not a defect. The publisher is skipped; the rollback node
> keeps the artifacts. The `verifier-result-engagement-check.json`
> sidecar carries the proof.

## What this proves

With `MO_RUN_ORCHESTRATOR` UNSET, the per-run orchestrator's
`on_node_boundary` MUST classify the `fail_probe` outcome at the
wave boundary and emit exactly one `orchestrator.<kind>` row into
`run_events` — through the canonical `mo_node_emit` writer, not a
side channel. The `engagement_check` verifier queries `run_events`
and fails the proof if:

- the row is missing (orchestrator never fired),
- the count is `>=2` (boundary crossed twice — a real defect),
- `MO_RUN_ORCHESTRATOR` is set in the verifier's environment
  (engagement is meaningful only with the variable unset),
- or `state.db` / the run id are not in scope (fail-closed).

## DAG at a glance

```
smoke_context_lens     (researcher, codex_lens)  ─┐
engagement_design_lens (researcher, glm_lens)     ─┼─→ engagement_check
determinism_lens       (researcher, kimi_lens)    ─┘            │
                                                                │
fail_probe (verifier, zero-LLM, rc=1) ─→ publisher (skipped)   │
                          │                                     │
                          └─→ rollback (keeps artifacts)        │
                                                                ▼
                                                    (proof sidecar)
```

- 3 family-diverse researcher lenses (codex / glm / kimi) — satisfies
  the **HARD ≥ 3 distinct model_lane families** floor in
  recipe-authoring contexts.
- `fail_probe` is the **EXACT** `orch_smoke` verifier, verbatim
  (rc=1, sidecar with `pass: false`).
- `engagement_check` is a new pure-python verifier (no network, no
  timing).
- The two exact `orch_smoke` edges are preserved
  byte-for-byte: `fail_probe → publisher (depends_on)` and
  `fail_probe → rollback (escalates_to)`.
- `rollback_strategy: keep_artifacts_discard_publish` — the
  expected-failing run keeps its artifacts (the sidecar is the
  proof) and skips publish.

## Files

```
orch-smoke-default-on/
├── workflow.yaml             # DAG, edges, rollback strategy
├── task_class.yaml           # 9 matcher keywords + 3 regex
├── artifact_contract.yaml    # source_artifact + outputs (run-dir only)
├── prompts/
│   ├── smoke-context-lens.md     # codex: frame the proof
│   ├── engagement-design-lens.md # glm:   design engagement_check
│   └── determinism-lens.md       # kimi:  prove rc-determinism
├── verifiers/
│   ├── fail-probe.py             # VERBATIM copy of orch-smoke's
│   └── engagement-check.py       # NEW pure-python engagement proof
├── example-kickoff.md
└── README.md
```

## Diverges-from

This DAG is **not** identical to `orch-smoke/`. The orch-smoke recipe
declares a 3-node core (`fail_probe → publisher / rollback`,
zero model_lane families, zero researcher nodes, no `prompts/`),
and that bare shape hard-fails the recipe-creator's structural
floors: it has 0 distinct families, 0 keywords beyond the floor,
and no `prompts/` directory.

This recipe is a **wrap-don't-touch** of the orch-smoke core: the
two exact `orch_smoke` edges are preserved verbatim, the fail-probe
verifier is the verbatim copy, and `keep_artifacts_discard_publish`
is the same rollback strategy. The new layer is around, not inside,
the smoke leg: 3 family-diverse researchers (codex / glm / kimi) +
the new `engagement_check` verifier (asserts the orchestrator row).

This recipe is also **not** identical to `obs-smoke/` (which has a
single 1-family `tiny_lens` + `tiny_reviewer` topology and a
non-failing sidecar contract). The orch-smoke-default-on DAG is
expecting a `fail` outcome; obs-smoke is expecting a `pass`. The
verifier contract — `verifier-result-engagement-check.json` queries
`run_events` for a `node_id = 'fail_probe'` boundary — is unique
to this recipe.

The closest lineage is `recipes/recipe-creator/` (meta-recipe that
spawns this one), but that recipe is multi-drafter with
`supplies_context_to` fan-in; orch-smoke-default-on has a single
verifier-only proof path and no LLM drafting stage.

## Cost and wall time

| node                     | type        | LLM cost | notes                     |
|--------------------------|-------------|----------|---------------------------|
| smoke_context_lens       | researcher  | ~$0.40   | codex_lens, budget_gate   |
| engagement_design_lens   | researcher  | ~$0.40   | glm_lens, budget_gate     |
| determinism_lens         | researcher  | ~$0.40   | kimi_lens, budget_gate    |
| fail_probe               | verifier    | $0.00    | rc=1, deterministic       |
| engagement_check         | verifier    | $0.00    | rc=0 on pass, rc=1 on fail|
| publisher                | publisher   | $0.00    | skipped (fail_probe)      |
| rollback                 | rollback    | $0.00    | keeps artifacts           |
| **total**                |             | **~$1.20** | ~3-7 min wall           |

## Why this exists

After any change to `mini_ork/orchestration/runner_orchestrator.py`
(especially `enabled()`, `on_node_boundary`, `_emit`,
`mo_node_emit`), re-run this recipe with `MO_RUN_ORCHESTRATOR`
UNSET to confirm:

- The orchestrator's `enabled()` returns True without the variable
  being set (the default-on claim)
- `on_node_boundary` emits exactly one `orchestrator.<kind>` row
  per failing-verifier boundary
- The row lands in `run_events` through the canonical
  `mo_node_emit` writer (not a side channel)
- `state.db` survives the run and the engagement row is queryable
  by `run_id + node_id`

A green run (or a missing engagement row) is a defect. A red run
with a green engagement verdict is the expected outcome.
