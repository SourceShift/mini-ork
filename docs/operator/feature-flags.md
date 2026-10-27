# Feature flags — operator reference

*Generated from the 2026-07-26 unused/integration audit. These `MO_*` /
`MINI_ORK_*` variables are read by live runtime code but were previously
undocumented (set them in `config/secrets.local.sh` or the process env).
Defaults shown are the in-code fallbacks — the behavior you get when the
variable is unset.*

## Execution / dispatch

| Variable | Default | Effect |
|---|---|---|
| `MINI_ORK_EXECUTE_GATE` | `"1"` | Pre-dispatch gate on needs_answers plans (exit 6) |
| `MO_LANE_HEALTH_GATE` | `"1"` | Pre-run lane-health gate (exit 75 on dead lane); set `0` to skip enforcement. Under `MO_LANE_PROBE=0` the gate rests on key presence, and those lanes are reported *unverified* (`verified=false`) rather than silently counted as checked. |
| `MO_LANE_REQUIRE_VERIFICATION` | `"0"` | Fail-closed switch for the lane-health gate's unverified lanes. `0` (default) warns on stderr and proceeds, recording the lanes under `unverified` in `<run_dir>/lane-preflight.json` (`verdict: proceed`). `1` aborts the run instead (exit 75, `blocked_reason=lane_unverified`, `verdict: blocked`) when any lane is `ok` but `verified=false` — a native lane the CLI authenticates, a probe turned off, or a probe that got no answer. A dead lane always blocks first, as `lane_unhealthy`. |
| `MO_LANE_PROBE` | env-derived | Probe-enabled switch for `lane_health`: `0` disables the live HTTP probe and `MINI_ORK_DRY_RUN=1` overrides to `0`. Under pytest without explicit `MO_LANE_PROBE=1` the probe is dark so tests never make a real HTTP call. A lane whose probe is off is `ok` but `verified=false` — see `MO_LANE_REQUIRE_VERIFICATION`. |
| `MO_APPLY_IMPL_OUTPUT` | `"1"` | Text-fallback capture: parse implementer output for a diff/fenced blocks when it applied nothing |
| `MO_LEARNING_WRITEBACK` | `"1"` | GRPO advantage writeback after runs |
| `MO_PRM_SCORE` | `"1"` | Process-reward heuristic scoring on traces |
| `MO_REWARD_ANCHOR` | `"0.5"` | Reward anchor for the graded stamp normalization |
| `MO_REWARD_STAMP` | `"1"` | Stamp reward on execution traces |
| `MO_ARTIFACT_REWARD` | `"1"` | Post-run: re-stamp each successful implementer trace with the pass rate of the run's `verifier_results` rows recorded during that attempt (`reward_source='artifact@v1'`). `0` keeps the exit-code stamp |
| `MO_HEARTBEAT_TIMEOUT_S` | `"300"` | Stale-heartbeat watchdog threshold |
| `MO_DISPATCH_TIMEOUT` | `"1500"` | Dispatch-level timeout (trace store) |
| `MO_MAX_TRANSCRIPT_BYTES` | `"1048576"` | Transcript cap per node |
| `MO_NODE_PROMPT_SHA` | *(flag)* | Record prompt sha in traces |
| `MO_TRAJECTORY_TTL_DAYS` | `30` | turn_jsonl retention; 0 disables the run-end prune |
| `MO_FALLBACK_CODING` | `"minimax,glm,deepseek_flash"` | Fallback lane chain for coding roles |
| `MO_FALLBACK_REVIEW` | `"opus,glm,sonnet"` | Fallback lane chain for review roles |
| `MO_FALLBACK_STOP_ON_CALLER_FAULT` | `"0"` | `1`: a `caller_fault` failure (prompt too long, a rejected request) stops the fallback walk instead of trying the next lane. Default walks on, since a prompt too long for one lane can fit another. Each failed `llm_calls` row records its typed class in `metadata_json.failure_class` (`mini_ork/dispatch/failure_class.py`); only a run-wide fault (a missing cwd, a confused target) always stops the walk, and its row is marked `:refused` so it never counts as a fuse strike |
| `MO_DISPATCH_RETRY_MAX_SLEEP_S` | `"45"` | Cap on one retry's sleep. A provider that asks for a longer wait (`Retry-After`, `try again in …`, `try again at …`) is not retried on the same chain; the walk moves on |
| `MO_LANE_AFFINITY` | `"1"` | A node's later rounds try first the lane that served its first success (`<run_dir>/.lane-affinity.json`), while the routing lead is unchanged; an operator remap always wins and a fused lane stays dropped. `0` disables |
| `MO_MOCK_SEED` | *(unset)* | Seed for the probabilistic rules of `kind: mock` lanes; a lane's own `env.MO_MOCK_SEED` wins. See [mock-provider.md](mock-provider.md) |
| `MINI_ORK_LEASE_TOKEN` | *(flag)* | Single-writer lease token passed through recovery |
| `MINI_ORK_RECOVERY_CLOSURE` | `""` | Recovery closure node set (set by `mini-ork recover`) |
| `MINI_ORK_WORKFLOW_VERSION_ID` | *(flag)* | Pin the workflow version stamped in traces |
| `MINI_ORK_NODE_DESC` | `"implementer"` | Node description used in publisher commit messages |

