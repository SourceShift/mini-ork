# Running the RSI cycle

One cycle finds a real gap in mini-ork's learning, routing or verification
surface, fixes it through a `framework-edit` child, and merges it **only** when
every deterministic gate and two exogenous critics agree — otherwise it
quarantines the branch and records the gate that said no. Repeating cycles
drain a queue rather than re-reading a doc, because every gap is a live
predicate that stops firing once fixed.

```
HARVEST ─► QUEUE ─► SUGGEST ─► IMPLEMENT ─► VALIDATE ─► DECIDE ─► LEARN ─► (next cycle)
 SQL+AST    rank    4-lens     framework-     11 gates    merge /    credit +
 + probes           panel +    edit child     + 2 critics quarantine collapse row
 (no LLM)           filter
```

Only SUGGEST and IMPLEMENT call a model. Harvest, queue, every gate and LEARN
are pure Python over SQLite, which is what makes `--dry-run` honest and bounds
what a cycle can cost.

## Quick start

```bash
cp config/rsi-cycle.example.yaml config/rsi-cycle.yaml
mini-ork rsi-cycle doctor            # is this checkout ready to cycle?
mini-ork rsi-cycle plan              # ranked gap queue, zero spend, zero writes
mini-ork rsi-cycle --dry-run         # the plan + the kickoff it *would* write
mini-ork rsi-cycle                   # one cycle (one config file: mode: once)
```

`plan` and `doctor` are always safe: no ledger writes, no worktrees, no model
calls. Start with them.

## Verbs

| Command | What it does | Cost |
|---|---|---|
| `rsi-cycle` (no verb) | run cycles per the config (`mode`, `max_cycles`) | up to `per_cycle_cap_usd` each |
| `rsi-cycle plan` | harvest + rank, print the queue | none |
| `rsi-cycle list [--status S] [--limit N]` | recent cycles | none |
| `rsi-cycle show <id> [--json]` | one cycle or candidate, incl. gates + critic verdicts | none |
| `rsi-cycle doctor [--json]` | readiness: config, DB, migrations, lanes, origin, budget, stop switch | none |
| `rsi-cycle quarantine list\|show\|release\|clean <id>` | inspect / release / remove held work | none |
| `rsi-cycle revert <cycle_id>` | revert a merged cycle in a fresh worktree, through the same gates | normal run cost |
| `rsi-cycle halt` | write `<home>/rsi/DISABLED`; every future cycle refuses to start | none |
| `rsi-cycle resume` | remove it | none |

Useful flags on the run verb: `--once`, `--interval N`, `--max-cycles N`,
`--config PATH`, `--budget USD` (may only **lower** the per-cycle cap),
`--domain D`, `--candidate ID`, `--dry-run`, `--suggest` (run the paid SUGGEST
step under `--dry-run`; still writes and dispatches nothing), `--probe-run`
(live probes for this invocation only), `--json`.

Exit codes: `0` ok / lease held / no candidate / halted by budget or a switch;
`1` internal error or a required probe failed; `2` usage or config.

## The eleven gates

A cycle merges only if all of them pass. Each failure names its code, which is
what `rsi-cycle show` prints and what the quarantine record keeps.

| # | Gate | Fails when |
|---|---|---|
| G1 | `harvest_complete` | a required probe errored or timed out |
| G2 | `child_verdict` | the child's `verdict.json` did not pass, or changed 0 files |
| G3 | `frozen_paths` | the diff touches a frozen path |
| G4 | `scope` | the diff escapes the kickoff scope or exceeds `gates.max_files` |
| G5 | `lint` | ruff `F,E9` fails on a changed `.py` |
| G6 | `pytest_scoped` | the scoped set, or any test importing a changed module, fails |
| G7 | `static_differential` | `validate --strict`/`garden` findings grew (`post ⊄ pre`) |
| G8 | `success_metric` | the candidate's own success predicate did not flip |
| G9 | `no_new_probe_failures` | probe failures grew versus before the change |
| G10 | `critic_unanimous` | fewer than 2 exogenous critics, or they split |
| G11 | `budget` | 24h spend or this cycle's cost exceeds its cap |

**G7 and G9 are differential on purpose.** `garden` and `validate --strict` are
already red on a healthy checkout; a strict "rc == 0" gate would mean this
feature merges nothing, ever. They compare *finding signatures* before and
after, so a real fix is proven by a **removed** finding and an unrelated red
does not block anything.

## Config

