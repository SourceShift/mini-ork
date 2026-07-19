# Remaining migration — first requirements audit gaps

Status: in progress

Last worked on: 2026-07-19 20:55 Europe/Berlin

Source task: `docs/migration/remaining-migration-handoff.md`

## Verify fork closure

Current status: completed and source-applied from the green isolated proposal produced by `run-1784482847-61479`.

### Subtasks

1. Publisher preserves composite run artifacts
   - Current status: completed locally. Run 3 preserved the mirrored diff, ledger, detailed verdict, reflection, and both requirements audits; the runtime now harvests those allowlisted artifacts before review and keeps generic orchestration status in `run-verdict.json`.
   - Remaining parts: confirm the repaired ordering in the next explicitly approved paid self-migrate run.

2. Deterministic fork-closure gate
   - Current status: completed. Run 3 and the source post-apply rerun both passed literal-plus-dynamic closure with process status matching JSON `pass:true`.
   - Remaining parts: none.

3. Complete verify integration surface
   - Current status: completed. Run 3 covered the top-level shell caller, legacy executor, Python executor, direct and lifecycle Python CLI dispatch, tests, and parity harness; `bin/mini-ork-verify` is retired.
   - Remaining parts: none.

4. Preserve the explicit isolated target through reviewer assembly
   - Current status: implemented, regression-tested, and confirmed by run 2; `review-diff.patch` was a non-empty 31,513-byte worktree delta.
   - Remaining parts: none for target selection; retain the regression test.

5. Fail closed across JSON and process verifier contracts
   - Current status: completed. Run 3's ledger gate initially rejected incomplete rows and later passed only after coverage reached 33 rows; all five final process exits matched their JSON result.
   - Remaining parts: none.

6. Qualified ledger symbol matching
   - Current status: completed. Run 3 cross-checked the final 45,905-byte diff and passed with 33 rows.
   - Remaining parts: none.

7. Resolve pre-existing global parity divergences
   - Current status: deferred outside the verify fork.
   - Remaining parts: diagnose `help`, `doctor`, `conductor --help`, `execute --help`, and `init` independently; they do not replace or block the fork-specific parity oracle.

8. Apply and commit the verify fork
   - Current status: completed. The source-applied verify fork, recipe hardening, artifact-handoff repair, tests, handoff, and audit are captured in one focused migration commit.
   - Remaining parts: none.

9. Make mirrored verifier evidence available before review
   - Current status: implemented and regression-tested. The executor reconciles the target worktree's run mirror before reviewer dispatch, writes an implementer summary, includes recipe-specific reports in reviewer inputs, and preserves canonical generic and detailed verdicts separately.
   - Remaining parts: live-confirm same-run reviewer visibility during the paid `reflect` fork.

## Later forks

Current status: `reflect` preflight prepared; its first paid launch stopped at planner dispatch before changing the isolated target. Classify → plan → cli → execute are not started, and the ordering is unchanged.

Remaining parts: relaunch from the dedicated temporary runtime home whose frozen policy uses only Kimi, Codex, and GLM, with Kimi/GLM credentials loaded process-locally from `/Users/admin/ps/scripts`; then run the same five-verifier pipeline, apply only passing diffs, update migration documentation, and repeat the requirements audit after each closure. The clean isolated target already passes all 8 pre-retirement reflect parity tests, so the two failures seen only in the dirty source checkout do not currently require a test change.

## Second requirements audit

Completed: 2026-07-19

- Re-read the handoff, feature manifest, recipe README, artifact contract, and corrected verify kickoff.
- Confirmed run 2's real reviewer diff, both requirements-review artifacts, focused functional evidence, fail-closed partial verdict, and unapplied rollback outcome.
- Confirmed the publisher regression suite preserves the diff, ledger, and verdict as distinct artifacts.
- Confirmed explicit worktree targeting wins over an external temporary kickoff path.
- Confirmed pre-retirement parity runs before the migrator and produces durable passing evidence for the verify fork.
- Confirmed all five recipe verifier scripts now make process status agree with JSON `.pass` and that qualified ledger symbols satisfy the diff cross-check.
- Confirmed the complete executor unit module passes (47 tests), Pyright reports zero errors, `mini-ork validate` passes, the restored 52-row ledger passes `ledger-shape.sh`, all verifier scripts pass `bash -n`, and `git diff --check` is clean.

## Third requirements audit

Completed: 2026-07-19

- Confirmed the Run 3 isolated detailed verdict has `pass: true` and all five migration gates passed.
- Confirmed the 33-row ledger cross-checks the final diff and no literal or dynamic verify caller remains.
- Confirmed the promoted source files are byte-identical to the verified worktree and `bin/mini-ork-verify` is absent.
- Confirmed post-apply results: 9 verify tests, 47 executor tests, 6 CLI tests, 8 integration assertions, 18 E2E assertions, focused runtime parity, Pyright with zero errors, and clean diff hygiene.
- Separated the green implementation verdict from the outer run-level failure caused by reviewer/report ordering.

## Fourth requirements audit

Completed: 2026-07-19

- Re-read the handoff and todo after implementing the artifact-handoff repair.
- Confirmed the target-run mirror is harvested before reviewer dispatch and only the self-migrate artifact/evidence allowlist is copied.
- Confirmed reviewer inputs include `verifier_*.json`, `integration-map.json`, `static-feature-ledger.json`, detailed `verdict.json`, and `self-migrate.diff`.
- Confirmed generic bookkeeping uses `run-verdict.json` and preserves a recipe-owned detailed `verdict.json`.
- Confirmed all 50 executor tests pass, including same-run mirror visibility, detailed-verdict preservation, and recipe-specific reviewer evidence.
- Confirmed 15 verify/CLI unit tests, 8 integration assertions, 18 E2E assertions, Pyright, validate, shell syntax, diff hygiene, and garden (0 errors, 0 warnings) pass.
- Ran the repository-wide suite directly because `make test` has no target: 1,805 passed, 5 skipped, and 3 source-checkout-state failures (one user-owned capability-policy mismatch and two reflect cases). The reflect failures reproduced in the dirty source checkout, were not changed, and do not reproduce in the clean isolated migration target, where all 8 tests pass.

The verify fork and its artifact-handoff prerequisite are done locally. The corrected `reflect` kickoff and isolated worktree are prepared, and the clean target's 8-test pre-retirement oracle is green. The later five forks remain pending in the required order and need explicit cost approval one run at a time; the next paid run is `reflect`.
