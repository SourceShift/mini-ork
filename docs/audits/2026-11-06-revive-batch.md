# Failed-run revive batch — 2026-11-06

Every `task_runs` row with `status='failed'` in the repo's `.mini-ork/state.db`
was evaluated with `bin/mini-ork recover <id> --status` (pure Python, no LLM,
no spend) and classified by what it actually left behind. This document records
the inventory, the batch that was revived, and the close-out decisions for the
runs that must **not** be revived.

## Inventory

| Bucket | Runs | Spend |
|---|---:|---:|
| Total failed rows | 730 | $1,931.33 |
| No run dir — unrecoverable | 574 | $1,498.74 |
| Run dir present — evaluable | 156 | $432.59 |
| … with a reusable checkpoint (`reuse > 0`) | 137 | — |
| … with a salvage patch | 29 | — |
| … whose own `verdict.json` says `pass: true` | 36 | — |

Two facts make the 574 worthless rows cheap to dismiss:

- **226 of them share one timestamp**, `2026-10-08 18:47:19`. That is a single
  bulk status rewrite, not 226 individual failures. Their run dirs were swept
  before the rows were written, so no checkpoint or patch survives.
- A row without a run dir has nothing to resume from. `recover` reports `no_patch`
  / `no_target` and can only re-run the whole DAG at full cost. That is a new
  run, not a revive.

```mermaid
flowchart TD
  A["730 failed task_runs rows"] --> B{"run dir present?"}
  B -->|"no — 574 rows, $1,498.74"| C["Unevivable: no checkpoint, no patch.<br/>226 of them = one bulk rewrite"]
  B -->|"yes — 156 rows, $432.59"| D{"recover --status retryable?"}
  D -->|"no"| E["Tier 2: cause must be fixed first<br/>reviewer_escalated, spec-test-defect, lane_unhealthy"]
  D -->|"yes — 45 runs"| F{"landed-ness oracle<br/>on the salvage patch"}
  F -->|"git apply -R --check OK"| G["Tier 3: already on main —<br/>never revive"]
  F -->|"nothing left to apply"| H["Tier 3: deliverable exists —<br/>no revive"]
  F -->|"still applies / no patch"| I["Tier 1: revive"]
  I --> J["Merged, re-dispatched, or blocked"]
```

## The landed-ness oracle

A salvage patch has exactly three states against today's `main`:

| `git apply --check` | `git apply -R --check` | Meaning |
|---|---|---|
| fails | succeeds | **already on main** — the failed row lies |
| succeeds | fails | **still needed** — a genuine revive |
| fails | fails | base drifted — hand reconciliation required |

