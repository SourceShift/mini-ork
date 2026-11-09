# Environment variables

MiniOrk works with no environment variables for a local project. Set the
variables below only to change where state is stored, separate engine and
target repositories, or enable an explicitly documented runtime behavior.

## Core paths

| Variable | Default | Purpose |
|---|---|---|
| `MINI_ORK_ROOT` | MiniOrk engine root | Locates shipped recipes, migrations, and configuration. |
| `MINI_ORK_HOME` | `.mini-ork/` under the project | Stores project-local state, configuration, and run artifacts. |
| `MINI_ORK_DB` | `$MINI_ORK_HOME/state.db` | Overrides the SQLite state database path. |
| `MINI_ORK_PROJECT_HOME` | current project | Identifies the project MiniOrk is operating for. |
| `MINI_ORK_TARGET_REPO` | current project | Identifies the repository available to agent and verifier work. |
| `MINI_ORK_ENGINE_ROOT` | resolved engine root | Overrides engine discovery for installed launchers. |
| `MINI_ORK_PROFILE` | unset | Selects `config/profile.<name>.yaml`: its `defaults` fill unset knobs, its `required` knobs fail closed. `simple` is the laptop tier; see [CONFIG.md](../CONFIG.md#profiles-one-file-per-tier). |

`MINI_ORK_DB` takes precedence over the database path derived from
`MINI_ORK_HOME`. Keep each concurrent project or test run on its own home and
database path.

## Credentials

Configure provider credentials with the CLI instead of passing keys on command
lines:

```bash
mini-ork providers configure <lane>
mini-ork providers status <lane>
```

The command writes local credentials to
`$MINI_ORK_HOME/config/secrets.local.sh`, which is ignored by Git. See
[provider triage](provider-triage.md) for provider-specific diagnosis.

`MO_HIDDEN_CHECK_SECRET` belongs in `$MINI_ORK_HOME/config/secrets.local.sh`;
when unset, hidden checks fall back to `run_id` then the patch digest and say
so in the certificate (`key_source`).

## Cost

Two different ceilings gate a dispatch, and they answer different questions:
`MO_DAILY_BUDGET_USD` is the machine-wide rolling window, `MO_RUN_BUDGET_USD`
is what one run was allowed to spend.

| Variable | Default | Purpose |
|---|---|---|
| `MO_DAILY_BUDGET_USD` | `50` | Rolling-24-hour USD spend cap across every run on the machine. Dispatch halts once trailing spend reaches it. |
| `MO_RUN_BUDGET_USD` | *(unset)* | Declared ceiling for **this one run**. Once its `llm_calls` total reaches the ceiling, the next node is refused and the run parks as `budget_exhausted` — resumable, never failed. Unset = no per-run ceiling. Read by `mini_ork.cost.budget`; a value that does not parse is a refusal, not "unlimited". |
| `MO_BUDGET_POLICY` | `declared` | Which budget policy answers "may this run spend another node?". `declared` reads `MO_RUN_BUDGET_USD` (then `run_profile.json` → `budget_usd`). A deployment can register its own policy naming a real cost service (`register_budget_policy`); an unknown name refuses rather than falling back to unlimited. |
| `MO_PAUSE_EVERY_USD` | `25` | Reactive pause interval. Every whole multiple of this the run crosses writes `<run_dir>/.cost-pause`; `mini-ork resume` clears it. Independent of both ceilings. |
| `MO_LANE_HEALTH_GATE` | `"1"` | Pre-run lane-health gate. `1` probes every model the DAG will dispatch and aborts the run with rc=75 / `blocked_reason=lane_unhealthy` if any lane is dead. `0` skips enforcement (the run can still see per-dispatch refusals; see `MO_LANE_PROBE`). |
| `MO_LANE_REQUIRE_VERIFICATION` | `"0"` | Fail-closed switch for lanes the gate could **not** verify — `ok` but no probe answered (a native lane the CLI authenticates, a probe switched off, an endpoint that never replied). `0` (default) warns on stderr, records them under `unverified` in `<run_dir>/lane-preflight.json` with `verdict: proceed`, and starts the run. `1` aborts instead: rc=75, `blocked_reason=lane_unverified`, `verdict: blocked`. A dead lane always blocks first, as `lane_unhealthy`. |

The window is **rolling 24 hours**, not a calendar day. The gate sums
`llm_calls.cost_usd` (falling back to `task_runs` on databases behind the
migrator) over the trailing 24 h and compares it with the cap, so budget
returns as older calls age out — there is no midnight reset.

There are two enforcement points:

- **Before the run starts**, `mini-ork run` refuses to dispatch anything and
  exits `75` (`EX_TEMPFAIL`, "the run never started") with a `blocked` verdict
  and `blocked_reason: cost_limit`. This is deliberate: an in-run trip used to
  fail every node within seconds, which is indistinguishable from a model that
  tried and failed — say it once, before anything is spent.
- **During the run**, `mini_ork.dispatch.llm_dispatch.cost_circuit_open`
  refuses each individual dispatch, writes a `cost_limit` refusal row, and
  returns `42`. The scheduler re-checks the same cap before every epic
  admission.

Both read `MO_DAILY_BUDGET_USD` only. If the state database is missing or
unreadable the gate fails **open** — absent spend reads as `$0.00`, so a
broken database never blocks work. Raise the cap for a session by exporting it
before the run:

```bash
MO_DAILY_BUDGET_USD=400 mini-ork run framework-edit kickoff.md
```

### Per-run ceiling (`MO_RUN_BUDGET_USD`)

The daily cap is shared: one runaway run starves every other run on the
machine, and a cheap run cannot be handed headroom of its own. The per-run
ceiling is the other axis — a limit on **one** run, checked before every
dispatch against that run's `llm_calls` total (`mini_ork.cost.budget`):

```bash
MO_RUN_BUDGET_USD=12 mini-ork run code-fix kickoff.md
```

It can also be recorded in the run's profile (`run_profile.json` →
`"budget_usd": 12`) so the ceiling travels with the run into recovery and the
IDE; `MO_RUN_BUDGET_USD` wins when both are present, so a single retry can be
given a different ceiling without rewriting the profile.

