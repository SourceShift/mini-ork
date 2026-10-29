# Changelog

All notable changes to mini-ork are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

## [0.10.0] - 2026-10-29

Headline: **more checks before a change lands, and runs that recover on their own.** A change
now passes a test-gaming scan, a pre-push code review and a risk-tiered merge gate in which
sensitive paths need a person's typed approval; `mini-ork reviewer-eval` scores the reviewer and
`mini-ork undo` reverts a landed commit through the same gates. Failed lane calls are classified
and acted on, a hung lane is cut at a first-output deadline, and a failed run repairs itself from
its failed node. Runs can be driven from a Zed thread or `mini-ork board`, and placed on remote
nodes or Daytona / E2B / Modal sandboxes. The verification stack is on by default.

### Added

- **Test-gaming scan**: `mini_ork.verify.gaming_scan` flags production code a change adds that
  probes the test harness (`PYTEST_CURRENT_TEST`, pytest imports, MonkeyPatch, frame walks, writes
  into another module's attributes). It runs by default as framework-edit static-check's
  `no-test-gaming` row (a kickoff `Allow-test-probe: <path>` line exempts a path) and in
  `make worktree-merge`. (961fe0ef, edb6b9b2, 08229cad)
- **Pre-push code review**: the pre-push hook runs the code review again (it had been skipping it
  since before v0.9.0) through `mini-ork review gate`, cached per `git patch-id`. Lenses are
  heuristic unless `MO_REVIEW_LLM_LENSES=1`, and the LLM review never runs inside the merge lock.
  The simple tier warns when the review cannot run; `tier: secure` in `config/review.yaml` blocks.
  Findings persist across pushes: `mini-ork review findings` and
  `mini-ork review resolve <fp> --as fixed|dismissed|pre_existing`. (f9a73858, 476e3648, fd5ce288,
  702123f9)
- **Merge risk tiers**: `make worktree-merge` classifies a branch against `config/risk-tiers.yaml`
  (enabled as shipped; the rules are read from origin/main, so a branch cannot loosen its own
  check). A high-tier change (frozen kernel, CI, git hooks, migrations, installers, vault and
  secrets, merge and approval code) merges only after `mini-ork approvals approve wt:<slug>`, typed
  at a TTY with the first 8 characters of the patch id. The merge driver also refuses paths outside
  the worktree's `--owns` claims and confidential terms or credential shapes
  (`MO_MERGE_ALLOW_UNCLAIMED=1`, `MO_MERGE_ALLOW_OSS=1`), and pushes under a lock with
  `MO_MERGE_PUSH_ATTEMPTS` (5) retries. (08229cad, bb308428, 64a4a42d)
- **Human review gates and `mini-ork approvals`** (off by default): with `before_merge: required`
  in `config/review.yaml`, a workspace merge waits for an approval bound to its exact content, and
  `mini-ork run` works in its own worktree (also selectable with `--workspace worktree|in-place`).
  `after_nodes` parks a run as `reviewing` (rc 9, no rollback) until
  `mini-ork approvals approve|request-changes <run> --node <id>`. (9c4a48f7, e71555a7, e27da337,
  a8dbc2dd)
- **`mini-ork undo <commit|rsi-cycle-id|run-id>`**: reverts a commit that landed on origin/main as
  a new `git revert` commit, through a claimed worktree, the green gate and the merge script's
  locked push. It never rewrites main and refuses unsafe targets (a merge commit without
  `--mainline`, a commit already reverted). Each undo writes an `undo` row to `audit_log`;
  `--dry-run` prints the plan. (27f04217)
- **`mini-ork reviewer-eval`**: scores the framework-edit reviewer on recorded incidents and clean
  landings (`evals/reviewer/cases/`), with the inputs a real run gives the reviewer. Verbs: `list`,
  `add-case`, `refresh-evidence`, `run`, `status`, `score`; `run --dry-run` spends nothing, and
  `status` exits 1 (and `mini-ork garden` warns) when the reviewer changed since its last score.
  (36715db8, 2340482d, 1ccebeb7)
- **Revise loop**: `retries` edges, which the compiler accepted but the runtime ignored, now send a
  failed verifier's or reviewer's findings back to the implementer for up to `max_rounds` rounds
  before rollback (`MO_REVISE_ROUNDS`, default 2; `0` restores the old behaviour); code-fix and
  framework-edit declare them. From round 2 the framework-edit reviewer sees its open findings
  (`revise/findings.json`) and only the changes since its last review (`MO_REVIEW_CONVERGE`, on).
  (2be82523, 547d0522)
- **Typed failure classes, a first-output deadline and lane affinity**: a failed lane call is
  classified (rate_limit, quota, credential, timeout, environment, ...) and one action table
  decides whether to retry, move to the next lane or stop. A streaming lane that is not last in its
  fallback chain is cut after `MO_DISPATCH_FIRST_OUTPUT_S` (default 300 s; `0` disables) with no
  model output. A node's later rounds start on the lane that served it while the routing lead is
  unchanged (`MO_LANE_AFFINITY=0` disables). The lane fuse is now on by default
  (`MO_FUSE_ENABLED=1`), keys on the lane that ran rather than the node's role, and ignores faults
  the lane did not cause. (437b2fa3, 7a9affcf, f6b762e0, edd0f1db, e3649b22)
- **Lane health gate before a run starts**: `mini-ork run` probes every lane its LLM nodes will use
  (native Anthropic lanes included) and refuses to start (rc 75) when one is unhealthy, naming the
  lane, role and node. `MO_LANE_HEALTH_GATE=0` skips it; a lane that could not be probed blocks
  only with `MO_LANE_REQUIRE_VERIFICATION=1`. `mini-ork validate` runs the same probe.
  (b3c80bb5, aaac165d, cbc6088b, 1172a9fd)
- **Per-node `retry:` and conditional `retries` edges**: a node that fails for an infra reason
  (timeout, provider limit, network, killed) is re-dispatched with LangGraph RetryPolicy defaults
  (3 attempts, exponential backoff with jitter) unless its `retry:` block says otherwise; a
  malformed block fails closed. A `retries` edge can carry a `condition:` (a JSON pointer into the
  node's output, a JSON Schema `match`, optional `negate`). (aadb712a, d6ebca30)
- **`kind: mock` provider**: a lane that answers from a YAML rules file (match on prompt regex,
  lane and attempt number; reply with text or a preset failure such as rate_limit, quota or
  timeout) with no network call or subprocess, for deterministic retry, fallback and fuse tests.
  See `docs/operator/mock-provider.md`. (f8dad712)
- **Auto-repair (on by default)**: a failed run is revived from its failed node: a lane switch for
  a dead lane, a re-run for an infra fault, a revise round for a code fault, otherwise a hand-off
  to a person. Capped by `MO_AUTO_REPAIR_MAX` (2) and `MO_AUTO_REPAIR_BUDGET_USD` (10);
  `MO_AUTO_REPAIR=0` turns it off, and `mini-ork repair <run> [--dry-run]` runs one decision.
  (42318604, 21306bf7, 331dc5c6, 6a812d5f)
- **Failure attribution and fix epics**: `mini-ork triage --run <id>|--latest [--promote]` (or
  `MO_FAILURE_TRIAGE=1`) attributes a failed run to mini-ork or to the consumer repo and can queue
  a fix epic. Auto-repair writes a typed, evidence-citing `diagnosis.json`; an engine fault opens a
  linked `framework-edit` epic and parks the run until the fix lands (`MO_AUTO_ESCALATE`, on),
  under a policy those epics cannot edit (`config/kernel-frozen.yaml`). (c94a9a29, 8932a011,
  1d88b72e, f36e372d)
- **`mini-ork supervise [--once|--interval S]`**: a sweep that revives runs parked on a fix epic
  once the fix lands on origin/main, marks failures that were delivered another way with a new
  `superseded` status, and applies proposed graph patches. Starting one ready epic per sweep is
  opt-in (`dispatch_epics.enabled`; needs a `target_root` outside the engine checkout).
  (f1f93760, a9cbcfc0, e15d8f42, c6de292b)
- **`mini-ork recover` options, retry hints and graph patches**: `--lane <alias>=<lane>` resumes
  from the failed node on another lane, `--strategy verify` re-runs only the verifier chain, and
  `--carry-patch` re-applies a rolled-back run's patch. A failed run's `retry-hint.json` names the
  failed node, the cause and the command to continue. `mini-ork graph-patch
  list|propose|validate|apply|credit` manages write-once patches (rerun, swap_lane, insert_before,
  replace, replan, split) applied through `recover --workflow`. (d0813766, e77fc90f, cfb33dee,
  7a85d5df)
- **Epic queue commands**: `mini-ork epics add --title T --kickoff FILE` queues a task,
  `epics status <id> <status> --note` corrects a row (cascading on `done`), and
  `epics archive <id> --note` retires one that will never run. `scheduler --epic ID` /
  `MO_SCHED_EPIC_FILTER` limits a pass to named epics, and `budget.per_epic_usd`, previously
  display-only, now stops an epic whose attempts reach it (`MO_PER_EPIC_USD` overrides).
  (446277b7, 35bc52d1, 69c1122d, 7aa2611f)
- **Per-run orchestrator, on by default**: on dependency-graph runs it reads each node's typed
  outcome at the node boundary and picks one action (advance, help with a guarded steering nudge,
  repair through a retries edge, mutate as a proposal only, or promote), recorded as an
  `orchestrator.<action>` run event; green nodes are skipped. Spend is capped by
  `MO_ORCH_BUDGET_USD` (default 1.0) against the run's ledger; `MO_RUN_ORCHESTRATOR=0` turns it
  off. (3a913a81, ec0edcd5, 0af700ca)
- **One orchestrator per run, on two cadences**: the per-run orchestrator keeps its node-boundary
  decisions and gains an interval cadence hosted by one detached, model-free process per run
  (`mini-ork orchestrator watch <run_id>`): every `MO_ORCH_WATCH_INTERVAL_S` (20 s) it samples the
  live node with the ACP thread's own detector, writes `orchestrator.watching` / `.signal` /
  `.steered` / `.ended` rows the run page already renders, and — on a stall, within
  `MO_ORCH_WATCH_MAX_STEERS` and `MO_ORCH_BUDGET_USD`, and after `guard_steer` — injects one
  operator-steering nudge. `MO_ORCH_WATCH_AUTONOMY` selects `steer` (default) / `notify` / `off`.
  Zero model calls, one OS process per run, never one per node. A live watcher suppresses the ACP
  thread's duplicate sampling; the attention strip now names a run whose watcher raised trouble or
  whose dispatcher died. See `docs/operator/feature-flags.md` and `docs/OPERATOR-STEERING.md`.
  (fae2a7a5, 3f1634b9, c204a836, 59589568)
- **Runs and slash commands in the Zed thread**: a run streams each agent's live output under its
  node, shows its workflow as Zed's live plan, puts its changes in Review Changes as per-file
  diffs, and titles the thread with its state (working, needs you, done, failed); Thread History
  reopens past runs and attaches to live ones. `/runs`, `/status`, `/cost`, `/lanes`, `/stop`,
  `/kill`, `/resume`, `/recover`, `/certify`, `/recipes`, `/automations` and others act directly;
  `/recipe new`, `/automation new`, `/kickoff` and `/task` show a draft card and create nothing
  until you click. (058a1fc7, 3b413bfe, 1aef1721, 70d5e08a)
- **Review and merge in Zed**: a thread's run works in its own git worktree and branch; a verified
  result arrives as Zed agent edits, otherwise the card offers Merge / Discard / Keep for later (a
  parked run: Approve / Request changes). `/race [lanes] <task>` runs one task on 2-3 models
  (`MO_RACE_LANES`, default sonnet, glm, minimax) and merges the one you keep. (8711b309, 54080b00,
  c942f63c, 5f073f9f)
- **`mini-ork zed setup|status|uninstall` and `mini-ork mcp-context`**: `zed setup` writes the ACP
  agent and MCP server entries into Zed's `settings.json` (absolute paths, the shell's PATH, a
  backup first; `--layout` arranges the panels), and `mini-ork acp --setup` checks the project, the
  claude login and lane keys and offers fixes. `mcp-context` is a read-only stdio MCP server over
  the project's `.mini-ork` (runs, learnings, cost, lanes, recipes, automations); `--control` or
  `MO_MCP_CONTROL=1` adds `start_run`, `wait_for_run`, `run_status`, `stop_run`, `certify` and
  recipe-draft tools that render a small JSON spec into a schema-valid recipe under
  `.mini-ork/recipe-drafts/`. (6d742553, 2fc35f25, 6115a29e, 3c6c0c39, ff362bc9)
- **`mini-ork board`**: the project's live state as one JSON document (runs, learnings,
  automations, scheduler, recipes, workspaces). `board run <id>` adds the kickoff, artifacts and
  diff, `board page <key>` returns any IDE page as data, and verbs (`merge`, `discard`, `stop`,
  `kill`, `resume`, `gate`, `steer`, `answer`, `race-keep`, ...) back the IDE's buttons;
  `mini-ork serve` answers the same reads under `/ide`. (cb4e8c24, 8595ddb5, 70635759, bec8543f)
