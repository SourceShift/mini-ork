# SDD mechanisms roadmap (I1–I9): epics doc for `mini-ork epics`

Plan: `docs/plans/2026-10-07-sdd-mechanisms-for-mini-ork.md`. Read it before
any epic below. Root-cause input: `kickoffs/sdd-mechanisms/k0-unpublished-spend-root-cause.md`.

How to use:
1. `bin/mini-ork epics ingest kickoffs/sdd-mechanisms/roadmap.md`, then
   `bin/mini-ork epics split kickoffs/sdd-mechanisms/roadmap.md`.
2. Before dispatching any epic, hand-check its generated kickoff:
   - **Files in scope** must be confirmed against the code. The lists below
     are candidates.
   - It needs a **runnable verification command**. The fallback that `split`
     synthesizes is wrong for these epics, so use the
     `### Verification command` given here.
   - A headless run blocks with exit 6 unless both are present.
3. Build in a worktree only: `make worktree SLUG=<epic-id> OWNS="<scope paths>"`.
4. Dispatch one epic per run. Every epic must report the K1 metrics before and
   after it lands.

Run order (revised after K0, `docs/audits/20261007-unpublished-spend-root-cause.md`
@28a2d9a3, AC5 accepted 2026-10-07):
K0.5a → K0.5b → K1 → I1 → I1-shadow → K0.5c → VA-verifier → I3 → I5 → I2 → I6 → I8 → I7 → I4 → I9.
- K0.5 is inserted first. Without it, about 37% of corrected unpublished spend
  cannot be attributed, so K1 before/after comparisons would measure noise.
- I3 now runs before I5. I3 has a measured target (`needs_answers`); I5 has no
  K0 bucket.
- K0.5c (added with the K0 erratum) runs after I1 and before I3: from I3 on,
  epics are dispatched through framework-edit, and verifier errors with no
  recorded cause (plus verifier durations that are always 0) would make its
  publish-rate measurement untrustworthy.

Build rules (user decisions, 2026-10-07):
- **Build mode is hybrid.** K0.5a, K0.5b, K0.5c and I1 repair the dispatch
  pipeline itself, so they are built directly in worktrees with
  mutation-tested gates. From I3 onward each epic is DISPATCHED through
  framework-edit (revise loop live), and `mini-ork metrics sdd` measures
  whether framework-edit's publish rate (6/173 at K0) improves. If a dispatch
  fails for harness reasons rather than the epic's content, record it in the
  metrics (it is a K0 bucket), then fall back to a direct build and say so in
  the commit.
- **Shadow before any flip (user decision, 2026-10-07).** A tightening flag
  first runs in shadow (`MO_PROBE_VALIDITY=shadow`: evaluate, record
  would-blocks, never block). Once >= 30 shadow runs per active recipe exist,
  inspect the would-blocks by hand (`mini-ork metrics sdd` shadow section);
  only if false refusals look small does the paid held-out A/B run.
- **Behaviour-tightening ships behind a default-OFF `MO_*` flag.** Any epic
  that makes verify, publish or the reviewer stricter (I1 first) lands with its
  flag off. Turning it on is a separate change, made only after an A/B of
  n ≥ 30 runs against the K1 frozen baseline
  (`backups/k0-baseline-20261007-104547.db` +
  `backups/k0-run-dirs-20261007-113952.json`).

## Finalize and record every run (id: sdd-k05a-finalize-every-run)
- recipe: framework-edit

### Goal
K0 root cause #1 ("silent death") is not an SDD problem. No run may end
without a recorded terminal status, and every failure reason must reach a log.
### Acceptance
- AC1: `set_status` (`mini_ork/cli/execute.py:1133`, and the late-binding
  wrapper at `mini_ork/cli/publisher.py:22`) raises after its retries are
  exhausted. Today it only prints `[warn]`, which violates the zero-fallback
  rule.
- AC2: An execute-only run that PASSED (e.g. libwit verified-artifact) ends
  in a terminal status that records the pass.
  - Use a status the `task_runs` CHECK constraint allows, and document the
    choice.
  - It must never be left non-terminal.
