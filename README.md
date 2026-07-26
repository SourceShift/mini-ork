# mini-ork

**A task operating system for software agents.** MiniOrk turns a goal into a planned, verifier-backed run: classify → plan → execute → verify → reflect → improve.

It is for teams that want an agent to do useful work without treating fluent output, a green-looking diff, or a panel of agreeing models as proof.

## The goal: make the oracle problem manageable

Software agents can propose a change. The hard part is deciding whether that change is actually right—the **oracle problem**. A model can inspect its own work, but that is evidence, not an oracle.

MiniOrk tackles the parts of this problem that software engineering can make checkable:

~~~text
goal → recipe → work in scoped lanes → executable verifier → evidence + verdict
                                                         ↓
                                              retain only useful lessons
~~~

- Recipes describe the work and its expected artifact.
- Verifiers use tests, type checks, schemas, linters, and other executable evidence. A run with no meaningful verification is reported as **vacuous**, not silently successful.
- Independent review lanes can help with judgment-heavy work, while publishing and learning remain bounded by evidence and policy.

This does **not** create a universal oracle. If there is no trustworthy external check—for example, a subjective product decision—MiniOrk should surface uncertainty or ask a person rather than manufacture confidence.

## Start here

Dry runs need Python 3.11+, Bash 4+, sqlite3, jq, yq, and Git. They do not call a model provider. Real runs additionally need the provider CLIs or provider configuration selected by your lanes.

~~~bash
# Get MiniOrk and install its per-user command (macOS, Linux, or WSL).
git clone https://github.com/SourceShift/mini-ork.git
cd mini-ork
./bin/mini-ork install

# Open a new terminal if the installer changed PATH, then confirm it works.
mini-ork version
~~~

On native Windows PowerShell, run this from the checkout instead:

~~~powershell
py -3 .\bin\mini-ork install
mini-ork version
~~~

The installer is safe to re-run after an upgrade. Use **mini-ork install --help** to see **--bin-dir**, **--no-path**, **--force**, and **--dry-run**.

### Your first verifier-backed workflow

Start in a real Git repository. Keeping the MiniOrk checkout path lets you copy its example into the project you want to work on.

~~~bash
# In the MiniOrk checkout, remember its location before leaving it.
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

After the dry run, inspect **.mini-ork/runs/** for run artifacts and **.mini-ork/state.db** for recorded state. The command sequence was exercised in a clean Daytona sandbox against the current installer on 2026-07-26.

For a real run, review **.mini-ork/config/agents.yaml**, authenticate the CLI or configure the providers it names, then run the same command without **MINI_ORK_DRY_RUN=1**:

~~~bash
mini-ork run code-fix ./kickoff.md
~~~

## Use MiniOrk well

1. **Write a verifiable kickoff.** State the target repository, allowed files, intended artifact, and the command or rule that proves success.
2. **Dry-run every new recipe or environment first.** It checks the lifecycle and artifact paths without model calls; it does not prove the eventual change is correct.
3. **Give an agent an oracle when you can.** Prefer an existing test, typecheck, schema, fixture, or observable acceptance criterion over an LLM-only score.
4. **Use multiple lenses deliberately.** Heterogeneous review is useful for discovery and diagnosis; it does not replace deterministic verification.
5. **Read the evidence before promotion.** MiniOrk retains traces and can learn from runs, but automatic promotion is intentionally restricted to classes with measurable external evidence.

### Pick a starting recipe

| Need | Start with |
|---|---|
| A focused patch with checks | **code-fix** |
| A documentation change | **docs** |
| A multi-perspective codebase audit | **refactor-audit** or **bug-audit-cmgk** |
| A literature or research brief | **research-synthesis** |
| A new workflow shape | Copy a recipe and follow the extension guide |

Recipes live in [recipes/](recipes/). To create one, define a task class, workflow, artifact contract, prompts, and verifiers; see the [extension guide](docs/EXTENSION.md).

## What is in the box

- A Python-first runner with a stable CLI: **mini-ork init**, **run**, **validate**, **doctor**, **providers**, and **garden**.
- Recipe-defined DAGs, scoped execution, provider lanes, cost controls, and worktree-aware workflows.
- Oracle gates and deterministic verifiers, including explicit handling for absent evidence.
- Run traces, SQLite state, reflection, and an evidence-gated learning path.

Read the [architecture](docs/ARCHITECTURE.md), [operator guide](docs/operator), [safety model](docs/SAFETY.md), and [feature catalogue](docs/FEATURES.md) when you need the detailed contracts.

## Roadmap

The near-term work is operational trust: truthful dispatch telemetry, error and finish-reason taxonomy, heartbeat/failure handling, capability-aware routing, cost accuracy, and operator intervention policies. See the full [roadmap](ROADMAP.md).

The next research track is **verifier-led escalation**. It is a proposal, not a shipped capability: use failure analysis to build a library of recovery behaviors, then learn a routing policy that chooses among a cheap tool call, more planning, a stronger model, or a user interruption. The learning signal should be verified progress at decision checkpoints—balanced against compute, latency, and the user's interruption budget—not a raw count of questions.

## Contributing and status

MiniOrk is Apache-2.0 licensed and early. Use a dedicated worktree for framework changes, keep a verifier with every behavior claim, and run the focused checks for the surface you change. The contribution workflow and quality gates are in [AGENTS.md](AGENTS.md); project direction lives in [GOVERNANCE.md](GOVERNANCE.md).