- **IDE pages**: the run page (DAG, Outcome, Checks, Changes, Orchestrator, Messages, Kickoff,
  Agents, Learnings, Artifacts) names the failed node and whether the fault looks like mini-ork's
  or the task's, checks each success criterion against evidence, and shows spend against its caps.
  Other pages: an attention-first Inbox, a Board with epics and imported tickets, an Orchestrator
  page, Learning & memory, Setup & health (writes `.mini-ork/config/features.yaml`), Nodes, and
  Context pages over ContextNest. (3ff8d045, 22c36d27, cd8774a8, 301af361)
- **Threads (`mini-ork threads list|show|send|continue|attach`)**: durable multi-turn work driven
  from the CLI with no server. `send` steers a live run or, once it has finished, starts the next
  attempt with the transcript and prior outcome in its kickoff; `continue` forces a new attempt;
  `attach` adds files the next run is told about. The agent-server's
  `POST /api/conversations/{id}/events` now continues a finished conversation instead of answering
  409. (76051a32, 214895ce, 1c575dc0, 012d3fe9)
- **Work intake from tickets**: `mini-ork pm sources|add|test|remove` (`config/pm.yaml`) registers
  mini-ork's own epics and read-only GitHub, Linear and Jira sources (only `local` is enabled as
  shipped; tokens resolve from the vault). `mini-ork intake once|poll|status|report` starts a run,
  or continues the ticket's thread, for a ticket that matches the source's opt-in trigger rules.
  The simple tier polls outbound; the secure tier is a signed `POST /api/v1/intake/{source}`, off
  unless `MO_INTAKE_WEBHOOKS=1`. With `write_back: true`, a finished run comments on its Linear or
  Jira ticket. (789a1376, e29fa1b3, 5dd61439, 1be093e8)
- **Chat transports (Telegram, Slack, Discord)**: a message from a channel listed in
  `config/chat.yaml` (`allow_channels` is required and default-deny) launches, steers or continues
  a thread's run. Simple tier: `mini-ork threads serve`, with no public URL (Discord: `--once`
  only). Secure tier: signed `POST /api/v1/chat/{telegram,slack,discord}`, which answers 503 while
  the vault reports a protection gap. Replies are not posted back to the chat. (bb258b91, 991a8f59,
  47de862d, 2d1852b2)
- **Secret vault (`config/vault.example.yaml`, `mini-ork vault status|which`)**: `${NAME}` secrets
  resolve through a backend chain: env, the 0600 `secrets.local.sh`, sops, a `command` (pass, op,
  ...) or OpenBao KV-v2 with per-role policies (`docker/openbao/`). With no `vault.yaml` the chain
  is env then `secrets.local.sh`, and `mini-ork doctor` names that protection gap.
  `mini-ork mcp launch|proxy <name>` lets Zed start MCP servers without holding their secrets.
  (35d04ace)
- **Scheduled automations (`mini-ork automations add|list|pause|resume|run|remove`)**: a recipe, a
  kickoff and a 5-field cron schedule per automation, stored in `<home>/automations.json`.
  `automations scheduler install` adds a 60 s tick (a LaunchAgent on macOS, a crontab line on
  Linux) that fires each due automation as an ordinary run in its own worktree, with the editor
  closed. Missed windows are not backfilled. (9570a943, c699b834)
- **Remote nodes**: `mini-ork node-agent` serves a bearer-token HTTP node agent
  (`--runtime docker|host`), and `mini-ork run --placement remote --env <profile>` (`MO_PLACEMENT`,
  `MO_NODE_ENV`) runs agent CLI spawns and verifiers there, while in-process logic, tool-less HTTP
  lanes and the publisher stay local. The target syncs as git snapshots that never touch your
  index, HEAD or stash, and the run dir is mirrored back. Profiles
  (`config/environments/<name>.yaml`) set setup, resources and `network: allowlist`. Output streams
  live, kill stops remote processes, and a restarted control plane reattaches instead of
  re-spawning. The node agent can also run long-lived services (`/v1/services`) with volumes,
  snapshots, private networks, exec and logs. (f7050f0d, fdc18a99, 2b499be3, 5748273f, c554c0ad)
- **Cloud sandbox providers: Daytona, E2B, Modal** (profile `provider:`, `MO_NODE_PROVIDER`): each
  run gets one sandbox running the node agent, removed at run end (`sandbox-gc --backend all`
  sweeps leaks); credentials fail closed. Daytona and E2B passed live smoke runs; Modal is tested
  offline only. A bare `mini-ork run` adopts `MO_DEFAULT_NODE_ENV` (default `daytona`) when that
  profile exists (`--placement local` or `MO_DEFAULT_NODE_ENV=off` opts out), and
  `MO_NODE_ISOLATION=1` (off by default) gives each workflow node its own sandbox. (8ac86ae9,
  418286f8, cac7749b, 64a3426c, dd15418f)
- **Per-lane secrets on remote nodes and `mini-ork nodes ls|ping|doctor`**: a remote spawn gets
  only the lane keys the profile's `secrets:` allows, or is refused before any request;
  anthropic-native lanes need `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`. Secrets in output
  are masked, and `nodes doctor --env <profile>` checks a node end to end. (74b22675, 1bd68763)
- **The verification stack, on by default**: six techniques from the 2000-paper verification
  review (see `docs/architecture/verification-stack.md`). Metamorphic relations in the certify
  oracle (`certify/relations.py`, veto + rescue); differential base-vs-patch check
  (`certify/differential.py`, collateral-damage veto); mutation-based suite adequacy in the
  code-fix verifier (`gates/suite_adequacy.py`); non-nested five-level verdict vector + publisher
  gate (`verify/levels.py`); gate-hackability audit + promotion consumer (`gates/hackability.py`,
  `gate-fuzz --hackability`); declared equivalence operator for the behavioral verifier
  (`verify/equivalence.py`). Each was built by a mini-ork framework-edit run with RSI on and proven
  by a real mini-ork run. Disable any feature with its master knob set to `0`. (b976ca71, 0e41c887,
  a7516a5b, f39cd4c6, cf5c4acf, 98145717, f4110a93)