`config/rsi-cycle.yaml` (copy from `config/rsi-cycle.example.yaml`) — every key
is documented in the example. Resolution: `--config PATH`, else `MO_RSI_CONFIG`,
else `<MINI_ORK_ROOT>/config/rsi-cycle.yaml`. A named path that is missing is
exit 2, never a fallback to defaults: defaults would mean a cycle choosing its
own budget.

The two that matter most:

- `decide.auto_merge` has **no default in code**. Absent means `false`, and
  `false` means every cycle quarantines. Turn it on with a critic panel you
  trust.
- `gates.pytest_paths` (or `merge.test_cmd`) must be non-empty. The merge
  script's default green gate is the **whole suite** (~60 min), which is not a
  gate a cycle can wait for.

### The deterministic probe set

`probes.run` may name any registered id; the shipped default is:

| id | what it reads | cost |
|---|---|---|
| `garden` | recipe/naming/output drift | ~1 s |
| `validate` | static workflow validation (`--strict`) | ~1 s |
| `verifier-audit` | verifier detectors (gate hacking, collapse, gate fuzz) — in-process, no CLI verb | one query |
| `calibrate` | router calibration (`ece`, blind-spot rate) | ~1 s |
| `metrics` | trajectory metrics | ~1 s |
| `oversight` | oversight backlog | ~1 s |
| `tests` | the scoped `pytest_paths` set | minutes |
| `db:<RULE>` | re-runs one harvest rule; `ok` only when the rule now reports nothing | one query |

None of them touch the network or a model. That is deliberate: the set is the
half of HARVEST that two cycles can be compared on, and G9 diffs it. A probe
that needed a provider call would turn a flaky network into a fake regression.

`lane-preflight` is **not** in the set, and naming it is exit 2. Its health
check is a provider network call; dead lanes are caught by the dispatch-time
gate, where they belong.

### Fails closed — exit 2 before anything is spent

- config path unreadable, empty, not a mapping, unknown key anywhere
- `per_cycle_cap_usd <= 0`, or greater than `daily_cap_usd`
- `probes.run` contains an unregistered probe id
- `probes.allow_live: true` in the file (live probes are `--probe-run` only)
- `critic.lanes` has fewer than two lanes, two share a model family, or one
  shares the implementer's family (G10 would quarantine every cycle)
- `critic.require_unanimous: false` (a split quarantines; it never
  majority-merges)
- `mode: loop` with `interval_s <= 0`; `mode: once` with `max_cycles > 1`
- no scoped test set at all (neither `gates.pytest_paths`, `probes.pytest_paths`
  nor `merge.test_cmd`)
- `merge.require_remote: true` and `git remote get-url origin` fails — checked
  at startup, so a missing origin costs nothing instead of quarantining after a
  paid child run
- `implement.runtime_backend` not in `local | bubblewrap | daytona | microsandbox`
- lease tables or the `rsi_*` tables missing (run `mini-ork update`)

## Stopping it

Four switches, all fail-closed:

| Switch | Scope | Undo |
|---|---|---|
| `MINI_ORK_CYCLE_DISABLED=1` | the shell / service environment | unset it |
| `<home>/rsi/DISABLED` | durable, survives restarts | `mini-ork rsi-cycle resume` |
| `enabled: false` in the config | durable | edit the file |
| `cost-pause.sentinel` | reactive, written by the dispatch cost guard | clear the sentinel |

`rsi-cycle halt` deliberately does **not** parse the config — a broken config
must still be stoppable. It releases the lease; it does not kill a child that
is already running.

A cycle also halts itself on: `daily_cap` (checked before the lease, so a
halted cycle spends nothing), `lease_held` (another cycle owns the lease — the
tick costs nothing), `harvest_incomplete` (a required probe failed), and
`no_proposal` (the panel declined to propose anything — rc 0, not an error).

### The collapse halt

LEARN appends one `collapse_history` row per measured cycle: `score` is the
rolling merge rate, `anchor` is the fraction of that cycle's probes that came
back green. After writing, the last rows go through
`mini_ork/learning/collapse_detector.py`; a score that rises while the anchor
decays writes `<home>/rsi/DISABLED` with the reason on line 1, and the next
cycle halts reporting `collapse: score_rise=… anchor_drop=…`.

The detector needs 4 rows, so **the halt cannot fire before the fifth cycle.**
That is deliberate: a loop that panics on its first measurement is a loop
nobody can trust. Deleting the file resumes; read `rsi-cycle list --limit 10`
first.