## Verification stack

ON by default; set a master switch to `"0"` to disable it. See
[the verification stack](../architecture/verification-stack.md) for what each
feature decides and the evidence it writes.

| Variable | Default | Effect |
|---|---|---|
| `MO_ASSAY_RELATIONS` | `"1"` | Metamorphic-relations term in the certify oracle (veto a would-be PROVEN on a broken relation) |
| `MO_ASSAY_RELATIONS_RESCUE` | `"1"` | Relations may PROVE the no-informative-invariant case (≥2 held, ≥1 repaired, 0 violations) |
| `MO_ASSAY_RELATIONS_K` | `3` | Relations per judgement (clamped 1–5) |
| `MO_ASSAY_RELATIONS_VETO_MIN` | `1` | Attributed violations needed to veto |
| `MO_ASSAY_DIFFERENTIAL` | `"1"` | Differential base-vs-patch term (veto on confirmed collateral divergence) |
| `MO_ASSAY_DIFFERENTIAL_N` | `6` | Shared inputs per judgement (clamped 2–8) |
| `MO_ASSAY_DIFFERENTIAL_VETO_MIN` | `2` | Confirmed `preserve` divergences needed to veto |
| `MO_SUITE_ADEQUACY` | `"1"` | Mutation kill-rate audit of the code-fix suite; INADEQUATE/UNVERIFIED downgrade a green |
| `MO_SUITE_ADEQUACY_MAX_MUTANTS` | `12` | Mutant cap (1–50) |
| `MO_SUITE_ADEQUACY_MIN_SCORE` | `0.6` | Kill-rate threshold for ADEQUATE |
| `MO_SUITE_ADEQUACY_TIMEOUT_S` | `300` | Per-run suite timeout during the audit |
| `MO_LEVEL_VECTOR` | `"1"` | Five-level verdict vector + publisher gate (publish only when every required level is PROVEN) |
| `MO_ALLOW_UNPROVEN_TARGET` | `"0"` | `1` publishes a run whose `target` level is unproven **because its instrument was deliberately switched off** (e.g. `MO_CODEFIX_REPLAY=0`). The verifier records the switch as `target_waiver`, and the verdict's `levels_reasons.target` says it was waived; without this flag the run still abstains, now with a reason that names the switch and the remedy. Never applies to a failing suite |
| `MO_PROBE_VALIDITY` | `"0"` | `0` off · `1` enforce · `shadow` evaluate + record only (`probe-validity.json` `would_block`/`reasons`, a `[shadow] would block: …` task_runs note; never blocks, prints nothing; counted by `mini-ork metrics sdd`). I1 pre-publish probe-validity gate (`mini_ork/verify/probe_validity.py`): refuses a run whose verify proved nothing (no verifier node ended `done` with a `verifier_*` evidence file reporting `pass: true`), whose probes are aliased across acceptance criteria, or whose probes already pass on the untouched base tree (`pre-implementer-ref`). Reason in `probe-validity.json` + task_runs notes. Default OFF until an n≥30 A/B against the K1 frozen baseline (`kickoffs/sdd-mechanisms/roadmap.md`) |
| `MO_EVIDENCE_LEDGER` | `"0"` | `0` off · `1` enforce · `shadow` evaluate + record only (`evidence-ledger-gate.json` `would_block`/`reasons`, a `[shadow] evidence ledger would block: …` task_runs note; never blocks, prints nothing). I5 pre-publish evidence-ledger gate (`mini_ork/verify/evidence_ledger.py`): refuses a run whose `evidence-ledger.jsonl` holds no valid (current-tree) row reporting `pass`, a valid `fail` row for the same `ac_id`, or an `implementer-summary.json` claim of implementation while the tree still equals `pre-implementer-ref`. Rows are bound to the working-tree hash (`tree_hash`) and are void once the tree moves; the generic verifier handler and the SDD ledger-writer append rows. Default OFF |
| `MO_KICKOFF_GUARD` | `"shadow"` | A run must not rewrite its own contract (`mini_ork/verify/kickoff_guard.py`). The kickoff is snapshotted before any node runs (`kickoff_sha256` + `kickoff_snapshot` in `context-pack.v2.json`); at publish the file is compared. `off` · `shadow` record `kickoff-guard.json` (`intact` / `modified` / `deleted` / `no_snapshot`, with a diff) and a `[shadow] kickoff guard would block: …` task_runs note, never block · `on` refuse the publish and restore the kickoff from the snapshot so the edited contract is never committed. `no_snapshot` (context v2 off, older runs) passes as unverified |
| `MO_REVIEW_ROUND_AWARE` | `"off"` | `on`: the plain reviewer is told which attempt it is judging (`## Review attempt N of M`), that a fail/needs_revision on the FINAL attempt discards the delivery, to block only on HIGH-severity findings (medium/low → pass and list them), and, from attempt 2, to judge the previous attempt's findings first (they are included). The arm and attempt are recorded in `<run>/review-round-aware.json`. Changes what gets approved, so default OFF until an n≥30 comparison |
| `MO_REVIEW_ROUND_AWARE_HOLDOUT` | `"0.2"` | Under `MO_REVIEW_ROUND_AWARE=on`, the deterministic share of runs (by run id, salted independently of context v2) that keep the plain reviewer |
| `MO_REVIEW_CONVERGE` | `"on"` | `off`: the classic reviewer gets no earlier-findings block or "changes since your last review" delta (the findings ledger `revise/findings.json` is still written). `mini-ork metrics revise` reports the rounds-to-pass distribution |
| `MO_GATE_HACKABILITY_N` | `4` | Proposer documents per gate in `gate-fuzz --hackability` (0–16) |
| `MO_GATE_HACKABILITY_BUDGET_USD` | `0.50` | Per-audit proposer budget |
| `MO_GATE_HACKABILITY_MAX` | `0.25` | Promotion refuses a gate measured above this |
| `MO_PROMOTION_GATE_HACKABILITY` | `"1"` | Promotion-gate consumer of the hackability records |
| `MO_BEHAV_EQUIVALENCE` | *(unset → `exact`)* | Behavioral comparison operator: `exact`, `set`, `canonical`, `tolerant`, or a JSON spec |
| `MO_BEHAV_EXPECT_BODY` | *(unset)* | Expected body (JSON) for the behavioral `expect_body` check |
| `MO_ORACLE_INTEGRITY` | `"1"` | Promotion consumer of the oracle-integrity audit; a failed latest record → `quarantined` (`oracle_integrity_failed`) |
| `MO_JUDGE_VALIDITY` | *(unset → off)* | `1` runs the reviewer-vs-execution agreement audit at promotion; a measured lane below the floor → `quarantined` (`judge_validity_below_threshold`) |
| `MO_JUDGE_VALIDITY_MIN` | `0.80` | Agreement floor; tighten-only (lower values clamp up) |
| `MO_JUDGE_VALIDITY_ROWS` | `200` | History rows audited per promotion check |
| `MO_REWARD_EVIDENCE_PAIRING` | `"1"` | `1` drops positive rewards execution contradicts; `shadow` records and enqueues without dropping; `0`/`off` restores pre-pairing |
| `MO_REWARD_DIVERGENCE_ENQUEUE_LIMIT` | `50` | Max reward-evidence divergences enqueued per writeback call |
| `MO_VERIFIER_MUTATION` | `"1"` | Mutation-discrimination scoring of verifier cards; measured-below cards are dropped from ranking |
| `MO_VERIFIER_MUTATION_MIN` | `0.8` | Minimum kill score for a verifier card |
| `MO_ASSAY_DISCRIMINATION` | `"1"` | Candidates-discrimination term in the certify oracle |
| `MO_DISCRIMINATION_MIN` | `1.0` | Required discrimination rate for a generated test |
| `MO_CONFORMAL_ORACLE` | `"0"` | Split-conformal abstention gate; OFF by default |
| `MO_CONFORMAL_ALPHA` | `0.10` | Conformal risk level; tighten-only |
| `MO_CONFORMAL_MIN_CALIB` | `30` | Minimum calibration rows; raise-only |
| `MO_ASSAY_HIDDEN` | `"1"` | Hidden regenerated PoC trials in the certify oracle |
| `MO_HIDDEN_TRIALS` | `2` | Hidden trials per judgement; a malformed value raises |
| `MO_HIDDEN_CHECK_SECRET` | *(unset)* | Key material for hidden-check variants; absent → `run_id`, then patch digest (recorded in `key_source`) |