- **Fail-to-pass replay beyond pytest**: the code-fix/certify delta-gate replay reads jest/vitest
  JSON and JUnit XML (or results a script writes to `MINI_ORK_TEST_RESULTS_DIR`), parses `go test`
  and `cargo test`, and judges any other command by an exit-code delta when both logs show tests
  ran. Overlapping tests run a second time so flaky ones are dropped (`MO_REPLAY_FLAKE_RERUN=0`
  turns this off). (3a943c58, 31770020, ad507150, fc4e94ec)
- **Toolchain failures abstain instead of failing the code**: a verifier may report
  `failure_kind: code | toolchain | infra`. A `toolchain` failure (the tool never started, or a
  signal killed it) gives the node a third outcome (rc 3): no failure is counted, nothing goes back
  to the implementer, the edit is kept, publish is held and the run shows as needing you. The tier1
  verifier emits it. (d00d0baa, 94333d9b, ce92f4d0)
- **Certify vetoes in the code-fix verifier**: on a green replay the test verifier also runs the
  metamorphic-relations and differential vetoes and records them under `certify` in
  `verifier_test.json`. `MO_CODEFIX_CERTIFY=shadow` (default) never changes the verdict, `gate`
  withholds publish on a REFUTED veto without rolling back, and `0` turns it off. When the suite is
  INADEQUATE, a lane writes tests aimed at the surviving mutants, recorded only
  (`MO_SUITE_STRENGTHEN=shadow`). (bdd5e02b, d526ca21, 564ce92f)
- **framework-edit verification gates**: before the implementer starts, a second model writes blind
  spec tests from the kickoff; `spec_test_verifier` runs them and fails any test edited after it
  was written. `kickoff_contract_verifier` runs the kickoff's `measure` blocks and checks every
  "Required artifacts" path. static-check runs the repo's ruff blocking tier and fails files
  outside an "ONLY" scope, and publish is refused if the diff changed after verification.
  (640f80d3, a7caba16, 9ce327e0, 0e0c9bd4)