- AC3: The publisher's stderr `[fail]` lines are captured into `execute.log`.
  K0 found 9 SDD rollbacks whose reason was never logged.
### Files in scope (candidates — confirm)
- `mini_ork/cli/execute.py`
- `mini_ork/cli/publisher.py`
- `mini_ork/cli/main.py` (lifecycle finalize and log capture)
- `tests/unit/test_run_finalization.py`
### Out of scope
- The run reaper (already shipped in 7954fa6f, ec54774d, bf2805dd).
- Backfilling old rows.
### Verification command
`python3 -m pytest -q tests/unit/test_run_finalization.py`

## Verdict hygiene (id: sdd-k05b-verdict-hygiene)
- recipe: framework-edit
Depends on: sdd-k05a-finalize-every-run

### Goal
Two verdict paths fail runs wrongly or without a reason:
- An "advisory" rubric pre-screen (`mini_ork/cli/main.py:905`) was the sole
  cause of failure in 12 runs where both verify and the reviewer passed.
- `verdict=unknown` (an unparseable reviewer output) produced 16 runs that
  failed without a recorded reason.
### Acceptance
- AC1: The rubric pre-screen never decides a run's final status. It only
  records a score.
- AC2: A reviewer `verdict=unknown` fails the reviewer node explicitly, with
  reason `reviewer_verdict_unparseable` in the trace and task_runs notes. It
  must never fail silently or roll back without a reason.
