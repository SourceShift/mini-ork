# Recursive Self-Improvement Run

## Goal

Run mini-ork against itself: find and fix the highest-impact bottlenecks
across performance, correctness, and architecture. Allow new infrastructure
(graph DB, additional verifiers, telemetry sinks) when the synthesis
justifies it with cited evidence.

## Scope

- Target repo: this mini-ork checkout (`MINI_ORK_ROOT`).
- Each iteration runs inside an isolated git worktree on a branch named
  `self-improve/iter-<N>-<timestamp>`.
- Patches are auto-merged into the parent branch only when all three
  verifiers (bottlenecks-found, self-tests-pass, no-regression) pass.
- Failed iterations preserve lens reports + arXiv refs + the failing
  diff under `.mini-ork/runs/<run_id>/patches/` for later review.

## Success Criteria

- The bottleneck scanner produces a ranked list with at least one
  evidence-grounded candidate per category (perf / correctness / arch).
- The arXiv research lane cites at least one paper relevant to the
  top-ranked bottleneck.
- The Opus synthesizer emits a patch plan referencing both internal
  evidence (run logs, code paths, benchmark deltas) and external
  evidence (arXiv refs).
- Existing tests pass and benchmark utility scores show non-negative
  delta on the implemented patch.

## Provider Policy

- Researchers: `minimax_lens`, `kimi_lens`, `codex_lens` (no Anthropic).
- Synthesis reviewer: `opus_lens`.
- Implementer: `codex_lens`.
- No `glm_lens` to avoid same-family ρ collapse with reviewer in
  recipes that later add an Opus-arch lens.

## Verification Command

- `python3 -m pytest -q` (the Python runtime is the only runtime; the
  pre-2026-07 bash `tests/run-all.sh` is retired).
- Plus the `benchmark_results` delta rollup (`mini-ork eval`).