- **`mini-ork rsi-cycle`**: runs self-improvement cycles. A cycle harvests gaps from the state DB,
  code checks and probes, has `recipes/rsi-cycle` propose a fix for the top one, and has a
  framework-edit child build it; it merges only if merge gates G1-G11 pass and two critics from
  distinct model families (neither the implementer's) agree, and otherwise quarantines. `plan` and
  `doctor` are read-only; `list`, `show`, `quarantine`, `revert`, `halt` and `resume` manage
  cycles. Config is one strict file (`config/rsi-cycle.example.yaml`; without
  `decide.auto_merge` every cycle quarantines), with a launchd plist and a collapse halt that
  writes `<home>/rsi/DISABLED`. (7321323d, 1a5b6075, 382bd86f, 785e91f7)
- **Research-driven RSI candidates and gate G12** (`research.enabled`, off by default): technique
  reviews become candidates that merge only when a sealed, paired test on a benchmark (built-in
  `verifier-replay`) shows a gain with no regressions; a no-gain attempt is revised from dev-split
  feedback. With research on, each cycle first refreshes a stale technique review and runs the
  benchmark canary, which drops technique candidates once it halts adoption. Adds
  `rsi-cycle research` and `rsi-cycle impact`. (d07ffa2a, d9ef99cc, 108e8d1e, 81272738, 18ac9954)
- **`mini-ork prefs list|set|approve|rm|preview`**: operator preferences reach every researcher,
  implementer and reviewer prompt (before, they reached only the planner's context pack, which no
  prompt read). `--scope path --target <glob>` limits a rule to runs whose file scope matches, and
  `prefs preview <kickoff> --node implementer` prints what that node would get. (4d21a384,
  4ceb277c)
- **Context v2** (`MO_CONTEXT_V2=off|shadow|on`, default `shadow`): selects prompt context by the
  kickoff's files in scope, mainly review and verifier findings on those files, instead of by task
  class. `shadow` only records `context-pack.v2.json`, and `mini-ork metrics context` measures it.
  Findings expire after `MO_CONTEXT_V2_MAX_AGE_DAYS` (30), or once stale at HEAD or landed.
  (eb00a784, 39cda6c3, 2c5f5ad9, 3515640d)
- **`mini-ork onboard`**: a first-run wizard that takes a fresh machine to a runnable workspace:
  prerequisites (`python` ≥ 3.11, `git`), `mini-ork init`, and a credential check that never calls
  a provider. It can also install the two optional satellites: **ContextNest** (clone,
  `cargo build --release`, seed `config/contextnest.yaml`) and **Zed** (wire `mini-ork acp` /
  `mini-ork mcp-context` into `settings.json` via `mini-ork zed setup`). Components default to on
  only when their toolchain is present. Flags: `-y`, `--workspace`, `--with`, `--no-contextnest`,
  `--no-zed`, `--secure` (fails closed instead of writing a local secret file), `--skip-build`,
  `--dry-run`, `--strict`, `--json`. `install.sh` offers the wizard at the end of an interactive
  install (including piped `curl … | sh`, via `/dev/tty`) and runs it in the directory the user
  started from; set `MINI_ORK_NO_ONBOARD=1` to skip. (c084b1a4, cb18ec78)
- **`spec-driven-delivery` recipe**: spec-dir → verified features with live smoke + UI-craft
  gates. Each spec in a `## Spec dir:` directory becomes a SpecCard whose acceptance gates are
  written before implementation and must fail on the untouched tree. Every deliverable goes through
  its own `recursive-validate-impl` child run; per-gate verdicts land in a traceability ledger.
  `mini-ork specs ingest|lint` checks a spec dir without a run, and
  `tests/test_sdd_e2e_dryrun.py` runs the deterministic spine on a fixture repo with no LLM lane.
  (9bbe170b, 041222ff, 6266ebeb, 37ae45b9)
- **New recipes**: judge panels `audit-judge-panel-3l`, `audit-judge-panel-4l` (its gate needs
  four distinct served models), `session-task-judge` and `star-growth-panel`; literature reviews
  `coord-technique-review`, `verification-technique-review`, `author-style-imitation-review`,
  `self-improving-harness-research` and `coverage-planning-research`; and `registry-audit`, which
  audits each feature-registry row in its own read-only child and resumes from a per-item
  checkpoint. (ccef3fd8, 738a938b, 0918cf4f, 1e96793c, c944bbba)

### Changed

- **`mini-ork acp` threads talk to an orchestrator**: in v0.9.0 each prompt launched a run; now a
  Zed thread is a conversation with an orchestrator agent (default lane opus, switchable from
  Zed's model picker) that answers, starts runs or edits files itself; `/run <task>` still
  launches directly. Threads persist in `<home>/acp-threads/`, reopen from Thread History and
  accept images. The orchestrator reads only the project's Claude settings, not the user's.
  (eca51e38, 7c0aa9e4, 784532f5, 26424c4b)
- **Agent sessions use mini-ork's Claude settings, and lanes cannot push**: claude-CLI nodes run
  with `--setting-sources project,local` plus `config/agent-claude-settings.json`, so the
  operator's personal hooks, CLAUDE.md and skills stay out (`MO_NODE_SETTING_SOURCES=` empty
  restores the old behaviour). Any `git push` inside a lane fails unless `MO_LANE_ALLOW_PUSH=1`.
  (b9242dc5, e34d4942)
- **`mini-ork run --resume <run_id>` skips what succeeded, and `run --set K=V`**: `--resume`
  re-dispatches only the nodes that did not succeed, from a per-node completion ledger and the
  run's existing plan; `--checkpoint-id` rewinds in completion order; a run with no checkpoints or
  no plan is refused instead of restarted. `--set K=V` (repeatable) overrides a feature flag for
  one run; premium knobs are dropped unless `MO_ACCEPT_PREMIUM=1`. (bd789718, c96414b1, 8c612b92)
- **Publish lands only the run's own, verified change**: an in-place publish commits the run's
  authored patch through a private index (it used to `git add` whole files, which swept in other
  sessions' edits). It abstains when the patch drops a declared file, when a gating verifier or
  `verdict.json` says `pass:false`, or when it touches a frozen path (`config/kernel-frozen.yaml`
  plus a built-in floor; `MO_ALLOW_FROZEN_EDIT=1` overrides). (3b56dbe3, c61236c8, b2814a35,
  e0e99542)
- **Rollback saves the run's work first**: before a failed run is reverted, its change set goes to
  `refs/mini-ork/salvage/<run_id>` plus `<run_dir>/salvage.patch` and `salvage.json` (restore
  commands), files it created are copied to `<run_dir>/rolled-back-created/`, and the revert is
  skipped if nothing could be saved. (a677ef1c, 1cd279d7, 16bc38a7)
- **Only verified learnings reach prompts**: nodes no longer get raw gradient records or patterns
  without an authored lesson (`MO_INJECT_UNVERIFIED=1` restores gradients), and the planner drops
  ContextNest, role-pack and active-state blocks unless `MO_PLANNER_SHARED_CONTEXT=1`.
  `mini-ork apply --revert-unverified` removes prompt directives that were never measured.
  (b13a285d, 2f99fee8, ea1ecd9d, c4e853b3)
- **The code-fix test verifier no longer passes a red or unrunnable baseline**: a red base is
  judged test by test (a test that passed on base and fails after the patch fails it; no
  regression means abstain; `MO_TEST_LEGACY_RED_BASE=1` restores the old pass). A base that never
  ran (rc 126/127/77 or a launch error) abstains as `base_unrunnable`. Deleting a test or adding
  skip/xfail markers fails unless the kickoff orders it (`MO_ALLOW_TEST_CHANGES=1` overrides).
  (4a958cde, d293ab0c, eef720b2, 86a0cf87)
- **`mini-ork validate` checks recipes against `schemas/workflow.schema.json`**: it reports schema
  violations, errors when two recipes declare the same task class, and warns when a workflow or
  contract names a different one. Five edge types (`supplies_context_to`, `verifies`, `blocks`,
  `human_decision_gate`, `verifies_user_choice`) are now deprecated aliases of `depends_on` for one
  release; `validate` warns once per recipe that uses them (rc 1 under `--strict`) and on
  plain-string `condition:` values, which the runtime ignores. (a7e1c07f, b10821ea)
- **README is install-first**: `README.md` now covers quick start, the `mini-ork onboard` steps,
  the optional ContextNest and Zed components, a first verified run, everyday commands,
  upgrade/uninstall, and a docs index. The positioning, measurements, feature inventory,
  self-improvement loops, and safety discussion moved to `docs/WHY.md`, which the README links to.
  The large in-README certify demo is condensed to one example. (c084b1a4)
- **License names the copyright holder**: `LICENSE` was the untouched Apache-2.0 boilerplate, so
  its appendix still read `Copyright [yyyy] [name of copyright owner]` and no distribution metadata
  named an author. It now carries `Copyright 2026 Amir Khakshour`, `pyproject.toml` declares
  `license = "Apache-2.0"` (PEP 639) with `license-files = ["LICENSE", "NOTICE"]`, and `authors`
  names Amir Khakshour instead of the org. The wheel's `License` field had been empty; it is now
  `License-Expression: Apache-2.0`. (9d264aa6)
- **One version source, and CI is the release gate**: `pyproject.toml` now reads
  `mini_ork/version.py` (`dynamic = ["version"]`), so the distribution metadata,
  `mini-ork version`, the `--help` header and the ACP `initialize` handshake cannot disagree —
  they did: `--help` said `v0.1` while `version` said `0.9.0`. `release.yml` now calls
  `.github/workflows/ci.yml` as a reusable workflow instead of running its own narrower ruff + unit
  gate, and it fails closed when the pushed tag does not match `mini_ork/version.py` or when the
  built wheel does not carry the tagged version. A `v*` tag can no longer publish an artifact from
  a commit the full matrix never accepted. (0264da4d)
- **The IDE crate has its own repository**:
  [`SourceShift/mini-ork-ui`](https://github.com/SourceShift/mini-ork-ui), vendored here as the
  `ui/` git submodule — and in the `zed-mini-ork` fork at `crates/mini_ork_ui/` — instead of
  being checked in. Clone with `--recurse-submodules`; edit the crate in its own repo.
  `install.sh` now fills the submodule in (best-effort — the Python runtime does not need it, so a
  fetch failure warns and continues), and `docs/UI.md` gained a *Building the IDE* section pointing at
  `git clone --recurse-submodules` + `script/mini-ork-build` in the fork. (e335370a, 6ba54378)

### Fixed

- **Run cost, budget guards and the cost pause**: the `MO_DAILY_BUDGET_USD` circuit and a run's
  reported cost now read the `llm_calls` ledger; guards that summed `task_runs.cost_usd` saw about
  a third of real spend, and each planner dispatch added a flat $0.05. The `MO_PAUSE_EVERY_USD`
  pause now stops new dispatches, and `mini-ork resume <run_id>` restarts the run (`--no-restart`
  only clears the sentinel). (5aa7f824, c975c84b, 0438ab65, ac76a64a)
- **Timeouts and kills stop everything a run started**: a node timeout also kills the commands the
  agent ran in their own process groups, killing a run signals the owner's whole process tree, and
  the wait after a kill is bounded (`MO_KILL_REAP_TIMEOUT_S`, default 30 s). A run whose
  dispatcher died no longer reads as in flight; `mini-ork reap [--dry-run] [--stale-after DUR]`
  fails such runs. (2b3532de, a0085c50, be27fb4d, 7954fa6f)
- **Run targets and failure records**: a named target that is missing (`MO_TARGET_CWD`,
  `MINI_ORK_TARGET_REPO`, profile `target_repo`) fails the run instead of falling back to the
  engine checkout, and a kickoff stored under the mini-ork home no longer pins the run to the
  engine checkout. Every failed run records why, and `execute.log` is written as the run goes.
  (83421eaa, 30b18f02, 85672b8f, ca261195)
- **`mini-ork recover` on dead or stopped runs**: it closes the dead attempt's open nodes, so the
  stale-heartbeat watchdog no longer aborts it or leaves the row `executing`; clears a stale stop
  request that made it skip every node; keeps the run's task class so the right publish gates
  apply; and no longer reuses a node whose verifier failed after it. (bfe2f838, 623e91ad, d53b5bb9,
  53004136)
- **Web control routes refuse a foreign Origin**: `POST /api/v1/task-runs/{id}/stop`, `/kill` and
  `/answers` now use the local-caller guard; before, any web page the operator visited could fire
  them with a no-cors POST or through DNS rebinding. Callers that send no Origin (IDE, ACP, SDK,
  curl) are unaffected. (c0f1b1e6)
- **`MINI_ORK_ON_EVENT` and OTel export run**: v0.9.0 documented both, but nothing called them.
  The hook fires after every run event plus a new `run_finished` (split with shlex, no shell, at
  most 8 at once; heartbeats only with `MINI_ORK_ON_EVENT_HEARTBEATS=1`). Every finished run is
  exported to Langfuse/OTLP when the Langfuse keys resolve through the vault (`MO_OTEL_EXPORT=0`
  opts out); the unwired `MO_OTEL=1` buffer and `otel_export --from-jsonl` are removed.
  (d322c5ab, d10a34a2)
- **Reflect and lesson injection**: nodes got an empty learned block when only `MINI_ORK_HOME` was
  set, and a refused dispatch ended gradient extraction with no message. The extractor's fixed
  120 s cap is now `MINI_ORK_GRADIENT_TIMEOUT_S` (default 300), and post-run reflect reads only its
  own run's traces and runs detached (`MO_REFLECT_INLINE=1` keeps it inline). (ef1765de, cd107afd,
  f1dc3604, 26717cdf)
- **Verifiers run the right command against the right tree**: the code-fix test gate uses the
  run's declared verification command before auto-detecting a runner; when the implementer
  committed its patch the replay base is `pre-implementer-ref`, not HEAD (correct patches were
  refuted as `tests-do-not-exercise-change`). A project recipe under `.mini-ork/recipes/` supplies
  its own contract and verifiers (such runs ended `vacuous`), framework-edit verifiers test the
  target worktree with its submodules rather than the engine checkout, and shell-out verifiers use
  the target's package manager instead of a hard-coded `npx`. (be67f084, 2591ca86, 1abbd531,
  f1ca58d9, c9e15858)
- **`install.sh` on a fresh Linux host**: runs `scripts/install-system-deps.sh` with bash instead of
  `sh` (dash on Debian/Ubuntu) and stops with a clear message if bash is missing, probes
  `python3.13` before a possibly-3.14 `python3`, limits the optional `verifiers` engine to
  Python < 3.14, and keeps the runtime pointer inside the venv. (90e08e75)
- **certify bounds and retries every oracle lane call**: `certify` used to call the lane with the
  dispatcher's defaults (25 min, 60 turns), so one stalled gateway call pinned a verdict for up to
  25 minutes with nothing printed; the demo's PROVEN path made six such calls (one measured at
  6m43s on a lane that answers in 4 s). Calls now run under `MO_CERTIFY_LANE_TIMEOUT_S` (180 s) and
  `MO_CERTIFY_LANE_MAX_TURNS` (8), are retried `MO_CERTIFY_LANE_RETRIES` (1) times, and print one
  progress line per call on a terminal (`MO_CERTIFY_PROGRESS=1` elsewhere); a call that still fails
  makes the oracle abstain (UNVERIFIED) instead of hanging. `mo_llm_dispatch` now threads its
  `max_turns` argument into the request; it was accepted and dropped on both the single-lane and
  fallback paths. (ca2074af)
- **Certifying oracle gates defer on absent evidence**: the coalition, liveness and stability gates
  defer instead of passing when there is no evidence (hackability 0.8 → 0.0). (b53b6c0d)
- **framework-edit verifier nodes**: they no longer require the `verdict.json` they are about to
  write (6/6 builds skipped review), and recipe verifiers import the engine that dispatched them.
  (ad37d26f, fc9a210a)
- **`mini-ork verify` evidence**: evidence logs are unique and diff paths are key-level, and the
  hackability proposer dispatches from a scratch target dir. (f5aa21ed, d69cbafd)

### Removed

- **Browser UI**: the vendored agent-canvas SPA (`ui/`) and the older `ui.old/` frontend
  are gone, with the release's SPA tarball and the `web-deps`, `web-build`, `web-dev`, `web-up` and
  `web-snapshot` make targets. `mini-ork serve` keeps the JSON API and answers `/` with a
  `{ui_built: false}` stub; the UI is now the Rust IDE crate in the `zed-mini-ork` fork. (40903048)
