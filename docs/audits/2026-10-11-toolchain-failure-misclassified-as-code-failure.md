# Audit — a toolchain failure read as a code failure, and the 10-node cascade it caused

*2026-10-11 · source: run `run-1791732001-79120` (live `recursive-validate-impl`
run against the researcher repo). Companion to
`docs/architecture/verification-stack.md`, whose third value this defect is a
counter-example to.*

## 1. What was observed

A correct implementation was rolled back. The run's own artifacts say why:

`verifier_tier1-compile-typecheck.json`:

```json
{"verifier": "tier1-compile-typecheck", "pass": false,
 "tsc_rc": 0, "lint_rc": 1, "shell_rc": 0}
```

`tier1-evidence.log`:

```
touched_files=1
source_files=1
shell_files=0
artifact_files=0
npm error code EBADDEVENGINES
npm error Invalid name "pnpm" does not match "npm" for "packageManager"
```

`verdict.json`: `failed_nodes: 10`.

**The type-check passed.** Nothing was wrong with the code. The lint arm never
ran: `npx` was refused by npm before eslint started, because the target repo
declares `devEngines.packageManager = {name: pnpm, onFail: error}` and npm
refuses to be used against a repo that declares another manager.

## 2. The mechanism, in one sentence

**A verifier's verdict is a single boolean, and a boolean carries no
provenance.** `pass: false` cannot say *"your code is wrong"* or *"I could not
run the tool"*, so the orchestrator must assume the first — and the two have
completely different correct responses.

### 2.1 The root cause (fixed)

`recipes/recursive-validate-impl/verifiers/tier1-compile-typecheck.py:89`
(since fixed) ran:

```python
lint_rc = subprocess.run(["npx", "eslint"] + SOURCE_FILES, ...)
```

The manager was hardcoded to the *engine host's* npm. A verifier runs with
`cwd` = the **target repo** (`mini_ork/cli/execute.py`, `_run_verifier_ref`), so
the manager is a property of *that* repo and had to be resolved from it.

### 2.2 The classification (NOT fixed — this is the follow-up)

The boolean is consumed here, with no way to see what produced it:

- `recipes/recursive-validate-impl/workflow.yaml:36` —
  `{ from: tier1_compile, to: tier2_unit, edge_type: depends_on, condition: pass }`
- and its mirror, `:37` —
  `{ from: tier1_compile, to: reflector, edge_type: escalates_to, condition: fail }`

A verifier that emits only `{pass, rc…}` gives the edge nothing else to branch
on. The edge machinery is behaving exactly as written.

## 3. The exact cascade

One `pass: false` at `tier1_compile` propagates through every `condition: pass`
edge (`workflow.yaml:36,38,40-43,49,51`):

| Node | Fate | Why |
|---|---|---|
| `tier1_compile` | **failed** | `lint_rc: 1` |
| `tier2_unit` | skipped | `tier1_compile → tier2_unit` is `condition: pass` (`:36`) |
| `tier3_property` | skipped | gated behind `tier2_unit` (`:38`) |
| `tier4_glm` / `tier4_kimi` / `tier4_codex` / `tier4_minimax` | skipped | gated behind `tier3_property` (`:40-43`) |
| `tier4_quorum` | skipped | gated behind the tier-4 lens fan |
| `tier4_synth` | skipped | `tier4_quorum → tier4_synth` is `condition: pass` (`:49`) |
| `publisher` | skipped | `tier4_synth → publisher` is `condition: pass` (`:51`) |
| `reflector` / `replanner` | **run** | `escalates_to, condition: fail` — and they roll the work back |

**1 real failure + 9 skipped = `failed_nodes: 10`**, and a rollback of code
that had already passed its own typecheck.

The cost is not only the wasted run: the loop's *learning signal* is also
corrupted. The reflector's input is `pass: false`, so it reasons about a code
defect that does not exist — the same defect class observed in
`docs/architecture/failure-triage.md`.

## 4. Why fixing the root cause does not close the class

The `npx`/`pnpm` bug is fixed (the verifiers now resolve the manager from the
target repo — `mini_ork/verify/pm.py`). But **any** other toolchain failure
reproduces this cascade verbatim: a missing binary, a config file the repo does
not ship, a lockfile mid-install, a sandbox with no network. The class is
*not* "hardcoded npm"; it is **"indistinguishable failure modes share one
boolean"**.

## 5. Proposed follow-up (not implemented here)

1. **Emit provenance with the verdict.** A verifier that shells out should
   classify each arm: `code` (the tool ran and rejected the input),
   `toolchain` (the tool could not be run — spawn failure, non-zero *before*
   the tool's own output, a refusal like `EBADDEVENGINES`, a missing binary),
   `infra` (sandbox/network/OOM). A minimal first cut: record the resolved
   argv in the evidence log (done — `$ <argv>` lines) so a human can tell in
   five seconds; the machine-readable field is the next step.
2. **Make the edge honour it.** `condition: fail` should not fire on
   `failure_kind: toolchain`. Per the three-valued rule in
   `verification-stack.md`, "the tool could not run" is `UNVERIFIED` — an
   **abstention**, not a `REFUTED`. The correct response is to fix the runner
   and re-run, never to reflect on the code and replan.
3. **Guard it.** The regression test
   `tests/unit/test_verify_pm.py::test_tier1_reaches_the_repo_own_eslint_on_a_pnpm_only_repo`
   pins the *specific* cause, and
   `::test_the_old_npx_argv_is_red_on_this_fixture` pins the fixture's ability to
   detect it. Neither can detect the next toolchain failure — only (1)+(2) can.

## 6. Evidence index

| Artifact | Path |
|---|---|
| Failing run | `.mini-ork/runs/run-1791732001-79120/` |
| Verifier verdict | `…/verifier_tier1-compile-typecheck.json` |
| Raw refusal | `…/tier1-evidence.log` |
| Cascade edge | `recipes/recursive-validate-impl/workflow.yaml:36` |
| Fixed root cause | `recipes/recursive-validate-impl/verifiers/tier1-compile-typecheck.py:89` (pre-fix) |
| Shared resolver | `mini_ork/verify/pm.py` |