### Files in scope (candidates — confirm)
- `mini_ork/cli/main.py`
- `mini_ork/cli/execute_handlers.py` (reviewer handler, ~:1094)
- `tests/unit/test_verdict_hygiene.py`
### Out of scope
Changes to reviewer prompts or the rubric scoring logic (I7 covers the
reviewer's authority).
### Verification command
`python3 -m pytest -q tests/unit/test_verdict_hygiene.py`

## Baseline metrics report (id: sdd-k1-baseline-metrics)
- recipe: framework-edit
Depends on: sdd-k05a-finalize-every-run, sdd-k05b-verdict-hygiene

### Goal
Add a `mini-ork metrics sdd` view that prints the plan's baselines from
`state.db` and the run dirs, so every later epic can prove its effect:
- publish rate, `$`/published, and the share of spend on unpublished runs
- vacuous-verify rate (overall and per month)
- verify-passed-but-run-failed counts
- reviewer-stage failure rate
- non-terminal run count
- planner-failure, `needs_answers`, and empty-diff counts from the run dirs
### Acceptance
- AC1: All plan baselines reproduce exactly on the frozen K0 snapshot
  `--db /Volumes/docker-ssd/Migration/Development/backups/k0-baseline-20261007-104547.db`
  (runs=1342, published=467, non-terminal=388, cost=$3151.17, unpublished
  67.0%). Open it with the URI `file:<path>?immutable=1`; plain `-readonly`
  fails with SQLITE_CANTOPEN (14) on this snapshot.
- AC2: `--json` output is stable and schema-checked.
- AC3: Read-only. Never writes to the DB.
- AC4: Runs that the run reaper marked `failed/CRASH` (upstream 7954fa6f +
  ec54774d, `mini-ork reap`) are reported as their own bucket. They never
  count toward false-reject or real-failure rates. Without this, before/after
  comparisons drift as the reaper heals old runs.
### Files in scope (candidates — confirm)
- `mini_ork/cli/metrics.py`
- `tests/unit/test_metrics_sdd.py`
### Out of scope
New tables. Changing how cost is recorded.
### Verification command
`python3 -m pytest -q tests/unit/test_metrics_sdd.py && bin/mini-ork metrics sdd --json`

## I1 probe validity in core verify (id: sdd-i1-probe-validity)
- recipe: framework-edit
Depends on: sdd-k1-baseline-metrics

### Goal
For every recipe:
- each acceptance criterion maps to exactly one probe, with no aliasing
- each probe must FAIL on the base tree and PASS on the changed tree
- a `vacuous` verify is never publishable

Move the broken-baseline logic out of the SDD recipe and into core verify.
### Acceptance
- AC1: Aliased probes (one probe shared by several ACs) are rejected, with a
  named reason.
- AC2: A probe that already passes on the base tree is rejected as vacuous.
- AC3: The publisher refuses a run whose verify is `vacuous`.
- AC4: `tests/test_sdd_*.py` stays green.
### Files in scope (candidates — confirm)
- `mini_ork/cli/verify.py`
- `mini_ork/verify/probe_validity.py` (new)
- `recipes/spec-driven-delivery/verifiers/test-validity.py`
- `mini_ork/cli/publisher.py`
- `tests/unit/test_probe_validity.py`
### Out of scope
LLM test authoring (that is I2). Recipe workflow changes.
### Verification command
`python3 -m pytest -q tests/unit/test_probe_validity.py tests/test_sdd_verifiers.py tests/test_sdd_e2e_dryrun.py`

## I1 shadow mode (id: sdd-i1-shadow)
- recipe: framework-edit
Depends on: sdd-i1-probe-validity

### Goal
Start the data clock for the I1 flip without changing any verdict. Built
directly (pipeline repair).
### Acceptance
- AC1: `MO_PROBE_VALIDITY=shadow` runs every I1 check and writes
  `probe-validity.json` with `mode`, `would_block` and `reasons`.
- AC2: a would-block adds `[shadow] would block: <reason>` to task_runs notes.
- AC3: shadow NEVER blocks and is behaviour-identical to OFF apart from those
  two writes (return, final status, stdout/stderr) — tested.
- AC4: `mini-ork metrics sdd` reports shadow n and would-block counts by
  recipe × reason.
### Files in scope (candidates — confirm)
- `mini_ork/verify/probe_validity.py`, `mini_ork/cli/publisher.py`
- `mini_ork/cli/metrics_sdd.py`, `schemas/metrics_sdd.schema.json`
- `tests/unit/test_probe_validity.py`, `tests/unit/test_metrics_sdd.py`
### Verification command
`python3 -m pytest -q tests/unit/test_probe_validity.py tests/unit/test_metrics_sdd.py`

## framework-edit verifier nodes actually run (id: sdd-k05c-verifier-nodes-run)
- recipe: framework-edit
Depends on: sdd-i1-probe-validity

### Goal
Pipeline repair (build directly, per the hybrid rule). Two findings from the
K0 erratum:
- `duration_ms` is 0 on every Python-era verifier `node_end`, run or not: the
  executor's fallback node_end (`execute_handlers.dispatch_node`, for handlers
  that never call `trace()`) hard-codes it. "0 ms" is therefore NOT evidence
  that a verifier did not run.
- Verifier nodes errored with no recorded cause in 57 runs (framework-edit 27,
  code-fix 20), 30 of them in K0 bucket B19 ($108.94 raw), plus 3 SDD-campaign
  runs ($80.24). vt1–vt6 failed in the same second they started.
Detection rule for those errors (run_events, not execution_traces):
`event_type='node_end' AND finish_reason='error' AND
json_extract(payload_json,'$.node_type')='verifier' AND
json_extract(payload_json,'$.duration_ms')=0`.
### Acceptance
- AC0: The fallback `node_end` records the real duration (from the
  recorded node start), not 0.
- AC1: Root cause of those verifier errors, with run ids (e.g.
  `concord-mo1-20261002-184302`, `vt1-mr-relations-20261005-193626` …
  `vt6-level-vector-20261005-203840`). Their reasons were not logged before
  `e245d002`; reproduce on a fresh run where stderr now lands in execute.log.
- AC2: A verifier node that did not execute fails loudly with reason
  `verifier_not_executed` — never as a bare `error`.
- AC3: 0 such cases on a fresh framework-edit smoke run.
### Files in scope (candidates — confirm)
- `mini_ork/cli/execute.py` (`_run_verifier_ref`)
- `mini_ork/cli/execute_handlers.py` (verifier handler)
- `tests/unit/test_verifier_nodes_run.py`
### Verification command
`python3 -m pytest -q tests/unit/test_verifier_nodes_run.py`

