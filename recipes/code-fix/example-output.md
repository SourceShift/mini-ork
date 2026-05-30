# Expected Output: code-fix recipe

The output below is the annotated stdout of:

```bash
mini-ork run code-fix examples/01-hello-world/kickoff.md
```

---

## Console Output

```
[mini-ork] run           run-20260530-091542-cf4a
[mini-ork] recipe        code-fix v0.1.0
[mini-ork] kickoff       examples/01-hello-world/kickoff.md
─────────────────────────────────────────────────────────────────
 mini-ork code-fix                         run=run-20260530-091542-cf4a
─────────────────────────────────────────────────────────────────

[classify]  task_class=code_fix  risk=medium  confidence=0.97

[plan]      model=claude-sonnet-4-5
[plan]      objective: Add session-token-rotation entry to CHANGELOG.md under [Unreleased]
[plan]      steps: 1  estimated_lines_changed: 5
[plan]      success_check: grep -A3 "## [Unreleased]" CHANGELOG.md returns ≥1 bullet; test.sh exit 0
[plan]      done

[implement] model=claude-sonnet-4-5
[implement] applying step 1/1: edit CHANGELOG.md — insert entry under [Unreleased]
[implement] files_changed: CHANGELOG.md
[implement] confidence=0.95
[implement] done

[verify]    typecheck.sh
[verify]    command: (no typecheck tool detected — skipped)
[verify]    result: PASS  evidence: .mini-ork/runs/run-20260530-091542-cf4a/verifier_typecheck.log

[verify]    test.sh
[verify]    command: npm test
[verify]    result: PASS  2 tests passed  elapsed=3s
[verify]    evidence: .mini-ork/runs/run-20260530-091542-cf4a/verifier_test.log

[review]    model=claude-opus-4-5
[review]    verifier_typecheck: pass=true
[review]    verifier_test:      pass=true
[review]    diff: 5 lines changed in CHANGELOG.md
[review]    scope: CHANGELOG.md in plan — ok
[review]    forbidden patterns: none found
[review]    verdict: APPROVE
[review]    rationale: All verifiers pass; diff matches plan; no scope violations; no forbidden patterns.

[publish]   deployment_gate: not required for doc change — skipped
[publish]   committing: CHANGELOG.md
[publish]   commit: 9f2e3a1b
[publish]   message: "feat(changelog): add session-token-rotation to [Unreleased]"
[publish]   done

─────────────────────────────────────────────────────────────────
 mini-ork DONE                   run=run-20260530-091542-cf4a  verdict=APPROVE
─────────────────────────────────────────────────────────────────
 CHANGELOG.md   APPROVE
 elapsed=2m 14s   cost=$0.18   tokens=3842
```

---

## What each section means

| Section | What happened |
|---|---|
| `[classify]` | The framework matched the kickoff to `task_class=code_fix` with 97% confidence. |
| `[plan]` | Planner emitted a 1-step plan with a concrete `success_check`. |
| `[implement]` | Implementer applied one Edit to `CHANGELOG.md`; confidence=0.95. |
| `[verify] typecheck` | No typecheck tool detected in this repo — verifier auto-skipped (pass). |
| `[verify] test` | `npm test` ran; 2 assertions passed in `tests/changelog.test.js`. |
| `[review]` | Reviewer checked all four APPROVE conditions; issued APPROVE. |
| `[publish]` | No deployment gate for a doc-only change; committed directly to branch. |

---

## Resulting diff

```diff
--- a/CHANGELOG.md
+++ b/CHANGELOG.md
@@ -3,6 +3,12 @@
 ## [Unreleased]
 
+### Added
+- Session token rotation: tokens now expire after 24 h idle and are
+  automatically refreshed on the next authenticated request, reducing
+  the blast radius of a leaked token.
+
 ## [0.3.1] - 2026-05-28
```

---

## Failure path example

If the implementer had introduced a syntax error, the output diverges at `[verify]`:

```
[verify]    typecheck.sh
[verify]    command: tsc --noEmit
[verify]    result: FAIL  exit=1
[verify]    evidence: .mini-ork/runs/run-20260530-091542-cf4a/verifier_typecheck.log
[verify]    error_summary: src/tally.js(42,5): error TS1005: '}' expected.

[review]    verdict: REQUEST_CHANGES
[review]    rationale: typecheck.sh failed. implementer introduced a syntax error at line 42.
[review]    suggested_changes:
              [1] file=src/tally.js location=line 42 change="close the if-block with a missing '}'"

[implement] (iter 2) applying suggested_changes[1] ...
```

The loop retries up to `max_iter` (default: 3) before escalating to `human_gate`.