### Verifier scoping (code-fix)

`recipes/code-fix/verifiers/typecheck.py` narrows a **detected** bare compiler
(`tsc`, `mypy`) to the run's own change surface, so a repo with pre-existing
diagnostics cannot redden a lane for a reason the patch did not cause. The
touched set is the child's working tree ∪ untracked ∪ `pre-implementer-ref…HEAD`
(falling back to `git merge-base HEAD origin/main`, then `main`) — never
`origin/main` alone, which drags in other sessions' commits once it moves.

| Variable | Default | Effect |
|---|---|---|
| `MINI_ORK_TYPECHECK_CMD` | *(unset → auto-detect)* | Explicit command. Run **verbatim** — the operator owns its scope |
| `MINI_ORK_TYPECHECK_FULL` | *(unset)* | `"1"` forces the unscoped whole-project run for a detected compiler |
| `MINI_ORK_TOUCHED_FILES` | *(exported to the child)* | Newline-separated repo-relative paths this run changed; a `MINI_ORK_TYPECHECK_CMD`/`MINI_ORK_TEST_CMD` script can scope itself by reading it |

## Planning / profile

| Variable | Default | Effect |
|---|---|---|
| `MINI_ORK_PLAN_CONFIDENCE_FLOOR` | `"0.7"` | Below this plan confidence the run is refused |
| `MO_PLAN_DETERMINISTIC_FALLBACK` | `"0"` | Force the deterministic recipe fallback plan |
| `MO_FORCE_RECIPE_FALLBACK_PLAN` | `"0"` | Skip the LLM and use the recipe fallback plan |
| `MO_PLAN_MAX_REPAIRS` | `"2"` | Plan-repair attempts before failing |
| `MO_GIVEN_PLAN` | `""` | Supply a plan directly (skip planning) |
| `MO_INJECT_LEARNINGS` | `"1"` | Inject learned failure modes into planner prompts |
| `MINI_ORK_PROFILE_STRICT` | `"0"` | Block planning when the profile has open questions |