## verified-artifact gets a real verifier (id: sdd-va-real-verifier)
- recipe: framework-edit
Depends on: sdd-i1-shadow

### Goal
Built directly; the recipe lives in the libwit/researcher repo at
`server/resources/miniork-overlay-recipes/verified-artifact`. Today
`verifiers/schema.sh` only checks "the output parses as JSON (after fence
extraction) + the manifest exists" and emits no `pass: true` evidence, which
is why I1 counts every verified-artifact run as `verify_vacuous` (219/219
published runs in the shadow estimate; K1: 217 published with a vacuous
verify).
### Acceptance
- AC1: port to `verifiers/schema.py` (bash verifiers are deprecated). It
  emits ONE JSON verdict `{"pass": bool, "checks": [{id, pass, reason}]}` and
  leaves the `verifier_*` evidence file I1 reads.
- AC2: real checks — (a) `verified-artifact.json` validates against the
  schema the manifest declares, not just "is JSON"; (b) required fields are
  non-empty; (c) references/citations in the artifact resolve to items in the
  inputs manifest (grounding), where the artifact type has them.
- AC3: a deliberately broken fixture fails and a known-good one passes; the
  checks are mutation-tested.
- AC4: a fresh batch of verified-artifact runs under
  `MO_PROBE_VALIDITY=shadow` shows 0 `verify_vacuous` would-blocks.
- Constraint: the host side (`verifiedArtifactClient`, the caller-owned repair
  callback) consumes this recipe — confirm its owner in libwit and do not break
  the envelope it reads.
### Files in scope (candidates — confirm, libwit/researcher repo)
- `server/resources/miniork-overlay-recipes/verified-artifact/verifiers/schema.py` (new; replaces `schema.sh`)
- `server/resources/miniork-overlay-recipes/verified-artifact/workflow.yaml`
- tests next to the recipe
### Verification command
Recipe tests (to be named when the files are confirmed) + a shadow batch read
with `mini-ork metrics sdd`.

## I5 evidence ledger bound to code state (id: sdd-i5-evidence-ledger)
- recipe: framework-edit
Depends on: sdd-i1-probe-validity

### Goal
Add a run-level `evidence-ledger.jsonl`. Each row records:
AC id → probe → verdict → `git write-tree` hash → log path.

Reviewers and the publisher may cite only ledger rows.
`implementer-summary.json` becomes advisory text.
### Acceptance
- AC1: A verdict whose tree hash differs from the current tree is void.
- AC2: The publisher rejects an approval that cites no ledger row.
- AC3: A fixture run where the implementer claims "implemented" with 0 files
  changed cannot publish.
### Files in scope (candidates — confirm)
- `mini_ork/verify/evidence_ledger.py` (new)
- `mini_ork/cli/publisher.py`
- `recipes/spec-driven-delivery/verifiers/ledger-writer.py`
- `tests/unit/test_evidence_ledger.py`
### Out of scope
Changes to the reviewer prompt (that is I7).
### Verification command
`python3 -m pytest -q tests/unit/test_evidence_ledger.py tests/test_sdd_verifiers.py`

## I3 kickoff contract + clarification answer path (id: sdd-i3-kickoff-contract)
- recipe: framework-edit
Depends on: sdd-k1-baseline-metrics, sdd-k05c-verifier-nodes-run

### Goal
`kickoff_lint` requires five sections:
- Goal
- Acceptance (with AC ids)
- Files in scope
- Out of scope
- Verification command

When information is missing, `needs_answers` becomes a structured ASK artifact
(`blocking_refs`, options, default). It is answerable through
`mini-ork inject` / `board` / ACP, and the run then continues via `resume`
instead of ending failed.
### Acceptance
- AC1: Lint names every missing section.
- AC2: A blocked headless run writes `asks/*.json` and exits resumable, not
  failed.