- **`mini-ork improve`, `mini-ork eval` and `mini-ork promote`**: removed with their `bin/` shims
  and `learning/group_evolver.py`. They never promoted anything (`eval` skipped every task,
  `promote` refused every candidate); promotion goes through `mini-ork apply`. (222219e0)
- **Unread `workflow.yaml` keys**: per-node `gates:` and node `max_cost_usd` leave the schema; runs
  ignore them as before, and `mini-ork validate` now errors on them. The abstain gate evaluator,
  which only that list could turn on, is gone too. `recursion:` takes exactly `max_iterations` and
  `budget_cap_total_usd`; `budget_cap_per_iter_usd`, `convergence_check` or `divergence_kill` now
  fail the load. (04b67bd5, e9682838, 669c181c)
- **Bash-era code with no caller**: the healer/cleaner/finalize chain, `mini_ork/cache.py` and the
  epic auto-merge/PR helpers. `MO_AUTO_MERGE` leaves the settings catalogue, and the docs stop
  advertising it and `MO_OPEN_PR` (neither did anything). Also gone: `scripts/mini-ork-worktree.sh`
  (`make worktree*` uses `scripts/mini_ork_worktree.py`) and the empty `textual_gradients` table
  (migration 0068). (e404bd14, b1c8e475, bcee2dff)

## [0.9.0] - 2026-10-01

Headline: **`mini-ork certify`** — judge whether a change really fixes the bug it claims
to fix, by running code against the repository. Measured results, sample sizes and the
misses are in [docs/RESULTS.md](docs/RESULTS.md).

### Added

- **`mini-ork certify`**: point it at a base/head (or a patch) and the bug report;
  get PROVEN / REFUTED / UNVERIFIED (exit 0 / 1 / 2) and a `mini-ork.certificate/v1`
  JSON. Builds a Docker image of a Python repo at the base commit. The reproduction
  probe sees the base source and must import the repository's own module (a probe that
  re-implements or redefines the code under test is rejected); invariants are generated
  against what the patch may special-case and must fail on the base to count; a failed
  invariant counts against the patch only if the patch fails it the same way the base
  did, otherwise it is inconclusive and the verdict abstains. Certificates carry each
  invariant's source and failure output.
- **One-line install**: `curl -fsSL https://raw.githubusercontent.com/SourceShift/mini-ork/main/install.sh | sh`
  (clones into `~/.local/share/mini-ork`; `MINI_ORK_INSTALL_DIR`, `MINI_ORK_REF`).
- **Held-out evaluation from the repo's own history**: a miner for SWE-bench-style
  tasks from `fix:` commits (92 tasks, dev/test split), a runner that solves them with
  `code-fix` and grades with hidden tests, and $0 replay of stored solves through any
  verifier.
- **Opt-in learning signals**: `MO_IMPL_REWARD=run_verified` rewards the implementer for
  the run passing its own checks minus a cost penalty (`MO_ROUTER_COST_LAMBDA`);
  `MO_APPLY_SIG_ALPHA` gates apply-loop promotions on an exact McNemar test (the p-value
  is recorded on every measured decision either way).
- **RSI guardrails**: matched-attempt control arm for probe scoring, collapse history and
  an independent collapse halt, a calibrated abstain gate, a verifier audit that
  quarantines promotions its own audits flag, harness-tampering and reward-hacking
  monitors, and base replay in `code-fix` (a fix must make a test go from failing to
  passing).
- **`mini-ork acp`**: a stdio Agent Client Protocol agent (one session = one run) for
  editors such as Zed.
- **Remote-node execution groundwork**: run-scoped workspace sessions, pinned run roots,
  path maps and portable transport argv.
- `examples/certify-demo/demo.sh`, `docs/RESULTS.md`, and a Simplified Chinese README.

### Changed

- **Cost is metered at list price**: each non-Anthropic model on a claude-CLI lane is
  priced from `pricing.yaml` (the CLI had priced unknown models at Anthropic rates —
  7x over on the held-out baseline). Falls back to the engine's shipped table.
- Metamorphic verification runs by default; a run whose verifiers measured nothing is
  reported as unmeasured.
- Per-user lane overlay: the tracked `agents.yaml` is a template; local lane choices live
  in an overlay the resolvers honour.
- README leads with certify and measured numbers; routing and cost claims now match the
  code (the shipped `code-fix` recipe pins its implementer lane).

### Fixed

- ACP / web launch started runs on the first `python3` on PATH (3.9 on macOS) and the
  ACP turn then waited forever; runs now start on the current interpreter and a dead
  launch ends the turn with the launch log.
- The router's z-score scale and `runs_count` (it had routed toward the worst lane).
- Refuse to start a run when the daily budget is already spent; fail an implementer that
  changed nothing; never run the engine on Python < 3.11 silently.
- **Failed-lane error attribution**: when a harness CLI exits non-zero in JSON
  mode, the recorded error (`DispatchResult.error`, `llm_calls.error_message`)
  now leads with the stdout result envelope's API error
  (`[api_error 403] … Key limit exceeded`) instead of stderr alone, which could
  hold only an unrelated advisory banner.
- **Static routing honours `agents.yaml` role lanes**: with no state DB,
  `learning_static_lane` no longer forces every unpinned `reviewer` to
  `MO_FRONTIER_LANE` (and researcher/implementer to `MO_CHEAP_LANE`) when the
  operator mapped that role under `lanes:`; the frontier/cheap defaults now fill
  only unmapped roles.
- **Rolled-back artifacts are attributed to rollback**: `revert_branch` records
  the paths it reverted in `<run_dir>/rolled-back.json`, and the post-run
  `verify` reports such a required artifact as `detail: rolled_back`
  ("reverted by rollback") rather than "missing or empty". The verdict is
  unchanged — a reverted run still fails.

### Security

- Verifiers no longer pass provider API keys, `MINI_ORK_SECRETS` or mini-ork's state
  paths into a target repository's test suite (certify base replay, framework-edit).
- Tests can no longer write to the caller's real credential store: a unit test had
  overwritten a real API key whenever the suite ran under a process that exported
  `MINI_ORK_SECRETS`.

---

## [0.8.0] - 2026-09-18

**Agent frontend + harness engines + verified learning.** mini-ork grows a
first-class web frontend (the OpenHands agent-canvas), speaks the
agent-server wire protocol so the canvas can drive real runs, standardizes
its harness/engine seam (including a UHP client and per-node capability
envelopes), and closes the learn→apply loop with measurement-gated
promotion.

### Added

- **Web frontend**: wholesale OpenHands agent-canvas fork vendored under
  `ui/` and served from `mini-ork serve` — command deck with control
  plane, flywheel + lane-health panels, needs-you triage, embedded
  terminal, durable-resume surface, and a ⌘K palette; zero-build lite UI
  for constrained environments.
- **Agent-server protocol shim** (`mini_ork/web/routes/agent_server.py`):
  the canvas's handshake/onboarding path, conversation create (a
  conversation IS a run — client uuid becomes the run id), event history
  with the full pagination matrix (sort windows, since-replay, cursor),
  and sendMessage (idle→launch, live→steering injection, terminal→409).
- **Harness engine standardization (SE-3 Phase A + B)**: hybrid-delegate
  spawn seam re-expressing local/docker/microVM as a `Workspace.spawn`
  axis; per-engine command-builder registry; UHP client backend (`uhp`
  provider kind, direct-HTTP `/v1/responses` transport); per-node
  capability envelope (`mcp_servers`/`skills`/`agent_doc` riding the node
  env bus, rc=66 envelope-reject so fallback routing can honor declared
  capabilities).
- **New dispatch engines/wires**: `opencode-native` executable engine;
  `openai-chat` direct-HTTP provider kind.
- **Sandbox backends**: DockerWorkspace per-agent containers on a shared
  drive; microVM isolation via microsandbox (default-preferred); leaked-
  sandbox reaper + `mini-ork sandbox-gc`.
- **Behavioral verifier (P0–P3)**: live API-surface checks, oracle
  hardening, IRT-ranked catalog with committee, and a function-level
  metamorphic engine wired as a behavioral surface.
- **Eval in the run flow**: `type: eval` node writing reward columns;
  verifiable process-reward stack (coherence gate default-ON, decomposed
  reward, VPRM step-checks) with the LLM judge demoted to veto-only.
- **Apply loop (learn→apply closed)**: GEPA-style gradient sweeps via
  `bin/mini-ork-apply`; probe-set held-out scorer + bounded auto-sweep;
  promotion requires a real measurement — fabrication and dead harnesses
  are refused; bring-your-own external evaluator seam.
- **Learning + memory**: mini-ork learning entities projected into
  ContextNest's graph; planner graph pack (neighbours + path routes);
  router as a cost-free contextual bandit.
- **Durable DAG resume**: checkpoints, lease/idempotency, tool receipts,
  and `--resume` at STEP or TURN granularity.
- **RSI goal-loop**: goal-agnostic wave recipe skeleton + cross-wave
  driver with RSI stops (GRAO, UCCI, divergence-kill).
- **Runtime**: contextvar-based per-run env isolation (dual-write
  publish, reader migration); stable `--json` run contract; in-process
  SDK primitives exposed from the top-level package; tmux-backed Live
  shell; recipe-local `register.py` loader; prompt override dir; new
  recipes (goal-loop, 10lens with gating edges, audit-judge-panel,
  ui-feature-parity).

### Changed