## Routing / learning loop

| Variable | Default | Effect |
|---|---|---|
| `MINI_ORK_OBJECTIVE_DOMAIN` / `MO_OBJECTIVE_DOMAIN` | *(flag)* | Objective-domain slice for the learning router (either spelling) |
| `MO_LANE_ROUTER` | `"1"` | Learning-governed lane routing |
| `MO_ROUTER_CONTEXTUAL` | `"0"` | Contextual (slice-aware) bandit routing |
| `MO_ROUTER_PER_NODE_CREDIT` | `"0"` | Per-node credit assignment in the router |
| `MO_ROUTER_PER_NODE_CREDIT_GAMMA` | `"1.0"` | Credit-assignment decay |
| `MINI_ORK_GRADIENT_EXTRACTOR_FN` | `""` | Override gradient extraction function |
| `MINI_ORK_GRADIENT_TIMEOUT_S` | `"300"` | Timeout of one gradient-extraction dispatch (was a fixed 120 s, which cut a quarter of the calls just before they finished). A non-numeric, zero or negative value falls back to 300 with a stderr warning, never to no cap |
| `MO_REFLECTION_EXTRACT_GRADIENTS` | `"1"` | Gradient extraction during reflect |
| `MO_GRADIENT_DEDUP_SIM` | `"0.65"` | Gradient dedupe similarity threshold |
| `MO_DEDUP_BATCH` | `10000` | Dedupe batch size |
| `MO_DEDUP_FUZZY` | `0.55` | Fuzzy-merge ratio (difflib) |
| `MO_REFLECTION_BATCH` | `"25"` | Reflect batch size (also set internally by the run loop) |
| `MINI_ORK_STALE_DAYS` | `14` | Stale-memory detection window |
| `MINI_ORK_PROMOTION_MIN_FREQ` | `3` | Min frequency before a pattern is promoted |
| `MO_PATTERN_MINER` | `"1"` | Pattern mining in reflect |
| `MO_PATTERN_MINER_MIN_CLUSTER` | `"3"` | Min cluster size for patterns |
| `MO_PATTERN_MINER_WINDOW` | `"7d"` | Pattern mining window |
| `MO_CROSS_EPIC_GRADIENTS` | `"1"` | Cross-epic gradient side-channel in reflect |
| `MO_CROSS_EPIC_MIN_CLASSES` | `"2"` | Min task classes for a cross-epic gradient |
| `MO_CROSS_EPIC_MIN_CONF` | `"0.7"` | Min confidence for cross-epic promotion |
| `MO_CROSS_EPIC_WINDOW` | `"14d"` | Cross-epic window |
| `MO_EMERGENT_INJECT` | `"1"` | Inject emergent patterns into context packs (confabulation guard) |
| `MO_EMERGENT_INJECT_LIMIT` | `"3"` | Max emergent patterns injected |
| `MO_EMERGENT_VERIFY` | `"1"` | Judge-verify emergent patterns before promotion |
| `MO_EMERGENT_VERIFY_MIN_STRENGTH` | `"3"` | Min strength score for verification |
| `MO_EMERGENT_VERIFY_MIN_EVIDENCE` | `"1"` | Min member-evidence count |
| `MO_RHO_AGGREGATE` | `"1"` | Rho aggregation in reflect |
| `MO_BUG_REPORT_SWEEP` | `"1"` | Bug-report sweep in reflect |
| `MO_BUG_REPORT_AUTO_PROMOTE` | `"0"` | Auto-promote swept bug reports |

