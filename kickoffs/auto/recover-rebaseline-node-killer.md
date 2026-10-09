# recover: self-re-baseline when HEAD advanced + find the attempt-1 node-killer

Live case: run-1791543223-76795 (framework-edit, agent-s-gui-smoke), 8 attempts,
2026-10-09. Three source-level gaps cost ~6 of those attempts. The prescreen-oracle
one is FIXED (428c2348); the two below remain.

## 1. recover must re-baseline itself when HEAD moved since capture

`recover --strategy resume` reuses `pre-implementer-ref` captured at the original
run start. When main advanced mid-run (operator merged an unrelated fix — or the
fix the run itself needed), `_implementer_moved_base` fails legitimately and every
recover dies at the implementer until the snapshot is rebuilt by hand:

1. hide the run's deliverables, `git stash create`, rewrite `pre-implementer-ref`,
   regenerate `pre-implementer-untracked` (minus the deliverables), `update-ref`,
   restore deliverables untracked.

Requested behavior: on `impl_moved_base`, if the current HEAD is a DESCENDANT of
the baseline's first parent (normal forward motion, not the implementer
committing), recover should perform this re-baseline automatically before
re-dispatching — the same surgery, owned by the framework instead of the operator.

Related wrinkle: after a `--carry-patch` apply, a naive re-snapshot folds the
carried files into the fresh untracked list → empty files_changed → verdict fail.
The auto-re-baseline must exclude the carried paths (they are in
implementer-summary.json files_changed).

## 2. concurrent-session harvest contamination (baseline chaser)

Same tree, two live sessions: a roommate session's tracked edits landed inside the
implementer window and the harvest (`_delta`) swept them into framework-edit.diff
(5 files instead of 2; the oracle gate correctly refused to publish the
contaminated diff). Operator workaround was a background loop re-folding foreign
tracked dirt into the baseline every 10s + appending new foreign untracked files
to the exclusion list (never the run's own deliverables).

Requested behavior: harvest-time scoping instead of run-time chasing — when the
run carries an authored-patch / files_changed record, `_delta` should diff only
those paths against the baseline, or the snapshot should be refreshed at HARVEST
time (stash create is read-only) rather than dispatch time.

## 3. attempt-1 node-killer (forensics, may be unfindable)

Completed implementer was interrupted then rolled back ~54s after harvest
(killclose CRASH 1791545457 at 13:23:0x, harvest 13:23:06, rollback 13:24:00).
No actor identified: not the watchdog (heartbeats fresh), not .stop-requested,
not OOM-proven. Suspects: ram-sentinel CPU guard, a stray `TaskStop`, or the
recovery planner of a sibling run. Reproduce with a long implementer under load
and watch for SIGTERM sources.
