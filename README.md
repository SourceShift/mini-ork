# mini-ork — Multi-Agent Code Orchestration Framework

**mini-ork** is a declarative, multi-agent orchestration system that decomposes a markdown kickoff spec into parallel work lanes, runs adversarial review, gates merge on executable BDD specs, and auto-merges validated code — all from a single command. Built on the Claude Code SDK, bash, and sqlite3; no application server, no Docker daemon, no managed cloud required. Use it to ship large features, run multi-model refactors, build self-evolving systems, or coordinate specialist agents across any codebase.

## Quickstart

```bash
# 1. Install
git clone https://github.com/ork-ai/mini-ork ~/ps/mini-ork
cd ~/ps/mini-ork && make install   # copies bin/mini-ork to ~/.local/bin, writes ~/.mini-ork/config

# 2. Initialize a repo
cd ~/my-project
mini-ork init                      # writes .mini-ork/agents.yaml + .mini-ork/config.env

# 3. Deliver
mini-ork deliver kickoff.md        # decompose → workers → review → BDD → merge
```

`mini-ork deliver` exits 0 on clean merge, 1 on unresolved gate failure. All intermediate state is in `.mini-ork/state.db`.

## Architecture

```
kickoff.md
    │
    ▼
┌─────────────────────────────────────────────────┐
│  decomposer (Opus)                              │
│  parse kickoff → seed epics → assign lanes      │
└───────────────────┬─────────────────────────────┘
                    │  N epics claimed
                    ▼
┌─────────────────────────────────────────────────┐
│  scaffold                                       │
│  mkdir worktrees, write per-epic context files  │
└───────────────────┬─────────────────────────────┘
                    │
                    ▼  (parallel, one lane per epic)
┌─────────────────────────────────────────────────┐
│  run-loop                                       │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐ │
│  │ worker   │  │ reviewer │  │  BDD spec     │ │
│  │ (Sonnet/ │→ │ (Opus/   │→ │  author +     │ │
│  │  GLM/    │  │  Kimi)   │  │  runner       │ │
│  │  DeepSeek│  └──────────┘  └───────┬───────┘ │
│  └──────────┘                        │ pass/fail│
│                              ┌───────▼───────┐  │
│                              │  self-heal    │  │
│                              │  (Sonnet)     │  │
│                              └───────────────┘  │
└───────────────────┬─────────────────────────────┘
                    │  all lanes: verdict=PASS
                    ▼
┌─────────────────────────────────────────────────┐
│  auto-merge                                     │
│  rebase-guard → conflict check → git merge      │
└───────────────────┬─────────────────────────────┘
                    │
                    ▼
              state.db  (full audit trail)
```

**lib/ scripts:** `dispatch` · `memory` · `auto-merge` · `bdd-runner` · `spec-author` · `spec-reviewer` · `rebase-guard` · `scope-overlap` · `llm-dispatch` · `contract` · `self-correction` · `cache` · `healer` · `finalize`

## Concepts

- **Epics** — units of work seeded from the kickoff. Each epic maps to one git worktree and one worker agent. Epics have a `verdict` column: `pending | pass | fail | escalated`.
- **Runs** — a single `mini-ork deliver` invocation. Identified by a UUID stored in `runs.id`. All epics, events, and verdicts are scoped to a run.
- **Iters** — worker + review cycles within an epic. Max iters is configurable (`agents.yaml: max_iters`). Self-heal consumes one iter slot.
- **Lanes** — parallel execution tracks. One lane = one epic claim = one Claude Code subprocess. Lanes share nothing except the sqlite state.db (write-serialized via WAL).
- **Verdicts** — the outcome of a BDD spec run: `PASS | FAIL | SKIP | ESCALATED`. A verdict of `PASS` on all epics unlocks auto-merge. `ESCALATED` writes to `.mini-ork/INBOX/` for human triage.

## Lifecycle

1. **kickoff** — user writes `kickoff.md` describing the feature, acceptance criteria, and any model preferences.
2. **seed** — decomposer (Opus) parses kickoff, inserts N rows into `epics` with `status=pending`.
3. **claim** — each lane subprocess atomically claims one epic (`UPDATE epics SET status='claimed' WHERE status='pending' LIMIT 1`).
4. **spawn worker** — `llm-dispatch` selects model by epic `complexity` tag; Claude Code SDK subprocess runs the implementation.
5. **review** — spec-reviewer (Opus or Kimi) reads diff + kickoff constraints; writes structured feedback to `epic_reviews`.
6. **gate** — BDD spec author writes Gherkin scenarios; `bdd-runner` executes them. Pass → verdict=PASS. Fail → self-heal iter or escalate.
7. **merge** — `auto-merge` rebases each worktree branch onto main via `rebase-guard`, resolves non-conflicting hunks, commits with audit metadata from state.db.

## Models + Cost

| Model | Role | Typical cost / epic | Notes |
|---|---|---|---|
| `claude-opus-4` | decomposer, reviewer, escalation | $0.15 – $0.60 | High reasoning, used sparingly |
| `claude-sonnet-4-5` | worker (default), self-heal | $0.03 – $0.12 | Best cost/quality for implementation |
| `glm-4` | hunter (bug/perf scan), heavy grep | ~$0.01 – $0.04 | Fast, cheap for structured analysis |
| `kimi-k2` | reviewer (alt), long-context diff | ~$0.02 – $0.08 | Strong at 128K diffs |
| `deepseek-v3` | worker (alt, budget mode) | ~$0.005 – $0.02 | Cheapest; good for boilerplate-heavy epics |

Cost varies with epic complexity and iter count. A typical 5-epic delivery runs $0.30 – $2.00 total.

## Roadmap

### v0.1 (current)
- `deliver` command: decompose → workers → review → BDD → merge
- sqlite state.db with full run audit trail
- `llm-dispatch` with Sonnet/Opus/GLM routing
- Self-heal on BDD failure (1 iter)
- `INBOX/` escalation for unresolvable failures

### v0.2
- `mini-ork resume <run-id>` — continue interrupted run from last checkpoint
- Kimi + DeepSeek provider wrappers
- Parallel lane cap (`--max-lanes N`)
- `mini-ork inspect <epic-id>` — show iter trace + model costs
- Scope-overlap detector (prevents two epics touching same file)

### v1.0
- Plugin hooks (`pre-worker`, `post-review`, `on-escalate`)
- Cost budget enforcement (`--budget 5.00`)
- Web dashboard (read-only, sqlite-backed)
- `mini-ork replay` — re-run a specific epic against current HEAD

## Dependencies

| Dep | Version | Purpose |
|---|---|---|
| bash | 4.0+ | runtime shell (arrays, `mapfile`, `[[ ]]`) |
| sqlite3 | 3.35+ | state.db; WAL mode required |
| jq | 1.6+ | JSON parsing for LLM responses |
| git | 2.28+ | worktrees, merge, rebase |
| claude CLI | 2.1+ | `claude --print` subprocess workers |

All deps invoked as external processes — nothing is bundled.

## License

Apache-2.0. See [LICENSE](LICENSE).
