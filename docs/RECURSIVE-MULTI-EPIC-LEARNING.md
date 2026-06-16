# Recursive Multi-Epic Learning Loop

This document describes the autonomous multi-epic delivery loop that sits on
top of `bin/mini-ork-self-improve`. Where the single-epic loop iterates on
one worktree until the inner recipe is happy, this loop walks a directed
acyclic graph of *epics*, dispatches each via the `epic-runner` recipe (or a
recipe of your choice), and threads four learning channels back into the
queue so that later epics inherit lessons from earlier ones.

It is a strict superset of `docs/RECURSIVE-SELF-IMPROVE.md`: the inner per-
epic loop is identical, the additions are the cross-epic scheduler, the
cross-epic gradient transfer, the per-agent bug-report channel, and the
arxiv-driven roadmap ingest.

## Quickstart

```bash
# 1. Write a roadmap markdown (one ## heading per epic + dep lines).
$EDITOR kickoffs/roadmap-mine.md

# 2. Ingest the roadmap into the epics + epic_dependencies tables.
bin/mini-ork epics ingest kickoffs/roadmap-mine.md

# 3. Auto-emit per-epic kickoff files (otherwise the scheduler falls back
#    to recipes/<recipe>/example-kickoff.md, which is wrong for your work).
bin/mini-ork epics split kickoffs/roadmap-mine.md

# 4. Inspect what's ready to dispatch.
bin/mini-ork epics list
bin/mini-ork epics ready

# 5. Fire the scheduler. --once exits after the queue drains; without
#    --once it idles 60s between empty-queue polls.
MO_OPEN_PR=1 \
GH_TOKEN=$(gh auth token) \
MO_AUTO_MERGE=1 \
MO_PR_SOAK_HOURS=24 \
MO_BUG_COLLECTOR=1 \
MO_BUG_REPORT_AUTO_PROMOTE=3 \
bin/mini-ork scheduler --once
```

Step 5 enables **everything**: real PR creation, soak + CI gated auto-merge,
per-node bug collection, and auto-promotion of the top-3 noticed bugs into
new epics on every reflect cycle. Drop the env vars one at a time to scope
the autonomy you want.

## Architecture

```
            roadmap.md
                |
                v
       mini-ork epics ingest        mini-ork epics split
                |                          |
                v                          v
      epics + epic_dependencies    kickoffs/auto/<epic_id>.md
                |                          |
                +--------------+-----------+
                               v
                  mini-ork scheduler  (loop)
                               |
                               v
              pick oldest READY epic from epics
                               |
                               v
              mini-ork run <recipe> <kickoff.md>
                               |
                               v
         classify -> plan -> execute -> verify -> reflect
                               |
                               v
                    panel-verdict.json
                               |
            +-----------+------+------+-----------+
            v           v             v           v
       gradient_     pattern_     bug_reports     learning_
       records       records      (per-agent)     record
            |           |             |           |
            +-----+-----+--------+----+-----+-----+
                  v              v          v
            context_assemble  promotion gate  epic_graph_on_done
            (next dispatch)   (workflow mut)  (cascade unblock)
                  |                |               |
                  v                v               v
            planner+nodes      workflow_memory   next epic ready
            see prior runs     candidate->prom.  in queue
                               | (or quarantine)
                               v
                       gh pr create / merge
```

### Components