- Learning hygiene: non-learnable infra exits masked from GRPO advantage;
  the gradient/reward loop refuses empty and ungrounded traces; opus
  patch-critic added as a fourth promotion gate.
- Launcher re-exec hardening (venv entry point resolution) and
  secrets/env propagation through Python dispatch.

### Fixed

- Lane-health fail-fast pre-dispatch (no silent stalls); transient
  throttle retry on all lanes; typecheck verifier keyed on project
  markers instead of global tool presence; publisher commits
  `files_changed` on APPROVE; `task_runs` cost clamp past the old
  10-dollar ceiling; framework-edit verdict/lane-binding class;
  headless runs severed from controlling TTY with planner shape-check
  rejection (rc=65).

---

## [0.7.0] - 2026-07-30

**Native Python runtime.** MiniOrk now runs its framework runtime entirely in
Python while retaining the public `mini-ork` command surface and its
verifier-first execution model.

### Added

- Native extension registries for node handlers, routing policies, gate
  evaluators, provider kinds/transports, subcommands, and embedders.
- Declarative provider lanes in `config/providers.yaml`, including a native
  Codex transport with usage and cost sidecars.
- Native ContextNest hook bridge, migration runner, gate evaluation, and
  Python compatibility launchers for legacy `mini-ork-*` commands.

### Changed

- Provider, gate, migration, launcher, tracing, recovery, learning, and
  orchestration ownership now resolves through the supported Python runtime.
- The release process now verifies the current Ruff, pytest, validate, and
  garden gates before tagging.

### Removed

- The internal Bash runtime: `lib/`, framework gate scripts, runtime selection,
  Bash-only test/CI layers, and Bash provider wrappers.

### Migration note

- Custom integrations must no longer source internal `lib/*.sh` or
  `gates/*.sh` files. Use the public `mini-ork` CLI, recipe contracts, or the
  documented native Python extension points. Existing recipe verifier and
  sandbox command boundaries continue to support shell commands where declared.

---

## [0.6.0] - 2026-06-30

**Live control plane, parallel-run safety, and the start of the Python
migration.** Adds an HTTP control plane for detached/steerable runs, makes
concurrent runs safe by isolating per-run config, and lands the first coexisting
slices of the Bash→Python dispatch migration (ADR-001). All additive — no
breaking changes.

### Added

- **Live control plane** — detached run-launch (`POST /api/v1/runs`), HTTP
  operator-steering (`POST /api/v1/task-runs/{id}/steer`), and a HITL steering
  checkpoint (`plan_status=needs_steering`, pause/resume).
- **Per-run config isolation (T1.0)** — a run snapshots its effective lane
  policy (`config/agents.yaml`) into its run-dir at launch via
  `lib/config_resolve.sh`, and the dispatch resolvers read run-dir-first. Editing
  the global `agents.yaml` no longer perturbs an in-flight run; concurrent runs
  hold independent frozen policies. **Parallel runs are now safe.**
- **`mini_ork.dispatch`** — Phase-0 Python dispatch layer (coexists with the
  bash path; nothing wired live yet): a typed dispatch core (prompt delivered
  over stdin → no `ARG_MAX`/E2BIG, faithful exit codes), an all-lane provider
  registry (codex via wrapper sidecars; claude-family via wrapper env reuse),
  `llm_calls` telemetry persistence, and a `python3 -m mini_ork.dispatch <model>`
  CLI entrypoint.
- **`lib/providers/cl_codex.sh` stdin prompt mode** — reads the prompt from stdin
  when none is on argv (E2BIG-proof caller-to-codex); argv still wins.
- **Docs** — Rivet-parity roadmap and ADR-001 (Bash→Python migration) under
  `docs/_meta/`.

### Fixed

- **codex lane E2BIG** — `cl_codex.sh` passed the codex output stream through the
  environment, exceeding `ARG_MAX` and killing every codex dispatch; the stream
  is now passed by file path.
- **Post-commit watchdog** now restores the branch ref when a racing process
  clobbers `HEAD` onto an unrelated commit, not just reverted working-tree files.
- **Planner no longer dead-ends** on `profile_status=needs_answers` with zero
  questions (`lib/profile_gate.sh`) — a self-sufficient planner dispatches
  instead of blocking on answers that cannot exist. The confidence-floor gate is
  unchanged.
- **bug-audit planner** emits structured JSON rather than markdown.

### Changed

- UI dev-dependencies: `vite` 5 → 8, `@types/node` 20 → 25.

---

## [0.4.1] - 2026-06-26

**Verifier infra hardening patch.** Fixes infrastructure false-negatives in
epic-runner child framework-edit verifiers and hardens dispatcher spawn-bin
resolution.

### Fixed

- `bin/mini-ork-execute` now honors `MO_TARGET_CWD` as the verifier cwd,
  preventing `git archive HEAD` from running in an untracked child workspace
  and producing an empty throwaway copy.
- `recipes/framework-edit/verifiers/test.sh` no longer requires a `tests/`
  directory for the throwaway-copy check and skips the web-smoke verifier
  gracefully when `tests/test_web_smoke.py` is absent.
- `recipes/epic-runner/lib/epic_dispatcher.py` rejects non-regular or
  non-executable `MINI_ORK_EPIC_SPAWN_BIN` paths (e.g. `/dev/null`), falling
  back to the default spawn behavior instead of raising `PermissionError`.

### Changed

- `lib/providers/cl_codex.sh` comment redacted local absolute paths
  (`/Volumes/docker-ssd/...`) to keep the source tree machine-agnostic.
- Added `docs/fixes/20260627-epic-runner-framework-edit-verifier-failures.md`
  documenting the failure modes, fixes, and remaining hardening notes.

---

## [0.4.0] - 2026-06-25

**Shared-brain RLM milestone.** A consumer-agnostic mini-ork "brain" now learns
per `objective_domain` and exposes a stateless inference-time `decide()` surface,
so multiple consumers (eng-team, book-gen, external researcher repos) share one
learning substrate while keeping their mechanics native. Ships alongside the
closed GRPO learning loop and a backend-pluggable store-port seam that lets the
brain run on Postgres as well as SQLite.

### Added

- **Stateless decision service** (`lib/decision_service.sh`, `feat(rlm-4a)`).
  `decide()` is a pure inference-time surface — given an objective domain and
  candidate lanes it returns a routing decision without mutating learner state.
  `bin/mini-ork-execute` now routes through it (`feat(rlm-4b)`).
- **Epsilon-greedy exploration** in `decide()` (`feat(rlm-4b-pre-b)`) —
  brain-side exploration honoring `MO_LEARNING_MIN_SAMPLES`.
- **Objective-aware reward contract** on `execution_traces` (`feat(rlm-1)`,
  migration `0042`) — normalized reward partitioned by `objective_domain`.
- **Relative-advantage lane routing grouped by `objective_domain`**
  (`feat(rlm-2)`, `lib/lane_router.sh`), with the sample floor honoring
  `MO_LEARNING_MIN_SAMPLES` (`fix(rlm-4b-pre)`).
- **Backend-pluggable store-port seam** for the brain libs (`feat(rlm-3)`) —
  lets the policy/trace stores target Postgres or SQLite behind one interface.
- **Per-request `--deadline` wall-clock budget** (`feat(rlm-5)`,
  `lib/deadline_budget.sh` + `bin/mini-ork`) — returns best-so-far between
  stages when the deadline hits.
- **Paged-context slice-provider seam** (`feat(rlm-6)`,
  `lib/context_assembler.sh`) — default reproduces the 64K-bounded output; a
  paged provider serves large manuscripts.
- **Shared-brain integration smoke** (`feat(rlm-7)`) — proves two
  `objective_domain` slices learn independent policies from one brain.

### Changed

- **Closed the GRPO learning loop** (`feat(learning)`, `0bc6ae6`) — outcome
  feedback + cold-DB-safe writeback so reward signal completes the
  classify→…→reflect cycle.
- Verifier tiers now run in the implementer's worktree rather than the main
  checkout (`fix(execute)`), preventing cross-run contamination.

### Fixed

- **`grounded_rejections` migration collision** between `0037` and `0040`
  (`fix(db)`, `9340a0d`) — `0040` neutralized, reconcile migration `0041`
  added, smoke harness moved to the canonical columns
  (`evidence_trace_ids` / `consumed_by_reflector_ts`).
- Untracked a stale root `verdict.json` that was leaking the review-33 artifact
  into verifier clones.

---

## [0.3.0.1] - 2026-06-12

**Phase 1 of Agent-ops hardening complete.** Substrate-only patch release
ships the truthful-run-telemetry layer the v0.3 roadmap names as blocking
everything downstream (LobeHub-informed deep review, 2026-06-10). All four
items shipped as small, verifier-gated framework-edit dispatches in a
single session (~$10 LLM spend total, ~3 hours wall clock).

### Added

- **A2 + A3 — error taxonomy on `llm_calls` + finish reasons on `node_end`**
  (`db/migrations/0021_error_taxonomy_finish_reasons.sql`, commit
  `ca4a165`). New `llm_calls.error_category` (9-category enum: auth /
  quota / capacity / request / safety / network / stream / provider /
  config / unknown) + `retryable` (0/1) classified inside `lib/llm-
  dispatch.sh`. New `run_events.finish_reason` (8-value enum: done /
  error / interrupted / max_steps / cost_limit / timeout /
  verdict_revise / verdict_fail) emitted by `bin/mini-ork-execute`.
  Reviewer node now reads VERDICT and returns non-zero on revise / fail
  / REQUEST_CHANGES / ESCALATE so `escalates_to → rollback` edges fire
  as recipe authors expect. Closes the framework-edit v1 (2026-06-11)
  failure mode where a recipe published despite arbiter `needs_revision`.