## Orchestration / conductor / scheduler

| Variable | Default | Effect |
|---|---|---|
| `MINI_ORK_RECURSIVE_MAX_DEPTH` | `"2"` | Recursive spawn depth cap |
| `MINI_ORK_RECURSIVE_MAX_CHILDREN` | `"4"` | Children per recursive node |
| `MINI_ORK_RECURSIVE_MAX_DESCENDANTS` | `"16"` | Total recursive descendants cap |
| `MINI_ORK_RECURSIVE_MAX_PARALLEL` | `"4"` | Parallel recursive branches |
| `MO_CONDUCTOR_ADAPTIVE_GAIN` | `"1"` | Adaptive conductor gain from outcomes |
| `MO_CONDUCTOR_GAIN_MIN_SAMPLES` | `"3"` | Min samples before adaptive gain engages |
| `MO_SCHED_MAX_PARALLEL` | `"3"` | Scheduler epic parallelism |
| `MO_REVIEW_PANEL` | `"glm deepseek_flash"` | Pre-push review LLM panel lanes (the two families live on 2026-10-26; minimax, kimi and codex failed their probes). A high LLM finding blocks only when two lenses flag the same file |
| `MO_REVIEW_LENS_TIMEOUT_S` | `"600"` | Per-lens review timeout (glm needed 179 s on a 15 kB diff, so 180 s timed out under load) |
| `MO_REVIEW_LLM_LENSES` | `"0"` | `1` runs the LLM panel with the heuristics (hybrid mode) |
| `MO_REVIEW_SKIP` | `"0"` | `1` skips the pre-push code review; a human decision |
| `MO_ORACLE_GATES_AUTO` | `"1"` | Auto oracle-gate run pre-publish |
| `MO_NODE_HEARTBEAT_SECONDS` | `"60"` | How often a running node writes a `node_heartbeat` row |
| `MO_ORCH_WATCH` | `"1"` | `0` stops `mini-ork run` from spawning the run's interval watcher |
| `MO_ORCH_WATCH_INTERVAL_S` | `"20"` | Seconds between watcher ticks |
| `MO_ORCH_WATCH_AUTONOMY` | `"steer"` | `steer` \| `notify` \| `off` — whether a stall is nudged or only signalled |
| `MO_ORCH_WATCH_MAX_STEERS` | `"2"` | Nudges per run |
| `MO_ORCH_WATCH_MAX_HOURS` | `"12"` | Wall-clock cap on the watcher's own lifetime |

