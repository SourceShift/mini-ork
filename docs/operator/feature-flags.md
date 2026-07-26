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

## Context / memory

| Variable | Default | Effect |
|---|---|---|
| `MINI_ORK_CTX_BUDGET_TOKENS` | `"64000"` | Context-pack token budget |
| `MINI_ORK_SLICE_PROVIDER` | `"default"` | Context slice provider selection |
| `MO_SEMANTIC_MODEL` | `"haiku"` | Semantic-memory helper model |
| `MO_EMBED_PROVIDER` | `""` | Embedder provider (see `register_embedder_provider`) |

## Web / observability

| Variable | Default | Effect |
|---|---|---|
| `MINI_ORK_ACTIVE_STALE_SECONDS` | `21600` (6h) | Fleet view staleness threshold |
| `MINI_ORK_RUN_STALE_SECONDS` | `"1800"` | Run-detail staleness threshold |
| `MINI_ORK_PROJECTS_FILE` | *(flag)* | Project registry file for the switcher |
