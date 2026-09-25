<p align="center">
  <img src="assets/mini-ork-icon.svg" alt="mini-ork" width="112" height="112">
</p>

<h1 align="center">mini-ork</h1>

<p align="center">
  <strong>A task operating system for AI agents — one that makes them prove their work.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Python 3.11 | 3.12" src="https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg">
  <img alt="Status: early, research-grade" src="https://img.shields.io/badge/status-early%20%C2%B7%20research--grade-orange.svg">
</p>

mini-ork turns a goal into a planned, executed, and *verified* run across a fleet of
different models: **classify → plan → execute → verify → reflect → improve**. The
verdict on every change is what the code **actually did when it ran** — tests, type
checks, schemas, real execution in an isolated sandbox — not a model's opinion of its
own output.

It is for teams who want an agent to do real work without treating fluent output, a
green-looking diff, or a panel of agreeing models as proof.

<p align="center">
  <img src="assets/mini-ork-hero.jpg" alt="An ork operator on a starship bridge overseeing many isolated workstreams, each a self-contained environment running its own crew." width="860">
</p>

## Warning: this system modifies itself, unattended

> [!WARNING]
> mini-ork's apply loop can rewrite its own recipe prompts, agent prompts, and workflow
> nodes and edges — and **promote those changes without a human approving each one**. It
> runs the candidate change over a held-out probe set, compares the publish rate before
> and after, and promotes only on a measured improvement that does not regress a
> previously-passing task. There is no "review this first" gate in that loop, and no
> environment variable that puts one back.

Read that as what it is: the machine is allowed to rewrite itself while you are not
looking. The measurement is real — no promote happens without one — but a measurement
is evidence, not a guarantee. A probe set is only as strong as its probes, and the loop
cannot know what it never tested.

This is **recursive self-improvement**, not a metaphor for it. The human approval gate
that earlier versions had was deliberately removed, and it was removed *because* the
measurement gate is what does the work: there is no code path that promotes without a
real held-out measurement, and there is no flag that restores the human branch. What
protects you from a bad promote is the probe set, the per-task no-regression rule, and
your version control — not a person in the loop.

**🛡️ If that is not what you want, the safe configuration is:**

- 🔒 Leave `MO_APPLY_ENABLED` and `MO_AUTO_APPLY` unset. Both default to off, and the
  unattended sweep requires *both* to be `1`.
- 🧪 Run it on a throwaway worktree, never on a checkout you care about.
- 🧮 Know your caps: `MO_APPLY_PROBE_BUDGET_USD` and `MO_APPLY_PROBE_MAX_TASKS` bound what
  a single apply run can spend.
- 🌿 Keep the target repository under version control. `mini-ork rollback agent <target>`
  restores the pre-promotion file and `mini-ork rollback workflow <name>` the workflow,
  but **your VCS is the ultimate backstop** — review the promoted diffs the way you
  would review a junior engineer's commit.

This is a research-grade, self-improving system under active development. A promotion
is a change that has already landed, not a proposal waiting for you. See
[docs/SAFETY.md](docs/SAFETY.md) for the full posture and the gates that do hold.

## 🎯 Why this exists

AI agents now write code faster than any team can review it. The bottleneck moved from
*generation* to *validation*. An agent that writes its own tests and then grades itself
produces output that agrees with itself — fluent, green-looking, and wrong often enough
to break production. And the naive fix (send everything to a frontier model, run it many
times) makes the bill grow faster than the unit price falls.

mini-ork is built for the world *after* "make it generate": ship agent work you can
trust, at a cost you can defend, on a system that gets sharper on your codebase the more
you run it.

**Why now:**