The pre-push review is cached by `git patch-id` in
`$MINI_ORK_HOME/state/pre-push-review-cache.json`, and in the secure review
tier (`config/review.yaml` with `review: {tier: secure}`) a review that cannot
run blocks the push. A hybrid review in which no LLM lens ran prints a warning
and, in the secure tier, blocks the push.

### Node heartbeats (`MO_NODE_HEARTBEAT_SECONDS`)

A node is a **blocking in-process call** — verifiers park in `run_check`'s Popen,
agent lanes park in `dispatch.core` — so for the whole window it runs nothing
else in the run process is awake to say the run is still alive. `Heartbeat`
(`mini_ork/observability/heartbeat.py`) is that ticker: one `node_heartbeat` row
per interval, started in `dispatch_node` right after `mo_node_start` and stopped
in the same `finally` that releases the node's skill grants.

It writes a row and nothing else. No model call, no file, no read, so it cannot
contend with the run's own writers, and every failure is swallowed — a broken
heartbeat must never fail a node.

`mo_node_emit` sets `last_heartbeat_at` only for `node_start` and
`node_heartbeat` (migration 0023), which is what three existing readers consume
and previously starved for, because nothing emitted it:

* the reaper's newest-sign-of-life probe, `run_reaper._last_activity`
  (`MAX(last_heartbeat_at)`, used by `mini-ork reap --stale-after`);
* the run card's `beat`, rendered as "current node, last seen"
  (`mini_ork/ide_pages/runs.py`);
* `fleet`'s active-run filter (`mini_ork/web/routes/fleet.py`).

It is also the only signal a **hung** node produces, which is what makes a stall
observable at all: a per-run orchestrator watches these rows to notice one (see
*The per-run watcher* below).

**Simple tier.** Nothing to configure — heartbeats are always on, and the
default interval (60 s) leaves a six-hour stale window covered with room to
spare. A missing, unparseable or non-positive `MO_NODE_HEARTBEAT_SECONDS` falls
back to the default rather than disabling the tick.

### The per-run watcher (`MO_ORCH_WATCH`)

The per-run orchestrator has two cadences. At node boundaries it decides
advance / help / repair / mutate / promote in-process (`on_node_boundary`).
Between boundaries a detached plain-Python process per run (`mini-ork
orchestrator watch <run_id>`) samples the live node on an interval and raises
trouble. Both cadences live in `mini_ork/orchestration/runner_orchestrator.py`
and share the run's `MO_RUN_ORCHESTRATOR` switch, `MO_ORCH_BUDGET_USD` budget
and `guard_steer` check, so there is exactly one orchestrator per run.

It makes **zero model calls**: it reads `run_events`, the node log's mtime and
`<run>/.pid`, and writes `orchestrator.watching` / `.signal` / `.steered` /
`.ended` rows. Detection is the ACP thread's own sampler
(`mini_ork/acp_orchestrator/watch.py`), so its thresholds apply here too —
`MO_WATCH_STALL_SECONDS` (300), `MO_WATCH_LONG_NODE_SECONDS` (900) and
`MO_WATCH_COST_USD` (1.0). Kinds: `stalled`, `repeating_error`,
`duration_overrun`, `cost_overrun`.

