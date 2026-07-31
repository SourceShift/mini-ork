# mini-ork

**A task operating system for AI agents — one that makes them prove their work.**

mini-ork turns a goal into a planned, executed, and *verified* run across a fleet of
different models: **classify → plan → execute → verify → reflect → improve**. The
verdict on every change is what the code **actually did when it ran** — tests, type
checks, schemas, real execution in an isolated sandbox — not a model's opinion of its
own output.

It is for teams who want an agent to do real work without treating fluent output, a
green-looking diff, or a panel of agreeing models as proof.

## Why this exists

AI agents now write code faster than any team can review it. The bottleneck moved from
*generation* to *validation*. An agent that writes its own tests and then grades itself
produces output that agrees with itself — fluent, green-looking, and wrong often enough
to break production. And the naive fix (send everything to a frontier model, run it many
times) makes the bill grow faster than the unit price falls.

mini-ork is built for the world *after* "make it generate": ship agent work you can
trust, at a cost you can defend, on a system that gets sharper on your codebase the more
you run it.

**Why now:**

- **Reliability is the new bottleneck.** 81% of enterprise technology leaders report an
  *increase* in production issues linked to AI-generated code (CloudBees,
  [*2026 State of Code Abundance Report*](https://www.theregister.com/ai-ml/2026/05/20/ai-code-boom-drives-production-failures-higher-spending/)).
- **Cost routing is a real lever.** Routing between a strong and a weak model can cut cost
  **more than 2×** without compromising quality
  ([RouteLLM, arXiv:2406.18665](https://arxiv.org/abs/2406.18665)) — the lever mini-ork
  automates, but conditioned on a verification bar rather than a guess.
- **Pilots stall on the same three things.** Most agent pilots don't reach production, and
  the blockers are consistently evaluation, reliability, and governance — the three layers
  mini-ork treats as runtime primitives instead of afterthoughts.

## Three things an orchestration framework won't do for you

Wiring agents into a graph is now commodity (LangGraph, CrewAI, AutoGen). mini-ork adds
the three layers that decide whether agent work is actually *shippable*.

### 1. It verifies correctness — it doesn't just orchestrate

The source of truth for a change is its **execution outcome**, captured in an isolated
runtime (mini-ork's `Crucible`, over Prime Intellect's MIT-licensed `verifiers`):

- **Execution-anchored reward.** A change is scored on what it *did* — did the test run,
  did the assertion pass — not on a reviewer's approval. An LLM judge may only **veto** a
  passing result, never fabricate a passing one (`reward_from_status`,
  `mini_ork/learning/writeback.py`).
- **A real failure ≠ a broken harness.** The runtime distinguishes a genuine assertion
  failure (a real reproduction) from a broken test or a broken environment, so a correct
  patch is never rejected because the *probe* had a typo.
- **Non-regression is certified.** Candidates that would break a previously-solved,
  held-out task are blocked before they ship (per-task no-regression gate,
  `mini_ork/cli/apply.py`).
- **A run with no meaningful check is reported as *vacuous*,** not silently successful.

### 2. It governs cost across a pool of models

You don't pay frontier prices for work a cheaper model can pass:

- **Heterogeneous dispatch.** Bring your own providers (OpenAI/Codex, MiniMax, Kimi, GLM,
  Anthropic, or any OpenAI-compatible endpoint) and route each node to a lane by role.
- **Cost-optimizing routing policies.** Selectable strategies from `frontier_only` to
  `cheap_only` to `learning_governed` — route to the cheapest lane that still clears the
  verification bar (`MO_ROUTING_POLICY`).
- **Hard cost controls.** A daily-spend circuit breaker, a periodic cost-pause sentinel
  an operator must approve, and a wall-clock deadline budget — so an autonomous run can't
  quietly burn your account.

### 3. It learns from what actually verified

Every run leaves a trail of *verified* outcomes, and the system feeds that signal back:

- **Cost-free contextual-bandit routing** adjusts which lane gets each role next time,
  from real advantage — no extra model calls (`mini_ork/lane_router.py`).
- **GRPO group-relative writeback** and **textual-gradient** prompt evolution improve the
  planner / implementer / reviewer prompts across runs.
- **Verified-outcome memory** persists *only what passed the gates*, so the learned
  signal is clean rather than noise. A closed **learn → apply** loop materializes,
  scores, and non-regression-gates each proposed improvement before it lands.

## Where it fits

mini-ork isn't a prettier agent graph or a cheaper autonomous coder. Orchestration
frameworks wire agents together; coding products write and ship; eval tools score after
the fact. mini-ork is the open-source runtime where **correctness is the primitive**:
every run yields a verified outcome, that outcome routes the next run to a cheaper model,
and the signal compounds on *your* repository.

That specific combination — correctness-conditional, cost-optimizing, compounding, and
open-source — is the wedge, and it doesn't exist together anywhere else today.

Honest about the edges: the execution oracle is only as strong as what you can *run*, so
its guarantees are richest on code with real tests and thinnest on subjective or
untestable work — where mini-ork is designed to surface uncertainty or ask a person
rather than manufacture confidence.

## What is in the box

**119 shipped capabilities across seven pillars** (full code-anchored list in the
[feature inventory](docs/reference/FEATURE-INVENTORY.md)):

| Pillar | What you get |
|---|---|
| **Orchestration core** | Full `run` lifecycle, keyword task classifier, planner with repair-on-bad-JSON, recovery DAG, a multi-epic scheduler, and a meta-policy conductor. |
| **Heterogeneous model dispatch** | BYO provider registry (5 kinds), 6 routing policies, role-aware fallback chains, per-provider throttle guards, and an owner-only secrets store. |
| **Runtime reliability** | Durable-DAG resume (resurrect a failed run at the step or turn), single-writer leases + fencing, idempotent tool receipts, and cost/deadline circuit breakers. |
| **Verification & gates** | An extensible gate registry (deterministic verifiers, reviewer/human/budget/scope gates), evidence-cited grounded rejections, and promotion gated on measurable evidence. |
| **Self-improvement & learning** | Anti-Goodhart reward contract, cost-free bandit router, GRPO writeback, reflection pipeline, semantic long-term memory, and a closed apply loop. |
| **Observability surface** | A FastAPI app (127.0.0.1:7090) with an SSE live event stream, run detail + DAG overlay, a "why did this fail" aggregator, learning dashboards, and OTel/Langfuse export. |
| **Operator & dev ergonomics** | A stable CLI — `init`, `run`, `validate`, `doctor`, `providers`, `garden`, `serve`, `recover` — plus worktree-aware, file-surface-leased workflows for safe concurrent agents. |

## Start here

`make install` installs the supported local runtime: required OS tools, a checkout-local
`.venv`, the `.[full]` Python profile (CLI, local web sidecar, and Crucible), and the
per-user `mini-ork` command. Dry runs do not call a model provider. Real runs
additionally need the provider CLIs or provider configuration selected by your lanes.

~~~bash
# Get mini-ork and install the full runtime (macOS, Linux, or WSL).
git clone https://github.com/SourceShift/mini-ork.git
cd mini-ork
make install

# Open a new terminal if the installer changed PATH, then confirm it uses .venv.
mini-ork version
~~~

On native Windows PowerShell, install the OS prerequisites with `winget` first
(`Python.Python.3.11`, `Git.Git`, `jqlang.jq`, `MikeFarah.yq`, and `SQLite.SQLite`), then
run this from the checkout:

~~~powershell
py -3 .\scripts\full_install.py
mini-ork version
~~~

`make install` is safe to re-run after an upgrade. It reuses `.venv`, updates the
editable package, repairs the managed command, and verifies the OS tools. Use
`INSTALL_SYSTEM_DEPS=0` only when those tools are already managed outside mini-ork. Use
**mini-ork install --help** to see **--bin-dir**, **--no-path**, **--force**, and
**--dry-run** for the command-only installer.

### Your first verifier-backed workflow

Start in a real Git repository. Keeping the mini-ork checkout path lets you copy its
example into the project you want to work on.

~~~bash
# In the mini-ork checkout, remember its location before leaving it.
MINIORK_SOURCE="$PWD"

# Make a small project to try it on.
mkdir -p ~/miniork-demo && cd ~/miniork-demo
git init
mini-ork init

# A kickoff states the goal, scope, artifact, and verification expectation.
cp "$MINIORK_SOURCE/examples/01-hello-world/kickoff.md" ./kickoff.md

# First run locally and without provider calls.
MINI_ORK_DRY_RUN=1 mini-ork run code-fix ./kickoff.md

# Confirm the project and recipe are wired before spending tokens.
mini-ork validate
~~~

After the dry run, inspect **.mini-ork/runs/** for run artifacts and **.mini-ork/state.db**
for recorded state. For a real run, review **.mini-ork/config/agents.yaml**, authenticate
the CLI or configure the providers it names, then run the same command without
`MINI_ORK_DRY_RUN=1`:

~~~bash
mini-ork run code-fix ./kickoff.md
~~~

## Use mini-ork well

1. **Write a verifiable kickoff.** State the target repository, allowed files, intended
   artifact, and the command or rule that proves success.
2. **Dry-run every new recipe or environment first.** It checks the lifecycle and
   artifact paths without model calls; it does not prove the eventual change is correct.
3. **Give an agent an oracle when you can.** Prefer an existing test, typecheck, schema,
   fixture, or observable acceptance criterion over an LLM-only score.
4. **Use multiple lenses deliberately.** Heterogeneous review is useful for discovery and
   diagnosis; it does not replace deterministic verification.
5. **Read the evidence before promotion.** mini-ork retains traces and can learn from
   runs, but automatic promotion is intentionally restricted to classes with measurable
   external evidence.

### Pick a starting recipe

| Need | Start with |
|---|---|
| A focused patch with checks | **code-fix** |
| A documentation change | **docs** |
| A multi-perspective codebase audit | **refactor-audit** or **bug-audit-cmgk** |
| A literature or research brief | **research-synthesis** |
| A new workflow shape | Copy a recipe and follow the extension guide |

Recipes live in [recipes/](recipes/). To create one, define a task class, workflow,
artifact contract, prompts, and verifiers; see the [extension guide](docs/EXTENSION.md).

## Honesty by design

mini-ork does **not** claim a universal oracle. Where there is no trustworthy external
check — a subjective product decision, untestable code — it should surface uncertainty or
ask a person rather than manufacture confidence. That discipline is wired in, not aspirational:

- A run whose verification is absent or meaningless is reported as **vacuous**.
- The dispatch and learning surfaces refuse to invent a number below their evidence
  threshold (Wilson-CI honesty: `<5` samples returns `evidence: "none"`).
- Every gate rejection cites the evidence trace it was based on, so a "no" is auditable.

## Learn more

Read the [architecture](docs/ARCHITECTURE.md), [operator guide](docs/operator),
[safety model](docs/SAFETY.md), and [feature inventory](docs/reference/FEATURE-INVENTORY.md)
when you need the detailed contracts.

## Roadmap

The near-term work is operational trust: truthful dispatch telemetry, error and
finish-reason taxonomy, heartbeat/failure handling, capability-aware routing, cost
accuracy, and operator intervention policies. See the full [roadmap](ROADMAP.md).

The next research track is **verifier-led escalation**: use failure analysis to build a
library of recovery behaviors, then learn a routing policy that chooses among a cheap
tool call, more planning, a stronger model, or a user interruption — with the learning
signal being *verified progress* at decision checkpoints, balanced against compute,
latency, and the user's interruption budget. (A proposal, not yet a shipped capability.)

## Contributing and status

mini-ork is **Apache-2.0** licensed and early. Use a dedicated worktree for framework
changes, keep a verifier with every behavior claim, and run the focused checks for the
surface you change. The contribution workflow and quality gates are in
[AGENTS.md](AGENTS.md); project direction lives in [GOVERNANCE.md](GOVERNANCE.md).
