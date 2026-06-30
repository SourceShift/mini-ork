---
name: mini-ork
description: >-
  Use whenever an agent needs to drive the mini-ork orchestrator from ANY repo —
  the repo that vendors mini-ork into its own `.mini-ork/`, not only the mini-ork
  source itself. mini-ork is a "task operating system for agents": a universal
  loop (classify→plan→execute→verify→reflect→improve→eval→promote), a `run
  <recipe> <kickoff>` recipe runner, a native multi-epic `epics`+`scheduler`
  delivery mode, heterogeneous model lanes (opus/sonnet/codex/kimi/minimax/glm),
  a GRPO learning loop, and the full `MO_*`/`MINI_ORK_*` env surface. Trigger on
  "run this kickoff", "dispatch a framework-edit / code-fix", "fix this via
  mini-ork", "ingest this roadmap / run the scheduler", "what recipe should I
  use", "how do I use mini-ork in this repo safely", or any question about
  mini-ork recipes, lanes, env knobs, the epics/scheduler queue, or the
  orchestrator lifecycle. Read the SAFE-USAGE CONTRACT first — getting cwd,
  MINI_ORK_ROOT, or kickoff sizing wrong causes the failures this skill exists to
  prevent (cross-repo corruption, hollow plans, silent lane stalls).
---

# mini-ork — driving the orchestrator from a consuming repo

mini-ork is a framework you **vendor into your repo** (a `.mini-ork/` directory)
and drive to do multi-file work through a verifier-gated loop instead of editing
by hand. This skill is the precise reference so an agent uses it correctly and
does not improvise flags, paths, or recipes.

> **Read the SAFE-USAGE CONTRACT (next section) before running anything.** Every
> serious failure mode this skill prevents — corrupting another repo, a hollow
> truncated plan, a 19-minute silent stall — comes from violating one of its
> three rules.

---

## SAFE-USAGE CONTRACT (non-negotiable)

### 1. cwd / target isolation — never let a lane run inside the framework tree
A provider lane (codex, claude) runs git operations in **its working
directory**. If that cwd drifts into a mini-ork *framework* tree instead of the
**target repo**, the lane's git ops corrupt the framework repo (observed:
`refs/codex/curated-sync` reset to a foreign commit, wiping the tree).

- **Always export `MO_TARGET_CWD=<absolute path to YOUR repo's worktree>`** before
  dispatching. It is the directory lanes run in.
- **Use ABSOLUTE paths in kickoffs** (`/abs/path/to/file`), never bare relative
  paths that resolve against an unknown cwd.
- The dispatcher refuses a lane whose cwd lands inside the mini-ork framework
  tree (`cwd guard failed`). For a genuine mini-ork self-edit only, set
  `MO_ALLOW_FRAMEWORK_CWD=1`.
- The framework repo additionally self-protects: a `reference-transaction` hook
  rejects any ref write to a commit foreign to its history (override:
  `MO_ALLOW_FOREIGN_REF=1`). Do not disable these.

### 2. Install / root isolation — point at YOUR vendored copy
- `MINI_ORK_ROOT` = your vendored install dir (the dir containing `bin/` `lib/`),
  e.g. `<your-repo>/.mini-ork`. **Never point it at a shared mini-ork source
  clone.**
- `MINI_ORK_HOME` = where state lives, normally `<your-repo>/.mini-ork`. It holds
  the gitignored, **non-git-recoverable** `state.db` (GRPO learning) and
  `config/secrets.local.sh` (API keys).
- Run the binary from your install: `"$MINI_ORK_ROOT/bin/mini-ork" …`.

### 3. Kickoff sizing — one deliverable per kickoff
An oversized kickoff truncates the planner JSON into a **hollow plan** and the
run wastes its budget. Keep each kickoff to **one deliverable**. For several
deliverables, write several kickoffs (disjoint code regions can run in
parallel) or ingest a roadmap as `epics` (below).

---

## The three delivery modes — pick one first

| You want… | Mode | Entry point |
|---|---|---|
| Run one kickoff through the loop / a recipe | `bin/mini-ork run` | `"$MINI_ORK_ROOT/bin/mini-ork" run [<recipe>] <kickoff.md>` |
| Fan a roadmap into dependency-ordered epics, dispatched serially by readiness, auto-merged to main | native `epics` + `scheduler` | `bin/mini-ork epics split <roadmap.md>` → `bin/mini-ork scheduler` |
| Fan a plan into N parallel worktree-isolated tracks | agentflow `.agentflow/mini-orch/deliver.sh` | a SEPARATE pipeline (not `bin/mini-ork`) |

`run` for one deliverable. `epics`+`scheduler` for many dependency-ordered epics
(serial, same tree). agentflow for many independent parallel tracks.

---

## `bin/mini-ork run` — the common path