Autonomy (`MO_ORCH_WATCH_AUTONOMY`, default `steer`):

* `steer` — on `stalled` or `repeating_error`, write one `operator_steering`
  row for the live node (severity `warn`, source `orchestrator-watch`, 30 min
  TTL), capped by `MO_ORCH_WATCH_MAX_STEERS`. Duration and cost overruns are
  never steered: they are statements about the run, not about the node.
* `notify` — write the signal rows only. The run page and the attention strip
  still show them.
* `off` — the watcher exits at startup, before its first tick.

Four things the watcher never does: it never writes `<run>/.pid` (a watcher pid
there would mask a dead dispatcher), never writes `task_runs.status` (the
reaper owns that), never sets `last_heartbeat_at` (only `node_start` and
`node_heartbeat` do), and never changes the run's exit code. A crash leaves a
traceback in `<run>/orchestrator-watch.log` and nothing else.

It exits on: the run reaching a terminal status; the dispatcher's pid going
dead; `.pid` missing for two ticks; `MO_ORCH_WATCH_MAX_HOURS`; or the flag
being turned off. A `remote` or `paused` run is idled rather than exited — a
remote run's local logs are not its logs, and a paused run is not stalled.

**Simple tier.** Nothing to configure. The defaults are the safe ones, the
spawn is best-effort (a failure prints one stderr line and the run continues),
and `--once` runs a single tick for a seeded run dir without any daemon. Two
watchers on one run cannot both run: a startup flock makes the second a no-op.

**Secure tier.** The child is spawned with `start_new_session=True`, its own
run-scoped environment (`MINI_ORK_RUN_ID`, `MINI_ORK_DB`, …), and node identity
removed (`MO_NODE_ID`, `MO_NODE_TYPE`, `CONCORD_PRINCIPAL`, `MINI_ORK_DRY_RUN`).
An explicit `--home` that holds neither `state.db` nor `config/` fails closed
with rc 3 rather than falling back to the working directory.

### Recipe-declared loop caps (`MO_RECURSION_*`)

These two are **derived, not set by hand**. When a recipe's `workflow.yaml`
declares a `recursion:` block, the executor reads it at dispatch and publishes
it under these names; the recipe's driver resolves its caps from them. For a
recipe that declares a block, editing the YAML is the supported way to change
its bounds — dispatch republishes these on every run and overwrites whatever
was in the environment. A recipe with no block publishes nothing, so its driver
keeps its own historical defaults.

| Variable | Driver fallback | Source in `workflow.yaml` |
|---|---|---|
| `MO_RECURSION_MAX_ITERATIONS` | `"30"` | `recursion.max_iterations` |
| `MO_RECURSION_BUDGET_CAP_TOTAL_USD` | `"150.0"` | `recursion.budget_cap_total_usd` |

A `recursion:` block now declares `max_iterations` and `budget_cap_total_usd`
only; the three keys nothing read were removed in 2026-10.

The fallbacks above apply only when no recipe declares a block, or when a
caller passes nothing and no recipe governs the loop. A partial `recursion:`
block fails `mini-ork validate` rather than silently half-applying; declare
both keys or none.

## Context / memory

| Variable | Default | Effect |
|---|---|---|
| `MINI_ORK_CTX_BUDGET_TOKENS` | `"64000"` | Context-pack token budget |
| `MO_CONTEXT_V2` | `"shadow"` | Context selected by the run's files in scope and the kickoff contract (`mini_ork/context_v2.py`) instead of by task class. `off` · `shadow` build + record `context-pack.v2.json`, inject nothing new · `on` the planner gets the v2 block in place of the three task_class blocks (failure modes, prior runs, graph context) and researcher/implementer/reviewer nodes get it after the operator preferences. The planner's exact injected text is always recorded in `learned/planner.md` + `.json`. Measured by `mini-ork metrics context` |
| `MO_CONTEXT_V2_HOLDOUT` | `"0.2"` | Under `on`, the deterministic share of runs (by run id) that keep the v1 context, so `metrics context` can compare recurrence of injected problems (`lift` = holdout − v2) |
| `MO_CONTEXT_V2_MIN_SEVERITY` | `"medium"` | Lowest finding severity a recurring-problem group needs to be injected (`low` lets harness remarks through) |
| `MINI_ORK_SLICE_PROVIDER` | `"default"` | Context slice provider selection |
| `MO_SEMANTIC_MODEL` | `"haiku"` | Semantic-memory helper model |
| `MO_EMBED_PROVIDER` | `""` | Embedder provider (see `register_embedder_provider`) |