When the ceiling is reached the next node is refused with finish reason
`budget_exhausted`, the run **parks** with the same rc a review pause uses
(9) — the plan, the worktree and every finished node survive — and
`<run_dir>/.budget-exhausted` records `spent_usd` / `budget_usd` / `detail`
for the IDE run page and the attention inbox (`mini-ork board inbox` leads with
it, above the periodic cost-pause). Raise the ceiling and resume the same run:

```bash
MO_RUN_BUDGET_USD=40 mini-ork resume <run-id>
```

Refusals are the interesting half of the contract:

- A declared value that does not parse (`twelve`, `-5`, `nan`, `Infinity`) **refuses** —
  a typo must not buy a run more spending room. The same strictness applies to
  `run_profile.json` → `budget_usd`; `nan`/`inf` are refused there too, because
  they parse as floats and pass every `spent >= budget` comparison.
- A `run_profile.json` that **exists but cannot be read** (corrupt JSON, not an
  object) **refuses** — it may carry a ceiling, so reading it as "no ceiling"
  would raise the run's spend. A profile with no `budget_usd` key (or `null` /
  empty) declares no ceiling, like having no profile at all: only a declared
  value that is present and unusable refuses.
- A named policy that is not registered, or one that raises, **refuses** —
  never "unlimited".
- A missing or unreadable ledger reads as `$0.00` spent, the same fail-open
  choice the daily cap makes, so a fresh home is never blocked by a database
  that does not exist yet.
- A run that was **parked by a ceiling which is no longer declared** stays
  parked. The marker is the durable evidence that a ceiling applied, so a
  restart in a process that lost `MO_RUN_BUDGET_USD` (and whose profile has no
  `budget_usd`) is refused at the gate rather than silently dispatching with no
  ceiling and erasing the marker. Re-declare a ceiling above the spend, or
  delete `<run_dir>/.budget-exhausted` deliberately.
- `mini-ork resume` re-asks the policy before un-parking: a run still over its
  ceiling gets rc 1 and the number to exceed, and a run whose ceiling was
  **removed** is refused too — unsetting `MO_RUN_BUDGET_USD` does not make a
  parked run unlimited. Only when the run fits is `.budget-exhausted` cleared
  (and the approval audited to `.cost-pause-approvals.jsonl`). When a run holds
  **both** a `.cost-pause` sentinel and a budget marker, both are answered
  together: a still-blocking budget refuses the resume before the sentinel is
  touched, and a resume that succeeds clears both with one audit row each.
- The marker is removed as soon as the run fits under the ceiling again, so a
  raised ceiling does not leave a stale "needs you" row behind.

To source the ceiling from a real cost service instead of the environment,
register a policy and select it by name (the secure tier — the policy owns
where the number comes from, this module owns when it is asked):

```python
from mini_ork.cost.budget import Verdict, register_budget_policy

def _quota(db, run_id, run_dir) -> Verdict:
    remaining = my_cost_service.remaining(run_id)   # fails closed on error
    return Verdict(spent=0.0, budget=remaining, exhausted=remaining <= 0,
                   detail=f"${remaining:.2f} left on the org quota")

register_budget_policy("myorg", _quota)
```

