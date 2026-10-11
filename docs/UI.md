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
