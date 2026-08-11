# framework-edit

Routine recipe for verifier-gated edits to mini-ork itself.

It proposes a unified diff and verdict for human review. It does not apply the
diff, commit it, or run a meta-recipe selection panel.

## Topology

```text
planner -> [code_impact_lens, prior_art_lens] -> implementer
                                                -> [static_check_verifier, test_verifier]
                                                -> reviewer
                                                -> publisher

reviewer/static_check_verifier/test_verifier -> rollback on failure
```

The recipe has exactly 9 nodes:

1. `planner`
2. `code_impact_lens`
3. `prior_art_lens`
4. `implementer`
5. `static_check_verifier`
6. `test_verifier`
7. `reviewer`
8. `publisher`
9. `rollback`

## Model Lanes

The LLM-dispatching lanes intentionally span four model families:

- `code_impact_lens`: `kimi_lens`
- `prior_art_lens`: `codex_lens`
- `implementer`: `minimax_lens`
- `reviewer`: `opus_lens`

Verifier, publisher, rollback, and decomposer lanes are operational lanes and
are not counted as research-family diversity.

## Artifacts

Required run-local artifacts:

- `${MINI_ORK_RUN_DIR}/framework-edit.diff`
- `${MINI_ORK_RUN_DIR}/verdict.json`

`verdict.json` must use:

```json
{ "files_changed": 0, "tests_pass": false, "static_pass": false, "pass": false }
```

## Smoke Shape vs Real Publish

For smoke-shape validation, `artifact_contract.yaml` defines
`publish_modes.smoke_shape.outputs: []`. This prevents a structural recipe test
from promoting or committing anything.

For a real framework-edit run, `publish_modes.real_publish.outputs` lists the
two run-local review artifacts. These are still not source-code destinations;
the operator reviews the diff and applies it manually if desired.

## Rollback Strategy

`rollback_strategy: keep_run_artifacts_discard_worktree`.

On verifier or reviewer failure, the recipe keeps diff, verdict, lens, and log
artifacts under the run directory, then abandons the isolated worktree. This is
aligned to the artifact contract: evidence is preserved, but no patch is
applied to main.

## Diverges From

- `recipes/code-fix`: same routine patch pattern, but this recipe adds two
  research lenses and framework-specific blast-radius policy.
- `recipes/recursive-self-improve`: not the same DAG. This draft omits
  recursive iteration, arXiv research, synthesis, learning persistence, and
  autonomous patch promotion.
- Current `recipes/framework-edit`: intentionally diverges. The existing
  canonical recipe has an 11-node shape with `opus_arbiter`, `verifier_smith`,
  `recipe_validator`, and a `codex_implementer`; this draft follows the v2
  binding 9-node routine code-edit shape and uses `implementer` on `minimax_lens`.

## Verifiers

This drafter candidate ships stub verifier scripts only. They are executable
and define structured JSON output shape, but the downstream selected recipe
should replace them with full checks before publication.