| Component | Where | Job |
|---|---|---|
| `bin/mini-ork-epics`    | E6 | Ingest roadmap → epics + dep edges. Split roadmap → per-epic kickoffs. |
| `bin/mini-ork-scheduler`| E4 | Pick next ready epic, dispatch, mark done or escalated, cascade. |
| `lib/epic_graph.sh`     | E5 | `epic_graph_ready_now`, `epic_graph_on_done`, dep cascade. |
| `lib/pr-create.sh`      | E3 | `mo_open_pr` opens a real GitHub PR per epic when `MO_OPEN_PR=1`. |
| `lib/auto-merge-pr.sh`  | E8 | Polls CI + approval + soak; `gh pr merge --squash --auto`. |
| `lib/cross_epic_gradient.sh` | E7 | Promotes recurring TARGETS (across task_classes) into `__cross_class__` gradients. |
| `lib/bug_report.sh`     | bug-channel | `bug_report_emit / sweep / prioritize / promote`. |
| `bin/mini-ork-bug-collector` | auto-collector | Fires after every node trace; heuristic regex scan of agent output. |
| `lib/context_assembler.sh` | reader | Injects `prior_similar_runs[]` + `known_failure_modes[]` per task_class, ranking `__cross_class__` above per-class. |

## The four learning channels

Multi-epic learning compounds along four independent channels, each on its
own cadence. Skipping any one degrades the loop but does not break it.

### 1. Per-task-class memory (D1–D5)

After each successful node, `lib/memory.sh` writes a `task_memory` row keyed
by `(run_id, task_class, kickoff_hash, outcome, duration_ms, cost_usd)`.
Failures get a `failure_memory` row keyed by `(workflow_stage, failure_category,
error_message)`. On the next dispatch for the same `task_class`, the
planner's `context_assemble` reads up to 10 prior runs and up to 10
high-confidence failure modes and bakes them into the planner prompt as
*"Here is what happened the last 10 times you did this."*

This is the fastest loop: it fires every dispatch and the effect is visible
in the **next** dispatch of the same task class.

### 2. Cross-class gradient transfer (E7)

Per-task-class is too narrow when the same root cause hits multiple
recipes. `cross_epic_gradient_promote` (called at every reflect, default on
via `MO_CROSS_EPIC_GRADIENTS=1`) clusters `gradient_records` by **target**
(not signal text — signals are unique per-incident prose), and any target
that appears in ≥ N task_classes (default 2) with confidence ≥ 0.7 is
promoted to `task_class='__cross_class__'`. The assembler reads these
regardless of the requested task_class and ranks them above per-class
findings.

Concretely: a reviewer-prompt bug noticed during a `framework_edit` run
will surface as a known failure mode during a brand-new `docs` epic's
planner injection on its first run.

Cadence: every `mini-ork reflect`. Burn-in on existing data promoted 120
real cross-class lessons in this repo on first run.

### 3. Pattern emergence (D1 patch #5)

`lib/pattern_store.sh::pattern_store_mine_from_traces` clusters
`execution_traces` rows by `(task_class, status)` over a rolling 7-day
window. A cluster with ≥ 3 occurrences becomes a `pattern_records` row
typed as `verifier_addition` (when status ∈ failure, vacuous) or
`best_practice_rule` (when status = success). The promotion gate
(`lib/promotion_gate.sh`) reads these to score candidate workflow
mutations: a pattern that consistently succeeds is evidence the workflow
should be promoted; a pattern that consistently fails is evidence the
verifier needs an addition.

Cadence: every `mini-ork reflect` when `MO_PATTERN_MINER=1` (opt-in
because pattern mining contends with reflect's gradient extraction for
sqlite locks under heavy concurrency).

### 4. Bug-report channel (b2c832b + 25a93da)

The other three channels mine **traces** (machine-observable signals). The
bug-report channel mines **agent prose** — the side-issues an agent
notices while doing its main work but does not have scope to fix.

Two trigger styles:

- **Per-agent (auto)**: `bin/mini-ork-execute` calls
  `bin/mini-ork-bug-collector` after every node's `_trace_write_node_rich`.
  The collector (heuristic mode by default) regex-scans the agent's
  output for `noticed ... but`, `out of scope but`, `should fix`,
  `deferred`, `TODO/FIXME/HACK/XXX`, etc., and emits up to 5 JSONL rows
  per node to `${MINI_ORK_RUN_DIR}/noticed_bugs.jsonl`.
  Gated by `MO_BUG_COLLECTOR=1`.

