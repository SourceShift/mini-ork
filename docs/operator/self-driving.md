# Self-driving supervisor

`mini-ork supervise` is the sweep that closes the loop the self-driving recovery
path leaves open. When an engine fault exhausts its repair attempts, the loop
parks the run on a `framework-edit` fix epic (`runs/<id>/repair.json` with
`state="waiting_on_fix"` and `fix_epic_id`) — but a human still had to revive the
run after the fix merged, the diagnosis ledger stayed unlabelled, and a
`repair.json` left in `repairing` by a dead process was never cleared.

The supervisor is **not** an approval gate. It only acts on facts already in the
tree, the DB and `origin/main`; when it needs a human it enqueues a *notice* in
the oversight inbox (`mo_inbox_gates`, gate `self-driving-supervisor`) and keeps
going. Nothing waits on that notice.

## What one sweep does (R1–R8)

**R1 — revive.** For every `runs/*/repair.json` with `state="waiting_on_fix"`
and a `fix_epic_id`, read the epic's status:

- `done`, and the fix has **landed** on `origin/main` → revive. "Landed" means
  either the patch id of the epic's last run diff appears in the commits on
  `origin/main`, or a commit on `origin/main` mentions the epic id in its
  message. The run is re-dispatched from the node that failed
  (`attempts[-1].from_node`, else `attempts[-1].diagnosis.node`, else
  `diagnosis.json`'s `node`), with `repair.json` moved to `state="reviving"` and
  the engine SHA and child pid recorded. At most `max_revives` revives happen per
  sweep.
- `done` with **no** landing signal → the run stays parked (`wait`); a fix that
  merged somewhere else has not reached the tree this engine runs.
- `failed` / `escalated` / `abandoned` → `state="gave_up"` with a note naming
  the epic, plus one oversight notice (`fix epic failed: <run_id>`). The notice
  is enqueued once — a row for the same gate and feature in *any* status
  suppresses a duplicate.

**R2 — label.** For every `repair.json` with `state="reviving"`: once the run is
terminal (published / failed / rolled_back / not_needed) and `task_runs.updated_at`
is after `revived_at`, label the `diagnosis_ledger` rows that carried this
`fix_epic_id`. The diagnosis was **right** when the run published or its
`node_checkpoints` row for the revived node says `success`; **wrong** when the
run failed again and that node did not succeed. State becomes `revived` (right)
or `revive_failed` (wrong). A run still executing is left alone.

**R3 — clear orphans.** A `repair.json` in `state="repairing"` whose last
attempt is older than `orphan_after_s` and whose owning process is gone (the
reaper's pid probe says `dead`) is moved to `state="gave_up"` with a note. With
no `.pid`, or a live owner, or a young attempt, it is left alone.

**R4 — supersede.** A failed run whose work was delivered some other way is
moved to the terminal status `superseded` — terminal, but *not* unsuccessful,
so it leaves the failed bucket and the repair sweeps (it buckets with
`not_needed`). The sweep scans the 500 most recent `task_runs` rows with
`status='failed'` and skips any run whose `repair.json` state is
`waiting_on_fix`, `reviving` or `repairing` (a loop owns it). A run is
superseded when any of these holds:

- a **later** run (greater `created_at`) with the same `kickoff_path` is
  `published` — an earlier published run does not count;
- the patch id of the run's own `framework-edit.diff` is in the commits on
  `origin/main` (the same `git log -p` map R1 uses, computed at most once per
  sweep);
- the run appears in `epic_attempts` for an epic whose status is `done`.

The update is a compare-and-set on `status='failed'` and the note gains
` superseded: <reason>`, naming the signal (`later run <id> published`,
`diff landed on origin/main`, `epic <id> done`). A row that moved on is left
alone, so a second sweep never appends the note twice.

**R5 — back-link.** For every epic with `status='done'` whose `last_run_id` run
is not `published` and whose notes do not already contain `delivered_by=`, the
sweep records what delivered it. The evidence is the latest `published` run
among the epic's `epic_attempts`, else the first commit on `origin/main` whose
message contains the epic id (`git log origin/main --format=%H -F
--grep=<id>`). The notes gain ` delivered_by=<run_id>` or
` delivered_by=commit:<sha>`; with no evidence the epic is left alone, and the
marker guard means a second sweep adds nothing.

**R6 — apply a proposed patch.** The per-run orchestrator proposes typed graph
patches on `MUTATE` (`runs/<id>/graph-patches/NNN.yaml`); R6 makes them act
without an operator running `mini-ork graph-patch apply`. For every run dir
with a `graph-patches/` directory whose `task_runs.status` is `failed` and
whose `repair.json` state is absent, `gave_up` or `revive_failed`:

- skip when one of the run's applied patches has no `NNN.outcome.json` yet —
  the revive it started is still in flight;
- skip once the run has `MO_SUPERVISE_MAX_PATCHES_PER_RUN` applied patches
  (default 2; the env var may only tighten that, a larger value is clamped
  back to 2);
- take the lowest-numbered patch still in status `proposed`. A `replan` patch
  is skipped unless `graph_patch.REPLAN_SUPPORTED` is true *at sweep time* —
  SDE-6a flips that flag once a replanned chain is safe to apply;
- control arm: `graph_patch.credit(home)` is computed once per sweep. When the
  picked kind is not `rerun`, both it and `rerun` have at least five outcomes,
  and the kind is not `credited` (strictly better than a blind rerun), the
  patch is skipped with `<kind> is not better than a blind rerun (<rate> vs
  <rerun rate>)`. Fewer than five samples on either side is exploration: apply;
- apply it with `graph_patch.apply`, sharing R1's `max_revives` budget. The
  run's `repair.json` becomes `reviving` with `revive_patch: "NNN"` and the
  revive starts from the patch's node. A patch that cannot materialize
  produces an `error` action for that run; the sweep continues.

**R7 — close a split.** The last rung of the repair ladder hands a failed run's
remaining work to child epics instead of reviving it. `graph_patch.apply` on a
`split` patch creates one `epics` row per child (`not started`, the parent's
recipe, a `<run_id>/graph-patches/NNN-split/<epic_id>.md` kickoff holding the
parent kickoff plus that child's scope) and moves the parent's `repair.json` to
`state="waiting_on_children"` with `child_epic_ids` and `split_patch: "NNN"`.
R7 closes that loop for every `repair.json` in `waiting_on_children`:

- every child epic `done` → compare-and-set the parent's `task_runs.status` from
  `failed` to `superseded` with a ` superseded: delivered by split children
  <ids>` note, set `repair.json` to `state="split_done"`, and write
  `graph-patches/NNN.outcome.json` `{"passed": true, ...}` (write-once). Action
  `split_done`.
- any child `escalated` or `abandoned` → `repair.json` `state="gave_up"` with a
  note naming the child, an outcome `{"passed": false, ...}`, and one oversight
  notice (`split child failed: <run_id>`) that is skipped when the same gate and
  feature already exist in any status. Action `split_failed`.
- anything else (children still running, blocked, or missing) → no action; the
  parent stays parked. A parent that is no longer `failed` is never touched.

The outcome file belongs to the split patch (`NNN.outcome.json`), so
`mini-ork graph-patch credit` scores the `split` kind alongside the others.
`dry_run` reports the same `split_done`/`split_failed` actions and writes
nothing.

**R8 — dispatch ready epics (opt-in).** Off by default, because a dispatch
starts a paid run. When `dispatch_epics.enabled` is true, each sweep **first
reclaims stale `in progress` epics** with the scheduler's own rules
(`reclaim_stale`), so an epic wedged by a crashed scheduler cannot hold the cap
forever; it then picks the highest-priority ready epic from
`scheduler.pick_ready` and spawns a detached
`mini-ork scheduler --once --epic <id>`; stdout and stderr go to
`<home>/state/supervisor-dispatch-<epic_id>.log`. It starts at most one epic
per sweep and pauses entirely once `max_in_progress` epics (default 1) are
still `in progress`. `dispatch_epics.only` is an optional allow-list of epic
ids; an empty list allows any ready epic. The scheduler still enforces the
per-epic budget and `MO_DAILY_BUDGET_USD` — R8 adds no budget logic of its own.

`dispatch_epics.target_root` is **required to enable R8**. A dispatched run
edits its working tree — and a framework-edit run can stash and roll it back —
so the target must be a dedicated, clean checkout or worktree, for example one
made with `make worktree SLUG=dispatch`, and never the engine checkout, a
directory inside it, or a parent of it. R8 refuses an unsafe target
(`dispatch_refused`) and spawns nothing. Dispatched runs commit in the target
tree; landing them on `main` is the operator's
`make worktree-merge SLUG=dispatch`. The run child gets
`MO_TARGET_CWD=<target_root>`, `MO_ALLOW_FRAMEWORK_CWD=1` — safe only because
R8 proved the target is disjoint from the engine — and `MO_PLACEMENT=local`
unless the operator's environment already sets a non-empty placement.

Turn it on by setting `dispatch_epics.enabled: true` and
`dispatch_epics.target_root: <path>` in the config file below; the simple
(`--once`) and service (`--interval`) tiers read the same keys.

`dry_run` computes the same action list without materializing the workflow:
it installs no `NNN.applied.json`, spawns nothing, and leaves `repair.json`
untouched.

**Outcome files.** When R2 labels a revived run whose `repair.json` carries
`revive_patch: "NNN"`, it also writes
`runs/<id>/graph-patches/NNN.outcome.json`:

```json
{"passed": true, "status": "published", "labelled_at": 1730000000}
```

`passed` is the same correct/incorrect judgement R2 computed (a failed run
whose revived node checkpointed `success` still counts as passed), and
`status` is the run's terminal `task_runs.status`. The file is **write-once**:
an existing outcome is never overwritten, so an operator or an earlier sweep
owns the first record. `dry_run` writes nothing (the label action still gains
`"patch": "NNN"`).

`mini-ork graph-patch credit` reads those outcome files — grouped by the patch
kind in each `NNN.yaml` — and prints, per kind, `n`, `passed`, `rate` and
`credited`. `rerun` is the control arm; another kind is credited only when
both it and `rerun` have at least five outcomes and its pass rate is strictly
higher. R6's control-arm gate is the same calculation, so the blind-retry
control arm decides which patch kinds keep getting applied.

Every `repair.json` rewrite is atomic (temp file in the same directory, then
`os.replace`) and preserves every key it does not change. One corrupt run dir
produces an `error` action for that run and never stops the sweep.

## Running it

### Simple tier (laptop, cron/launchd)

No daemon, no server. One sweep per invocation against the local SQLite home:

```bash
mini-ork supervise --once
mini-ork supervise --once --json          # machine-readable action list
mini-ork supervise --once --dry-run       # same actions, writes/spawns nothing
```

Schedule it, for example, every five minutes:

```cron
*/5 * * * * cd /path/to/project && mini-ork supervise --once
```

The sweep holds the `__supervisor__` lease in the local `state.db`, so an
overlapping cron run prints `another supervisor holds the lease` and exits 0
instead of double-acting.

### Service tier (shared DB, long-running)

```bash
mini-ork supervise --interval 300
```

The process sweeps, refreshes its lease (TTL = 3 × interval, minimum 900 s) and
sleeps. If another supervisor takes the lease, this one stands down. The
operator is notified through the oversight inbox; a pending notice never blocks
a sweep. `--dry-run` and `--json` work here too.

### Configuration

Pass `--config PATH`, or set `MO_SUPERVISE_CONFIG`, to a YAML file like
[`config/self-driving.example.yaml`](../../config/self-driving.example.yaml):

```yaml
interval_s: 300      # seconds between sweeps when looping
max_revives: 2       # revives per sweep
orphan_after_s: 3600 # repairing this old + dead owner = orphaned
dispatch_epics:      # R8 — opt-in; off because it starts paid runs
  enabled: false
  max_in_progress: 1
  only: []
  target_root: ""    # required to enable R8: a dedicated clean worktree
```

With no config named, the same values are the built-in defaults. A path that is
*named* but missing or unparseable exits 2 with an error naming the path — the
supervisor never silently falls back to defaults.

## Reading the actions

Each action is a small dict: `{"run_id", "action", "reason", ...}`.

| action | meaning |
| --- | --- |
| `revive` | the fix landed; the run was re-dispatched (carries `from_node`) |
| `wait` | the epic is not done, not landed, missing, or the revive budget ran out |
| `skip` | landed, but no failed node was recorded — nothing to resume from |
| `gave_up` | a dead fix epic, or an orphaned `repairing` state, was closed |
| `label` | a revived run's diagnosis was labelled (`correct`: true/false; `patch` when a graph patch revived it) |
| `apply_patch` | a proposed graph patch was applied (`patch`, `kind`); R6 |
| `supersede` | a failed run's work was delivered elsewhere; it is now `superseded` |
| `split_done` | every split child is `done`; the parent was superseded (`patch`, `child_epic_ids`) |
| `split_failed` | a split child `escalated`/`abandoned`; the parent gave up and the operator was notified (`patch`, `child_epic_ids`) |
| `backlink` | a done epic's notes gained `delivered_by=<run|commit>` |
| `dispatch_epic` | R8 spawned one ready epic through the scheduler (`pid`, `log`); dry-run reports it with `dry_run` |
| `dispatch_refused` | R8 refused before spawning because `target_root` is unsafe; `reason` names the fix (required, must be absolute, does not exist, inside the engine checkout, contains the engine checkout). A refusal exits 0, so fix `dispatch_epics.target_root` in the config |
| `reclaim_stale` | R8 returned wedged `in progress` epics to the queue before counting the cap (`epic_ids`) |
| `error` | that run dir could not be processed, or a reclaim failed (`reclaim: ...`); the sweep continued |

Without `--json` the output is one line per action; with `--json` it is the full
list, which is what an automation should consume.
