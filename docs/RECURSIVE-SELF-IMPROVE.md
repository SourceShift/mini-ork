# Recursive Self-Improvement Loop

`bin/mini-ork-self-improve` is a wall-clock-budgeted outer loop that drives
the `recursive-self-improve` recipe against the mini-ork checkout itself.
Each iteration scans the repo + the run database + benchmark deltas for
bottlenecks, dispatches three heterogeneous-family research lenses plus an
arXiv research lane, asks Opus to synthesize a ranked patch plan, and has
Codex apply the top patch inside a git worktree gated by three
deterministic verifiers.

## Quickstart

```bash
# One iteration, no LLM calls — proves wiring is OK.
bin/mini-ork-self-improve --dry-run --max-iters 1

# Real run: 3h soft cap, 5h hard cap, single commit per successful iter,
# no auto-merge (user reviews branches).
bin/mini-ork-self-improve --soft-cap-hours 3 --hard-cap-hours 5

# Real run with auto-merge into the current branch.
bin/mini-ork-self-improve --soft-cap-hours 3 --hard-cap-hours 5 --auto-merge

# Resume after Ctrl-C or crash.
bin/mini-ork-self-improve --resume --soft-cap-hours 3 --hard-cap-hours 5
```

## Architecture

```
                      ┌──────────────────────────────────────────┐
                      │  bin/mini-ork-self-improve (outer loop)  │
                      │  - wall-clock budget                     │
                      │  - per-iter worktree                     │
                      │  - learning_record sqlite                │
                      │  - branch commit + optional auto-merge   │
                      └──────────────────────────────────────────┘
                                       │
                                       ▼  per iteration
                      ┌──────────────────────────────────────────┐
                      │  mini_ork/cli/execute.py                    │
                      │  --recipe recursive-self-improve         │
                      └──────────────────────────────────────────┘
                                       │
                                       ▼
      bottleneck_scanner ─┬─► perf_lens        (minimax) ─┐
                          ├─► correctness_lens (kimi)     ─┤
                          ├─► arch_lens        (codex)    ─┤
                          └─► arxiv_research   (codex)    ─┴─► opus_synthesizer (opus)
                                                                  │
                                                                  ▼
                                                          bottlenecks_found
                                                                  │
                                                                  ▼
                                                          implementer (codex)
                                                                  │
                                                                  ▼
                                                          self_tests_pass
                                                                  │
                                                                  ▼
                                                          no_regression
                                                                  │
                                                                  ▼
                                                              publisher
```

## Provider lanes

The outer runner stages `config/agents.recursive-self-improve.yaml` into
`$MINI_ORK_HOME/config/agents.yaml` before dispatching, so the recipe sees:

| Lane | Model | Why this family |
|---|---|---|
| `minimax_lens` | MiniMax-M3 | Perf voter; lowest pairwise ρ with Opus per recent multi-judge studies |
| `kimi_lens` | Moonshot Kimi | Correctness voter; strong on edge-case spotting |
| `codex_lens` | OpenAI Codex | Arch + arxiv voter; best repo-locality |
| `opus_lens` | Anthropic Opus | Synthesizer; final ranking + patch composition |
| `planner` | OpenAI Codex | Cheap planner for the bottleneck scan |

`reviewer` defaults to `opus` (Anthropic) but `opus_synthesizer` uses
`opus_lens` directly to make provenance unambiguous.

## Safety boundaries

1. **Worktree isolation.** Each iteration runs in
   `.mini-ork/worktrees/iter-<N>-<ts>` on a fresh branch named
   `self-improve/iter-<N>-<ts>`.
2. **Implementer never commits.** Codex writes the patch + the regression
   test in the worktree; the outer runner is the only thing that calls
   `git commit`, and only after all three verifiers pass.
3. **Branches stay quarantined** unless you pass `--auto-merge`. Failed
   iterations leave the branch alone so you can inspect the diff manually.
4. **Verifier triple gate:**
   - `bottlenecks-found` — synthesis has at least one ranked patch and is
     not polluted with leaked `★ Insight` / `<z-insight>` envelopes.
   - `self-tests-pass` — every existing `tests/{integration,unit}/test_*.sh`
     still passes. Refuses to vacuous-pass when zero suites are present.
   - `no-regression` — `bash -n` clean on every changed shell file;
     benchmark utility scores show non-negative delta when historical
     data is available; implementer's own report does not say
     `refused-*` or `failed-*`.
5. **Hard budget caps.** `--hard-cap-hours` kills mid-iteration via
   `timeout(1)` around the native execute runtime. Defaults: 3h soft / 5h hard.
