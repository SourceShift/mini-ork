# Session task judgment — 2026-10-10

## Goal

Decide, with three independent model families (MiniMax-M3, GLM-5.3,
DeepSeek-V4-Flash), which of this session's six pending tasks still have
positive impact and which are stale — then reconcile into one execution
order. Each judge also grades the proposed solution for every task.

## Deliverable

Exactly one: `synthesis.md` in the run dir — the reconciled verdict matrix,
consensus/dissent breakdown, and execution order.

## Success criteria

1. All three judge reports exist with the five sections in order and 12
   verdict blocks each (6 tasks + 6 solutions).
2. All three sidecars share one schema; the panel gate passes:
   `MINI_ORK_RUN_DIR=<run dir> .venv/bin/python recipes/session-task-judge/verifiers/panel-completeness.py`
3. No repo source file was modified by any judge.

## Scope

READ-ONLY. Repo root: `/Volumes/docker-ssd/ps/mini-ork` (branch `main`).
Secondary repo: `/Users/admin/ps/zed-mini-ork` (branch `mini-ork`). Run dir
of the failed run under judgment: `.mini-ork/runs/run-1791567564-35413/`.
You may run git read commands, `ls`, `grep`, and read files — including
`git -C /Users/admin/ps/zed-mini-ork ...`. You may not edit anything.

Staleness is judged against the CURRENT tree and CURRENT git log — not
against the session's own account of its leftovers. Two receipts below are
in deliberate conflict (T1); adjudicate against primary evidence.

## Tasks under judgment

### T1 — Revive failed run run-1791567564-35413 from its salvage patch

- Claim: the run failed on an artifact-scope sweep, but its reviewer blessed
  a 2-file edit (`mini_ork/recovery/retry_notify.py` +
  `tests/unit/test_retry_notify.py`): `_step_code` must split a multiline
  NEEDS-CHANGE detail into one `  - ` sub-bullet per reason instead of
  packing it into a single bullet. The blessed pair is recoverable from
  `.mini-ork/runs/run-1791567564-35413/salvage.patch` (49.3 KB, a 15-file
  dirty-tree sweep — only the 2 blessed files are in scope) and
  `review-reviewer.json` in the same run dir records the blessing.