- **Per-agent (explicit)**: agents that want richer reporting can source
  `lib/bug_report.sh` and call `bug_report_emit <role> <severity> <title>
  <description> <suggested_fix> <observed_in> <confidence>` directly,
  bypassing the heuristic.

At the next `mini-ork reflect`, `bug_report_sweep` walks every
`.mini-ork/runs/*/noticed_bugs.jsonl`, dedupes by SHA of the normalized
title, and upserts into the `bug_reports` table (max severity + max
confidence on dedupe, frequency++). When
`MO_BUG_REPORT_AUTO_PROMOTE=N`, the top-N ranked rows (by
`severity_weight × frequency × confidence`) are promoted to new epics
with auto-generated kickoff files under `kickoffs/auto/bug-<id>.md`. The
scheduler picks them up in the normal queue.

Operator can review without auto-promotion:

```bash
bin/mini-ork bugs sweep        # force sweep (reflect already does this)
bin/mini-ork bugs prioritize   # ranked view
bin/mini-ork bugs show <id>    # full detail
bin/mini-ork bugs promote --top 3   # manual promotion
```

## How the four channels compound

A concrete example walking through one epic's lifecycle and the effect on
the next:

1. **Run 1, epic `arx-1-vero-harness`** dispatches. The reviewer node
   notices the rubric verifier returns `pass=true` despite two FAIL items
   on critical labels. The reviewer is focused on its own verdict so it
   notes the observation but doesn't act.

2. **At node end**, `_trace_write_node_rich` writes the trace. With
   `MO_BUG_COLLECTOR=1`, the collector regex-scans the reviewer's
   output, picks up "noticed ... but" and "should fix" markers, and
   writes 2 JSONL rows.

3. **Inner recipe finishes**, scheduler reads `panel-verdict.json`, marks
   `arx-1-vero-harness` done, cascades to unblock `arx-2-grasp-skills` +
   `arx-4-skillcat-stages`.

4. **`mini-ork reflect` auto-fires** at run end. It:
   - extracts gradients from execution_traces (writes ~150 rows for this run)
   - mines patterns (`pattern_records`++ for the framework_edit success cluster)
   - promotes any new cross-class targets (the rubric bug may already be a
     known target since it surfaced earlier)
   - sweeps `noticed_bugs.jsonl` (dedupes the reviewer's 2 observations
     into existing `bug_reports` rows, bumping frequency)
   - with `MO_BUG_REPORT_AUTO_PROMOTE=3`, the rubric bug — now at the top
     of the priority queue because frequency=5 and severity=high — gets
     promoted into a new epic `bug-12-rubric-pass-true-despite-fail`.

5. **Next scheduler tick** picks the new bug-epic, dispatches a recipe
   against it. The planner's `context_assemble` reads:
   - 10 prior runs of `framework_edit` (channel 1)
   - 10 known failure modes, with cross-class lessons ranked first
     (channel 2 — the rubric bug itself shows up here)
   - the `verifier_addition` pattern_records suggesting rubric needs
     hardening (channel 3)

   So the planner enters the dispatch already knowing what's broken and
   why, before any code is read.

6. **Workflow mutation** (via `lib/group_evolver.sh` + promotion gate)
   may propose an updated rubric verifier prompt. If the benchmark suite
   confirms `utility_delta > 0`, the change is `promoted` to
   `workflow_memory`. Future runs of any recipe inherit the improved
   rubric prompt without any roadmap intervention.

## Operator workflow

```
write roadmap -> ingest -> split -> scheduler --once
         ^                                  |
         |                                  v
         +---- review bug_reports < bugs prioritize
```

The full daemon-style operation looks like:

```bash
# Initial seeding from the SOTA-paper roadmap.
bin/mini-ork epics ingest kickoffs/roadmap-arxiv-self-learn.md
bin/mini-ork epics split  kickoffs/roadmap-arxiv-self-learn.md

# Long-running daemon. 60s idle between picks; honors cost-pause sentinels
# and the daily $50 budget cap (MO_DAILY_BUDGET_USD).
MO_OPEN_PR=1 MO_AUTO_MERGE=1 \
MO_BUG_COLLECTOR=1 MO_BUG_REPORT_AUTO_PROMOTE=3 \
nohup bin/mini-ork scheduler > .mini-ork/scheduler.log 2>&1 &

# Periodic review.
bin/mini-ork epics list
bin/mini-ork bugs prioritize --top 10
bin/mini-ork bugs promote --top 3   # only if AUTO_PROMOTE=0

# When done.
touch .mini-ork/cost-pause.sentinel    # scheduler exits at next iter
```

## Observability + tuning knobs

| Knob | Default | Effect |
|---|---|---|
| `MO_OPEN_PR` | 0 | Open a GitHub PR per epic that produces a branch. |
| `MO_AUTO_MERGE` | 0 | Squash-merge PRs that pass CI + approval + soak. |
| `MO_PR_SOAK_HOURS` | 24 | Required PR age before auto-merge eligibility. |
| `MO_REQUIRE_REVIEWER` | 1 | Require approving GH review before auto-merge. |
| `MO_PATTERN_MINER` | 0 | Mine `execution_traces` clusters → `pattern_records`. |
| `MO_CROSS_EPIC_GRADIENTS` | 1 | Promote recurring targets → `__cross_class__`. |
| `MO_CROSS_EPIC_MIN_CLASSES` | 2 | Min distinct task_classes to promote. |
| `MO_CROSS_EPIC_MIN_CONF` | 0.7 | Min gradient confidence to promote. |
| `MO_BUG_COLLECTOR` | 0 | Auto-dispatch heuristic scanner after every node. |
| `MO_BUG_COLLECTOR_MODE` | heuristic | `heuristic` or `llm` (stub). |
| `MO_BUG_REPORT_SWEEP` | 1 | Sweep `noticed_bugs.jsonl` at reflect. |
| `MO_BUG_REPORT_AUTO_PROMOTE` | 0 | Promote top-N bugs per reflect (0 = manual). |
| `MO_SCHED_RECIPE` | epic-runner | Recipe the scheduler dispatches per epic. |
| `MO_DAILY_BUDGET_USD` | 50.0 | 24h rolling cost cap (scheduler refuses past this). |
| `MO_TOPOLOGY_LOOP_EVERY` | (planned ARX-3) | Run workflow mutations only every Nth reflect. |
| `MINI_ORK_INTERVENTION_ASSUME_YES` | 0 | Auto-grant intervention-gate confirmations. |

Querying state:

```sql
-- Recent runs.
SELECT id, status, cost_usd, started_at, ended_at FROM task_runs
 WHERE created_at >= strftime('%s','now','-24 hours') ORDER BY started_at DESC;

-- Per-task-class learning curves.
SELECT task_class, outcome, COUNT(*) AS n, ROUND(AVG(cost_usd),3) AS avg_cost,
       ROUND(AVG(duration_ms)/1000.0,1) AS avg_secs
  FROM task_memory GROUP BY task_class, outcome;

-- Cross-class lessons currently in scope.
SELECT target, confidence, substr(signal,1,80) AS sig
  FROM gradient_records WHERE task_class='__cross_class__'
  ORDER BY confidence DESC LIMIT 20;

-- Top open bug_reports.
SELECT id, severity, frequency, ROUND(confidence,2),
       printf('%-15s', agent_role), substr(title,1,80)
  FROM bug_reports WHERE status='open'
  ORDER BY
    CASE severity WHEN 'critical' THEN 8 WHEN 'high' THEN 4
                  WHEN 'medium' THEN 2 ELSE 1 END * frequency * confidence DESC;
```

## Failure modes and recovery