## Quarantine, release, revert

A quarantined cycle keeps its worktree, branch and claims by default
(`decide.quarantine_keep_worktree: true`). It is evidence — read it before
deleting it:

```bash
mini-ork rsi-cycle quarantine list
mini-ork rsi-cycle quarantine show <id>        # path, diff, failing gates
mini-ork rsi-cycle quarantine release <id>     # drop the claims, keep the tree
mini-ork rsi-cycle quarantine clean <id>       # remove worktree + branch
```

`revert <cycle_id>` builds a revert commit from the cycle's `merged_sha` in a
fresh worktree and merges it through the same gate path. Nothing here ever runs
`git reset --hard`, and nothing force-pushes.

## Scheduling

Simple tier: launchd fires `--once` every hour.
`deploy/launchd/com.miniork.rsi-cycle.plist.example` is a working file with the
three constraints that actually bite (run the interpreter, not a shell script;
logs on the boot volume; `--once` so a cycle never outlives its tick).

Secure tier: `mode: loop` under the same config. The lease makes overlap a
no-op, so a manual `rsi-cycle` during a scheduled one halts with `lease_held`
instead of racing. Point `secure.db_path` at the shared DB and
`implement.runtime_backend` at the sandbox backend; no call site changes.

## When it misbehaves

| Symptom | First thing to run | Usually means |
|---|---|---|
| `halted` + `lease_held` | `rsi-cycle list --status running` | a cycle is genuinely running; if it is stale, the TTL expires |
| `halted` + `daily_cap` | `rsi-cycle doctor` | 24h spend at the cap; raise `daily_cap_usd` or wait |
| every cycle `quarantined` `critic_split` | `rsi-cycle show <id> --json` | the critics disagree; read their `blocking_findings` |
| every cycle `quarantined` `critic_not_exogenous` | `rsi-cycle doctor` | a critic lane share a family with `implement.lane` |
| `no_candidate` forever | `rsi-cycle plan --json` | nothing above `queue.min_severity`, or everything is closed in `rsi_candidates` |
| `2` and a config message | the message names the key | see the fail-closed list above |
| `error` with a traceback summary | `rsi-cycle show <id>` | a bug in the cycle itself — the row carries the exception type |

`rsi-cycle doctor` checks all of it in one pass: config parse, database,
migrations, the four `rsi_*` tables, lease tables, `bin/mini-ork` +
`scripts/mini_ork_worktree.py`, the `origin` remote, harvest rules (schema
drift shows up as `probe_broken`), lane-family distinctness, worktree directory
writability, 24h budget headroom, and the stop switch.

## The two recipe runs

A cycle dispatches two recipes and reads exactly two artifacts from them. Both
run with their own home under `<home>/rsi/<cycle_id>/<recipe>/home`, on the
cycle's database, under a run id derived from the cycle id — so every artifact
of one cycle sorts together and no run can write into the operator's own home.

| Recipe | Stage | Artifact the cycle reads | Shape |
|---|---|---|---|
| `rsi-cycle` | SUGGEST | `proposal.json` (→ `mini_ork.rsi.select.Proposal`) | 4 lenses on 4 families, anonymised, one synthesis |
| `rsi-critic` | DECIDE | `critics.json` + `panel-composition.json` (→ G10) | 2 critics on 2 families, no synthesiser, no vote |

The machine brief travels by environment variable (`MO_RSI_SUGGEST_BRIEF`,
`MO_RSI_CRITIC_BRIEF`), not as an artifact: the run directory does not exist yet
when the cycle builds the brief. The recipes render it themselves
(`verifiers/brief.py`), strip provenance before any lens or critic sees it, and
refuse to run without it — that is why they are never dispatched by hand.

A failed run never crashes the cycle. SUGGEST without a proposal halts with
`no_proposal`; a critic run that crashes returns no verdicts, G10 fails closed
with `critic_not_exogenous`, and the cycle **quarantines** — keeping the
worktree and the diff so the work is still there to review.

## What it does not do yet

- **Lesson authoring.** A cycle records credit and the collapse measurement; it
  does not write lessons. A lesson drawn from one cycle teaches the loop its own
  noise — authoring waits for a repeated failure signature and the
  `lesson_conflict` judge.
- **Un-gameable gates.** G7/G9 are differential and G10 is out-of-family, which
  raises the cost of gaming them; it does not make them impossible. The
  implementer never sees the gate implementation it is measured against, and
  `repo_integrity_guard` refuses frozen paths at apply time.
