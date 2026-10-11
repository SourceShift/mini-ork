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
| base is ancestor of HEAD | 59 | reuse-only (guard fires if implementer re-runs) |
| base object missing (gc'd) | 1 | was no → now yes (`bf5a02e9`) |
| base **orphaned** (diverged) | **91** | was no → **now yes** (`bf5a02e9`) |
| base not in the repo (other target / gc'd) | 76 | unknown target |

(An earlier count said 174 orphaned; it lumped in the 76 whose base object is
not even in the mini-ork repo. Corrected 2026-10-11.)

## Done this session

- Triaged the 10 failed revive targets → all 10 **closed as superseded**
  (2 already landed in main; 8 orphaned-base). Exit-75 gates rejected. See
  `docs/todos/2026-10-10-revive-set-base-drift-triage.md`.
- Landed at-source fix **`7e113de9`** (via mini-ork `framework-edit` run
  `run-1791665283-39803`, verdict pass). On a resume that re-runs the implementer,
  refresh `pre-implementer-ref` to current HEAD. 8 new tests pass.

## Remaining tasks

### T1 — Orphaned-base failed runs  ·  **RESOLVED (2026-10-11)**
- **Done:** corrected census = **91** confirmed orphaned (base in the mini-ork
  repo but diverged from HEAD), + **76** whose base object isn't in the repo
  (other target / gc'd). Of the 91: **43 landed** on `origin/main` → superseded,
  **20** not-landed (first rounds of landed series), **28** no-artifact
  (reviews / migrations / child runs). Full table:
  `docs/todos/2026-10-11-orphaned-run-census.md`.
- **Gates closed:** 13, 26, 28 (superseded); 27 was not pending.
- **Reviving is now possible** (fix `bf5a02e9` unblocks recover) but **do NOT**
  recover one against the PRIMARY while a peer run is live — a live
  framework-edit stashes the launching tree, and these runs target
  `/Volumes/docker-ssd/ps/mini-ork`. Re-launch **fresh in a worktree**:
  ```bash
  make worktree SLUG=<slug>
  cd /Volumes/docker-ssd/ps/mini-ork-worktrees/<slug>
  MINI_ORK_HOME=/Volumes/docker-ssd/ps/mini-ork/.mini-ork \
  MO_TARGET_CWD=$PWD MO_ALLOW_FRAMEWORK_CWD=1 \
  MINI_ORK_AGENTS=/Volumes/docker-ssd/ps/mini-ork/.mini-ork/config/agents.local.yaml \
    /Volumes/docker-ssd/ps/mini-ork/bin/mini-ork run framework-edit <abs kickoff>
  ```

### T2 — Refine the base-drift fix  ·  **DONE (2026-10-11)**
- Landed **`bf5a02e9`** via framework-edit `run-1791723941-11171` (verdict pass,
  9 nodes / 0 failed, rubric 8/8). A baseline that is **not** an ancestor of
  HEAD now refreshes unconditionally, skipping the ledger refusal; only a
  forward-ancestor baseline reaches it. +2 tests; live smoke green (fixed engine
  refreshes + guard passes on `run-1791541963-98230`; old engine refuses +
  FAILs). See the triage doc.

### T3 — Compact the memory index  ·  **partial — still above target**
- `~/.claude/projects/-Volumes-docker-ssd-ps-mini-ork/memory/MEMORY.md` was
  **19.9 KB**; compacted to **~19.0 KB** by merging cluster lines and dropping
  5 resolved entries. The hook wants `< 17.1 KB`.
- The remaining gap cannot be closed by trimming prose: the ~250 filenames are
  ~13 KB of the file and each hook is only tens of bytes, so reaching 17.1 KB
  requires *dropping* ~20 memory links — a curation call, deliberately left
  open. A peer session also writes MEMORY.md concurrently, so a full rewrite
  risks a lost update.

### T4 — Pre-existing test failure  ·  **RESOLVED — merged `3abfdd3e`**
- `tests/unit/test_mini_ork_execute_py.py::test_learning_static_lane_parity`
  failed on any machine with a reachable `.mini-ork/` home: the fixture deleted
  `MINI_ORK_HOME`/`MINI_ORK_ROOT` instead of pointing HOME at an empty dir, so
  `resolve_lane_family` walked the ambient repo home and still found a mapping —
  the "unmapped" premise was false. It passed in CI only because CI has no home.
- **Note on the earlier "do NOT fix unless asked":** the session's approved task
  plan listed this as `local-test-debt`, and the repair changes only the fixture
  — no expected value, assertion, or skip. Fixed, verified
  (`PYTHONPATH=<wt> .venv/bin/python -m pytest tests/unit/test_mini_ork_execute_py.py -q`
  → 80 passed), and merged as `3abfdd3e` (kickoff `c84e9b65`).

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