```bash
export MINI_ORK_ROOT="$PWD/.mini-ork"          # your vendored install
export MINI_ORK_HOME="$PWD/.mini-ork"          # state + secrets live here
export MO_TARGET_CWD="$PWD"                     # lanes run HERE (your repo)

# auto-classify + walk classify→plan→execute→verify
"$MINI_ORK_ROOT/bin/mini-ork" run kickoffs/my-thing.md

# force a recipe (skip classification)
"$MINI_ORK_ROOT/bin/mini-ork" run framework-edit kickoffs/change.md
"$MINI_ORK_ROOT/bin/mini-ork" run code-fix       kickoffs/fix-bug.md

# lifecycle
"$MINI_ORK_ROOT/bin/mini-ork" init      # bootstrap .mini-ork/
"$MINI_ORK_ROOT/bin/mini-ork" doctor    # check deps, env, lib presence
"$MINI_ORK_ROOT/bin/mini-ork" version
```

A kickoff is a markdown file stating ONE deliverable: goal, concrete acceptance
criteria, and absolute paths to the files in scope.

### Recipes (force with `run <recipe> <kickoff>`)
`framework-edit` (propose-not-commit framework change, produces a diff),
`code-fix` (apply a bug fix), `bug-audit`, `epic-runner`, `research-synthesis`,
`recursive-self-improve`, `chapter-review`, and ~24 more under
`$MINI_ORK_ROOT/recipes/`. List them: `ls "$MINI_ORK_ROOT/recipes/"`. Don't
invent recipe names — check the directory.

---

## Model lanes (the routing policy)

Lanes map a loop role to a model wrapper (`lib/providers/cl_<lane>.sh`), set in
`$MINI_ORK_HOME/config/agents.yaml`.

- **Implementer / code lanes:** `codex`, `minimax`, `kimi`. **Not** `glm`
  (analysis-only) and **not** the `claude`/`opus` lanes for implementation.
- **Reviewer / judgment lanes:** `opus` is allowed (strongest reasoner) for the
  final reviewer / synthesizer / cross-epic prioritizer.
- Each gateway lane needs its key in `config/secrets.local.sh`
  (`GLM_API_KEY`, `KIMI_API_KEY`, `MINIMAX_API_KEY`, …). **A missing key makes
  the lane die silently** — the dispatcher's pre-flight (`lane_health`) now fails
  fast with `lane preflight failed: $X_API_KEY is not set`, but set the keys
  before a run regardless.

---

## Multi-epic delivery (`epics` + `scheduler`)

```bash
"$MINI_ORK_ROOT/bin/mini-ork" epics split  <roadmap.md>   # → dependency-ordered epics in state.db
"$MINI_ORK_ROOT/bin/mini-ork" epics list                  # inspect the queue
"$MINI_ORK_ROOT/bin/mini-ork" scheduler                   # dispatch ready epics serially, auto-merge to main
```

- **The scheduler has no epic filter** — bare `scheduler` drains EVERY
  not-started ready epic in `state.db`. Check `epics list` first; for a specific
  roadmap, dispatch your epic ids in a scoped loop.
- **The scheduler is long-running.** Do NOT launch it from inside an interactive
  agent turn (it gets reaped at the turn boundary). Run it under a real
  supervisor — `tmux`/`launchd`/CI — or in your own terminal.

---

## Common pitfalls → the rule that prevents them

| Symptom | Cause | Fix |
|---|---|---|
| Another repo's files get wiped / `refs/codex/*` appears | a lane's cwd drifted into the framework tree | set `MO_TARGET_CWD` to your repo; use absolute paths |
| Hollow/generic plan, budget wasted | oversized kickoff truncated the planner JSON | one deliverable per kickoff; split or use `epics` |
| A lane silent for ~19 min then nothing | missing API key / dead provider | set keys in `secrets.local.sh`; the lane pre-flight fails fast now |
| Lane read the wrong repo | `MINI_ORK_ROOT`/cwd pointed at the framework, not your repo | pin `MINI_ORK_ROOT` to your `.mini-ork`, `MO_TARGET_CWD` to your repo |
| Scheduler dies, DAG stalls | launched from a harness turn (reaped) | run under tmux/launchd/CI, not in-turn |

---

## Updating mini-ork safely

Re-vendor the latest framework code into your `.mini-ork/` **without touching the
source clone or your state**: rsync the framework dirs (`bin/ lib/ recipes/ db/`)
and **exclude** the gitignored state — `state.db`, `config/secrets.local.sh`,
`runs/`. Never `git pull` the source clone into your repo, and never run the
source clone's binary against your repo (that is the cwd-confusion vector).

---

## Env surface (most-used)

`MINI_ORK_ROOT`, `MINI_ORK_HOME`, `MO_TARGET_CWD`, `MINI_ORK_PROFILE_GATE`
(0 to skip the planner Q&A gate), `MO_AUTO_ANSWER_PROFILE`, `MO_NODE_TIMEOUT_S`,
`MO_ROUTING_POLICY` (`learning_governed` default), `MO_ALLOW_FRAMEWORK_CWD`,
`MO_ALLOW_FOREIGN_REF`. Full list: `grep -rhoE 'MO_[A-Z_]+|MINI_ORK_[A-Z_]+'
"$MINI_ORK_ROOT/lib" | sort -u`.

When unsure of a flag, recipe, lane, or path: **read it from the install**
(`ls`, `grep`, `bin/mini-ork doctor`) rather than guessing. Improvised syntax is
the most common cause of wasted runs.