6. **Convergence shortcut.** When the bottleneck scanner emits
   `## Status: converged`, the outer loop exits cleanly without running
   further iterations.

## Cost model

| Step | Family | ~Cost per iter |
|---|---|---|
| bottleneck_scanner | Codex | ~$0.20 |
| perf_lens | MiniMax | ~$0.30 |
| correctness_lens | Kimi | ~$0.20 |
| arch_lens | Codex | ~$0.40 |
| arxiv_research | Codex + jina MCP | ~$0.30 |
| opus_synthesizer | **Opus** | **$3-6** (dominant) |
| implementer | Codex | ~$0.50 |
| verifiers | — | $0 (bash) |
| **Per iter** | | **~$5-8** |
| **3h budget (3-4 iters)** | | **~$15-30** |
| **5h budget (5-7 iters)** | | **~$25-50** |

Caps live in `config/agents.recursive-self-improve.yaml`:

- `per_run_usd: 6.00` — one iteration
- `per_epic_usd: 50.00` — one outer-loop session
- `daily_cap_usd: 60.00` — global guard

## Cross-iteration learning

State lives in `$MINI_ORK_DB` (default `.mini-ork/state.db`):

- `self_improve_runs` — one row per iteration with outcome, deadlines,
  branch, and notes.
- `learning_record` — one row per ranked bottleneck. Status moves through
  `open → queued → attempted → resolved | rejected | deferred`.
- `self_improve_arxiv_refs` — many-to-many between iterations and the
  arXiv papers they cited. `used_in_patch` flips to 1 when a paper's
  technique actually lands in a successful patch.

Inspect after a run:

```bash
sqlite3 .mini-ork/state.db \
  "SELECT iter, outcome, notes FROM self_improve_runs ORDER BY iter;"

sqlite3 .mini-ork/state.db \
  "SELECT iter, category, title, outcome, severity, confidence
     FROM learning_record ORDER BY iter, rank;"

sqlite3 .mini-ork/state.db \
  "SELECT arxiv_id, title, COUNT(*) AS refs, SUM(used_in_patch) AS hits
     FROM self_improve_arxiv_refs GROUP BY arxiv_id, title
     ORDER BY hits DESC, refs DESC;"
```

## What the loop will *not* do

- Modify `main` directly — every commit lands on a `self-improve/iter-*`
  branch. Auto-merge is opt-in.
- Skip tests with `--no-verify`. Verifier failures route to rollback.
- Apply a patch with new infrastructure (graph DB, new lib helper, new
  MCP tool, new SQL table) unless `arxiv-refs.md` contains an arXiv
  paper supporting it. The opus_synthesizer drops unjustified-infra
  patches to lower-ranked, and the implementer refuses them with an
  `infra-unjustified` report if they end up at rank 1 by mistake.
- Run more than one patch per iteration. The runner's rollback model
  assumes a single point of failure per iter.

## Failure modes & recovery

| Symptom | Likely cause | Fix |
|---|---|---|
| `[err] migration 0017 failed` | sqlite3 missing | install sqlite3 |
| Outer runner exits immediately | invalid cap hours (soft>hard) | fix flags |
| Iter ends with `outcome=timed_out` | one Opus call hung | inspect `runs/<id>/execute.log`; consider lower per-iter budget |
| Iter ends with `outcome=failed` | scanner found no actionable items | inspect `runs/<id>/bottleneck-scan.md`; manual seed |
| All iters end with `outcome=rejected` | implementer can't satisfy verifiers | inspect `runs/<id>/implementer-report.md` + `patches/iter-*.diff` |
| Outer loop hits `--converged` quickly | mini-ork is stable enough that the scanner finds nothing | success |

## Extending

- New lens family? Add a `<name>_lens` to `config/agents.recursive-self-improve.yaml`
  and a new node in `recipes/recursive-self-improve/workflow.yaml`. Keep
  pairwise voter correlation low — avoid two researchers on the same
  provider family.
- Want a stricter no-regression gate? Edit
  `recipes/recursive-self-improve/verifiers/no-regression.sh` to require
  a positive benchmark delta rather than just non-negative.
- Want to suppress arXiv calls? Remove the `arxiv_research` node from
  `workflow.yaml` — the synthesizer will run without external evidence
  but new-infra patches will fail (correctly) at the synth-rank stage.

## Testing

```bash
bash tests/integration/test_recursive_self_improve_recipe.sh
```

Currently asserts: scaffolding presence (16 files), workflow lane
routing for all 7 dispatched nodes, agents override lane bindings,
migration idempotency + table presence, verifier behavior on empty /
polluted / clean / converged inputs, and the outer runner's `--dry-run`
+ invalid-cap rejection.
