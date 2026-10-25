
<h1 align="center">mini-ork</h1>

<p align="center">
  <strong>A task operating system for AI agents — one that makes them prove their work.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Python 3.11 | 3.12" src="https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg">
</p>

<p align="center">English | <a href="README.zh-CN.md">简体中文</a></p>

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-demo.mp4">
    <img src="assets/mini-ork-demo.gif" alt="A code-fix run in the mini-ork IDE: classify, plan, implement, verify, three review lenses, publish, then Keep or Reject for each changed file" width="900">
  </a>
</p>
<p align="center"><sub>A <code>code-fix</code> run in the mini-ork IDE: classify → plan → implement → verify → three review lenses on three model families → publish, then each changed file waits for Keep or Reject. Sample run from the <a href="https://miniork.dev/">walkthrough</a> · <a href="https://miniork.dev/media/mini-ork-demo.mp4">full-quality video</a>.</sub></p>

mini-ork turns a goal into a planned, executed, and **verified** run across a fleet of
models: **classify → plan → execute → verify → reflect → improve**. Every verdict comes
from what the code actually did when it ran — tests, type checks, schemas, execution in
an isolated sandbox — not a model's opinion of its own output.

This page is the install guide. For the argument behind the project, see
[Why mini-ork exists](docs/WHY.md).

## Quick start

macOS, Linux, or WSL:

~~~bash
curl -fsSL https://raw.githubusercontent.com/SourceShift/mini-ork/main/install.sh | sh
~~~

The installer:

- clones mini-ork into `~/.local/share/mini-ork` (`MINI_ORK_INSTALL_DIR` overrides it),
- creates a checkout-local `.venv` and installs the full Python profile
  (CLI, local web sidecar, Crucible),
- puts `mini-ork` on your PATH in `~/.local/bin`,
- offers the first-run wizard when it is running on a terminal.

From an existing checkout, the same install:

~~~bash
git clone --recurse-submodules https://github.com/SourceShift/mini-ork.git
cd mini-ork
make install
mini-ork version        # open a new terminal if PATH changed
~~~