- AC3: An answered ASK resumes the run, which passes the profile gate.
### Files in scope (candidates — confirm)
- `mini_ork/kickoff_lint.py`
- `mini_ork/cli/main.py` (`gen_profile`, ~:327-407)
- `schemas/ask.schema.json` (new)
- `mini_ork/cli/resume.py`
- `tests/unit/test_kickoff_contract.py`
### Out of scope
Splitting kickoffs (that is I4).
### Verification command
`python3 -m pytest -q tests/unit/test_kickoff_contract.py`

## I2 fresh-context probe author, held-out split (id: sdd-i2-probe-author)
- recipe: framework-edit
Depends on: sdd-i1-probe-validity, sdd-i5-evidence-ledger

### Goal
Add a `probe_author` step that runs before implementation:
- It sees only the kickoff and the base repo, never the patch.
- It runs on a lane different from the implementer's. Enforce
  `heterogeneity.authorship_separation`, which today is declared but not
  enforced.
- It writes `acceptance-probes.json`.
- A held-out share of the probes is hidden from the implementer.
- It is gated by `risk_class`: on for high/critical and epics, off for
  trivial code-fix.
### Acceptance
- AC1: When the probe-author lane equals the implementer lane, the run is
  refused.
- AC2: Held-out probes never appear in the implementer's context; assert this
  on a fixture.
- AC3: Every probe passes I1 validity.
### Files in scope (candidates — confirm)
- `mini_ork/cli/execute_handlers.py` (handler registration)
- `prompts/probe-author.md` (new)
- `mini_ork/planning/recipe_plan.py`
- `tests/unit/test_probe_author.py`
### Out of scope
Changing the default lanes in `config/agents.yaml`.
### Verification command
`python3 -m pytest -q tests/unit/test_probe_author.py`

## I6 policies.yaml negative rules enforced by the harness (id: sdd-i6-policies)
- recipe: framework-edit
Depends on: sdd-k1-baseline-metrics

### Goal
Add `.mini-ork/policies.yaml`, holding machine-checkable "never" rules:
- delete test or gate files
- edit outside Files in scope
- add a new dependency
- do an unrelated refactor

Each rule is enforced three ways:
1. Rendered as negative constraints in prompts.
2. Enforced in the harness (scope guard, `branch_quarantine`, hooks).
3. Audited over the run trajectory, not only the final diff.
### Acceptance
- AC1: A fixture edit that deletes a test file is blocked or reverted before
  commit.
- AC2: A trajectory audit flags a mid-run violation that was later undone.
- AC3: Policy violations per run are recorded for the K1 metrics.
### Files in scope (candidates — confirm)
- `schemas/policies.schema.json` (new)
- `mini_ork/cli/execute_handlers.py` (`scope_guard_block`, ~:619)
- `mini_ork/vcs/branch_quarantine.py`
- `hooks/scope-enforce.sh`
- `tests/unit/test_policies.py`
### Out of scope
Prose constitution files. The plan drops these: the evidence shows no gain.
### Verification command
`python3 -m pytest -q tests/unit/test_policies.py`

## I8 executed guarded repair loop (id: sdd-i8-guarded-loop)
- recipe: framework-edit
Depends on: sdd-i5-evidence-ledger

### Goal
**Build on the `revise-loop` work. Do not write a second loop.** Framework-edit
run `fl-k3-20261006-230228` (kickoff `.mini-ork/kickoffs/revise-loop-before-rollback.md`,
worktree `mini-ork-worktrees/revise-loop`, owner session mini-ork-ec):
- It made `retries` edges real: it adds `CompiledWorkflow.retry_edges`, and a
  failed node now sends its findings back to the implementer for up to
  `max_rounds` rounds.
- It PASSED: reviewer pass, static checks 14/14, tests 10/10, unit tests
  11/11.
- Merged to main at 2be82523 on 2026-10-07 (`MO_REVISE_ROUNDS`, default 2).

I8 adds the guards below on top of `retry_edges`.

Today `recursion:` blocks are declared but never executed: the executor
ignores `recursive` edges (`workflow/compiler.py:309`) and edge conditions.
Extract goal-loop's driver into a shared guarded loop with these rules:
- default cap of 3 iterations
- checkpoint every verified state
- re-run ALL probes each iteration
- never revise a passing slice
- regenerate from contract + ledger, not from chat history