- **A1 — dispatch-time config snapshot**
  (`db/migrations/0022_dispatch_config_snapshot.sql`, commit `fbb59f5`).
  New `task_runs.dispatch_config_json` (full resolved lane → {family,
  model_id, provider, base_url} map frozen at dispatch start) +
  `task_runs.agents_yaml_sha`. The UI's `mini_ork/web/agents.py` now
  prefers the snapshot over re-resolving from current `config/agents.yaml`,
  closing the 2026-06-10 sonnet-vs-codex badge bug.

- **A4 — heartbeat watchdog + failure fuse for nodes**
  (`db/migrations/0023_node_heartbeat_fuse.sql`, commit `d8d2417`).
  Background per-node heartbeat loop emits `node_heartbeat` events
  every `MO_HEARTBEAT_INTERVAL_S` (default 30s). Inline watchdog
  SIGTERMs nodes whose last heartbeat is older than
  `MO_HEARTBEAT_TIMEOUT_S` (default 300s = 5 min), marking
  `finish_reason=timeout`. Failure fuse halts a lane after 3
  consecutive same-category retryable failures — depends on A2's
  classifier. Closes the 25-min dead codex dispatch class observed
  2026-06-10.

### Fixed

- **Heartbeat loop noise** (`bin/mini-ork-execute`): silence stderr from
  the background heartbeat subshell to keep `execute.log` clean.

### Notes

- Backward-compatible with v0.3.0. All new columns NULL-able for legacy
  rows; legacy code paths read NULL as "snapshot not available, fall
  back".
- Four follow-up items deferred to v0.3.1, blocked by intermittent
  planner-output parser intolerance (D-011/D-016 class) and the
  framework-edit verifier-methodology bug discovered by A4's reviewer
  on 2026-06-11:
  1. `recipes/framework-edit/verifiers/{static-check,test}.sh` —
     throwaway copy is not its own git root; verifiers measure HEAD
     instead of the patched diff. Discovered by opus reviewer; local
     re-verify after `git apply` has been the operator-side gate.
  2. `recipes/framework-edit` — implementer writes `verdict.json` with
     `pass=false` defensively before verifiers run; reviewer reads
     stale verdict and emits `needs_revision`. Verifier-checks.tsv is
     the source of truth.
  3. `bin/mini-ork-plan` parser — opus markdown-wrap + codex streaming
     envelope both produce parse_error / missing-field rejections on
     long kickoffs. Tolerant extraction works for short kickoffs.
  4. Phase 2 (cache-aware cost accounting, capability flags, pricing
     strategy table), Phase 3 (checkpoint/resume), Phase 4 (UX polish),
     Track B (calibration gates), Arbor plan days 3-5 — all blocked
     on a working framework-edit dispatch.

### Dispatch artifacts (audit trail)

- A2+A3: run-1781192890-33995 ($4.60, 18 min, 23/23 verifier-checks pass)
- A1: run-1781194771-50446 ($2.64, 16 min, 23/23 verifier-checks pass)
- A4: run-1781195887-21821 ($2.79, 18 min, 23/23 verifier-checks pass; first
  reviewer to surface the framework-edit verifier-methodology bug)

---

## [0.3.0] - 2026-06-11

### Added

- **Arbor-style Idea Tree primitive** (`db/migrations/0020_idea_tree.sql`):
  generic `idea_tree_nodes` table for hypothesis-tree exploration patterns,
  status enum (`pending` / `running` / `harvested` / `pruned` / `rejected`),
  insights JSON column for future upward propagation. Idempotent backfill
  script (`scripts/backfill_idea_tree.py`) materializes existing
  `self_improve_runs` history as tree nodes chronologically per day-cluster.
- **Idea Tree read API** under `/api/v1/idea-tree/*` — `roots`, `{root_node_id}`
  subtree, single-node, ancestor-chain endpoints. Loopback-only.
- **Idea Tree visualization** on the Trajectory UI page — `@xyflow/react`
  panel with status-color-coded nodes, click-through to per-run forensics or
  self-improve iter detail.
- **`recipe-creator` meta-recipe** (`recipes/recipe-creator/`) — authors new
  recipes from natural-language epics via a 3-family drafter panel
  (glm/kimi/codex) → opus arbiter → verifier_smith → HARD heterogeneity-floor
  validator. The framework dogfooding itself on small-N recipe authoring.
- **`silent-catch-audit` recipe** (`recipes/silent-catch-audit/`) — first
  recipe authored end-to-end by `recipe-creator`. 3-lens audit of TS/JS
  codebases for silent `.catch(() => {})` anti-patterns.
- **`framework-edit` recipe** (`recipes/framework-edit/`) — recipe-creator-
  authored. Routine mini-ork self-modification with verifier-gated discipline
  (`bash -n`, `py_compile`, `tsc --noEmit`, `pytest tests/test_web_smoke.py`
  against a throwaway patched copy). Emits a unified diff for operator review;
  does NOT auto-apply.
- **Three locally-authored recipes**: `blog-cohesion`, `bug-audit-cmgk`,
  `feature-inventory-cmgk`.
- **Routing policy primitive** (`MO_ROUTING_POLICY` env): five policies
  (`workflow_default`, `frontier_only`, `cheap_only`, `static_hybrid`,
  `trace_governed`) swap workflow-declared lanes at dispatch time without
  recipe edits.
- **Agent in-flight banner** on the Run Detail Agent transcript: pulsing
  amber indicator + explanatory copy when an agent dispatch is still running.

### Fixed

- **`recipes/recipe-creator/verifiers/recipe-validator.sh`** — Python
  `NameError: name 'true' is not defined` when bash `pass=true` was
  interpolated into a Python heredoc. Translate at the bash→Python boundary
  with `True`/`False` literals.
- **`lib/lane-helpers.sh`** — feature-detect
  `--exclude-dynamic-system-prompt-sections` on the `claude` CLI before
  emitting it. Older CLIs (≤2.1.47 observed) hard-fail every dispatch on
  unknown options; probe once per process and degrade to cache-miss instead.
- **`bin/mini-ork-execute` publisher branch** (shipped 2026-06-10 in
  `f11f380`, included here for v0.3.0 finalization): resolves `${VAR}`
  substitutions in `artifact_contract.yaml` `source_artifact` + `outputs[]`
  via envsubst, and copies directory sources via `cp -R src/. dst/`. Required
  for the meta-recipe pattern to auto-publish derivative recipes.
- **`bin/mini-ork-plan`** (shipped 2026-06-10 in `ad2ea05`): dispatcher
  stderr now persisted to `$RUN_DIR/plan-dispatch.err.log` and echoed on
  dispatch failure. Closes the opaque "LLM dispatch failed for planner node"
  failure mode.

### Notes

- Backward-compatible with v0.3.0-rc2. All additions are opt-in.
- `framework-edit` recipe's `verdict.json` has a known write-ordering bug
  (implementer writes `pass=false` defensively before verifiers run). The
  authoritative source is each verifier's `verifier-<name>.checks.tsv` file,
  not `verdict.json`. Fix tracked for v0.3.1.

---

## [0.3.0-rc2] - 2026-06-10

### Added

- Bounded recursive orchestration primitive: `mini-ork spawn` can approve and
  execute child mini-ork runs under an isolated child workspace while recording
  lineage, event-log, artifact-edge, and merge-decision tables.
- Python facade support for recursive delegation via `SpawnRequest` and
  `MiniOrk.spawn(...)`.
- Recursive validation coverage: integration, security, e2e, and live Python
  validation scenarios for parent -> child -> grandchild delegation without
  Anthropic-family provider calls.
- CI release gates for README mechanical claim checks, ShellCheck, bash test
  layers, Python web tests, UI typecheck/build, CodeQL, GitGuardian, and
  dependency review.
- Observability web smoke CI stage that exercises `make web-test`.

### Fixed

- UI typecheck in clean GitHub runners by adding explicit Node 20 type
  declarations for `vite.config.ts`.
- ShellCheck parsing of the optional-check comment in `tests/smoke.sh`.
- Linux/GNU portability for empty plan discovery in `mini-ork-execute` and
  `mini-ork-verify`.
- Linux/macOS `stat` portability in the symlink-attack security test.
- Dependency Review no longer keeps every PR red when GitHub Dependency graph is
  unavailable; the workflow still runs the strict gate when the graph is
  enabled.

### Verified

- GitHub CI for PR #8: README claim check, ShellCheck, bash smoke/unit/
  integration/e2e/security, Python 3.11/3.12, UI typecheck/build, web smoke,
  CodeQL, GitGuardian, and Dependency Review all passed.
- Full test pyramid: 60 files, 543 assertions, 0 failures.
- Recursive focused tests: `test_bin_spawn.sh` 9 OK, recursive spawn security 3
  OK, recursive e2e 6 OK.
- Live recursive validation: `PYTHONPATH=. python3 tests/live/recursive_live_validation.py`
  passed with root -> child -> grandchild lineage and 2 completed child events.

---

## [0.3.0-rc1] - 2026-06-08

### Added

- Python framework facade: importable `mini_ork` package with typed run,
  workflow, recipe, provider-policy, and extension contracts.
- Production scenario framework for real markdown kickoffs across the shipped
  recipe catalog.
