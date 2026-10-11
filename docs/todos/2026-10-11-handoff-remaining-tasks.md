# Handoff — remaining tasks (2026-10-11)

For a new joiner. Read the **Glossary** at the end first if terms are unfamiliar.

## Background (why these tasks exist)

mini-ork runs **recipes** (multi-step agent workflows) against a repo. One recipe,
`framework-edit`, lets mini-ork edit **its own source**. Each run records a
**base commit** (`<run_dir>/pre-implementer-ref`) = the repo HEAD *before* the
implementer node edits the tree. A guard (`_implementer_moved_base`) checks the
tree HEAD still matches that base before harvesting the run's diff.

Problem found 2026-10-10: `main`'s history was **rewritten**, so the base commits
of many old runs are no longer ancestors of HEAD ("**orphaned base**"). Any
`recover` of those runs fails the guard — the diff would cross diverged history.

Census of **807 failed runs**:

| base state | count | recoverable in place? |
|---|---:|---|
| no base file | 580 | yes (base written fresh at recover) |
| base is ancestor of HEAD | 53 | reuse-only (guard fires if implementer re-runs) |
| base **orphaned** | **174** | **no** |

## Done this session

- Triaged the 10 failed revive targets → all 10 **closed as superseded**
  (2 already landed in main; 8 orphaned-base). Exit-75 gates rejected. See
  `docs/todos/2026-10-10-revive-set-base-drift-triage.md`.
- Landed at-source fix **`7e113de9`** (via mini-ork `framework-edit` run
  `run-1791665283-39803`, verdict pass). On a resume that re-runs the implementer,
  refresh `pre-implementer-ref` to current HEAD. 8 new tests pass.

## Remaining tasks

### T1 — Decide fate of 174 orphaned-base failed runs  ·  **BLOCKED on you**
- **What:** 174 failed runs can't be revived in place. Each is either still-wanted
  (→ fresh re-launch) or superseded (→ close).
- **How to list them:** run the classification (in the triage doc) or:
  `sqlite3 .mini-ork/state.db "SELECT id FROM task_runs WHERE status='failed'"`,
  then per run check `git merge-base --is-ancestor <base> HEAD`.
- **If wanted — fresh re-launch** (NOT `recover`; a fresh run gets a correct base):
  ```bash
  MO_TARGET_CWD=$PWD MO_ALLOW_FRAMEWORK_CWD=1 \
  MINI_ORK_AGENTS=$PWD/.mini-ork/config/agents.local.yaml \
    bin/mini-ork run kickoffs/auto/<slug>.md
  ```
- **If superseded — close its gate:**
  `bin/mini-ork board gate reject <inbox_id> --note "superseded: <reason>"`

### T2 — Refine the base-drift fix (optional)  ·  **open**
- **What:** landed fix refuses to refresh when the run's ledger records a prior
  `impl_moved_base`, so a run that *already* tripped the guard stays blocked even
  when its base is orphaned by an external rewrite (proved live on
  `rsi-i1-control-arm-…115150`).
- **Sharper rule:** also refresh when the base is **not an ancestor** of HEAD
  (can't carry the implementer's prior change into HEAD). Caveat: a forward
  ancestor base is ambiguous with a laundered commit — keep failing closed there.
- **Where:** `mini_ork/recovery/planner.py`, `mini_ork/cli/execute.py` (commit
  `7e113de9`). Route via mini-ork `framework-edit` (multi-file rule).

### T3 — Compact the memory index  ·  **open, low risk**
- `~/.claude/projects/-Volumes-docker-ssd-ps-mini-ork/memory/MEMORY.md` is
  **19.9 KB**; hook wants `< 17.1 KB`. Keep one line per entry; push detail into
  the topic files.

### T4 — Pre-existing test failure  ·  **do NOT fix unless asked**
- `tests/unit/test_mini_ork_execute_py.py::test_learning_static_lane_parity`
  fails. **Not** caused by `7e113de9` (that function isn't in the fix's files).
  Belongs to a peer's in-flight lane-mapping WIP.
- Repro: `python3.11 -m pytest -q tests/unit/test_mini_ork_execute_py.py::test_learning_static_lane_parity -p no:asyncio`

### T5 — Reopen a revive target if you disagree with "superseded"
- The 10 closed gates can be re-opened; the triage doc records the evidence per run.

## Glossary

- **run / run_dir** — one recipe execution; artifacts live in `.mini-ork/runs/<id>/`.
- **base / `pre-implementer-ref`** — the tree state to diff the implementer's change against.
- **orphaned base** — base commit exists as a git object but is no longer on `main` (history rewritten).
- **ground-truth guard** (`_implementer_moved_base`) — refuses to harvest a diff whose base no longer matches HEAD.
- **framework-edit** — the recipe mini-ork uses to edit its own source; *proposes* a diff, does not commit.
- **recover --strategy resume** — re-run a failed run's failed closure, reusing green nodes.
- **exit-75 gate** — a failed run blocks new dispatch until acked; each gate has an `inbox_id`.
- **lane** — the model/provider a node runs on (e.g. `glm`, `deepseek`, `minimax`).
- **worktree** — isolated checkout; mini-ork dev happens there, not on `main`.
