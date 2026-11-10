# Task status audit — every task emitted 2026-10-07 → 2026-11-04

**Question.** ContextNest holds four weeks of `tasks[]` records that agents wrote in their
`<z-insight>` blocks. Which of them are actually still open?

**Answer.** Of 851 tasks whose last self-report was `pending` / `in_progress` / `failed` /
`blocked`, **554 (65%) had already shipped**, **61 (7%) are superseded**, **118 (14%) left no
trace either way**, and **118 (14%) are genuinely still open**. Recounted 2026-11-10: 567
shipped and 105 still open — thirteen rows closed in the week after the audit (see
"Result").

The failure mode is not false completion claims. It is **stale re-emission**: a session
finishes work, stops mentioning the task, and ContextNest keeps the last thing it said.

## Method

1. `/api/v1/inbox` was unusable — it sorts newest-first and hard-caps at
   `INBOX_MAX_LIMIT = 500` (`src/api/inbox.rs:57`), which reached back only to 2026-11-01.
2. Harvested `kind=todo` records straight from the substrate WAL
   (`contextnest-data/wal.jsonl`, 1 GB) — **71,790** raw emissions, deduped by `task_id`
   keeping the latest status → **4,894** distinct tasks.
3. Scoped to this repo and the Zed fork: **2,004** tasks (1,168 mini-ork · 195 worktrees ·
   641 zed fork), of which **851** were still open by self-report.
4. Split the 851 into 8 themed groups and had one subagent audit each, against `git log`
   (`--since=2026-10-07` — 1,136 commits in mini-ork alone), branch ancestry
   (`git merge-base --is-ancestor`), live worktrees, and file:line probes.

Every verdict carries a receipt. The vocabulary is `DONE` (commit / file / branch proves it),
`OPEN` (the defect or absence is visible now), `STALE` (a later decision superseded it), and
`UNKNOWN` (searched, found nothing either way). "Probably done" is `UNKNOWN`, never `DONE`.

## Result

| group | rows | DONE | OPEN | STALE | UNKNOWN |
|---|---:|---:|---:|---:|---:|
| A git mechanics (mini-ork + worktrees) | 126 | 93 | 3 | 5 | 25 |
| B git + run triage (zed fork) | 121 | 75 | 16 | 27 | 3 |
| C run triage (mini-ork) | 106 | 82 | 3 | 6 | 15 |
| D lane / framework-edit | 97 | 55 | 16 | 2 | 24 |
| E engine core + RSI | 75 | 52 | 12 | 7 | 4 |
| F zed IDE frontend | 133 | 80 | 28 | 10 | 15 |
| G1 IDE surface, 10-14→11-04 | 96 | 60 | 16 | 3 | 17 |
| G2 IDE surface, 10-07→10-13 | 97 | 70 | 11 | 1 | 15 |
| **total** | **851** | **567** | **105** | **61** | **118** |

Recounted 2026-11-10 by tallying the verdict column of every row in the eight
sibling files rather than trusting the totals above them: **851 rows, 567 DONE,
105 OPEN, 61 STALE, 118 UNKNOWN**. Because the four verdicts partition the same
851 rows, OPEN falling 118 → 105 means exactly thirteen rows moved OPEN → DONE
and nothing else moved; STALE and UNKNOWN are unchanged, and no row was added or
dropped. Which thirteen is a per-group question — each group file carries a
"Closed since the audit" section naming its own, each with the commit or decision
record that closed it.

Per-row evidence for all 851 lives in the sibling files (`A-git-mo.md` … `G2-ide-mo-old.md`).
`tests/unit/test_audit_totals_py.py` re-derives this table from those files — per group and in
total — and fails when they disagree, so the numbers cannot drift again silently.

## The open tasks (118 at audit time, 105 as of 2026-11-10)

The raw count overstates the work: agents found several rows collapsing onto one item.

### One decision blocks a whole epic

- **SDD chain** (4 rows: `sdd-epic-run`, `sdd-epic-launch`, `sdd-engine-run`,
  `g4-author-fences`; `b2-preflight` closed 2026-11-10 as `mini-ork board
  preflight`) — the roadmap exists, I2 shipped, I4/I6/I7/I8/I9 never
  dispatched, `DECISIONS.md` marks them MISSING, and SESSION-GOALS shows four unanswered
  go/no-go requests. Related: `sdd-missing-epics`, `sdd` (ratification gate),
  `sdd-ide-page`, `sdd-roadmap`, `sdd-roadmap-continue`.
  **Next: one go/no-go from the operator unblocks ~9 rows.**

