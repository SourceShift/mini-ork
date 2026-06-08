# recipes/recursive-self-improve

Recursive self-improvement recipe for mini-ork. One iteration of this
recipe scans the running mini-ork checkout for bottlenecks, runs three
heterogeneous-family research lenses (minimax perf / kimi correctness /
codex architecture) plus an arXiv research lane, asks Opus to synthesize
a ranked patch plan, then has Codex apply the top patch in a git
worktree gated by three deterministic verifiers.

The outer driver `bin/mini-ork-self-improve` invokes this recipe in a
wall-clock-budgeted loop (3h soft cap, 5h hard cap).

## DAG

```
bottleneck_scanner ──┬─► perf_lens (minimax) ──┐
                     ├─► correctness_lens (kimi) ─┤
                     ├─► arch_lens (codex) ───────┤
                     └─► arxiv_research (codex) ──┴─► opus_synthesizer (opus)
                                                       │
                                                       ▼
                                              bottlenecks_found (verifier)
                                                       │
                                                       ▼
                                                  implementer (codex)
                                                       │
                                                       ▼
                                              self_tests_pass (verifier)
                                                       │
                                                       ▼
                                               no_regression (verifier)
                                                       │
                                                       ▼
                                                   publisher
```

Any verifier failure routes to `rollback`, which preserves lens
reports + arXiv refs + the failing diff for the next iteration's
context.

## Provider lanes

| Node | Lane | Model family | Why |
|---|---|---|---|
| bottleneck_scanner | `planner` | sonnet (default) | Cheap planning, cached on stable scan output |
| perf_lens | `minimax_lens` | MiniMax-M3 | Different family than reviewer (Opus) — keeps pairwise ρ low |
| correctness_lens | `kimi_lens` | Moonshot Kimi | Strong on edge-case spotting |
| arch_lens | `codex_lens` | OpenAI Codex | Strong on repo-level pattern recognition |
| arxiv_research | `codex_lens` | OpenAI Codex | Reuses code-grounded lane to map papers to code locations |
| opus_synthesizer | `opus_lens` | Anthropic Opus | Final ranking + patch-plan composition |
| implementer | `codex_lens` | OpenAI Codex | Patch authoring |

Override via `config/agents.recursive-self-improve.yaml`.

## Outer loop safety

The outer runner enforces:

1. Each iteration runs in a fresh `git worktree`.
2. Implementer never commits — the runner does, after verifiers pass.
3. Branches are named `self-improve/iter-<N>-<timestamp>` and never
   force-push.
4. The `learning_record` table preserves every iteration's evidence
   trail regardless of outcome.

See `docs/RECURSIVE-SELF-IMPROVE.md` for full operator guide.
