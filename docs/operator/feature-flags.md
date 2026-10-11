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
| `MO_APPLY_IMPL_OUTPUT` | `"1"` | Text-fallback capture: parse implementer output for a diff/fenced blocks when it applied nothing |
| `MO_LEARNING_WRITEBACK` | `"1"` | GRPO advantage writeback after runs |
| `MO_PRM_SCORE` | `"1"` | Process-reward heuristic scoring on traces |
| `MO_REWARD_ANCHOR` | `"0.5"` | Reward anchor for the graded stamp normalization |
| `MO_REWARD_STAMP` | `"1"` | Stamp reward on execution traces |
| `MO_HEARTBEAT_TIMEOUT_S` | `"300"` | Stale-heartbeat watchdog threshold |
| `MO_DISPATCH_TIMEOUT` | `"1500"` | Dispatch-level timeout (trace store) |
| `MO_MAX_TRANSCRIPT_BYTES` | `"1048576"` | Transcript cap per node |
| `MO_NODE_PROMPT_SHA` | *(flag)* | Record prompt sha in traces |
| `MO_TRAJECTORY_TTL_DAYS` | `30` | turn_jsonl retention; 0 disables the run-end prune |
| `MO_FALLBACK_CODING` | `"minimax,codex,sonnet"` | Fallback lane chain for coding roles |
| `MO_FALLBACK_REVIEW` | `"opus,kimi,sonnet"` | Fallback lane chain for review roles |
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
| `MO_PROBE_VALIDITY` | `"0"` | `0` off · `1` enforce · `shadow` evaluate + record only (`probe-validity.json` `would_block`/`reasons`, a `[shadow] would block: …` task_runs note; never blocks, prints nothing; counted by `mini-ork metrics sdd`). I1 pre-publish probe-validity gate (`mini_ork/verify/probe_validity.py`): refuses a run whose verify proved nothing (no verifier node ended `done` with a `verifier_*` evidence file reporting `pass: true`), whose probes are aliased across acceptance criteria, or whose probes already pass on the untouched base tree (`pre-implementer-ref`). Reason in `probe-validity.json` + task_runs notes. Default OFF until an n≥30 A/B against the K1 frozen baseline (`kickoffs/sdd-mechanisms/roadmap.md`) |
| `MO_EVIDENCE_LEDGER` | `"0"` | `0` off · `1` enforce · `shadow` evaluate + record only (`evidence-ledger-gate.json` `would_block`/`reasons`, a `[shadow] evidence ledger would block: …` task_runs note; never blocks, prints nothing). I5 pre-publish evidence-ledger gate (`mini_ork/verify/evidence_ledger.py`): refuses a run whose `evidence-ledger.jsonl` holds no valid (current-tree) row reporting `pass`, a valid `fail` row for the same `ac_id`, or an `implementer-summary.json` claim of implementation while the tree still equals `pre-implementer-ref`. Rows are bound to the working-tree hash (`tree_hash`) and are void once the tree moves; the generic verifier handler and the SDD ledger-writer append rows. Default OFF |
| `MO_KICKOFF_GUARD` | `"shadow"` | A run must not rewrite its own contract (`mini_ork/verify/kickoff_guard.py`). The kickoff is snapshotted before any node runs (`kickoff_sha256` + `kickoff_snapshot` in `context-pack.v2.json`); at publish the file is compared. `off` · `shadow` record `kickoff-guard.json` (`intact` / `modified` / `deleted` / `no_snapshot`, with a diff) and a `[shadow] kickoff guard would block: …` task_runs note, never block · `on` refuse the publish and restore the kickoff from the snapshot so the edited contract is never committed. `no_snapshot` (context v2 off, older runs) passes as unverified |
| `MO_REVIEW_ROUND_AWARE` | `"off"` | `on`: the plain reviewer is told which attempt it is judging (`## Review attempt N of M`), that a fail/needs_revision on the FINAL attempt discards the delivery, to block only on HIGH-severity findings (medium/low → pass and list them), and, from attempt 2, to judge the previous attempt's findings first (they are included). The arm and attempt are recorded in `<run>/review-round-aware.json`. Changes what gets approved, so default OFF until an n≥30 comparison |
| `MO_REVIEW_ROUND_AWARE_HOLDOUT` | `"0.2"` | Under `MO_REVIEW_ROUND_AWARE=on`, the deterministic share of runs (by run id, salted independently of context v2) that keep the plain reviewer |
| `MO_GATE_HACKABILITY_N` | `4` | Proposer documents per gate in `gate-fuzz --hackability` (0–16) |
| `MO_GATE_HACKABILITY_BUDGET_USD` | `0.50` | Per-audit proposer budget |
| `MO_GATE_HACKABILITY_MAX` | `0.25` | Promotion refuses a gate measured above this |
| `MO_PROMOTION_GATE_HACKABILITY` | `"1"` | Promotion-gate consumer of the hackability records |
| `MO_BEHAV_EQUIVALENCE` | *(unset → `exact`)* | Behavioral comparison operator: `exact`, `set`, `canonical`, `tolerant`, or a JSON spec |
| `MO_BEHAV_EXPECT_BODY` | *(unset)* | Expected body (JSON) for the behavioral `expect_body` check |

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
| `MO_REVIEW_PANEL` | `"codex kimi glm"` | Pre-push review LLM panel lanes |
| `MO_REVIEW_LENS_TIMEOUT_S` | `"180"` | Per-lens review timeout |
| `MO_ORACLE_GATES_AUTO` | `"1"` | Auto oracle-gate run pre-publish |
| `MO_OPTIMIZER_MODEL` | `"minimax"` | GEPA optimizer lane |
| `MO_OPTIMIZER_BUDGET` | `"4"` | GEPA optimizer budget |

### Recipe-declared loop caps (`MO_RECURSION_*`)

These five are **derived, not set by hand**. When a recipe's `workflow.yaml`
declares a `recursion:` block, the executor reads it at dispatch and publishes
it under these names; the recipe's driver resolves its caps from them. For a
recipe that declares a block, editing the YAML is the supported way to change
its bounds — dispatch republishes these on every run and overwrites whatever
was in the environment. A recipe with no block publishes nothing, so its driver
keeps its own historical defaults.

| Variable | Driver fallback | Source in `workflow.yaml` |
|---|---|---|
| `MO_RECURSION_MAX_ITERATIONS` | `"30"` | `recursion.max_iterations` |
| `MO_RECURSION_CONVERGENCE_CHECK` | *(unset)* | `recursion.convergence_check` |
| `MO_RECURSION_BUDGET_CAP_PER_ITER_USD` | *(unset)* | `recursion.budget_cap_per_iter_usd` |
| `MO_RECURSION_BUDGET_CAP_TOTAL_USD` | `"150.0"` | `recursion.budget_cap_total_usd` |
| `MO_RECURSION_DIVERGENCE_KILL` | *(unset)* | `recursion.divergence_kill` |

The fallbacks above apply only when no recipe declares a block, or when a
caller passes nothing and no recipe governs the loop. A partial `recursion:`
block fails `mini-ork validate` rather than silently half-applying; declare all
five keys or none.

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
