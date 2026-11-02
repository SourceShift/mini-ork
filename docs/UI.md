# The mini-ork UI and its API server

mini-ork's user interface is the **mini-ork IDE** — a Rust (GPUI) application
built as a fork of Zed, living in the **`zed-mini-ork`** repository. It draws
the Threads board, run forensics, project switcher and setup pages natively in
the fork's window.

The IDE's source lives in its own repository,
[`SourceShift/mini-ork-ui`](https://github.com/SourceShift/mini-ork-ui), which
this repo vendors as a git submodule at `ui/` (the fork vendors the same crate
at `crates/mini_ork_ui/`). Edit the crate there, not here — this repo cannot
compile it. It reaches mini-ork only through the CLI (`mini-ork board …`,
`automations …`, `init`) and, when a server is already up, the HTTP read surface
below. There is no web frontend here any more — the old React SPA and the
OpenHands `agent-canvas` fork it was replaced with have both been removed.

## What needs a person: the attention taxonomy

Every surface that says "needs you" says it through one declared table,
`mini_ork.attention.TAXONOMY`. A run is always in exactly one of four stages:

| stage | meaning |
|---|---|
| `person` | nothing moves until a human acts |
| `working` | the run is moving; waiting is the only action |
| `idle` | nothing has started |
| `ended` | over — done, or failed in a way that cannot be retried as-is |

`stage_of(run_id)` answers from the stores that already hold the facts
(`task_runs.status`, `mo_inbox_gates`, the cost-pause sentinel, the workspace
records, the retry hint); it is derived, never persisted, so the terminal and
the IDE cannot drift. The IDE's `outcome.resolve` words map onto it exactly:
`running → working`, `needs_you → person`, `failed → retry` (when the hint is
retryable, otherwise `ended`), `done → ended`.

The rows, in the order the inbox prints them — severity, not arrival:

| key | label | waits for | what to do | command |
|---|---|---|---|---|
| `cost_pause` | Paused | your budget decision | raise the budget, then resume | `mini-ork resume <run>` |
| `merge_approval` | Needs you | your approval to merge | read the diff, approve or reject | `mini-ork approvals approve <run>` |
| `node_review` | Needs you | your review of one node | accept the node or send it back | `mini-ork approvals approve <run> --node <node>` |
| `worktree` | Needs you | your keep-or-discard decision | inspect, then merge or discard | `mini-ork board merge <run>` |
| `retry` | Failed | your retry decision | read the retry hint, then retry | `mini-ork board retry <run>` |
| `oversight_gate` | Needs you | your resolution of a gate | approve or reject the gate | `mini-ork oversight --resolve <inbox> --status approved` |

Oversight is last on purpose: resolving one does not unblock a run, and it is
the only channel that accumulates (`oversight --calibrate` measures exactly
that).

`mini-ork inbox` prints the union — the six rows above, one row per run, never
two decisions for the same run:

```bash
mini-ork inbox            # human page, capped at 40 with a "showing N of M"
mini-ork inbox --json     # {"rows": [...], "total": N}; each row has run_id,
                          # what, why, next, label, state
mini-ork inbox --limit 5
```

Both tiers use the same command. It is read-only and needs no server: it
resolves `MINI_ORK_HOME` like every other subcommand, opens `state.db`
read-only, and never writes a retry hint into a run directory. The mutation is
always the `next:` command on the row — the inbox tells you what to run and
runs nothing itself.

## Building the IDE

The crate cannot be built from this repo: it is a member of the fork's cargo
workspace and inherits `edition` and every dependency from it, so the build lives
in `zed-mini-ork`.

```bash
git clone --recurse-submodules git@github.com:SourceShift/zed-mini-ork.git
cd zed-mini-ork
script/mini-ork-build                 # ~1 min after the first, long, build
open -a "mini-ork IDE" ~/path/to/project
```

`script/mini-ork-build` materialises the crate submodule itself
(`script/mini-ork-sync-ui`) and refuses to run if it is missing. Full detail —
job limits, `sccache`, `mini-ork-check` for a seconds-long type-check while
editing — is in the fork's `MINI-ORK.md`.

```bash
mini-ork serve        # JSON API at http://127.0.0.1:7090
```

`mini-ork serve` is **API-only**: it serves the JSON API at `/api/v1/…` and
returns a JSON hint at `/`. The server binds `127.0.0.1:7090` and is a local
tool — nothing here mutates pipeline state except the two explicitly-labelled
run controls (Stop / Kill) reachable through the API.
`GET /api` returns the index of every mounted endpoint.

---

## `/ide/*` — the desktop IDE's read surface

The IDE polls the board and opens pages by running `mini-ork board … --json` as
a subprocess, which costs a Python interpreter per read. When a server is
already up, the IDE asks these routes instead and falls back to the subprocess
for anything they do not answer.

| Route | Answers | CLI equivalent |
|---|---|---|
| `GET /ide/board?shell=1` | the shell poll (header, runs, counts) | `board --json --shell` |
| `GET /ide/page?key=…&tab=…&arg=…` | one IDE page | `board page <key> …` |
| `GET /ide/node?run=…&node=…&view=…&offset=…` | one node's detail | `board node <run> <node> …` |
| `GET /ide/runs?query=…&offset=…&limit=…` | run search | `board runs …` |

Every route parses the argv it would have been invoked with and hands it to
`board_cmd.payload_for_verb`, so both paths run the same code; `?home=` scopes
the read to a project (the schema's `X-Mini-Ork-Home` header works too), so one
server serves whichever project is open. Writes — `merge`, `gate`, `retry`,
`steer` — are deliberately **not** here: they stay on the subprocess path.

---

## Where the data comes from

| Surface | Backing store |
|---|---|
| Stat cards, run tables | `task_runs` (SQLite) |
| Cost figures everywhere | `llm_calls` ledger (billed result envelopes) |
| DAG + agent roster | recipe `workflow.yaml` + `execution_traces` + `run_events` |
| Transcripts | per-node `.transcript` / `.stream.jsonl` sidecars under `.mini-ork/runs/` |
| Learnings | `gradient_records`, `pattern_records`, `learning_record` |
| Trajectory | `task_runs` aggregates + `self_improve_runs` |
| Fingerprint | recipe lane → family resolution (`config/agents.yaml` + provider registry) |

Everything the IDE shows is reproducible with `sqlite3 .mini-ork/state.db`
queries and files on disk — the UI adds navigation and correlation, not
private state.