- Countervailing receipt: commit `e72cb34d` ("fix(retry_notify):
  NEEDS-CHANGE reasons as sub-bullets under the step", 2026-10-09 20:42)
  landed exactly that pair — 2 files, +34/−7 — and
  `tests/unit/test_retry_notify.py` exists on main (36.8 KB). Check whether
  anything in the blessed pair is still missing from `main`.
- Reproduce: `git log --oneline -- mini_ork/recovery/retry_notify.py`;
  `grep -n "_step_code" mini_ork/recovery/retry_notify.py`;
  `git show e72cb34d --stat`.

### T2 — Push the zed fork's `mini-ork` branch

- Claim: the Zed fork `/Users/admin/ps/zed-mini-ork` carries local-only
  commits on branch `mini-ork` (HEAD `707c74b`, the DAG-strip auto-fit fix;
  plus `d5dfbce` before it) with no upstream: `git rev-parse origin/mini-ork`
  fails (`fatal: Needed a single revision`). Pushing protects the work
  against a lost clone.
- Reproduce: `git -C /Users/admin/ps/zed-mini-ork rev-parse --short HEAD`;
  `git -C /Users/admin/ps/zed-mini-ork rev-parse --verify origin/mini-ork`.

### T3 — Execute the artifact-completion-contracts kickoff (item D)

- Claim: `kickoffs/auto/artifact-completion-contracts.md` (5.5 KB) is fully
  designed but never executed — items A/B/C of its parent effort landed
  (verify via `git log --oneline --grep="artifact"` and the registry /
  execute_compat history), item D is the remainder: declared-artifact
  completion contracts in the run flow.
- Reproduce: `ls kickoffs/auto/artifact-completion-contracts.md`; read it;
  check the scheduler queue state if reachable.

### T4 — Jest grader adapter + high-water-mark incremental mining

- Claim: the held-out eval stack (`scripts/run_heldout.py`,
  `scripts/mine_heldout_tasks.py`) grades only pytest suites, and mining is
  a full re-run; the researcher repo
  (`/Volumes/docker-ssd/Migration/Development/researcher`) uses jest, so its
  `fix:` commits cannot feed the
  task set. An adapter (pytest | jest grader) plus high-water-mark
  incremental mining would grow the set cross-language.
- Reproduce: `grep -n "pytest" scripts/run_heldout.py | head`; read
  `evals/heldout/` docs for the grader assumption.

### T5 — Hygiene commit of the dirty tree

- Claim: `git status` shows ~10 modified tracked files (among them
  `mini_ork/gates/promotion_gate.py`, `mini_ork/learning/verifier_audit.py`,
  `mini_ork/recovery/circuit_breaker.py`, `db/migrations/0060_collapse_
  history.sql`, `recipes/code-fix/verifiers/test.py`,
  `.mini-ork/config/agents.yaml`, `README.md`) and ~103 untracked files
  (`kickoffs/auto/*`, `evals/heldout/results/*`, docs). Nothing is staged
  right now. Left dirty, concurrent sessions keep sweeping each other's
  work into unrelated commits.
- Reproduce: `git status --porcelain | head -20`; `git status --porcelain |
  wc -l`.

### T6 — ram-sentinel pytest kill-protection veto

- Claim: `~/bin/ram-sentinel.py` gained `pytest` in its KILL_PROTECTED
  substring list (alongside cargo/rustc), pending a user veto that never
  came. Protection prevents the sentinel from killing runaway pytest
  processes; the precedent (build tools) argues keep, the memory-hog risk
  argues narrow it to `python3 -m pytest`.
- Reproduce: `grep -n "KILL_PROTECT" ~/bin/ram-sentinel.py`.

## Proposed solutions

- **P1 (→T1)**: diff the salvage patch's blessed pair against `main` at
  `e72cb34d`; resurrect only hunks still missing, via
  `make worktree SLUG=retry-notify-revive` → apply → scoped green gate →
  `make worktree-merge`. If the diff is empty, drop T1 and record it stale.
- **P2 (→T2)**: `git -C /Users/admin/ps/zed-mini-ork push -u origin mini-ork`.
- **P3 (→T3)**: execute the kickoff through a worktree owning the
  `mini_ork/` + `recipes/` surfaces it touches; verify with the recipe's
  own gates; merge via the green gate.
- **P4 (→T4)**: add a grader-adapter seam to `scripts/run_heldout.py`
  (`--grader pytest|jest`, jest path = `npx jest --json` parse of
  fail_to_pass ids), and a `--since <ref>` high-water-mark filter to the
  miner so only new fix commits are graded for candidacy.
- **P5 (→T5)**: three grouped commits — (a) the modified tracked code+tests
  after each is re-checked against its owning effort, (b) kickoffs/docs,
  (c) evals/heldout results after a secret scan (results embed run costs
  and paths, not keys — verify). Never one sweep commit.
- **P6 (→T6)**: keep `pytest` protected but narrow the match to the full
  phrase `python3 -m pytest` so `pytest` run under other wrappers stays
  killable; document in the sentinel header.

## Judge instructions

For every task: re-run the receipts above against the live tree, decide
`positive_impact` / `partially_positive` / `stale` / `unverifiable`, and
grade the paired solution `sound` / `partially_sound` / `unsound` /
`unverifiable`. Severity is the cost of leaving the task undone (or, for a
stale task, of doing it anyway). Where your verdict turns on a premise the
kickoff got wrong, record the correction. Propose your own better solution
inside the solution block's `reasoning` when the proposed one is weak —
the user asked every judge for a solution, not just a verdict.