Out of tree, register it through the plugin seam instead of patching core — an
entry point in the `mini_ork.plugins` group whose target calls
`context.budget_policy("myorg", _quota)` (see `mini_ork/plugins.py`). The CLI
runs that discovery once per process **at `mini_ork.cli` import**, so every
subcommand sees it — including the `mini-ork resume` / `mini-ork recover` that
a budget refusal tells you to run, which the dispatcher spawns as separate
`python -m mini_ork.cli.<sub>` processes (`execute` runs in-process, and sees
the same import). A plugin that fails to load is
reported on stderr and skipped. Set `MINI_ORK_NO_PLUGINS=1` in the process
environment to skip discovery where third-party code must not execute; because
discovery happens at import, this must be in the environment before the process
starts — a profile value set later is too late, and the opt-out does not undo a
plugin that already registered.

```bash
MO_BUDGET_POLICY=myorg mini-ork run code-fix kickoff.md
```

The lane-health gate (`MO_LANE_HEALTH_GATE`, default `"1"`) reads the same
`lane_health()` machinery `dispatch_model` consults per node — the difference
is that the gate runs **once, before any dispatch**, so a starved lane (e.g.
`opus` with no credit, `deepseek` returning HTTP 402 "Insufficient Balance")
aborts the run with rc=75 + `blocked_reason=lane_unhealthy` and a per-node
message naming lane → role → node, plus a `recover --lane ...` fix line. The
verdict vocabulary is the body regex `MO_LANE_PROBE` already shares: status
`401/402/403`, or any 2xx/4xx/5xx carrying `credit balance is too low`,
`insufficient (credit|balance|quota)`, `usage limit`, `out of credits`, or
`billing`.

Dead is not the same as unchecked. A lane that came back `ok` **without** a
probe answering — a native lane whose auth the CLI owns, a probe switched off,
or an endpoint that never replied — is reported as *unverified*
(`verified=false`), named per waiting node, and written under the `unverified`
key of `<run_dir>/lane-preflight.json`. By default such a run **proceeds**
with a `warn:` line on stderr (`verdict: proceed`, no `blocked_reason=`
machine line, no `task_runs` write). Set `MO_LANE_REQUIRE_VERIFICATION=1` to
make an unverifiable lane abort instead: rc=75, `blocked_reason=lane_unverified`
(the same `lane_preflight_blocked` run event, no new event type or status).
A dead lane always wins: with a `lane_unhealthy` failure present the run is
refused as `lane_unhealthy` regardless of this flag.

Limitations:

- `MO_LANE_PROBE=0` (or `MINI_ORK_DRY_RUN=1`, or running under pytest
  without opt-in) leaves the gate resting on key presence: the lane reads
  `ok` with `verified=false`, and native lanes (`anthropic-native`,
  `codex-native`, `opencode-native`) have no `api_key_env` to read at all.
  Those lanes are unverified, not dead — see `MO_LANE_REQUIRE_VERIFICATION`
  to fail closed on them, or `MO_LANE_HEALTH_GATE=0` to skip enforcement
  entirely.
- An ambient login (no credential env var) is unknown and does not block — it
  is reported as unverified. The gate honours three credential env vars for
  `anthropic-native` lanes: `ANTHROPIC_API_KEY` → `x-api-key`, else
  `ANTHROPIC_AUTH_TOKEN` → `Authorization: Bearer`, else
  `CLAUDE_CODE_OAUTH_TOKEN` → Bearer + the
  `anthropic-beta: oauth-2025-04-20` header.

### Lane liveness at instantiation (`mini-ork validate`)

The gate above fires when a **run** starts. `mini-ork validate` (Check 5) asks
the same `lane_health()` question at **configuration** time: every lane in the
active `providers.yaml` whose credential is set gets one cached 1-token probe,
and a lane that answers a definitive refusal (401/402/403, or a quota-walled
429) is an **error**, not a warning:

```
[error]   lane 'kimi' is unhealthy: kimi: API key rejected (HTTP 401) at https://api.kimi.com/coding/v1/messages — every dispatch on this lane would fail
          Fix: fix the credential in <home>/config/secrets.local.sh, or remove the lane from <home>/config/providers.yaml — every dispatch on it will fail
```

This exists because the run-start gate is too late to be the only one: a dead
lane that only fires once a run has been planned, dispatched and paid for has
already cost more than the probe. It is also the check that makes the shipped
`MO_FALLBACK_CODING` / `MO_FALLBACK_REVIEW` defaults safe to trust.

