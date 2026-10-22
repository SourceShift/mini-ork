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

## What one sweep does (R1–R5)

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
| `label` | a revived run's diagnosis was labelled (`correct`: true/false) |
| `supersede` | a failed run's work was delivered elsewhere; it is now `superseded` |
| `backlink` | a done epic's notes gained `delivered_by=<run|commit>` |
| `error` | that run dir could not be processed; the sweep continued |

Without `--json` the output is one line per action; with `--json` it is the full
list, which is what an automation should consume.
