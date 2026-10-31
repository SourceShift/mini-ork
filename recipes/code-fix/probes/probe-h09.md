---
oracle: git
---
# Frozen probe probe-h09 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix: derive framework-edit's diff from the tree, and implement the in-place rollback strategy (#207)

* fix(framework-edit): derive the implementer diff from the tree, not the agent's claim

framework-edit's implementer writes framework-edit.diff itself and nothing
validated that artifact against reality. A hallucinating agent emitted a
corrupt diff — garbage hunk context, already-landed files re-declared as new
creations — AND fabricated its verification claims ("git apply --check ->
EXIT=0"), so the run burned the whole verifier+reviewer wave before the lie
surfaced. The working tree is the only witness that cannot lie.

After apply_impl_output, re-derive the delta from git and rewrite
framework-edit.diff from it: the artifact then always applies, the static
gate's reverse-check is trivially true, and the dispatcher's landing is exact.

The baseline is captured before the first implementer iteration
(_capture_pre_impl_baseline), so a concurrent session's uncommitted work is
never swept into this run's diff. `git stash create` snapshots TRACKED state
only, so the untracked inventory is recorded alongside it — without that,
every pre-existing untracked file (another session's scratch file, a
node_modules symlink) would be re-declared as implementer-created and
published as part of the fix.

A diff-only agent stays legitimate: when the tree is untouched and the
artifact applies cleanly, it is applied once and the tree becomes the source
again. When neither yields anything the implementer node fails immediately
with impl_no_changes / impl_diff_unusable rather than five nodes later, and
the agent's artifact is preserved as framework-edit.diff.agent so the lie
stays auditable.

The tests run against real temp git repos and each one was falsified by
re-breaking its mechanism (dropping the untracked-baseline filter, the
.mini-ork/ skip, the empty-delta guard, and the delegate registration).

* fix(rollback): implement keep_run_artifacts_discard_worktree for in-place runs

Both recipes/framework-edit/workflow.yaml and recipes/self-migrate/workflow.yaml
declare `rollback_strategy: keep_run_artifacts_discard_worktree`, and the
strategy was implemented nowhere — only revert_branch was. A failed in-place run
therefore left the harvested diff sitting in the target tree (a preflight against
run-1788363267-21773-se1 found 4 staged paths behind), so the next serial epic
started from a dirty base and its reviewer diff swept in the dead run's work.

_revert_inplace_diff reverses the run's own framework-edit.diff. The in-place
agent may leave any mix of states, so the restore is per-path and
state-agnostic: `git checkout HEAD -- <path>` resets index AND worktree for
paths in HEAD, while created paths are unstaged and unlinked — `git apply -R`
alone reverts worktree content but leaves the agent's staged entries behind.
Paths outside the diff are never touched, and the containment check keeps a
traversing diff path from reaching outside MO_TARGET_CWD. Run artifacts (diff,
verdicts, logs) survive, per the strategy's name.

MINI_ORK_ROLLBACK_KEEP_WORKTREE is hoisted so it covers both strategies. It is
the outer-loop escape hatch: in a closed RSI loop the authoritative gate is
downstream (deploy -> regen -> DB flip) and it tests the FILE state the
implementer produced, so an in-sandbox revert would destroy a verified-correct
edit before the real gate ever sees it. Leaving the flag wired to revert_branch
alone would have newly destroyed exactly those edits, so the check moved above
the strategy dispatch and the in-place branch honours it too.

* feat(config): add MINI_ORK_AGENTS, and honour MINI_ORK_PROVIDERS everywhere

A vendored mini-ork checkout is a pure mirror of upstream: re-vendoring RESETS
it. So a consumer that configures the engine by editing
$MINI_ORK_HOME/config/{providers,agents}.yaml inside the checkout loses that
configuration on every re-vendor, silently — the files are tracked, and
`reset --hard` restores the upstream defaults with no error. Both files hold
lane policy (model, provider, cost), so the loss is invisible until the wrong
model bills.

MINI_ORK_PROVIDERS already existed as a path override in dispatch/providers.py,
but three other readers of the same files ignored it, so the engine could
resolve lanes from one file while validating, profiling, and gating against
another. Honour it in the validate secrets check, and add the matching
MINI_ORK_AGENTS seam for agents.yaml in the profile step and the coalition
gate. Both are documented in the CLI help next to MINI_ORK_PROVIDERS.

web/recipes.py:load_lanes is deliberately not wired to MINI_ORK_AGENTS: it
already takes an explicit `home` parameter rather than joining a hardcoded
path, so callers can already point it at the consumer's config.

* test(recovery): give the closure-filter test a real edit surface

`[hidden]` ran a four-node synthetic
workflow whose nodes are all `implementer`, under MINI_ORK_RECIPE=
framework-edit, with a mock LLM that writes nothing and no MO_TARGET_CWD.
Then it asserted rc == 0. That expectation is exactly the behaviour the
ground-truth harvest exists to forbid: a framework-edit implementer that
changes no file now fails the node with `impl_no_changes` instead of
falling through to the verifiers.

The fixture is what was unrealistic, not the guard. The real
recipes/framework-edit/workflow.yaml has one implementer and it edits the
target tree by definition; this test's all-implementer shape is a device to
avoid verdict.json gating, and its own comment already says the closure
filter under test is node-shape agnostic.

So: create a scratch git repo, point MO_TARGET_CWD at it, and have the mock
implementer write its edit there — an in-place implementer edits
MO_TARGET_CWD directly. Node identity still comes from MO_NODE_ID, so
`seen == ["D"]`, the assertion this test exists for, is untouched.

MO_TARGET_CWD is set with monkeypatch rather than the raw os.environ the
neighbouring lines use: the surrounding vars leak past teardown, and a
leaked MO_TARGET_CWD would silently retarget a later test's harvest.

Falsified: with the mock's file-write removed the test fails again with
`[ground-truth] FAIL: implementer produced no tree changes`. Before the fix
the harvest also fell back to the mini-ork checkout itself (the `[cwd] codex
target:` line named it), so the test was reading the wrong tree entirely.

tests/ -q --ignore=tests/unit: 398 passed, 24 skipped. Ruff clean.

## Files in scope

- mini_ork/cli/execute.py
- mini_ork/cli/execute_handlers.py
- mini_ork/cli/main.py
- mini_ork/cli/validate.py
- mini_ork/gates/native_gates.py

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