- Dispatcher run-profile enrichment: `mini-ork run` now writes
  `run_profile.json`, emits profile questions before planning, and supports
  `MINI_ORK_PROFILE_STRICT=1` for high-risk incomplete profiles.
- Live Phase E Codex validation report for improve -> benchmark -> eval ->
  promote.

### Verified

- Full test pyramid: 57 files, 525 assertions, 0 failures.
- Production scenario sweeps: 9/9 explicit recipe and 9/9 markdown-only
  dispatcher dry-runs passed with Codex-only provider policy.
- Phase E live harness: 8 OK / 0 FAIL using `PHASE_E_PROVIDER=codex`.

---

## [0.1.1] - 2026-05-30

### Added — OSS-readiness deltas

- `GOVERNANCE.md` — lazy-consensus single-maintainer model + reviewer/lead-maintainer path
- `MAINTAINERS.md` — current maintainers list (template; replace handle when forking)
- `SUPPORT.md` — help-channels priority list + scope-of-support clarity
- `ROADMAP.md` — v0.2 / v0.3 / v1.0 buckets + explicit out-of-scope list
- `RELEASING.md` — SemVer policy + cut-release recipe + deprecation policy
- `.github/CODEOWNERS` — safety-critical paths require explicit lead approval
- `.pre-commit-config.yaml` — shellcheck + bash-syntax + schema-validate hooks
- `examples/00-demo.sh` — 60-second runnable demo (dry-run, no API keys needed)
- README badges (license / version / CI / status) + "60-second demo" callout

### Fixed — universal-loop integration bugs surfaced by end-to-end smoke

- (schema) Added migration `0013_task_runs.sql` — universal-loop runtime needs a
  table distinct from the the host application-shape `runs` table (which requires `epic_id` FK)
- (classify) Now writes to `task_runs` (was: `runs`, which failed FK + column mismatch)
- (classify) Matchers tolerate BOTH flat `matches: [kw...]` AND structured
  `matches: { keywords, regex }` shapes — recipes had inconsistent schemas
- (plan) Replaced sed-based `{{KICKOFF_CONTENT}}` substitution (crashed on
  multi-line markdown) with python `str.replace` — hermetic
- (plan) Dry-run writes placeholder JSON to `OUT_FILE` so downstream verify can
  read it; diagnostics moved to stderr (stdout reserved for `plan_path=…`)
- (dispatcher) Top-level `mini-ork run` captures classify+plan stdout to thread
  `MINI_ORK_TASK_CLASS` + `MINI_ORK_PLAN_PATH` into subsequent steps via env
- (dispatcher) Pre-allocates `MINI_ORK_RUN_ID` so all 4 steps write to the same
  `task_runs` row
- (init) Seeds `config/task_classes/` from `recipes/*/task_class.yaml` on
  bootstrap so the classifier matches on a fresh project
- (init) Updated "next steps" hints: `deliver` → `mini-ork run <recipe> <kickoff>`
- (agents.yaml) Added canonical loop-role lanes (planner, worker, reviewer,
  verifier, reflector, publisher, rollback, researcher) — workflow.yaml refs now resolve
- (bin/) Added `bin/mini-ork-invoke-prompt` — single-prompt LLM helper for
  recipe-internal sub-steps (was referenced by bdd-first-delivery but never built)
- (bdd-first) Fixed `lib/dispatch.sh` invoke-prompt path: `MINI_ORK_HOME/bin` →
  `MINI_ORK_ROOT/bin` (helper lives in framework dir, not user's project home)

### Known issues (deferred to v0.1.2)

- `mini-ork-verify` exits 1 in dry-run when no artifact is produced — harmless
  but makes CI green-state ambiguous; needs explicit dry-run code path
- `recipes/bdd-first-delivery/task_class.yaml` uses top-level `keywords:` instead
  of the canonical `matches: { keywords: [...] }` shape; classifier handles both
  but should be normalized

### Verified

- 72 sqlite tables across 13 migrations apply cleanly (idempotent)
- 61/61 bash -n syntax check across all bin/lib/hooks/tests/recipe scripts
- `tests/smoke.sh`: 97/97 OK
- `bash examples/00-demo.sh` runs end-to-end in dry-run; produces a `task_runs`
  row with `task_class=code-fix`, `recipe=code-fix`, `status=classified`
- 0 domain leaks (private hostnames, internal IPs, host-application refs, etc.)
- 0 legacy env-var refs (`AGENTFLOW_*`, `MO_AGENTFLOW_*`)

---

## [0.1.0-redesign] - 2026-05-30

### BREAKING — full architectural rewrite

Replaces the literal port from internal mini-orch (v0.0-extract) with a
framework-and-recipes architecture per
`ideal-mini-orch-self-evolving-system-book.md`.

The v0.0 baseline is preserved at git SHA `0ec2bf1`. See
[docs/REDESIGN.md](docs/REDESIGN.md) for the migration guide.

### Removed

- 42 pipeline-specific prompts — relocated to `recipes/` (domain coupling removed from framework)
- 6 BDD-specific lib scripts — relocated to `recipes/bdd-first-delivery/lib/`
- `skills/` dir (domain-coupled, replaced by `recipes/<recipe>/verifiers/`)
- `bin/mini-ork-deliver` as top-level command (now a recipe; use `mini-ork run bdd-first-delivery <kickoff.md>`)

### Added — framework primitives

13 new `lib/` scripts implementing the universal loop primitives:

| Script | Role |
|---|---|
| `lib/trace_store.sh` | Execution trace writes + reads |
| `lib/gradient_extractor.sh` | TextualGradient extraction from traces |
| `lib/reflection_pipeline.sh` | Reflection worker orchestration |
| `lib/context_assembler.sh` | Relevance-bounded context packing |
| `lib/pattern_store.sh` | Pattern emergence + deduplication |
| `lib/benchmark_suite.sh` | Benchmark task runner |
| `lib/utility_function.sh` | Default utility scorer + override hook |
| `lib/promotion_gate.sh` | Utility delta + benchmark gate enforcement |
| `lib/version_registry.sh` | Promote / quarantine / rollback + audit log |
| `lib/gate_registry.sh` | Gate type registration + dispatch |
| `lib/group_evolver.sh` | Workflow candidate generation (parent selection) |
| `lib/artifact_contract.sh` | Per-class artifact contract validation |
| `lib/agent_registry.sh` | Agent role + model version registration |

### Added — entrypoints

8 new `bin/` entrypoints for the universal loop stages:

```
bin/mini-ork-classify
bin/mini-ork-plan
bin/mini-ork-execute
bin/mini-ork-verify
bin/mini-ork-reflect
bin/mini-ork-improve
bin/mini-ork-eval
bin/mini-ork-promote
```

### Added — schemas

4 new JSON schemas:

- `schemas/task_class.schema.json` — task class YAML validation
- `schemas/workflow.schema.json` — workflow DAG validation (nodes, edges, gates)
- `schemas/agent_version.schema.json` — agent version metadata
- `schemas/artifact_contract.schema.json` — artifact contract shape

### Added — migrations

4 new DB migrations:

- `db/migrations/004_memory_namespaces.sql` — 8 memory namespace tables
- `db/migrations/005_benchmarks.sql` — benchmark tasks + results
- `db/migrations/006_evolution.sql` — workflow candidates, versions, promotion log
- `db/migrations/007_safety.sql` — audit_log (append-only), quarantine registry

### Added — recipes

- `recipes/code-fix/` — minimal reference recipe (classify → plan → implement → verify)
- `recipes/bdd-first-delivery/` — full BDD pipeline ported as a user-land recipe
- `recipes/_legacy_host_app_prompts/` — archived v0.0 prompts (read-only reference)

### Migration path

Replace:
```bash
mini-ork deliver kickoff.md
```
With:
```bash
mini-ork run bdd-first-delivery kickoff.md
```

The workflow shape is identical. The implementation now lives in
`recipes/bdd-first-delivery/` instead of hard-coded in `lib/`.

---

## [0.0.0-extract] - 2026-05-30

Initial extraction from internal mini-orch (literal port; baseline preserved at
git SHA `0ec2bf1`).

### Added

- `mini-ork deliver <kickoff.md>` — end-to-end decompose → workers → review → BDD → merge
- `mini-ork init` — scaffold `.mini-ork/` config directory in any repo
- `lib/dispatch` — epic claim + lane subprocess manager
- `lib/memory` — sqlite WAL read/write helpers
- `lib/auto-merge` — rebase-guard + git merge with audit metadata
- `lib/bdd-runner` — Gherkin scenario executor
- `lib/spec-author` — LLM-backed BDD spec generation
- `lib/spec-reviewer` — adversarial diff reviewer
- `lib/rebase-guard` — conflict detection before merge
- `lib/scope-overlap` — prevents two epics from claiming the same file
- `lib/llm-dispatch` — model routing by epic complexity tag
- `lib/contract` — kickoff constraint extraction
- `lib/self-correction` — structured feedback loop for failed BDD gates
- `lib/cache` — prompt + response caching keyed by content hash
- `lib/healer` — self-heal iter on BDD failure
- `lib/finalize` — post-merge cleanup + state.db verdict write
- `agents.yaml` config schema (max_iters, model overrides, lane cap)
- sqlite `state.db` schema: runs, epics, epic_reviews, bdd_runs, events, model_costs
- `.mini-ork/INBOX/` escalation for unresolvable failures
- `examples/` directory with smoke-testable kickoff fixtures
- `tests/smoke.sh` — offline smoke test with mocked claude binary
- Apache-2.0 license