## Web / observability

| Variable | Default | Effect |
|---|---|---|
| `MINI_ORK_ACTIVE_STALE_SECONDS` | `21600` (6h) | Fleet view staleness threshold |
| `MINI_ORK_RUN_STALE_SECONDS` | `"1800"` | Run-detail staleness threshold |
| `MINI_ORK_PROJECTS_FILE` | *(flag)* | Project registry file for the switcher |
| `MINI_ORK_ON_EVENT` | `""` | A command (split with shlex, never a shell) run for each node and run event as `<cmd> <event_type> <run_id> <payload_json>`; non-blocking, killed after 5 s. The no-server tier of event push (the served tier is SSE on `mini-ork serve`). See [EVENT-HOOKS.md](../EVENT-HOOKS.md) |
| `MINI_ORK_ON_EVENT_HEARTBEATS` | `"0"` | `1` also sends heartbeat events to `MINI_ORK_ON_EVENT` |
| `MINI_ORK_ON_EVENT_MAX_INFLIGHT` | `"8"` | Hook processes allowed at once; an event that arrives while the cap is reached is dropped |

## The wizard and the controlled-feature registry

A **controlled feature** is a runtime behavior a user may enable, disable, or
tune per run. Instead of three hand-maintained lists, one registry describes
them all:

* `mini_ork.features.registry` — the source of truth. Each entry is a
  `Feature` (env knobs, a value kind, and a cost model) registered with
  `register_feature(...)`.
* `mini-ork features [--json] [--recipe <r>]` — print the catalogue for a human
  or a script.
* `/wizard` in a mini-ork Thread — walks a user through recipe → features →
  plan → start, showing each feature and its projected cost.

Add a feature once, in the registry, and it appears on all three surfaces. The
skill file `skills/wizard/SKILL.md` carries a generated block between the
`<!-- BEGIN GENERATED:features -->` / `<!-- END GENERATED:features -->`
sentinels; `mini-ork features check-skill` exits non-zero when that block no
longer matches the registry. A feature that is registered but not re-rendered
is a **red gate**, not a silent omission — that is what makes "new features are
added to the wizard automatically" a guarantee rather than a habit. Run
`mini-ork features render-skill` to resync.

### Premium features (the cost gate)

A feature whose own multiplier exceeds **1.5×** is *premium*. Premium features
are off for a wizard-launched run unless the user explicitly opts in
(`MO_ACCEPT_PREMIUM=1`), so a stray click cannot triple a bill; baseline
features (≤1.5×) keep their existing defaults. The check runs **server-side**,
in `mini_ork.web.control.launch_run`, and the response reports what was dropped
as `blocked_premium` — a caller cannot slip a premium knob past it by posting
raw env to `POST /api/v1/runs`.

### Two tiers

Both tiers carry the same selection; they differ in where it is stored and who
may set it.

**Simple tier — one laptop.** No server, daemon, or cloud account. The
selection is passed as per-run env overrides (via `/wizard`, or by hand in
`config/secrets.local.sh` / the process env). `mini-ork features` needs only
the standard library. It fails closed: `gate_env` removes premium knobs unless
`MO_ACCEPT_PREMIUM=1`, off features contribute nothing, and an unknown feature
id is ignored rather than guessed — a missing value is never a silent empty
string.

**Secure tier — a shared or hostile deployment.** The run is launched through
`mini-ork serve` behind Bearer auth (`mini_ork.web.auth.require_token`), and
which features a node may use is narrowed by scoping — the run-scoped
`config/skills/` directory and per-node provider config (see `docs/CONFIG.md`)
decide which features a node is even offered. The registry holds no secrets and
reads only stdlib; the premium gate is enforced in the launch path, not in the
UI, so it holds when the caller is another process rather than the Thread. One
config file chooses the tier, so a user starts simple and switches without
rewriting call sites.