Using only the forward check (`recover --status`'s `would_apply`) collapses the
first and third states into `conflict` and makes landed work look like drift.
Always run both directions.

## Tier 1 — revived

| # | Run | Plan | Outcome |
|---|---|---|---|
| 1 | `rsi-t1-kernel-hole-3-freeze-policy-inputs-20261027-134723` | `retryable` refused by a stale retry-hint; the cited success command was re-run instead | **Merged** as `bcc98e10` — *feat(kernel): freeze the self-driving policy's inputs* |
| 2 | `run-1792400538-82050` | `resume` from `implementer`, 4 checkpoints reused | **Superseded** — see below; the resume died on a remote node-agent 502 |
| 3 | `rsi-t0f-r8-hardening-20261027-085038` | `resume` from `code_impact_lens`, 1 checkpoint reused | **Already landed** — see below |
| 4 | `rsi-c1793808348-e4c9d816` (child of `rsi-c1793808348`) | not a `recover` plan — the unmerged commit was found in its worktree | **Merged** as `8e4aeddb` — *fix(themes): keep pre-split authored themes in the clustering space* (RSI rule L6) |
| 5 | `rsi-c1793086961-7396275d` (child of `rsi-c1793086961`) | the killed child's patch applied cleanly to today's `main` | **Merged** as `b812c86f` — *feat(learning): hold out a deterministic slice of every lesson block* (RSI rule L3) |
| 6 | `rsi-c1792978180-2da93174` (child of `rsi-c1792978180`) | worktree branch, one commit ahead of `main` | **Merged** as `a19f1448` — *feat(dispatch): record the serving lane on every `llm_calls` row* (RSI rule R8) |
| 7 | `rsi-c1793827696-4b94d9a5` (child of `rsi-c1793827696`) | worktree branch, one commit ahead of `main` | **Merged** as `046948c8` — *feat(trace): persist the code region routing decided on* (RSI rules R1/R3) |
| 8 | `rsi-c1793834730-f5bfec93` (child of `rsi-c1793834730`) | worktree branch, one commit ahead of `main` | **Merged** as `cefb0433` — *rsi: verifier calibration ground truth is never recorded* (RSI rule V3) |

For revives 4–8 the `Run` column names a **worktree slug**, which is not a
`task_runs.id`. An `rsi-cycle` child has two rows, `<slug>-rsi-cycle` (the
driver) and `<slug>-fe` (the `framework-edit` child); both are `published`,
which is exactly why the killed-driver shape hides its unmerged commit from
every run-table query.

Two of the first three dispatches resolved without a merge, and neither because
the work was bad: both because `main` had moved 495 and 10 days respectively, and
the deliverable was already there.

Revive 1 is the instructive one. `recover` refused it with `rc=1` —
"retry-hint refuses this run (strategy='none')" — because the *recorded*
`review-verdict.json` from the October attempt was red. That file is evidence
of what happened then, not of what the branch does now. Running the kickoff's
own success command in the worktree gave **54 passed**, and the frozen-path
consumers gave **219 passed**, so the branch was merged rather than re-run.
`--force` was never used: the hint was audited, not overridden.

### Revives 4 and 5 — found in worktrees, not in the run table

Both were `rsi-cycle` children: an `rsi-cycle` run dispatches a `framework-edit`
child in a worktree of its own, and when the driver is killed the child leaves a
published-looking row, a worktree, and an unmerged commit behind. `recover`
never sees this shape — the child's own row can read `published` while its
branch was never pushed — so the landed-ness oracle has to be applied to the
*worktree*, not to the run dir. The sweep of the `rsi-c*` worktrees found these
two with real, unlanded work in them.

Revive 4 (`theme_clusters`) also proves the oracle's third case. The patch had
been rebased away from the code it was written against (it was 66 commits
behind), and the live DB already carried its table — created ad hoc months
earlier, with no migration file on `main` and no code referencing it. So the
gap was real (6364 of 6497 `lesson_themes` rows had no lesson text, exactly what
rule L6 measures), the table's presence was not evidence that the work landed,
and a `git apply --check` against `main` was the only thing that settled it.

The pre-push panel blocked the first merge attempt with a defect worth keeping:
copying only the *blank* rows into `theme_clusters` drops the authored themes
from the clustering space, because pre-split **every** `lesson_themes` row was a
cluster candidate. The rest of the patch was sound; the migration and the read
path were fixed in the same worktree (`8e4aeddb`) and merged on the second pass.
That is the panel earning its cost on a patch that had already been read by a
human.

Live outcome after the migration: `theme_clusters` 6513 rows, `lesson_themes`
133 (authored) with **0** blank — `mini-ork rsi-cycle plan` no longer emits L6,
while 7 other gaps from the same harvest still do.

### Revive 6 — `rsi-c1792978180`: `llm_calls` records no lane (R8)

Merged as `a19f1448`. Additive `lane` column (nullable, no default, no backfill),
filled by both writers from the model that actually served the call, guarded
ALTER, renumbered from the patch's `0085` because `0085_review_findings.sql`
already occupies that number. Applied to the live DB: `pragma_table_info('llm_calls')`
has `lane`, `schema_migrations` has the row.

**A trap worth recording, caught while applying it:** the guarded ALTER is
`.read "|sh -c …${MINI_ORK_DB}…"`. Running the file with the `sqlite3` CLI
instead of through `migrate_apply` leaves `MINI_ORK_DB` unset, so the shell
guard emits no ALTER — and the file's own trailing INSERT still records the
migration. The result is a recorded no-op. Repair: delete the
`schema_migrations` row, re-run `migrate_apply` (it exports `MINI_ORK_DB`), then
assert the column exists. Never hand-apply a guarded migration.

### Revive 7 — `rsi-c1793827696`: `execution_traces.code_region` is never persisted (R1/R3)

Merged as `046948c8`. `decide()` derives `code_region` from the scoped run dir;
`routing.learning_governed_lane` passed its decision through without it and
`_make_trace_fn` never wrote it, so the column stayed empty. Measured on the
live DB before the fix: **10 589 of 12 732** `execution_traces` rows NULL (83%),
of which 1 946 carried a `route_source` — a signal starved at the write
boundary, not starved at the producer, so `lane_region_advantage` had nothing
to learn from.

The fix forwards the decision's region untouched and writes it **only when the
decision had one**: a NULL stays NULL instead of being substituted from the run
dir, and the implementer's `files_written`-derived UPDATE still overrides it.
`trace_store` treats `code_region` as a `coalesce` column, so a later NULL write
can never blank a region already recorded, and a pre-0044 schema simply drops
the column from its narrowed write. Nine tests pin the chain end to end,
including the two absences that matter: no region substituted when `decide()`
had none, and no region stamped without a `route_source`.

### Revive 8 — `rsi-c1793834730`: verifier calibration has no ground truth (V3)

Merged as `cefb0433`. `verifier_chain_repair` existed with **zero production
callers** — a grep over `mini_ork/`, `scripts/` and `tests/` finds only its
definition and its own unit test — so every `verifier_results` row landed with
`repair_run_id` NULL. Live DB: 1 987 rows, 158 `verdict='fail'`, **0** repair
links, 0 false positives, 0 false negatives, 0 annotated. The calibration half
of the verifier had no ground truth at all.

`planner.cli_main` now links the resumed run's unlinked `fail` rows to the run
it just dispatched. Three constraints hold it honest:

- it arms only on the captured `MO_AUTO_REPAIR_RUN_ID` marker, so a repair the
  auto-repair loop spawned writes and a human `recover` writes nothing;
- it sits after every pre-spawn refusal (the plan-invalid early return, RB-06
  placement, RB-08) and before `apply_env_overrides`, so a refused, paused or
  undispatched recover cannot claim a repair that never ran;
- the value is the run the recover genuinely resumed, **never** the marker's
  per-attempt child id.

`repair_run_id IS NULL` keeps re-runs idempotent; pass / indeterminate /
vacuous rows, other runs' rows and operator FP/FN annotations stay byte
identical. Fail-soft: a missing table or a locked DB writes a stderr line and
never fails the repair it describes. Eight tests.

**Honest limit:** a self-link records *that* a run continued after a repair, not
*whether* the repair helped. The real calibration signal is still the operator's
FP/FN labels, and those are still 0.

### Declined — `rsi-c1792959841`: `lesson_conflicts` audit pass (L10)

Read, not landed. **The premise no longer holds:** `lesson_conflicts` holds 40
rows, every one with a non-empty `why`, and `harvest._l10` raises `_GapClosed`
as soon as `count > 0` — `rsi-cycle plan` on today's DB lists 7 candidates and
L10 is not among them. The detector already sits on the authoring path; the gap
closed without this patch.

Even if it had not, the shape is wrong for a default-on path. `reflect` would
run up to `DEFAULT_AUDIT_LIMIT = 20` judge calls on **every** reflect, and the
patch's own docstring admits a re-run "re-judges the same head of the order":
recording is idempotent, so the second and every later pass buys nothing and
bills anyway. It also changes behaviour it does not test — `_class_is_relevant`
moves from literal string equality to member-set intersection, which widens the
neighbour set on the *authoring* path, where a `duplicate` verdict withholds a
lesson and retries it. That is a bigger change than wiring an audit, and the
patch ships with zero tests.

Two ideas in it are worth keeping if the rule ever fires again: score sources by
best containment so a capped pass spends its budget where the lattice is dense,
and exclude a source from its own neighbour set.

### Declined — `rsi-c1793812835`: `learning_record.benchmark_delta` (L9)

The patch applies cleanly to today's `main` and would compose
`AVG(utility_score - baseline_utility_score)` from `benchmark_results` rows
joined on `benchmark_results.candidate_id = learning_record.run_id`. On the
live DB that join matches **nothing**: `benchmark_results` holds 2 rows, its
`candidate_id` values are non-numeric slugs, and no `learning_record.run_id`
equals one. The patch would add a producer that writes nothing and leaves the
rule firing, while reading as fixed. The real blocker is the missing
candidate↔run link (`project_l9_benchmark_delta_has_no_link`), so this stays
declined until the link exists.

## The worktree sweep — how each was settled, and the final state

Twelve `rsi-c*` worktrees held a branch ahead of `main` when the batch began.
The oracle was run **against `main`** (not inside each worktree, where a delta
built off its own merge base trivially re-applies). Five applied forward
cleanly; six were base-drifted; one had nothing left to apply.

The five forward-clean ones are all settled:

- **landed** — revive 6 (`llm_calls.lane`, R8), revive 7
  (`execution_traces.code_region`, R1/R3) and revive 8
  (`verifier_results.repair_run_id`, V3);
- **declined** — `lesson_conflicts` audit pass (L10, whose rule no longer fires:
  40 rows with a reason each) and `learning_record.benchmark_delta` (L9, whose
  join matches nothing).

The six base-drifted ones were **re-measured rather than ported**, and none
needed its patch:

| Worktrees | Gap claimed | What was actually true |
|---|---|---|
| `…2968663`, `…2986926`, `…2992296` | R8 `llm_calls.lane` | three independent children wrote the same fix, already landed as `a19f1448`; one even wrote `0085_llm_calls_lane.sql`, colliding with an unrelated `0085_review_findings.sql` |
| `…3801689` | L3 held-out lessons | already implemented by `b812c86f` (`context_assembler._lesson_held_out`; `lesson_injections` carries 2 `held_out=1` rows) |
| `…2964477`, `…3001258` | V1 `verifier_results.confidence` | the shortcut `harvest._v1_confidence_never_recorded` names in its **own `fix_hint`**: cycle 15 merged exactly that reader and every row stayed NULL, because no verifier emits a top-level `confidence`. The live ratio is 1 915 / 1 987 = **0.964**, already under the rule's 0.99 threshold |

A patch that reads a field no producer writes is not a fix, and a patch against
a rule that no longer fires is `fixed_externally`. Landing them would have
added four duplicate implementations of two landed features and one known
shortcut. The rule is now: re-run the rule and read its `fix_hint` **before**
porting a stale child's patch.

Final state: **zero** `rsi-c*` worktrees and **zero** `wt/rsi-c*` branches
remain, and every claim is released. Getting there took two steps, and the
second one is worth knowing: `scripts/mini_ork_worktree.py clean` removes the
worktree but its `git branch -d` **refuses an unmerged branch**, so it silently
left ten of them behind (the two whose tip was already on `main` did get
deleted). Those ten were removed with `git branch -D` only after the record
above proved each one landed, superseded or declined — never on the assumption
that the oracle was merely confused.

## Tier 2 — deferred, cause first

| Gate | Runs | What blocks a revive |
|---|---:|---|
| `reviewer_escalated` | 7 | the reviewer escalated on an unresolved design question; a re-dispatch asks the same question again |
| `spec-test-defect` | 4 | the spec-test author emitted tests the spec cannot satisfy (`feedback_spec_test_author_can_emit_unsatisfiable_tests`); re-running reproduces the defect |
| `lane preflight: lane_unhealthy` | 2 | a lane credential or quota, not the work |

One run — `sde-4-supervisor-20261022-092423` — is blocked on a missing
deliverable rather than a gate: its kickoff
`mini-ork-worktrees/rsi-gaps/kickoffs/auto/sde-4-supervisor.md` went away with
the worktree, and only 5 checkpoints survive. A revive cannot restate the goal
it was never given, so it stays parked until the kickoff is recovered or
rewritten.

One run in this bucket, `w2-sdd-ratify-cli-20261030-174813`, is a
`spec-test-defect` failure whose patch already reverse-applies cleanly — the
defect stopped the run *after* the change was written. It is listed in Tier 3,
not here; the bucket label and the landed-ness oracle disagree, and the oracle
wins.

### Revive 3 — `rsi-t0f-r8-hardening-20261027-085038`: already landed

The kickoff asked for two things: R8 must refuse to dispatch into the engine
checkout, and it must reclaim stale epics *before* counting them against its
cap. `main` has both:

- `mini_ork/recovery/supervisor.py:1296` — `_dispatch_refusal(target_root, engine_root)`,
  called first in `_rule_dispatch_epic`, returns the refusal action instead of a
  dispatch.
- `supervisor.py:1359` — `scheduler.reclaim_stale(db)` runs before the cap count.

`recover --status` still calls the run `retryable` with 1 reusable checkpoint,
because retryability is a property of the *run*, not of the repository. Nothing
in the retry-hint checks whether the deliverable already exists.

### Revive 2 — `run-1792400538-82050`: superseded, both halves settled

The kickoff (oracle evidence-consistency guard + oracle-integrity producer) has
two halves, and `main` has moved 495 commits past the run's base:

- **Evidence-consistency guard — landed.** The three missed injections
  (`apply_fail_masked_as_pass`, `stale_evidence_replay`, `runner_error_as_pass`)
  are closed, and the judge's predicate is pinned by
  `tests/unit/test_certify_evidence_py.py` so the injected defect case must
  decide `UNVERIFIED` rather than `REFUTED`. `oracle_integrity.py`'s docstring
  records that this gap "was real once".
- **Producer — manual at the time, wired on 2026-11-08 (`693dc18c`).**
  `docs/architecture/verification-stack.md` then listed instrument ⑦ as
  *"consumer on, producer manual"*, and `promotion_gate.py` read
  `oracle_integrity.latest(db_path)`, returning `{"status": "unmeasured"}` when
  no one had run `mini-ork instruments oracle-integrity` — so the guard could
  not fire unless an operator ran the producer first. The promotion gate now
  runs the audit itself; see the close-out below.

The revive attempt itself died for an environmental reason, not a work reason:
the run recorded `placement: "remote"`, so the resumed `implementer` was shipped
to a node-agent that answered `HTTP 502: Bad Gateway` on
`/v1/sessions/…/files` three times, and the 6 downstream nodes were skipped as
"blocked by failed parent(s)". The stale worktree
(`mini-ork-worktrees/vt9b-oracle-consistency`, branch `wt/vt9b-oracle-consistency`)
holds 150 uncommitted lines over 4 files against a base 495 commits old; merging
that delta would revert newer work, so it stays untouched.

**Closed 2026-11-08 (`693dc18c`).** The producer is wired into the promotion path
in the shape this note suggested: `oracle_integrity.promotion_check(db_path)` runs
the hermetic audit (scripted runner and dispatch, ~0.06 s, no network, no model)
for the promotion it is scoring, persists the record, and quarantines on a failed
audit. ⑦ is no longer `unmeasured` by construction, and
`mini-ork instruments oracle-integrity` is now the out-of-band producer rather
than a prerequisite.

## Tier 3 — closed, no revive

### Already on main

All four reverse-apply cleanly against `bcc98e10` and fail to apply forward —
their work is in the tree and the `failed` row is stale bookkeeping.

| Run | Files | Proof |
|---|---:|---|
| `w2-sdd-ratify-cli-20261030-174813` | 2 | `git apply -R --check` OK |
| `rsi-build-rsi-i4-collapse-halt-20260929-122018` | 5 | `git apply -R --check` OK |
| `rsi-build-rsi-i4-collapse-halt-20260929-115155` | 2 | `git apply -R --check` OK |
| `rsi-build-rsi-i3-bsg-va-wiring-20260929-131925` | 2 | `git apply -R --check` OK |

### Deliverable exists — `rsi-review-20260929-084403` ($55.11)

An `rsi-technique-review` run that stopped on a cost pause with 20 failed nodes,
but only *after* writing its deliverable: `impact-report.md` (36 KB) plus
`clusters.json`, `groups/`, `group-techniques/`, and `extractions/`. The report
distils 131 techniques and names a build order; it is the survey that produced
the technique-adoption map. Re-running would spend another ~$55 to recompute a
document that already exists and has already been consumed. **Decision: no
revive** — the artifact is the output, the failed nodes were post-processing.

## Ledger caveat

Revived work leaves its `task_runs` row at `status='failed'`. There is no
sanctioned command that corrects a run's status after the fact, so this document
— not the DB — is the record of which failed rows are dead, revived, or landed.
Any future "collect failed runs" pass should read this list first, and re-run the
landed-ness oracle before spending on anything that looks salvageable.
