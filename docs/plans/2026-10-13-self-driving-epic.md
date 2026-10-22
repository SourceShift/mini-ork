# Self-driving epic — diagnose, repair, escalate, revive

*2026-10-13 · status: plan · escalation policy: **aggressive** (operator decision)*

## Why

`run-1791914481-48337` (code-fix on Daytona) did the work correctly — one-line fix,
tests green, reviewer + eval pass, `verdict.json` pass with every required level
PROVEN — and still ended `failed`: the publisher abstained
`publish-no-authored-patch` because the remote implementer's edit came back as an
unattributed tree delta. Auto-repair could not classify it (no rule for "withheld,
yet every level proven"), and `retry_hint._case_unknown` fell back to the last
`impl-*.log` and blamed `implementer`, a node that finished `done`. The run was
handed to a human.

The cause was an engine fault, fixed after the run (`88a3f99e`). Between 16:57 and
18:01 the same target produced 8 failed runs; each was diagnosed by a person or an
agent session, the engine was patched, and a **fresh** run was launched. No failed
run was ever revived. That outer loop — diagnose → fix the engine → revive the
same run, under a budget — is what mini-ork lacks.

## What exists (verified 2026-10-13)

| Piece | State |
|---|---|
| `recovery/auto_repair.py` same-run repair | wired, default on; fixed 7-action menu; unknown → human; fires only from the dying `run`/`recover` process |
| `triage/` blame → `framework-edit` fix epic | shipped, dormant (`MO_FAILURE_TRIAGE[_PROMOTE]`); deterministic traceback blame only; no back-link revival |
| `bug_reports.run_id` + `promoted_to_epic_id` | the fix-epic ↔ failed-run link already exists in the schema |
| run orchestrator MUTATE / PROMOTE | events only; nothing consumes them |
| planner / replanner nodes in execute | no-ops (`execute_handlers.py:_handle_planner_early`) |
| `recover --from-node --workflow <path>` | exists; reuses finished nodes — the graph-patch application point |
| supervisor for failed / reaped runs | none; `run_reaper` never calls `maybe_repair` |
| per-epic budget | `per_epic_usd` declared, displayed, never enforced |

## RSI pieces this program wires (measure-first, act-now)

The RSI program shipped measurements and deliberately deferred the acting half.
Aggressive self-repair is largely that deferred half.

| Need | Shipped piece | Use |
|---|---|---|
| calibrated confidence bar | `gates/abstain_gate.py` LTT threshold (G04-T05) | threshold for model-made diagnoses, labelled by revival outcome |
| repair beats blind retry | matched-attempt control arm (G06-T03, `probe_scorer.py`) | credit a repair operator only above N blind retries |
| halt a degrading loop | collapse halt (G01-T04, `circuit_breaker.py`) | trip fix-epic escalation when the fix success rate collapses |
| layer attribution | E7 layer routing (kickoff `rsi-e7-layer-routing.md`, never launched) | the diagnosis taxonomy |
| bound what self-edits touch | E8 unfireable kernel + E4 scope allowlist (never launched) | frozen paths an auto fix epic may never edit |
| no re-proposal of a failed fix | GRAO outcome-tagged edit memory (survey 2026-09-12) | never re-open a fix epic for a signature whose fix already failed |
| reversible self-edits only | EvoUndo G09-T09 | every auto fix epic ships with a revert path |
| self-verdict mirage | 2607.25152 (meta-diagnosis survey) | diagnoser lane family ≠ fixer lane family; labels come from the revived run's verifiers, never the diagnoser |

The "no human approval gate" rule (`project_true_rsi_chains_closed`) carries over:
an auto fix epic is gated by measurement, a frozen kernel, a daily cap and a revert
path — not by sign-off. The operator is notified through the oversight inbox, never
blocked on it.

## Design

```
run fails / reaped / publish withheld
        │
        ▼
 supervisor (lease) ─► diagnose ─► Diagnosis{layer, stage, node, evidence[], source, confidence}
        │                                   │
        │ layer ∈ change/model/provider/env │ layer = engine
        ▼                                   ▼
 cheapest graph patch on the ladder     escalation policy (aggressive):
 validate (compiler + contracts)          rule → escalate
 recover --from-node --workflow           model ≥ calibrated bar → escalate
        │                                 daily cap · collapse halt · GRAO · frozen kernel
        │                                   ▼
        │                               framework-edit fix epic ─► merged
        │                                   ▼
        │                               revive the SAME run from its failed node on the new engine
        └──────────── per-epic budget envelope ─┘
```

**Diagnosis layers:** `change` (the diff is wrong → revise) · `model` (the lane
cannot do it → swap lane) · `provider` (transport/quota/timeout → retry or swap) ·
`environment` (owner-fixable env/credentials/budget → notify owner) · `engine`
(mini-ork harness/runtime/recipe → fix epic) · `kickoff` (the task is ill-posed →
kickoff repair) · `user` (stopped by a person → nothing) · `unclassified`.
Every diagnosis cites evidence; an uncited one is `unclassified`.

**Repair ladder (cheapest first):** rerun → swap lane → re-verify → revise → insert
node → set placement → replan remaining subgraph → split → fix epic → human. Each
attempt must change the failure signature (existing no-progress rule).

**Graph patches** are typed, immutable files `<run_dir>/graph-patches/NNN.yaml`
(`rerun`, `swap_lane`, `set_placement`, `insert_before`, `replace`, `replan`,
`split`), compiled + contract-checked before `recover --workflow` applies them.

## Steps (one kickoff = one `framework-edit` run, own worktree)

| # | Kickoff | Deliverable | Depends on |
|---|---|---|---|
| SDE-1 | `kickoffs/auto/sde-1-diagnose.md` | `recovery/diagnose.py` typed Diagnosis + rules; retry hint never blames a `done` node; auto-repair records the diagnosis | — |
| SDE-2 | `kickoffs/auto/sde-2-frozen-kernel.md` | `config/kernel-frozen.yaml`; framework-edit static-check refuses frozen paths for auto-triage kickoffs; promote stamps origin + strips the ALLOW token from evidence | — |
| SDE-3 | `sde-3-escalate` (written after 1+2 land) | `recovery/escalation_policy.py` aggressive gate (rule → yes; model ≥ LTT bar; daily cap; GRAO; collapse halt); LLM diagnoser for `unclassified` on a non-implementer lane family; `escalate` action; run → `waiting_on_fix` | 1, 2 |
| SDE-4 | `sde-4-supervisor` | `mini-ork supervise [--once]`: run lease, reaped-run pickup, revive `waiting_on_fix` runs when the fix epic is done, epic status sync, per-epic budget enforcement, diagnosis labelling | 3 |
| SDE-5 | `sde-5-graph-patch` | patch schema + validator + application via `recover --workflow`; orchestrator MUTATE emits patches; repair credit vs blind-retry control arm | 4 |
| SDE-6 | `sde-6-replan` | planner re-entry for the remaining subgraph (graft, G02-T10); split to child epics | 5 |

SDE-1 and SDE-2 touch disjoint files and run in parallel.

### Progress

- **SDE-1 merged** (`f791a160`; pass 1 + one repair). Live replay over the 50
  most recent failed runs: unclassified 54% → 22%; exactly one `engine`
  diagnosis — `run-1791914481-48337`, `routes_to: fix_epic`.
- **SDE-2 merged** (`73a11cde`; pass 1 + one repair). The auto-triage origin is
  read from the pre-run kickoff snapshot; sha drift fails closed.
  `escalation_policy.py` is a forward-declared frozen path — the commit that
  adds it must drop it from `_PENDING_FROZEN_PATHS` in `test_frozen_kernel.py`.
- **SDE-3 split:** `sde-3a-escalate` (wire the operator policy; diagnosis-driven
  fix epic linked to the run; `waiting_on_fix`) then `sde-3b` (model diagnoser
  for `unclassified`, outcome labels, LTT bar). 3a waits on the operator's
  `should_escalate`.
- **SDE-3a merged** (2026-10-15; policy `e76c13ab`, pass 1 + one repair). The
  policy was written on the operator's delegation: hard stops non-engine →
  fix-epic breaker → failed signature → daily cap; rule diagnoses escalate;
  model diagnoses need 0.85 until 30 labels, then the calibrated bar, and an
  uncertifiable bar refuses. Replay over 50 failed runs: exactly one escalates
  (`run-1791914481-48337`). The repair made a `waiting_on_fix` run inert to
  later sweeps (pass 1 let a sweep overwrite it with a give-up).
- **VG-1 merged** (`a7caba16`): framework-edit's `kickoff_contract_verifier`
  runs ```` ```measure ```` blocks from the pre-run snapshot and enforces the
  kickoff's Required artifacts — the verifier gap that let SDE-1/2 pass 1 ship
  untested. Next verifier gap: F3, blind spec tests on a second lane.
- **SDE-3b merged** (2026-10-15): ledger + calibrated bar `6f60a89c` (end to end:
  35 right @0.95 + 5 wrong @0.30 → bar 0.95; confident-wrong history → refuse);
  model diagnoser `a1f458f1` (verbatim-cited evidence, never the implementer's
  model, asked once per run; live smoke on two real runs grounded both, ~$0.03
  each, and found a cause the hand triage missed). Both were salvaged from runs
  that failed only on harness bugs.
- **VG-3 merged** `640f80d3` (blind spec tests by GLM before implementation) and
  **VG-1b** `23722ba8` (kickoff commands keep `MINI_ORK_RUN_DIR`). The prose-scan
  gap was fixed upstream by `ddc88c43`. Remaining VG-1c: measures on the engine
  venv interpreter (`python3` on PATH is 3.9).
- **SDE-4 to SDE-6 merged 2026-10-22** (program `docs/plans/2026-10-22-rsi-gaps.md`):
  - SDE-4a `f1f93760` — `mini-ork supervise`: R1 revives a `waiting_on_fix` run once
    its fix epic is done and on `origin/main`; R2 labels the diagnosis ledger from the
    revived run's outcome; R3 clears orphaned `repairing` (G2).
  - SDE-4c `7aa2611f` — the scheduler enforces `budget.per_epic_usd`.
  - SDE-4b `a9cbcfc0` — `superseded` run status (migration 0080); R4 supersedes
    delivered-elsewhere failures (G1), R5 back-links done epics (G3).
  - SDE-5a `7a85d5df` — typed, immutable graph patches applied through
    `recover --workflow`; MUTATE proposes them; `mini-ork graph-patch`.
  - SDE-6a `824d5700` — replan patches built from existing node templates.
  - SDE-5b `d7694192` — R6 applies proposed patches and records outcomes; a kind
    that is not better than a blind rerun after 5 samples each is skipped.
  - SDE-6b `ad52777f` — split patches hand the remaining work to child epics; R7
    closes the split.
  Live on the primary home: migrations 0079–0083 applied; supervise sweeps
  superseded 153 failed runs, cleared 3 orphaned `repairing` states and back-linked
  `wit-2-documents`. **Not yet scheduled:** nothing runs `supervise` periodically.

## Two tiers

- **Simple:** `mini-ork supervise --once` from cron/launchd or a foreground loop;
  SQLite lease; fix epics run locally; caps in env with safe defaults.
- **Secure / cloud:** `supervise` as a service, leases in the shared DB, fix epics
  dispatched to sandboxed nodes with per-node scoped credentials, escalation
  notifications through the oversight inbox.

One config (`config/self-driving.example.yaml`, SDE-4) selects the tier.

## Delivery lessons carried from the RSI program

- Every RSI first pass needed one repair pass (two were inert on live data). Budget
  one repair pass per step and live-smoke each step on real failed runs.
- Check the call site, not the doc: a shipped measurement is not a wired action.
- `MO_DAILY_BUDGET_USD=2000` for the dispatch queue (24 h spend was $44 at start).