| Symptom | Likely cause | Recovery |
|---|---|---|
| Epic stays `not started` despite no deps | Scheduler not running or budget cap hit | Check `MO_DAILY_BUDGET_USD` vs 24h spend; inspect scheduler log. |
| Epic flips to `escalated` while inner recipe verifiers passed | Verdict file name mismatch — already fixed in `bin/mini-ork-scheduler` (panel-verdict.json + verdict.json fallback). If you ship a new recipe with a different verdict filename, extend the loop at `bin/mini-ork-scheduler:131-160`. | `UPDATE epics SET status='done' WHERE id=...` + `source lib/epic_graph.sh; epic_graph_on_done <id>` to cascade. |
| Bug-collector floods queue with TODOs | Heuristic is conservative but can over-fire on agent commentary. | Raise `MO_BUG_REPORT_AUTO_PROMOTE` minimum severity, or `UPDATE bug_reports SET status='wontfix' WHERE severity='low'`. |
| Cross-class gradient list dominated by one target | Single recipe is generating noisy gradients. | Inspect `gradient_records WHERE target=?`. Reduce gradient extraction noise upstream or raise `MO_CROSS_EPIC_MIN_CONF` to 0.8+. |
| Scheduler picks an old leftover smoke epic | `epic_graph_ready_now` is oldest-first fair. | `UPDATE epics SET archived_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id LIKE 'old-prefix-%';` |
| PR open succeeds but never auto-merges | Soak hours not satisfied, or required reviewer missing. | `gh pr view <url> --json reviewDecision,createdAt` then either approve or wait. |
| Auto-promoted bug-epic has no kickoff body | Heuristic produced a one-line title only. | Manually edit `kickoffs/auto/bug-<id>.md` before scheduler picks it up. |

## Roadmap shape

A roadmap markdown is parsed by:

- `## <Title>` → one epic per heading; id is slugified title, override with
  `## <Title> (id: my-explicit-id)`.
- Body bullets matching one of:
  - `- depends on: <id>[, <id> ...]` (hard, blocks)
  - `- blocked by: <id>` (hard)
  - `- after: <id>` (hard)
  - `- requires: <id>` (hard)
  - `- should follow: <id>` (soft, informational only)
  - `- related to: <id>` (informational)
- Anything else in the body is preserved as the epic's prose. `epics split`
  extracts it into `kickoffs/auto/<id>.md` as the **Goal** section, with
  auto-synthesized Scope Hint and Verification commands sections built from
  backtick-quoted paths.

See `kickoffs/roadmap-arxiv-self-learn.md` for a worked 9-epic example with
3 dispatch waves backed by SOTA arxiv papers (VeRO, GRASP, TacoMAS,
SkillCAT, SIGA, MemMachine, ENGRAM).

## How this maps to the published literature

| Concept | Paper |
|---|---|
| Recursive self-improvement harness | VeRO, *A Harness for Agents to Optimize Agents*, arXiv:2602.22480 |
| Gated regression-aware skill proposer | GRASP, arXiv:2605.29668 |
| Fast capability + slow topology co-evolution | TacoMAS, arXiv:2605.09539 |
| Three-decision skill pipeline | SkillCAT, arXiv:2606.13317 |
| Self-rewriting grounding from trajectories | SIGA, arXiv:2606.09774 |
| Auto-prompt optimization from memory | MemMachine, arXiv:2604.04853 |
| Typed memory orchestration | ENGRAM, arXiv:2511.12960 |
| Long-horizon multi-task | SWE-Marathon, arXiv:2606.07682 |

Each ARX-* epic in `kickoffs/roadmap-arxiv-self-learn.md` ports one paper
into a concrete mini-ork change. See that file for the dispatch order and
expected effects.

## See also

- `docs/RECURSIVE-SELF-IMPROVE.md` — the inner single-epic loop.
- `docs/ARCHITECTURE.md` — overall system architecture.
- `docs/SCHEMA.md` — all 22 mini-ork tables.
- `recipes/recursive-self-improve/` — the canonical inner recipe.
- `recipes/epic-runner/` — the multi-epic dispatcher recipe.