### One boolean blocks another

- `store.rs:709` still passes `directories: false`. That single flag blocks
  `f1-dir-picker`, `sdd-crate-run`, and the pick-files epic three sessions have logged.
- ~~`native_gates.py:215` returns `"pass"` for `indeterminate` instead of `"defer"`~~ —
  **DONE `a5001821`**. It was a **seeded** weakness: the positive control for the
  hackability audit, so the flip had to ship with a replacement. Neither named sibling
  (`oracle-liveness` / `oracle-stability`) could take the job — both measure 0.0 — so the
  control moved into the harness (`hackability.control()`: a gate that always passes must
  score 1.0, run by every `gate-fuzz --hackability`).

### Built but never wired

- ~~`v4-dead-detector`~~ — **DONE** by the RSI loop itself: `d97c9ee1` (on
  `origin/main`) calls `blame_attributor.attribute()` from the triage funnel
  (`mini_ork/triage/failures.py:388-416`, "this funnel is the production writer of
  `defect_attributions`"). The V4 rule still reports a candidate, but its detail now
  reads "the call never fires on a real failure" — a live-data condition, not the
  unwired writer the audit row described.
- ~~`cost-sidecar-gap`~~ — **DONE `e0a02954`**: the audit's premise was half wrong — panel lanes do
  pass the pre-dispatch `.cost-pause` gate (`execute_handlers.py:866-887`) and every lane's node
  goes through `charge_node_cost`. What under-enforced was the amount: `.last-llm-cost` is
  rewritten per LLM call, so a multi-lane node moved the meter by its last lane while the run
  spent the sum. `cost_pause.reconcile()` now raises the meter to `cost_ledger.run_cost`, `max()`-guarded.
- `apply-migration` — Attestia's control plane is written but `0100_control_plane.sql` and
  `0150_platform_rls_coverage.sql` were never applied.
- `benchmark-results-no-producer` — `record_benchmark_delta` (the L9 patch, `785963d0`)
  averages `benchmark_results` joined to `benchmark_tasks`, and the only writers of those
  two tables — `benchmark_suite.add()` / `.run()` — are called by nothing anywhere in the
  tree. The writeback (`785963d0`) is unmerged *and* computes nothing: its AVG is NULL, its
  UPDATE matches no row. Declined twice (`docs/audits/2026-11-06-revive-batch.md` and the
  2026-11-08 pre-push review); L9 now refuses that hollow closure (`8a6cc25e`) by
  requiring the delta writer **and** both benchmark producers to be wired.
- ~~`on-event-hook` / `bif-e1`~~ — **DONE**: `d322c5ab`/`d10a34a2` wired
  `mo_emit_hook` into `observability/node_events.py:452`, `orchestration/recursive.py:170` and
  the reaper's `run_finished` (`run_reaper.py:296`), guarded by `tests/unit/test_event_hook_wired.py`.
- ~~`failure-kind-sweep` (= `p0-4d-emitter-sweep` = `emitter-sweep-29`)~~ — **done
  2026-11-08** (`04980796`, then `d2902b6a` after the pre-push review): nine shell-out
  verifiers declare `failure_kind=toolchain` through the new
  `mini_ork.verify.pm.run_logged`, which returns the spawn's output so the abstention
  can be decided on it rather than on rc alone. The review caught the first cut
  classifying suite logs with the broad `classify_spawn` — a failing test that prints
  `No such file or directory` would have been excused — so every suite verifier now
  uses `classify_suite_spawn` (launcher messages only, first 20 lines), and a held-out
  parametrized probe's case id is masked in the evidence log. See D's row.

### Built, needs a live run rather than code

- `modal-live-verify` / `vercel-sandbox-provider` / `k8s-agent-sandbox-provider` — Modal has
  11 fake-SDK tests and no live token run; Vercel and Kubernetes are research only.
- `rls-tenant-gate` — `rlsIsolation.test.ts:25` skips without `DATABASE_URL`; the skip
  should fail closed.
- `ctx-lift`, `sdd-va-ac4` — both need n ≥ 30 measured, not more code.
- Roughly a dozen GUI rows across F and B ("verify", "smoke", "screenshot") where the
  capability shipped and nobody recorded looking at the screen.

### Unmerged work sitting in branches

- ~~`verifiers-pin-ceiling`~~ — **DONE**: it landed as `d16aa83d`; the dangling `f0d764db`
  is a byte-identical duplicate (`git diff` over its five files is empty). The "awaiting
  operator TTY approval" note was about the duplicate, not about work.
- ~~`t1-certificate` / `rsi-held`~~ — **DONE `56c6dacf` + `79c24ff7` + `4dbf8dbd` + `a9c7b818`** (certificate + alpha ledger; migration 0089/0090; arm-time pre-flight and the evidence-overwrite fix).
- ~~`rrl-docs`~~ — **DONE 2026-11-08**: `92681b27` + `dae784f8` + `a8d14b49` on `main`.
- ~~`t1-kernel-hole-3`~~ — **DONE `bcc98e10` + `1c018305`** (freeze + the 2026-10-29 review's three fixes).
- ~~`t1-collapse-floor-breaker`~~ — **DONE `10b73fd6` + `b232eae3`** (collapse floor ships in shadow, `task_class`-keyed latch).
- ~~`rsi-cycle-publish`~~ — **DONE `12e85c3f` + `3dd76201`**: the `audit_live_corpus` backfill pass plus its four tests; `77517b50` was superseded by main's `398889bc`. Nothing left unmerged from the RSI cycle.

### Live defects

- `kimi-lane-401` — the key in `config/secrets.local.sh:5` is rejected; 83 failed rows, latest
  2026-11-04.
- `researcher-install-update` / `sync-researcher-engine` — researcher's engine is 780 commits
  behind `origin/main`.
- ~~`thread-runs-node-detail`~~ — **DONE `0b7461c0`**: the runs table carries the open node of each
  moving attempt (one batched query via `runs._live_nodes_bulk`) and every row opens its run for
  the DAG. The "`thread_runs` is empty for every thread" half was the CLI smoke fixtures in this
  home's DB — `threads.service.send` and the agent-server mirror both link the run after launch.
- ~~`reap-run-filter`~~ — **DONE `5075b919`**: `mini-ork reap --run <id>` (repeatable, deduped) scopes the sweep; no `--run` keeps the whole pass.
- ~~`flow-rubric-negative` / `flow-legend-clip`~~ — **DONE `mini-ork-ui` `2480d71`**,
  pinned by the fork's `6a34cb59`: `score_of` hides any negative rubric score at the
  parse boundary (the `-1` is the grader-parse-error sentinel, never a result), and the
  legend is now a wrapping strip above the map viewport instead of an overlay that
  painted over the last column.
- ~~`classified-state-working`~~ — **DONE `788c4722`**: rule 6 asks
  `run_reaper.probe` before answering "working", so a live status whose `.pid` names only dead
  processes projects as failed with "/recover restarts it, /stop closes it"; `run_mark` mirrors
  it so the tile count agrees with the row. ~~`wave3-durable`~~ — **DONE `fe32deca`**:
  `mini-ork recover <run> --take-lease` reclaims a run a dead worker still holds, fencing the
  displaced token out (`stores.lease.take_lease`); the 15-minute TTL no longer blocks a run.
- ~~`probe-cache`~~ — **DONE `f1ceb40a`** (found by the pre-push reviews of the two commits
  above): asking `run_reaper.probe` per rendered row meant one blocking `ps` fork per row, so a
  200-row board paid 200 forks. `_process()` now memoizes `(stat, lstart)` for 2 s with a
  512-entry cap (`MO_PROBE_CACHE_TTL_S`, `0` disables) and `_pid_alive` still settles death
  with `kill(pid, 0)` before any process table is read.
- `repo-ops` — the fork's `main` has no branch protection (`gh api … /protection` → 404).

### Housekeeping

`docker-image-prune` (7 × 1.83 GB e2e images), `disk-root-owned-caches`.
~~`MEMORY.md` at 17.7 KB vs a 17.1 KB target~~ — **done 2026-11-09**: seven settled themes
moved verbatim into `MEMORY-ARCHIVE.md`, index now 17,051 bytes.

~~`$CONTEXT_FILE` in 6 recipe prompts~~ — **DONE** `0ca1b9d6`: every prompt names the
artifact its own node id selects (`${MINI_ORK_RUN_DIR}/lens-<lens>.md`), and the four
lens prompts read the scanner's real output instead of the retired `bottleneck-scan.md`.
`tests/unit/test_recipe_prompt_artifact_refs_py.py` keeps it that way.

~~`config/rsi-cycle.yaml` never committed~~ — **STALE, not open**: the operator copy is
gitignored by design (`.gitignore:35-39`, "per-machine truth… one `git add -A` away from being
committed by accident"), and `config/rsi-cycle.example.yaml` *is* committed and is what the
header tells you to copy from.

~~`add-gitleaks-gate`~~ — **done**: the `secret-scan` job exists in `.github/workflows/ci.yml`,
is a `needs:` dependency of `ci result` *and* in that job's `expected` set. 2026-11-08 closed the
half that was still wrong: the fixture allowlist was scoped on `paths = ['^tests/.*']` as well as
on the secret's shape, and gitleaks allows a finding when *any* condition matches, so the path
alone exempted every credential under `tests/`.

### Needs a human decision, not code

`abstention-fold`, `verify-boundary`, `design-parity-decision`, `acp-trio-decision`,
`outcome-web-ui-btn`, `enable-v2-injection` (still `shadow`), `engine-node-runtime`,
`sa1-scope-question`, `codex-config-model`, `s3-policy`, `act-b-e`, `lf-wave2`,
`contextnest-boot-decision`.

## Cross-cutting findings

1. **Stale emission, not dishonesty.** 65% of "open" work had landed on audit day,
   67% a week later. A session that
   finishes a task usually stops naming it, so its last record freezes at `in_progress`.
   A todo queue fed by such blocks decays toward optimism-in-reverse: it accumulates tasks
   that are already done.
2. **Task ids multiply; work items do not.** 6 sandbox rows → 3 providers, 3 emitter rows →
   1 sweep, 4 SDD rows → 1 chain (a fifth, `b2-preflight`, closed 2026-11-10 as
   `mini-ork board preflight`). Deduping by `task_id` counts emissions, not work.
3. **A rewritten file hides an unfixed defect.** G2 first read the `flow.rs` diff as proof the
   rubric-score defect was fixed; the `score` branch still rendered `-1` until 2026-11-09
   (`mini-ork-ui` `2480d71`). Verify the branch, not the diff.
4. **`UNKNOWN` (118 rows) is honest, and concentrated.** It is user-facing observation,
   external credentials, operator acts, or subjects outside the three audited repos. Guessing
   those `DONE` would have inflated the pass rate by ~14 points.

## Correction log

Verification was not taken on trust. Each group's OPEN claims were re-probed; these held
exactly: the three unmerged shas, `wt/…-23b811cd` one commit ahead, untracked
`config/rsi-cycle.yaml`, no stash guard in `.githooks/`, 7 e2e images, `$CONTEXT_FILE` in 6
recipes (since fixed: `0ca1b9d6`), zero `partition_key` readers, `native_gates.py` `indeterminate → pass`, the
`run_reaper` flag set, `cost_pause.check`'s single callsite, zero node refs in `thread.py`,
`store.rs` `directories: false`, `MO_CONTEXT_V2` shadow default, absent `issues` page,
`flow.rs`'s `score` branch, `MEMORY.md` size, absent `mini_ork/cost/`, and the fork's 404
branch protection.

Two did not:

- **D's `failure_kind` count** — reported 4 verifier files; actual is 1 under
  `recipes/framework-edit/verifiers/` (8 across all Python, mostly core). The conclusion
  (sweep unbuilt) survives; do not quote the number.
- **B's `run-page-last-node` verdict** — called OPEN. It is **DONE**:
  `crates/mini_ork_ui/src/page.rs:2812-2819` computes
  `focus = model.last_ran().map(...)` and passes it to `MiniOrkGraph::new(model, focus, …)`
  with the comment "Open on the node the run reached last". Introduced by `bbb00e1`,
  dated 2026-10-11 — the same day the task was emitted. B read `open_run()`'s signature and
  concluded no node was accepted, without following the construction path that computes it.

Both errors are the same species: **a verdict drawn from a signature or a count rather than
from the behaviour**. The table above already counts B's row as DONE, so the totals reflect
both corrections.

### Post-audit re-probe, 2026-11-09

One of the "held exactly" claims has since changed — it was true on audit day, not now:

- **`absent mini_ork/cost/`** — still absent at audit time, but the package now exists:
  `mini_ork/cost/budget.py` (per-run USD ceiling, `MO_RUN_BUDGET_USD`, plugin-selectable
  policy) landed in `fc1323b2`, with the printed unblock made real in `d9455494` and the
  fail-closed edges in `f023005a` / `287e9dbe` / `d779e3d6` / `c331387c` (plugin discovery
  moved to the `mini_ork.cli` import so native-subcommand children see the policy; the HTTP
  resume accepts a marker-only park and carries the refusal reason). `G2-ide-mo-old.md`'s
  `cost-policy` row is therefore **DONE**, not OPEN.
- **`p2-16` (per-run scope disclosure + cross-run view)** — open on audit day, closed by
  `e9bcbf63`: the run page carries a Scope section (declared root, allow-list, deny lines,
  worktree branch — explicitly *not* a write sandbox) and the runs page a `Scope & claims`
  tab flagging a path two live runs both declare. `C-runs-mo.md`'s row is **DONE**.
- **`native_gates.py` `indeterminate → pass`** — open on audit day, closed by `a5001821`
  once the hackability positive control moved from the seeded weakness into the harness
  (`hackability.control()`). `D-lane-framework.md`'s row is **DONE**.
- **The `partition_key` finding** — the probe above ("zero `partition_key` readers") was
  right, and the conclusion drawn from it was wrong. It was not a missing feature to wire
  up: `dispatch_mode: partitioned` groups nodes by *type*, and the sanctioned data-driven
  fan-out is `mini_ork.orchestration.item_fanout` (a transform writes the work list; an
  implementer submode runs one item per worker). So the field is now *refused* —
  `mini_ork/workflow/compiler.py:272` raises `WorkflowCompileError` naming `item_fanout`,
  the property is gone from `schemas/workflow.schema.json`, and the four partitioned nodes
  in `recipes/bdd-first-delivery/workflow.yaml` no longer declare it.
  `E-engine-rsi.md`'s `sweep-node` row is **DONE**.

### Found while closing, 2026-11-10

The evidence-worktree disposition (`docs/decisions/20261110-evidence-worktree-disposition.md`)
was written to delete moot trees, and its probe found the opposite of a moot tree:

- **`w2-review-resume-from-waiting` held a real unmerged fix** (`b1afcaa2`, 2026-10-31),
  merged as **`1317e87f`**: approving a parked node spawned `recover --strategy resume`
  with no entry point, and because verifier nodes write no `node_checkpoints` the planner
  restarted at `specdir_ingest` and re-dispatched the node the operator had just
  approved — a $4.29 loop in the live SDD smoke that produced it. The node-approve path
  now resumes `--from-node <waiting_node>` from `.review-pause`.
- **Its pre-push panel found three defects in that fix, fixed in `58f2d119`**: the
  sentinel read caught only `OSError` while `UnicodeDecodeError` is a `ValueError` (so a
  stray byte crashed a path whose docstring promises it never raises); the sentinel's
  `waiting_node` went straight into `recover --from-node <value>` unvalidated; and the
  "resuming without an entry point" warning printed under `--no-resume` and while other
  inbox rows were still pending. Tests: 17 in `test_approvals_resume_waiting_node_py.py`.

Neither row existed in this audit. They are recorded here rather than added to A's table,
because A's 126 rows are the tasks the harvest *emitted*: a task found in a parked worktree
was never emitted, so a row for it would answer a different question from every other row in
the table.

## Caveat on the group tables

Several agents counted their own output rows in `verification[]` rather than citing the
repo probe. Their substantive citations are in the tables and were sampled and reproduced
above, but treat a row as established by its `evidence` column, not by any agent's
self-reported count.

## Grep hygiene — this directory is now part of the repo

These reports now sit under `docs/audits/`, so any future evidence search inside mini-ork
will match them. Exclude the directory when auditing:

```bash
rg -n '<symbol>' /Volumes/docker-ssd/ps/mini-ork --glob '!docs/audits/2026-11-04-task-status-audit/**'
```

A hit here is the audit talking about the code, not the code itself.
