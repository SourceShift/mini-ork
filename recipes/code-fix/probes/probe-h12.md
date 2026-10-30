---
oracle: git
---
# Frozen probe probe-h12 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix(code-fix): publish reviewer verdict; split eval onto judge lane (#201)

* fix(code-fix): publish reviewer verdict; split eval onto judge lane

Two independent defects kept the code-fix recipe from routing to a
heterogeneous fleet and from ever committing its result.

1. The publisher node could never commit. `_publisher_try_commit_files`
   reads `REVIEW_FILE` and `VERDICT` from the environment, but nothing in
   the Python engine ever published them — a leftover from the bash
   `_dispatch_node`, which used to export them. Its only other verdict
   candidates (`panel-verdict.json`, `review-verdict.json`) are panel-gate
   artifacts that recipes like code-fix never write, so the resolved
   verdict was unconditionally `'<none>'` and every run logged
   `[skip-publish] ... is not APPROVE — no commit`. `_handle_reviewer` now
   publishes both after extracting the verdict.

   Reproduced and verified against the real `_publisher_try_commit_files`
   in a scratch repo: pre-fix shape → `committed = False`; post-fix shape
   → `committed = True` with the `mini-ork(code-fix): ...` commit.

2. The `eval` node could not run on its intended judge lane. Its
   `model_lane` was `reviewer`, and the node's *resolved* lane is what gets
   published into `MO_DISPATCH_CHAIN` — which `_default_llm_dispatch`
   reads before the jury's per-juror lane argument. So the judge was
   pinned to Opus and `MO_EVAL_JURY_LANES` was inert. The node now has a
   dedicated `judge` lane, mapped in `config/agents.yaml`.

No behaviour change for recipes that do not use a classic reviewer or an
eval node.

* fix(execute): make the code-fix publisher commit path reachable

The publisher's commit gate reads two run-dir artifacts, and nothing in the
code-fix recipe ever wrote either one — so a run that passed typecheck, tests,
and review still could not commit:

1. review-verdict.json — the reviewer's verdict was handed over via publish_env.
   Nodes execute in a ProcessPoolExecutor (cli/execute.py), so that write dies
   with the reviewer's worker process and the publisher's own process observes
   nothing; REVIEW_FILE/VERDICT read as "". Write the verdict to run_dir instead
   (publisher._publisher_try_commit_files already scans for it there).

2. implementer-summary.json — only the self-migrate path wrote one, and
   _write_self_migrate_implementer_summary has zero callers. Add
   _write_implementer_summary and call it from _handle_implementer. It derives
   files_changed from the working tree rather than the model's self-report,
   scoped by <run_dir>/pre-implementer-ref so a concurrent session's pre-existing
   dirt is never swept into the commit.

Also report the actual non-approving verdict in the skip message: '<none>' when
a reviewer returned verdict='fail' reads as "no verdict found at all" and sends
the reader hunting a file that is present.

Tests cover both writers and the full implementer → reviewer → publisher seam
with review_file="" and verdict_env="" (the production condition under the
process split).

## Files in scope

- config/agents.yaml
- mini_ork/cli/execute.py
- mini_ork/cli/execute_handlers.py
- mini_ork/cli/publisher.py
- recipes/code-fix/workflow.yaml

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
