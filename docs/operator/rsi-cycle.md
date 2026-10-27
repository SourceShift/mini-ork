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
| G6 | `pytest_scoped` | the scoped set — configured paths, a test the patch itself changes, or any test importing a changed module — fails |
| G7 | `static_differential` | `validate --strict`/`garden` findings grew (`post ⊄ pre`) |
| G8 | `success_metric` | the candidate's own success predicate did not flip, or the rule could not answer at all |
| G9 | `no_new_probe_failures` | probe failures grew versus before the change |
| G10 | `critic_unanimous` | fewer than 2 exogenous critics, or they split |
| G11 | `budget` | 24h spend or this cycle's cost exceeds its cap |

**G7 and G9 are differential on purpose.** `garden` and `validate --strict` are
already red on a healthy checkout; a strict "rc == 0" gate would mean this
feature merges nothing, ever. They compare *finding signatures* before and
after, so a real fix is proven by a **removed** finding and an unrelated red
does not block anything.

**G8 measures two ways, and neither credits silence.** It re-runs the
candidate's own success predicate on both trees. A schema or data repair shows
up directly (the post copy has the patch's migrations applied). A gap that
only a *producer* closes — a held-out injection, a promotion outcome, a
benchmark delta — cannot be repaired in history, so when both sides are still
red the gate runs the candidate's scoped tests against the copy and asks the
predicate again; it credits the change only if the base tree stays red while
the patched tree closes, i.e. only the new code wrote the row. And a predicate
that cannot answer at all (an absent table, an empty input, a dropped column)
is recorded as *unmeasured*, not as fixed — reading an error as "no finding"
was a way to walk a broken schema through this gate.

**The scoped set is what the change can actually be pinned by.** Configured
`gates.pytest_paths` are always in; so is every `tests/**/test_*.py` the patch
touches (nothing imports a test, so only its own path can name it); so is every
test that imports a changed module, matched on the module *path*
(`from mini_ork.cli import execute_handlers`, `import mini_ork.cli.execute_handlers`,
`from mini_ork.cli.execute_handlers import x`). A stem-only match once missed the
dotted forms, so the pinning test silently never ran. On the base tree, paths
that do not exist are skipped rather than named: pytest aborts the entire run
when one named path is missing, and a test the patch adds cannot exist there.

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
| every cycle `quarantined` `critic_rejected` | `rsi-cycle show <id> --json` | every critic rejected the same change; the *fix shape* is wrong, not the panel — re-read the candidate's `fix_hint` before retrying, since the next child derives its approach from it |
| every cycle `quarantined` `critic_split` | `rsi-cycle show <id> --json` | the critics disagree with each other; the *question* is underspecified — read their `blocking_findings` against each other. If the rejecting critic is demonstrably right (it names a producer that does not exist, a table no path writes), the defect is the candidate's `fix_hint`, not the child: sharpen the rule in `mini_ork/rsi/harvest.py` and release the quarantine, or the next child derives the same wrong approach from the same hint |
| every cycle `quarantined` `critic_not_exogenous` | `rsi-cycle doctor` | a critic lane share a family with `implement.lane` |
| `no_candidate` forever | `rsi-cycle plan --json` | nothing above `queue.min_severity`, or everything is closed in `rsi_candidates` |
| `rejected` naming `worktree_claim_conflict` | `rsi-cycle quarantine list` | another worktree holds one of the files the fix needs. A live cycle walks the ranked shortlist past a claim (bounded by `queue.max_candidates`) and only ends here when *every* eligible candidate is claimed; a cron job that keeps landing here means a stale claim — `quarantine release <id>` drops it without deleting the work |
| `rejected` naming `implement_empty` and `rc=124` | `<home>/rsi/<cycle>/child-home/runs/<id>/execute.log`, then `.last-llm-node` and `.orchestrator-watch.json` | the child hit `implement.child_timeout_s` before it produced a diff. `rc=124` is the wall clock, not a verdict: check which node was live and whether one node ate the budget — an advisory lens can hit the executor's own `MO_NODE_TIMEOUT_S` (1500 s) and starve the implementer behind it. Cycle 21 (2026-10-27) lost $0.86 this way (`glm_lens` spent 1500 s, then `spec_test_author` was killed). Raise `child_timeout_s` — the local config runs 7200 — or cut the lens set |
| `rejected` naming `implement_empty` but a worktree exists with modified files | `salvage.diff` in `<home>/rsi/<cycle_id>/` | the child was killed mid-edit: the work exists, the diff artifact does not. Since 2026-10-27 the reject reason names the uncommitted files and copies them to `salvage.diff` before the worktree is force-removed, so the row no longer reads as "the model had nothing to say". Treat the salvage as evidence of *where the child was*, never as a candidate fix — nothing in it was validated |
| `worktree clean for '<slug>' reported success but …` | the path the message names | a `clean` that exits 0 without removing anything: the directory or, worse, the file claims survived. Claims are what the next cycle collides with, so release them explicitly (`rsi-cycle quarantine release <id>`, or `scripts/mini_ork_worktree.py release <slug>` to keep the branch) and remove the tree by hand. The error exists because cycle 21 (2026-10-27) recorded `rejected` and left exactly this state for 20+ minutes |
| `rejected`/`halted` with `cost_usd` on the row but `gates_json` null | `rsi-cycle show <id> --json` | normal: the cycle died before VALIDATE, so no gate ran. The cost is still recorded (every terminal path sums `llm_calls` by run-id prefix since `afba7ecb`); a `0.0` on a row that dispatched a child means the cycle predates that fix |
| `2` and a config message | the message names the key | see the fail-closed list above |
| `error` with a traceback summary | `rsi-cycle show <id>` | a bug in the cycle itself — the row carries the exception type |
| `error` naming the SUGGEST wall (`hit its … wall before writing proposal.json`) | the run dir it names, then `llm_calls` for that run id | one node consumed its whole share and the stage after it had no time. The row records what the stage spent; check `.last-llm-node` in the run dir to see which node held the call |
| `error` naming a failed contract verify (`gate(s) failed: <gate>`) | the run dir's `verify-result.json`, then the gate module | the plan was written but a *globally registered* gate disagreed with it. The recipe's own verifier passing alongside a failed `__gates__` row is the signal that the gate is wrong about the run, not that the run is wrong: a proposal-only stage has no patch to apply and no test path to find. `step-rules-gate` in particular used to read the inside of `python3 -c "…"` checks as shell paths — fixed in `c79d189a`; if a different gate vetoes again, reproduce it against that run's `plan.json` before touching the proposal |
| `error` naming a planner that never dispatched (`never dispatched the planner: plan_status=needs_answers`) | the run's `run_profile.json`, then the lane in `llm_calls` for that run id | the profile still had questions and the model lane that answers them failed (a dead lane shows as `lane fuse open … quota`). The SUGGEST kickoff declares its own `## Verification commands`, so the profile is readied without any model call and this should not recur; if it does, the kickoff lost that section or `MO_PROFILE_REQUIRE_ANSWERS=1` is forcing the interrogation |
| `error` naming a preflight block (`blocked before dispatch: lane_unhealthy`) | the lane it names, then `agents.local.yaml` / `providers.yaml` for what that alias serves | lane preflight refused the run before any node dispatched (exit 75): the named lane answered a probe with a dead verdict — a quota wall, a rejected key. Nothing was spent; the fix is a live lane or a different alias for that panel. Cycle 20 (2026-10-26) hit this with `minimax on node lens_oracle: out of quota` after the minimax Token Plan ran out. A `quota` failure costs **one** strike, not three, so the fuse stays open for `MO_FUSE_WINDOW_HOURS` (default 6 h) after a single 429 |

Every `rejected` row carries its reason in `halt_reason`, not only on the
terminal it was printed to: a pre-dispatch filter writes its code
(`panel_invented_file`, `worktree_claim_conflict`, `frozen_path`, …) and a child
that landed nothing writes `implement_empty: <detail>`. That matters for a
scheduled run, whose stdout nobody keeps — `rsi-cycle list` is then the only
record of *why* the cycle stopped, and `rsi-cycle show` renders it beside the
candidate. It does not change avoidance: the queue skips a failed signature by
signature and TTL, not by reason, so a rejection that keeps recurring under a
different code is the same candidate coming back.

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
| `rsi-cycle` | SUGGEST | `proposal.json` (→ `mini_ork.rsi.select.Proposal`) | 3 lenses on 3 families, anonymised, one synthesis |
| `rsi-critic` | DECIDE | `critics.json` + `panel-composition.json` (→ G10) | 2 critics on 2 families, no synthesiser, no vote |

Both lanes sets are chosen for what a stock home can actually reach:
`glm_lens` (zai) and `minimax_lens` (minimax) for the critics, `deepseek_lens`
(deepseek), `codex_lens` and the other lens aliases for the suggest panel, with
the implementer on `codex_lens`. Deepseek is deliberately *not* a critic: a host
may map `codex_lens` and `deepseek_lens` onto one provider — this repo's own
overlay does — and the panel would then answer with one model wearing two
names. **An alias is not a family.** A host's
`agents.local.yaml` overlay can point `opus_lens` at the glm lane — the config's
startup check still passes, because it resolves the *provider* lane, and then
every cycle quarantines on G10 because both critics were served by one family.
`panel-composition.json` records the lanes that actually answered; read it
before blaming the critics.

Each recipe run gets its own wall (`SUGGEST` 1800 s, the critic run
`critic.model_timeout_s`). The SUGGEST run additionally bounds every node to
**half of that wall** by setting `MO_NODE_TIMEOUT_S` to `wall / 2` in the run's
environment, lowering (never raising) an operator's own value. Without that
bound a node inherited the runtime default of 1500 s, which is 83% of the wall:
on 2026-10-26 one lens call held a single dispatch for 1111 s and the
synthesizer, which by design runs *after* the lenses, had none left. The run was
killed at its wall and the cycle ended as an `error` with no proposal. The share
is a ratio rather than a constant so a longer wall buys proportionally more per
node instead of re-opening the same hole.

The critic run is deliberately **not** capped: its two critics dispatch in
parallel and are the whole run, so one of them using most of the wall costs
nothing, and a cap there would only turn a slow but correct panel into an
incomplete one — a G10 quarantine the cycle did not earn.

A cycle that ends in `error` still records what it spent: `cost_usd` is summed
from every `llm_calls` row whose `run_id` carries the cycle id as a prefix, so a
stage that died before reporting its own cost is not invisible. Cycle 17 spent
$0.42 and wrote $0.00 before this — which made a crash loop look free in the
one column the definition of done reads.

Whatever the critics answered is copied into `rsi_cycles.critic_json` on every
path from DECIDE onwards — the verdict, the resolved family, the blocking
findings and each critic's own `why` — so `rsi-cycle show <id> --json` is
enough to tell a wrong *fix* from a wrong *question* without opening the run
directory. A cycle that never reached the panel stores `NULL`, never `{}`:
"nobody was asked" and "asked and silent" are different answers.

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
