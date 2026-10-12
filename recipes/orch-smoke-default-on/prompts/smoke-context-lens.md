# smoke-context-lens — frame the engagement proof in domain terms

You are the **codex** lens for the orch-smoke-default-on engagement
harness. Your job is to read the orch-smoke declaration and emit a short
markdown lens (≤ 60 lines) that grounds the engagement proof in the
"process observability / event-sourced telemetry" framing.

## Inputs

- The orch-smoke recipe directory under `${MINI_ORK_ROOT}/recipes/orch-smoke/`.
  In particular, `workflow.yaml`, `artifact_contract.yaml`, and
  `verifiers/fail-probe.py`.
- `mini_ork/orchestration/runner_orchestrator.py`, focusing on
  `on_node_boundary` and the `_emit` call that writes the
  `orchestrator.<kind>` row through `mo_node_emit`.
- The plan's `derived_recipe_name` and the `task_class` `orch_smoke_default_on`.

## Output

Write a markdown file with:

1. A first non-blank line that is a markdown header (`# …`).
2. ≥ 4 non-blank lines total.
3. No chat-transcript markers (the harness overwrites the file with
   reply text if you emit fenced chat markers).
4. Sections covering:
   - **The contract** — what the orch-smoke declaration binds: rc=1,
     no retries edge, single-sidecar output, `keep_artifacts_discard_publish`.
   - **The proof target** — exactly one `orchestrator.<kind>` row in
     `run_events` for the `fail_probe` node, written by the canonical
     `mo_node_emit` writer.
   - **Why engagement is auditable from a row, not assumed** — chaos
     engineering's trigger-verification pattern: drop untriggered trials
     from any "default-on works" claim.

## Hard rules

- Do NOT modify `fail-probe.py`, `workflow.yaml`, or
  `artifact_contract.yaml` under `${MINI_ORK_ROOT}/recipes/orch-smoke/`.
- Do NOT propose "fixing" the failing verifier. A green fail_probe voids
  the proof.
- Do NOT write a bash verifier. The bash-removal world (2026-07) uses
  pure-python verifiers; the engagement_check is `.py`.
- Do NOT emit an artifact under the target working tree. The
  `verifier-result-fail-probe.json` sidecar lives in `${MINI_ORK_RUN_DIR}`.

## Output path

Write your lens to the run dir as
`${MINI_ORK_RUN_DIR}/lens-smoke-context.md`.
