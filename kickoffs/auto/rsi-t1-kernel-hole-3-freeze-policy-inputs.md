# RSI Tier-1 #4c: freeze the self-driving policy's inputs, not only the policy

## Goal

`mini_ork/recovery/escalation_policy.py` (frozen) decides whether the self-driving
loop may open a framework-edit fix epic. It is pure: it decides only from the
`EscalationInputs` other modules hand it, and **those modules are not frozen**:

- `mini_ork/recovery/escalation.py` `gather_inputs` builds every input: the daily
  cap and breaker defaults, the calibrated bar, the labelled-row count.
- `mini_ork/recovery/diagnose.py` assigns `layer` / `routes_to`. The policy's
  first gate is `layer != "engine"`; `auto_repair._escalatable` also needs
  `routes_to == "fix_epic"`.
- `mini_ork/recovery/diagnoser_llm.py` produces model diagnoses and their confidence.
- `mini_ork/stores/diagnosis_ledger.py` holds the labelled rows the calibrated bar
  is computed from; `mini_ork/recovery/supervisor.py` rule R2 writes those labels.
  The supervisor also dispatches epics (R8) and applies graph patches.
- `mini_ork/recovery/graph_patch.py` decides which DAG edits a patch may make to
  a failed run.
- `mini_ork/cli/publisher_authored_patch.py` holds the publish-time frozen
  refusal (`land_patch`). `mini_ork/gates/merge_risk.py` reads the frozen list at
  merge time.

An auto-opened fix epic can edit any of these and so change what the frozen
policy decides without touching the policy. Add them to the frozen kernel.

All paths are relative to the repository root.

## Mechanism (exact spec)

The eight new entries, in this order:

```
mini_ork/recovery/escalation.py
mini_ork/recovery/diagnose.py
mini_ork/recovery/diagnoser_llm.py
mini_ork/recovery/supervisor.py
mini_ork/recovery/graph_patch.py
mini_ork/stores/diagnosis_ledger.py
mini_ork/cli/publisher_authored_patch.py
mini_ork/gates/merge_risk.py
```

### 1. `config/kernel-frozen.yaml`

- Append the eight entries to `paths:`, after
  `mini_ork/observability/bug_report.py` and before the
  `recipes/code-fix/probes/fixtures/**` comment block. Put this comment above them:

  ```
    # The self-driving policy's INPUTS (RSI Tier-1 #4). The frozen escalation
    # policy decides only from what these hand it: layer/route (diagnose,
    # diagnoser_llm), the calibrated bar's labelled rows (diagnosis_ledger,
    # labelled by supervisor R2), the cap/breaker/bar inputs (escalation
    # gather_inputs), the DAG edits the supervisor applies (graph_patch), the
    # publish-time refusal (publisher_authored_patch.land_patch) and the
    # merge-time reader of this list (gates/merge_risk).
  ```
- Rewrite the header comment's first sentence to state the real scope:
  `# Paths no self-driving change may alter. Enforced at publish by`
  `# mini_ork/cli/publisher_authored_patch.py land_patch for every in-place run`
  `# (and every RSI-cycle land), by the framework-edit verifier for auto-opened`
  `# (origin=auto-triage) runs, by the RSI cycle's selector, by the apply loop, and`
  `# at merge as a high risk tier (mini_ork/gates/merge_risk.py) that needs a`
  `# person's approval.`
  Keep the remaining header lines (the union with the floor and the
  `MO_ALLOW_FROZEN_EDIT=1` token) as they are.
- Every existing entry stays, in its current order.

### 2. `mini_ork/vcs/repo_integrity_guard.py`

- Append the same eight strings, in the same order, to the end of
  `FROZEN_PATHS_DEFAULT` (after `"mini_ork/observability/bug_report.py"`),
  preceded by a one-line comment `# The self-driving policy's inputs (RSI Tier-1 #4).`
- Replace the comment block above `FROZEN_CONFIG_RELPATH` that begins
  `# The paths an auto-opened (origin=auto-triage) framework-edit epic may never`
  with one that says the floor binds every enforcement point (publish, the
  auto-triage verifier, the RSI selector, the apply loop, and the merge risk tier),
  and that the built-in list is the fail-closed floor every consumer unions with.
- No function changes. `load_frozen_paths`, `frozen_hits`,
  `enforced_frozen_paths` and `frozen_edit_allowed` are untouched.

## Files in scope (touch ONLY these)

- `config/kernel-frozen.yaml`
- `mini_ork/vcs/repo_integrity_guard.py`
- `tests/unit/test_frozen_kernel_policy_inputs.py` (new)

Do NOT modify any other file. In particular do not modify
`recipes/framework-edit/verifiers/static-check.py`. Its inline list is a fallback
for an engine without `repo_integrity_guard`, and the live path imports the
engine's floor.

## Tests (`tests/unit/test_frozen_kernel_policy_inputs.py`: hermetic, no network)

`REPO = Path(__file__).resolve().parents[2]`; `NEW = [<the eight paths in order>]`.

- **Both lists carry them.** For each `p` in `NEW`:
  `p in repo_integrity_guard.FROZEN_PATHS_DEFAULT`,
  `p in repo_integrity_guard.load_frozen_paths(str(REPO))`, and
  `repo_integrity_guard.frozen_hits([p], repo_integrity_guard.enforced_frozen_paths(str(REPO))) == [p]`.
- **Order of the floor.** `FROZEN_PATHS_DEFAULT[-8:] == NEW`.
- **Nothing was dropped.** The 14 entries the floor held before this change
  (copy the `FULL_LIST_YAML` paths from `tests/unit/test_frozen_kernel.py`) are
  all still in `FROZEN_PATHS_DEFAULT` and in `load_frozen_paths(str(REPO))`, and
  `recipes/code-fix/probes/fixtures/**` is still in `load_frozen_paths(str(REPO))`.
- **They are real files.** `(REPO / p).is_file()` for every `p` in `NEW`.
- **A negative control.** `frozen_hits(["mini_ork/recovery/retry_hint.py", "mini_ork/recovery/planner.py"], enforced_frozen_paths(str(REPO))) == []`.
- **The publish refusal covers them.** Copy `GIT_IDENTITY`, `_git`, `_write`
  from `tests/unit/test_frozen_paths_enforced.py`. In a throwaway repo with NO
  `config/kernel-frozen.yaml`, commit `mini_ork/recovery/supervisor.py` with
  content `# base\n`, then cut a real `git diff` that changes it to
  `# base\nedited = True\n` and restore the file. With `MO_ALLOW_FROZEN_EDIT`
  deleted, `publisher_authored_patch.land_patch(str(repo), patch, "")` returns an
  `Abstain` whose `reason == "publish-frozen-path"`, and HEAD is unchanged. With
  `MO_ALLOW_FROZEN_EDIT=1` the same call returns a 40-hex sha.

## Verification commands

- `pytest -q tests/unit/test_frozen_kernel_policy_inputs.py tests/unit/test_frozen_kernel.py tests/unit/test_frozen_paths_enforced.py`
- `pytest -q tests/unit/test_merge_risk.py tests/unit/test_rsi_select_py.py tests/unit/test_rsi_gates_py.py tests/unit/test_framework_edit_static_check.py`
- `pytest -q tests/unit/test_escalation.py tests/unit/test_supervisor.py tests/unit/test_diagnose.py tests/unit/test_diagnosis_ledger.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_frozen_kernel_policy_inputs.py --junitxml="${MINI_ORK_RUN_DIR}/frozen-policy-inputs-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/frozen-policy-inputs-junit.xml`

## Success criteria

- All verification commands pass with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/vcs/repo_integrity_guard.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