- 📈 **Reliability is the new bottleneck.** 81% of enterprise technology leaders report an
  *increase* in production issues linked to AI-generated code (CloudBees,
  [*2026 State of Code Abundance Report*](https://www.theregister.com/ai-ml/2026/05/20/ai-code-boom-drives-production-failures-higher-spending/)).
- 💸 **Cost routing is a real lever.** Routing between a strong and a weak model can cut cost
  **more than 2×** without compromising quality
  ([RouteLLM, arXiv:2406.18665](https://arxiv.org/abs/2406.18665)) — the lever mini-ork
  automates, but conditioned on a verification bar rather than a guess.
- 🚧 **Pilots stall on the same three things.** Most agent pilots don't reach production, and
  the blockers are consistently evaluation, reliability, and governance — the three layers
  mini-ork treats as runtime primitives instead of afterthoughts.

## 🧱 Three things an orchestration framework won't do for you

Wiring agents into a graph is now commodity (LangGraph, CrewAI, AutoGen). mini-ork adds
the three layers that decide whether agent work is actually *shippable*.

### 1. ✅ It verifies correctness — it doesn't just orchestrate

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

### 2. 💰 It governs cost across a pool of models

You don't pay frontier prices for work a cheaper model can pass:

- **Heterogeneous dispatch.** Bring your own providers (OpenAI/Codex, MiniMax, Kimi, GLM,
  Anthropic, or any OpenAI-compatible endpoint) and route each node to a lane by role.
- **Cost-optimizing routing policies.** Selectable strategies from `frontier_only` to
  `cheap_only` to `learning_governed` — route to the cheapest lane that still clears the
  verification bar (`MO_ROUTING_POLICY`).
- **Hard cost controls.** A daily-spend circuit breaker, a periodic cost-pause sentinel
  an operator must approve, and a wall-clock deadline budget — so an autonomous run can't
  quietly burn your account.

### 3. 🔁 It learns from what actually verified

Every run leaves a trail of *verified* outcomes, and the system feeds that signal back:

- **Cost-free contextual-bandit routing** adjusts which lane gets each role next time,
  from real advantage — no extra model calls (`mini_ork/lane_router.py`).
- **GRPO group-relative writeback** and **textual-gradient** prompt evolution improve the
  planner / implementer / reviewer prompts across runs.
- **Verified-outcome memory** persists *only what passed the gates*, so the learned
  signal is clean rather than noise. A closed **learn → apply** loop materializes,
  scores, and non-regression-gates each proposed improvement before it lands.

## 🧭 Where it fits

mini-ork isn't a prettier agent graph or a cheaper autonomous coder. Orchestration
frameworks wire agents together; coding products write and ship; eval tools score after
the fact. mini-ork is the open-source runtime where **correctness is the primitive**:
every run yields a verified outcome, that outcome routes the next run to a cheaper model,
and the signal compounds on *your* repository.

That specific combination — correctness-conditional, cost-optimizing, compounding, and
open-source — is the wedge, and it doesn't exist together anywhere else today.

Honest about the edges: the execution oracle is only as strong as what you can *run*, so
its guarantees are richest on code with real tests and thinnest on subjective or
untestable work — where mini-ork is built to surface the uncertainty and refuse the
promote rather than manufacture confidence. (Refusing does not mean asking you: the
self-improvement loop has no approval prompt. See the [warning](#warning-this-system-modifies-itself-unattended) above.)

## 📦 What is in the box

**119 shipped capabilities across seven pillars** (full code-anchored list in the
[feature inventory](docs/reference/FEATURE-INVENTORY.md)):

| Pillar | What you get |
|---|---|
| 🧩 **Orchestration core** | Full `run` lifecycle, keyword task classifier, planner with repair-on-bad-JSON, recovery DAG, a multi-epic scheduler, and a meta-policy conductor. |
| 🔀 **Heterogeneous model dispatch** | BYO provider registry (5 kinds), 6 routing policies, role-aware fallback chains, per-provider throttle guards, and an owner-only secrets store. |
| 🛡️ **Runtime reliability** | Durable-DAG resume (resurrect a failed run at the step or turn), single-writer leases + fencing, idempotent tool receipts, and cost/deadline circuit breakers. |
| ✅ **Verification & gates** | An extensible gate registry (deterministic verifiers, reviewer/human/budget/scope gates), evidence-cited grounded rejections, and promotion gated on measurable evidence. |
| 🔁 **Self-improvement & learning** | Anti-Goodhart reward contract, cost-free bandit router, GRPO writeback, reflection pipeline, semantic long-term memory, and a closed apply loop. |
| 📊 **Observability surface** | A FastAPI app (127.0.0.1:7090) with an SSE live event stream, run detail + DAG overlay, a "why did this fail" aggregator, learning dashboards, and OTel/Langfuse export. |
| ⌨️ **Operator & dev ergonomics** | A stable CLI — `init`, `run`, `validate`, `doctor`, `providers`, `garden`, `serve`, `recover` — plus worktree-aware, file-surface-leased workflows for safe concurrent agents. |

## 🚀 Start here

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

### 🧪 Your first verifier-backed workflow

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

## 🛠️ Use mini-ork well

1. ✍️ **Write a verifiable kickoff.** State the target repository, allowed files, intended
   artifact, and the command or rule that proves success.
2. 🧪 **Dry-run every new recipe or environment first.** It checks the lifecycle and
   artifact paths without model calls; it does not prove the eventual change is correct.
3. 🎯 **Give an agent an oracle when you can.** Prefer an existing test, typecheck, schema,
   fixture, or observable acceptance criterion over an LLM-only score.
4. 🔍 **Use multiple lenses deliberately.** Heterogeneous review is useful for discovery and
   diagnosis; it does not replace deterministic verification.
5. 👀 **Read the evidence before promotion.** mini-ork retains traces and can learn from
   runs, but automatic promotion is intentionally restricted to classes with measurable
   external evidence. Note that this is the *only* thing standing between a learned
   directive and your recipe files — there is no human approval step to catch a bad
   promote.

### 🧰 Pick a starting recipe

| Need | Start with |
|---|---|
| 🩹 A focused patch with checks | **code-fix** |
| 📝 A documentation change | **docs** |
| 🔎 A multi-perspective codebase audit | **refactor-audit** or **bug-audit-cmgk** |
| 🔬 A literature or research brief | **research-synthesis** |
| ♻️ Self-improvement of this repository | **recursive-self-improve** (see below) |
| 🧩 A new workflow shape | Copy a recipe and follow the extension guide |

Recipes live in [recipes/](recipes/). To create one, define a task class, workflow,
artifact contract, prompts, and verifiers; see the [extension guide](docs/EXTENSION.md).

## ♻️ Recursive self-improvement: two loops

mini-ork improves itself through two loops that are easy to conflate, yet their blast
radii are nothing alike. The [warning](#warning-this-system-modifies-itself-unattended)
at the top of this file is about the second one. Know which one you are starting.

### 🔬 The self-improvement loop — bounded, branch-isolated, dry-runnable

`mini-ork self-improve` (`bin/mini-ork-self-improve`) is a wall-clock-budgeted outer
loop that runs the [`recursive-self-improve`](recipes/recursive-self-improve/) recipe
against the mini-ork checkout. One iteration scans the repo, the run database, and
benchmark deltas for bottlenecks, runs three heterogeneous-family research lenses plus
an arXiv research lane (MiniMax / Kimi / Codex — low-correlation voters, not three
prompts on one model), asks Opus to synthesize a *ranked* patch plan, has a cheaper lane
implement the top patch, and gates the result.

The gate is four layers, not one: three deterministic verifiers — `bottlenecks-found`,
`self-tests-pass` (runs mini-ork's own pytest suite in a hermetic sandbox, so the verdict
reflects the patch and not the machine around it), and `no-regression` — plus an Opus
patch critic, so a diff that passes pytest but is off-plan, gamed, or a no-op is still
caught. The **runner**, not the implementer, is the only thing that calls `git commit`,
and only once the gates pass. Every iteration lands on its own
`self-improve/iter-<N>-<ts>` branch in a fresh worktree under `$MINI_ORK_HOME/worktrees/`.
Nothing reaches the branch you are on unless you pass `--auto-merge`.

```bash
# Prove the wiring with no model calls. Start here.
bin/mini-ork-self-improve --dry-run --max-iters 1

# A real session: 3h soft cap, 5h hard cap; branches left for you to review.
bin/mini-ork-self-improve --soft-cap-hours 3 --hard-cap-hours 5

# Resume after Ctrl-C — picks the last iteration back out of MINI_ORK_DB.
bin/mini-ork-self-improve --resume --soft-cap-hours 3 --hard-cap-hours 5
```

`--soft-cap-hours` finishes the iteration in flight then stops; `--hard-cap-hours` kills
mid-iteration. Caps are also enforced in dollars (`MO_DAILY_BUDGET_USD`, plus per-iter
and per-epic budgets in `config/agents.recursive-self-improve.yaml`), and a pre-iteration
cost check (`MINI_ORK_PRE_ITER_COST_CHECK=1`, the default) refuses to start a new
iteration once the daily cap is hit. Override lanes by copying
`config/agents.recursive-self-improve.yaml` to `$MINI_ORK_HOME/config/agents.yaml`.

### ⚙️ The apply loop — unattended, and the one to be careful with

`mini-ork apply` closes learn → apply for *prompt and workflow* changes: it picks the
highest-confidence proposed change, materializes it as a workflow candidate, scores it on
a **frozen held-out probe set**, applies a per-task non-regression gate, and then either
rewrites the target file or quarantines it with a reason. Quarantine is the gate doing its
job, not an error — the command exits `0` either way.

Only the `probe` scorer can promote. `mock` and `gepa` are deterministic placeholders
that fabricate a utility and never promote; they exist for tests. With no measurement
there is no promote.

Probe sets live at `recipes/<recipe>/probes/*.md` — `code-fix` and `obs-smoke` ship them
today, so those are the task classes you can build on. You also need an initialized
project (`pattern_records` is created by `mini-ork init`), because a run against a bare
home has nothing to pick from.

```bash
mini-ork init                      # once, in the project you are improving
mini-ork apply --task-class code-fix \
  --target recipes/code-fix/prompts/implementer.md --dry-run
```

Both flags that make this dangerous are off by default:

- `MO_APPLY_ENABLED=1` — the master gate. Without it a candidate is prepared but the
  file write and version registration are skipped. `apply --enable` sets it for one call.
- `MO_AUTO_APPLY=1` — the unattended sweep *inside a run*. The sweep fires only when
  **both** this and `MO_APPLY_ENABLED` are `1`; there is no code path that promotes on
  one alone.
- `MO_APPLY_DRY_RUN=1` — score and decide, write nothing.
- `MO_APPLY_PROBE_MAX_TASKS` (default 2) and `MO_APPLY_PROBE_BUDGET_USD` (default 2.0) —
  the spend ceiling for a single probe evaluation.

Because that sweep has **no approval prompt**, the rollbacks are yours to know:
`mini-ork rollback agent <name>` restores the pre-promotion prompt file and
`mini-ork rollback workflow <name>` the workflow; your VCS is the backstop. Both read
the version registry straight out of `MINI_ORK_DB`, which nothing sets for you — export it
first, or the command exits on `MINI_ORK_DB unset`:

```bash
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
mini-ork rollback agent <name>
```

See [docs/SAFETY.md](docs/SAFETY.md) for quarantine semantics.

### ✍️ The manual loop that surrounds both

Between the two, the same primitives are drivable by hand — propose, score, decide:

```bash
mini-ork improve --dry-run                     # show what it would propose, spend nothing
mini-ork improve --task-class code-fix --limit 3
mini-ork eval    --candidate <id>              # run the benchmark suite against a candidate
mini-ork promote --candidate <id> --dry-run    # compute the gate decision, write nothing
```

### 🗄️ Where the state lives

Everything above writes to `$MINI_ORK_DB` (default `$MINI_ORK_HOME/state.db`):

```bash
sqlite3 .mini-ork/state.db "SELECT iter, outcome, notes FROM self_improve_runs ORDER BY iter;"
sqlite3 .mini-ork/state.db "SELECT iter, rank, category, title, outcome, severity, confidence FROM learning_record ORDER BY iter, rank;"
sqlite3 .mini-ork/state.db "SELECT decision, COUNT(*) FROM apply_attempts GROUP BY decision;"
```

`learning_record` carries the per-bottleneck trail — a bottleneck is written `open` when
the scanner finds it, `resolved` when an iteration commits a fix for it, and `superseded`
once a later successful iteration lands over a `deferred` one. `apply_attempts` records
every apply decision, including each quarantine and the reason, so a directive that failed
a gate is never re-proposed.

## ⚖️ Honesty by design

mini-ork does **not** claim a universal oracle. Where there is no trustworthy external
check — a subjective product decision, untestable code — it surfaces the uncertainty
rather than manufacturing confidence. That discipline is wired in, not aspirational:

- 🫥 A run whose verification is absent or meaningless is reported as **vacuous**.
- 📉 The dispatch and learning surfaces refuse to invent a number below their evidence
  threshold (Wilson-CI honesty: `<5` samples returns `evidence: "none"`).
- 🧾 Every gate rejection cites the evidence trace it was based on, so a "no" is auditable.
- 🚫 The promotion gate has no human branch. "Not promotable" is a recorded verdict with a
  reason attached, never a request sent to a person — the loop has nobody to ask, and it
  does not pretend otherwise.

## 📚 Learn more

Embedding mini-ork in your own app? The [Python SDK](docs/PYTHON-SDK.md) covers both
the importable primitives (verification, dispatch, memory, routing) and the `MiniOrk`
orchestrator client.

Read the [architecture](docs/ARCHITECTURE.md), [operator guide](docs/operator),
[safety model](docs/SAFETY.md), and [feature inventory](docs/reference/FEATURE-INVENTORY.md)
when you need the detailed contracts.

## 🗺️ Roadmap

The near-term work is operational trust: truthful dispatch telemetry, error and
finish-reason taxonomy, heartbeat/failure handling, capability-aware routing, cost
accuracy, and operator intervention policies. See the full [roadmap](ROADMAP.md).

The next research track is **verifier-led escalation**: use failure analysis to build a
library of recovery behaviors, then learn a routing policy that chooses among a cheap
tool call, more planning, a stronger model, or a user interruption — with the learning
signal being *verified progress* at decision checkpoints, balanced against compute,
latency, and the user's interruption budget. (A proposal, not yet a shipped capability.)

## 🤝 Contributing and status

mini-ork is **Apache-2.0** licensed and early. Use a dedicated worktree for framework
changes, keep a verifier with every behavior claim, and run the focused checks for the
surface you change. The contribution workflow and quality gates are in
[AGENTS.md](AGENTS.md); project direction lives in [GOVERNANCE.md](GOVERNANCE.md).