Deliberately **not** findings: a lane whose `api_key_env` is unset (the
secret-presence check owns that message, so a credential-less CI does not go
red), and a lane whose kind is not probeable (`codex-native`,
`opencode-native`, or a malformed entry — no cheap assertion exists). When
probes are off (`MO_LANE_PROBE=0`, `MINI_ORK_DRY_RUN=1`) or a lane is native
with no credential var, validate says so rather than reporting a liveness check
that never ran: `lane liveness not verified for N lane(s): …`.

Credentials resolve through `provider_environment()` — the owner-only local
store overlaid by the environment, the same resolution `dispatch_model` uses.
Reading only `os.environ` reports every store-backed credential as missing;
fixing that is what took one live home from 12 phantom "secret missing"
warnings to the single real fault.

The `budget:` block in `agents.yaml` declares the epic's spend envelope.
`per_epic_usd` is **enforced by the scheduler** per epic across all its
attempts: once recorded spend reaches the cap, `dispatch_epic` sets the epic
`blocked` with `held_reason=per_epic_budget` and dispatches nothing. To resume
it, raise the cap and then release the hold with `mini-ork epics retry` (a
release without a higher cap re-blocks on the next dispatch).
Override the configured value with `MO_PER_EPIC_USD`; a non-numeric or negative
override fails closed. `per_run_usd` and `daily_cap_usd` remain display only,
and `MO_DAILY_BUDGET_USD` is still the dispatch circuit.

## Runtime behavior

Advanced `MO_*` and `MINI_ORK_*` switches are cataloged in
[Feature flags](feature-flags.md). Set only the flags required by a specific
runbook or recipe; defaults are selected to keep the standard CLI path safe.

Two common extension selectors are:

| Variable | Purpose |
|---|---|
| `MO_ROUTING_POLICY` | Chooses a registered native routing policy. |
| `MO_EMBED_PROVIDER` | Chooses a registered semantic-memory embedder. |

### Delegation ledger

Every dispatched run records who asked for it, a copy of the kickoff bytes it
saw, and the absolute path of the `state.db` holding its row. `mini-ork runs`
reads those rows — the current home by default, or `--all-homes` across every
registered home.

| Variable | Default | Purpose |
|---|---|---|
| `MO_INITIATOR` | unset | Explicit initiator string recorded on `task_runs.initiator` for this dispatch. Control characters are stripped and the value is capped at 120 chars. When unset, the runtime falls back to `MO_SCHEDULER_DISPATCH`, `MO_RECOVER_OF`, `MO_RSI_CYCLE_ID`, a process-ancestry `claude-session:<id>`, then `manual[:<uid>]`. |
| `MO_SCHEDULER_DISPATCH` | unset | Set to `1` by the scheduler to record the initiator as `scheduler` rather than `manual`. |
| `MO_HOMES_REGISTRY` | `~/.mini-ork/homes.json` | Points the homes registry at another file. The default is the laptop (simple) tier; point this at a shared location when several homes should be unioned by `mini-ork runs --all-homes`. An unreadable registry is reported as `incomplete`, never presented as a complete empty union. |

`MO_REVISE_ROUNDS` (default `2`) caps the revise-loop rounds a failed
`retries`-edge node may spend re-dispatching its target before the
`escalates_to` rollback edges fire. Set it to `0` to restore the pre-revise
behaviour (a failed node blocks its descendants and rollback runs).

### Revise-round resume

A revise round is the same node, against the same target, with a delta-sized fix
instruction. Its implementer prompt deliberately drops the first attempt's
background so the round does not re-pay for it — which is only safe if the lane
still holds that background. So the round **continues the previous attempt's
conversation** (`claude --resume <id>`) instead of cold-starting, using the
session id the dispatcher stashes at `<run_dir>/.sessions/<node>.session` on
every attempt (including failed ones). `mini-ork recover` uses the same
machinery from durable state.

| Variable | Purpose |
|---|---|
| `MO_REVISE_RESUME` | `1` (default) resumes the previous attempt on a revise round. `0` cold-starts every round. |

The resume is fail-soft in one specific way that matters: it is only attempted
when the transcript is actually resolvable, because a `--resume` the lane cannot
honour fails the node — worse than paying for the background again. No session,
a codex/gemini lane (which runs its own session model), or a missing transcript
all mean the round simply runs from scratch.

To see whether the saving happened rather than infer it from an
unexpectedly small input-token count, every resumed dispatch stamps
`resumed: <session id>` into `llm_calls.metadata_json`:

```bash
sqlite3 "$MINI_ORK_HOME/state.db" \
  "SELECT model_id, input_tokens, json_extract(metadata_json,'\$.resumed')
     FROM llm_calls
    WHERE json_extract(metadata_json,'\$.resumed') IS NOT NULL
    ORDER BY id DESC LIMIT 10"
```

### Worktree placement

Normally the implementer edits the target checkout in place. When another
session is also working in that tree, the diff a reviewer reads can contain work
this run did not do — a correct gate grading the wrong artifact.

Declaring placement makes the run edit its own git worktree (branch
`mini-ork/<run_id>`, recorded at `$MINI_ORK_HOME/worktrees/<run_id>.json`) and
points the run's roots at it, so every node, verifier and git step sees one tree
only this run can write. The base is the target's HEAD at run-start, recorded in
the workspace record.

| Variable | Purpose |
|---|---|
| `MO_WORKTREE_PLACEMENT` | `1` isolates this run; `0` runs in place **even if the recipe asks for isolation**. Unset: the recipe's `placement.worktree` decides. |

A recipe declares it once, so every run of it is isolated:

```yaml
# recipes/my-recipe/workflow.yaml
placement:
  worktree: true
```

Declared but impossible (target is not a git repo, `MINI_ORK_HOME` unset) **fails
the run start** with rc 2 and names the opt-out. It never falls back to the
shared checkout: that fallback is the defect, since the run would look isolated
and be graded on someone else's tree.

Finishing is deliberate, not automatic. The run's commits land on
`mini-ork/<run_id>`, your checkout is untouched, and `mini-ork inbox` lists the
keep-or-discard decision once the run ends — merge or discard from the board.

Two tiers, one switch: this needs no server, no daemon and no account, so the
laptop tier is `MO_WORKTREE_PLACEMENT=1` on one command; the shared-machine tier
is `placement.worktree: true` in the recipe, which no operator has to remember.

### Interactive dispatch

A node dispatched on a person's behalf is not batch work: a human is waiting,
so it goes to the front of its batch and the pool is sized one wider than
`MINI_ORK_MAX_PARALLEL` (default 4) while such a node is in it. `mini-ork
recover` marks itself interactive automatically.

| Variable | Purpose |
|---|---|
| `MO_INTERACTIVE` | `1` marks the whole invocation interactive. `execute --interactive` sets it. |
| `MO_INTERACTIVE_NODES` | Comma-separated node ids to mark inside an otherwise batch invocation. |
| `MINI_ORK_MAX_PARALLEL` | The batch limit. An interactive node buys one slot beyond it, never more. |

The extra slot is per batch; nothing here arbitrates between separate
processes.

### Certify lane calls

`mini-ork certify` calls its model lane several times per verdict (reproduction,
invariants, relations, differential). Each call is bounded on its own, so a stalled
gateway costs minutes rather than the dispatcher's 25-minute default, and a call
that still fails makes the oracle abstain (UNVERIFIED) instead of hanging.

| Variable | Default | Purpose |
|---|---|---|
| `MO_CERTIFY_MODEL` | `minimax` | The lane that writes probes and invariants (a code lane). |
| `MO_CERTIFY_LANE_TIMEOUT_S` | `180` | Wall-clock cap per lane call. |
| `MO_CERTIFY_LANE_MAX_TURNS` | `8` | Agentic turn cap per lane call. |
| `MO_CERTIFY_LANE_RETRIES` | `1` | Retries after a failed or timed-out call (`0` disables). |
| `MO_CERTIFY_PROGRESS` | unset | `1` prints one line per lane call to stderr even when it is not a terminal. |

A present-but-malformed value fails closed: no call is made and the verdict is
UNVERIFIED, with the reason on stderr when a terminal is attached.

The verification stack — six instruments from the 2026-10-05/06 review
(metamorphic relations, the differential check, mutation suite adequacy, the
level vector, the gate-hackability audit, and the declared equivalence operator)
plus eight from the 2026-10-18 review (`verify-agentic-20261018-215339`) — is
documented there. All but two are ON by default; judge validity
(`MO_JUDGE_VALIDITY`) and conformal abstention (`MO_CONFORMAL_ORACLE`) are
opt-in. Knobs are listed under [Feature flags › Verification
stack](feature-flags.md#verification-stack) and explained in [the verification
stack](../architecture/verification-stack.md).

Do not use the retired `MINI_ORK_RUNTIME` selector. The supported framework
runtime is native Python; shell execution remains available only for declared
recipe verifier, target-repository, migration, and sandbox boundaries.
