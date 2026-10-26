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

A single environment variable gates every dispatch:

| Variable | Default | Purpose |
|---|---|---|
| `MO_DAILY_BUDGET_USD` | `50` | Rolling-24-hour USD spend cap. Dispatch halts once trailing spend reaches it. |
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

`MO_REVISE_ROUNDS` (default `2`) caps the revise-loop rounds a failed
`retries`-edge node may spend re-dispatching its target before the
`escalates_to` rollback edges fire. Set it to `0` to restore the pre-revise
behaviour (a failed node blocks its descendants and rollback runs).

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