`--recurse-submodules` fills in `ui/`, the IDE crate
([`SourceShift/mini-ork-ui`](https://github.com/SourceShift/mini-ork-ui)). The Python
runtime does not need it; see [docs/UI.md](docs/UI.md) to build the IDE.

## First run: `mini-ork onboard`

One command takes a fresh machine to a workspace you can run a recipe in. It never
calls a model and never spends money — every step is a local check or a local build.

~~~bash
cd ~/my-project
mini-ork onboard
~~~

| Step | What happens |
|---|---|
| prerequisites | Checks Python ≥ 3.11 and git; notes `sqlite3` / `jq` if present |
| workspace | Creates `.mini-ork/` in the current directory (runs `git init` if needed) |
| provider lanes | Reports whether credentials are stored, or names the command that adds them |
| contextnest | *Optional:* clones, builds, and seeds a config for the local memory service |
| zed | *Optional:* wires mini-ork's agent + MCP servers into Zed's `settings.json` |

Optional components are offered only when their toolchain is present (`cargo`, `zed`),
and each can be chosen explicitly:

~~~bash
mini-ork onboard --with contextnest,zed      # force both
mini-ork onboard --no-contextnest --no-zed   # core only
mini-ork onboard --dry-run                   # print the plan, change nothing
mini-ork onboard -y                          # accept defaults (scripts, CI)
mini-ork onboard --json                      # machine-readable step results
mini-ork onboard --help                      # every flag
~~~

Exit codes: `0` done (skipped components are fine), `1` a selected component failed
under `--strict`, `2` usage error. A non-interactive run has no prompts: `-y` accepts
every default.

### ContextNest — local memory substrate

A Rust HTTP service that remembers what past sessions decided, failed at, verified, and
shipped. The wizard seeds `config/contextnest.yaml` in the **simple** tier; with
`--secure` it writes nothing and requires `CN_TOKEN` before the service will start.

~~~bash
mini-ork onboard --with contextnest     # clone + cargo build --release + seed config
mini-ork cn status                      # tier, endpoint, reachability, latency
~~~

By hand: `git clone https://github.com/SourceShift/ContextNest.git`, then
`make cn-serve` (binds `127.0.0.1:28080`). Requires Rust 1.80+. Details:
[docs/CONTEXTNEST-INTEGRATION.md](docs/CONTEXTNEST-INTEGRATION.md).

### Zed — editor integration

Wires `mini-ork acp` (external agent) and `mini-ork mcp-context` (read-only run,
learning, and cost context) into Zed. Every write to `settings.json` is preceded by a
timestamped backup.

~~~bash
mini-ork onboard --with zed     # or at any time:
mini-ork zed setup
mini-ork zed status
mini-ork zed uninstall
~~~

See [docs/ZED.md](docs/ZED.md) and [docs/UI.md](docs/UI.md).

## Your first verified workflow

~~~bash
cd ~/my-project
mini-ork onboard                      # initialize the project
mini-ork providers configure <lane>   # hidden prompt; keys never touch argv
mini-ork doctor                       # dependencies, environment, provider preflight

cp ~/.local/share/mini-ork/examples/01-hello-world/kickoff.md ./kickoff.md

# Check the wiring first: no model calls, no spend.
MINI_ORK_DRY_RUN=1 mini-ork run code-fix ./kickoff.md
mini-ork validate

# Then run it for real.
mini-ork run code-fix ./kickoff.md
~~~

A kickoff states the target repository, allowed files, intended artifact, and the
command or rule that proves success. Run artifacts land in `.mini-ork/runs/`, recorded
state in `.mini-ork/state.db`. Review `.mini-ork/config/agents.yaml` to change which
lane serves which role.

### Prove a fix in one command

`mini-ork certify` takes a change and the bug it claims to fix, reproduces the bug
against your code, generates inputs the change was not written for, runs everything in
a Docker sandbox, and returns a verdict with the evidence attached:

~~~text
$ mini-ork certify --base base --head fix-cheat \
    --issue "median([1, 2, 3, 4]) returns 3, should return 2.5"
REFUTED  the fix does not generalise — only 0% of 4 invariants hold,
         which is the signature of a patch that special-cases the reported input
exit code: 1
~~~

Exit codes `0` PROVEN, `1` REFUTED, `2` UNVERIFIED — it abstains rather than guesses,
so CI can gate on it. Needs Docker and one model lane.
Try `bash examples/certify-demo/demo.sh`.

## Verification, learning, and self-improvement

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-loops.mp4">
    <img src="assets/mini-ork-loops.gif" alt="The zone map on miniork.dev: the run loop, the learning loop, the repair loop and the RSI loop light up in turn, each showing what flows between the engine, verification, learning and recovery" width="900">
  </a>
</p>
<p align="center"><sub>Four loops around one engine, from the <a href="https://miniork.dev/">walkthrough</a> (sample data) · <a href="https://miniork.dev/media/mini-ork-loops.mp4">full-quality video</a>.</sub></p>

The GIF at the top of this page is the run loop. Three more layers sit around it.

### Verification the agents cannot talk their way past

Every verdict is execution evidence. A recipe's verifiers run a tamper guard, the test
suite, a base replay (the test must fail on the old code) and a mutation-adequacy check.
A second model lane writes blind spec tests from the kickoff before the implementer
starts, and the implementer cannot edit them. The publisher's gate chain refuses anything
that did not pass, and a reviewer model may veto a result but can never create a pass.
`certify` applies the same stance to one change and answers PROVEN, REFUTED, or
UNVERIFIED when it cannot prove either way. On an adversarial SWE-bench corpus, 21 of 21
PROVEN verdicts were correct with 0 false completions; the method and its limits are in
[docs/RESULTS.md](docs/RESULTS.md), the mechanisms in
[docs/architecture/verification-stack.md](docs/architecture/verification-stack.md).

### Learning that keeps only what verified

After every run the traces become a process reward per node, and a positive reward has to
be paired with verifier evidence. From there the loop fans out: per-lane advantages the
router learns from (GRPO), textual gradients the reflection step mines into lessons, and
a conflict check before any lesson is written. Only verified lessons reach a prompt, every
prompt carries the learned block, and an injection ledger records exactly what each node
received, so credit flows back to the lesson that earned it. Today the gradients and
verified themes do flow into prompts, while learned routing still decides a small minority
of routes. Lifecycle and status:
[docs/LEARNING-LOOP-LIFECYCLE.md](docs/LEARNING-LOOP-LIFECYCLE.md).

### Self-improvement, gated

mini-ork edits mini-ork through the same recipe graph: `framework-edit` (verified
self-edits with blind spec tests), `self-improve` (scan, lenses, synthesis, implement,
gates), `rsi-technique-review` (reads the literature and proposes techniques to build) and
`goal-loop`. A learned change must pass a hackability audit and a verifier audit, then a
non-regression gate on frozen probes against a blind-rerun control arm, and every promoted
version keeps a rollback pointer. The apply loop that rewrites prompts unattended ships
**off by default** (see the warning below). `framework-edit` is the most-run recipe, and
`self-improve` found and merged its own fix on 2026-10-23. Details:
[docs/RECURSIVE-SELF-IMPROVE.md](docs/RECURSIVE-SELF-IMPROVE.md),
[docs/SAFETY.md](docs/SAFETY.md), [docs/ECOSYSTEM.md](docs/ECOSYSTEM.md).

## Everyday commands

| Command | Does |
|---|---|
| `mini-ork run <recipe> <kickoff.md>` | Run one recipe end to end |
| `mini-ork doctor` | Check dependencies, environment, provider preflight |
| `mini-ork validate` | Static pre-run checks with `Fix:` hints |
| `mini-ork providers status <lane>` | Show credential presence and source (never values) |
| `mini-ork serve` | Local web UI and API on `127.0.0.1:7090` |
| `mini-ork garden` | Drift detection: collisions, orphans, stale runs |
| `mini-ork recover` / `resume` | Resurrect a failed or interrupted run |
| `mini-ork mcp` / `vault` | MCP server registry; secret-vault backend chain |
| `mini-ork zed setup` / `cn status` | Editor wiring; ContextNest tier and reachability |

Starting recipes: `code-fix`, `docs`, `refactor-audit`, `bug-audit-cmgk`,
`research-synthesis`, `spec-driven-delivery`. See [recipes/](recipes/) and the
[extension guide](docs/EXTENSION.md).

## Upgrade

Re-run the installer, or from a checkout run `make install` — it is safe to re-run: it
reuses `.venv`, updates the editable package, repairs the managed command, and verifies
the OS tools. Then `mini-ork update` applies pending database migrations and reports
config drift.

## Uninstall

~~~bash
mini-ork zed uninstall                     # remove the Zed entries
mini-ork automations scheduler uninstall   # only if you installed the OS tick
rm -rf ~/.local/share/mini-ork ~/.local/bin/mini-ork
~~~

Project state lives in each project's `.mini-ork/`; delete it where you want it gone.
Your source checkouts and git history are never touched.

## Requirements

| | |
|---|---|
| OS | macOS, Linux, WSL. Native Windows: install Python 3.11, Git, jq, yq, and SQLite with `winget`, then run `py -3 .\scripts\full_install.py` |
| Python | 3.11 or 3.12 |
| Git | required |
| Docker | only for sandboxed verification (`certify`, Crucible) |
| Rust 1.80+ | only for ContextNest and the IDE crate |
| Model access | one configured provider lane for real runs; dry runs need none |

## Learn more

| | |
|---|---|
| [docs/WHY.md](docs/WHY.md) | Why the project exists; measurements; what it does not claim |
| [docs/SAFETY.md](docs/SAFETY.md) | Self-modification posture and the gates that hold |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design and component diagrams |
| [docs/operator](docs/operator) | Running mini-ork: env vars, troubleshooting |
| [docs/PYTHON-SDK.md](docs/PYTHON-SDK.md) | Embed mini-ork in your own app |
| [docs/RESULTS.md](docs/RESULTS.md) | Methods and sample sizes behind the numbers |
| [ROADMAP.md](ROADMAP.md) | Where the project is going |

> [!WARNING]
> mini-ork contains an apply loop that can rewrite its own prompts and workflows and
> promote the change without human review. It is **off by default** (both
> `MO_APPLY_ENABLED` and `MO_AUTO_APPLY` must be `1`), and its blast radius, caps, and
> rollbacks are documented in [docs/WHY.md](docs/WHY.md#warning-this-system-modifies-itself-unattended).

## Contributing and license

mini-ork is **Apache-2.0**, © Amir Khakshour. It is early and research-grade: use a
dedicated worktree for framework changes, keep a verifier with every behaviour claim, and
run the focused checks for the surface you change. See [AGENTS.md](AGENTS.md) for the
contribution workflow and [GOVERNANCE.md](GOVERNANCE.md) for project direction.