Use it in the goal-loop, SDD and epics recipes.
### Acceptance
- AC1: The `recursion:` caps (iterations, per-iteration and total budget) are
  enforced.
- AC2: An earlier-passing AC that regresses in a later iteration rolls back
  to the checkpoint.
- AC3: goal-loop behavior is unchanged; its tests stay green.
### Files in scope (candidates — confirm)
- `mini_ork/orchestration/guarded_loop.py` (new)
- `recipes/goal-loop/lib/drive.py`
- `mini_ork/cli/execute.py`
- `tests/unit/test_guarded_loop.py`
### Out of scope
Changing the defaults of existing recipes' `recursion:` blocks.
### Verification command
`python3 -m pytest -q tests/unit/test_guarded_loop.py tests -k goal_loop`

## I7 evidence-grounded convergence audit (id: sdd-i7-convergence-audit)
- recipe: framework-edit
Depends on: sdd-i5-evidence-ledger, sdd-i8-guarded-loop

### Goal
Per-AC status comes deterministically from the ledger. The LLM step changes
from an approve/reject vote to a gap finder:
- It may only ADD findings of type `missing`, `partial`, `contradicts` or
  `unrequested`.
- Each finding carries a file:line pointer, and the pointer is checked.
- A diff hunk that maps to no AC is flagged `unrequested`.
- No LLM vote decides pass.
### Acceptance
- AC1: A run with all ACs proven in the ledger publishes even if the LLM
  reviewer "rejects".
- AC2: An unrequested hunk is flagged.
- AC3: A finding whose pointer is not real is dropped.
### Files in scope (candidates — confirm)
- `mini_ork/cli/execute_handlers.py` (reviewer handler)
- `mini_ork/cli/publisher.py`
- `prompts/reviewer.md`
- `tests/unit/test_convergence_audit.py`
### Out of scope
Changes to the GRPO reward formula.
### Verification command
`python3 -m pytest -q tests/unit/test_convergence_audit.py`

## I4 slice-to-fit before planning (id: sdd-i4-slice-to-fit)
- recipe: framework-edit
Depends on: sdd-i3-kickoff-contract

### Goal
- A kickoff over the AC-count or size budget routes automatically through
  `epics split`, producing one independently testable slice per run.
- A plan whose `verifier_contract` does not cover every AC id is rejected.
### Acceptance
- AC1: A 12-AC fixture kickoff produces at least 2 slices, with no planner
  truncation.
- AC2: Zero `missing_verifier_contract` failures on a fixture set.
### Files in scope (candidates — confirm)
- `mini_ork/cli/plan.py`
- `mini_ork/cli/epics.py`
- `mini_ork/kickoff_lint.py`
- `tests/unit/test_slice_to_fit.py`
### Out of scope
Scheduler parallelism changes.
### Verification command
`python3 -m pytest -q tests/unit/test_slice_to_fit.py`

## I9 shared contracts experiment + spec-kit input adapter (id: sdd-i9-shared-contracts)
- recipe: framework-edit
Depends on: sdd-i4-slice-to-fit

### Goal
This is an EXPERIMENT; the evidence for it is weak.
- An epic group gets one `contracts/` artifact (interfaces and data model)
  attached to every child kickoff, plus one end-to-end "producer wired" probe.
- A/B it on epics, with n ≥ 30, using the held-out eval stack.
- Also add a spec-kit input adapter that parses `specs/NNN/{spec,quickstart,tasks}.md`
  into the I3 contract.
### Acceptance
- AC1: An A/B report on integration failures, with and without shared
  contracts.
- AC2: A spec-kit fixture parses into a valid I3 contract.
### Files in scope (candidates — confirm)
- `mini_ork/specdir/speckit.py` (new)
- `tests/fixtures/sdd_speckit/`
- `tests/unit/test_speckit_adapter.py`
### Out of scope
Shipping shared contracts by default before the A/B result.
### Verification command
`python3 -m pytest -q tests/unit/test_speckit_adapter.py`
