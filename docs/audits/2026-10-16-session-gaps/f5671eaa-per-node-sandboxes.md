# Session gaps audit — `f5671eaa-8c0c-4051-9477-10aadbf837fd`

Session: the mini-ork session that carried the Daytona per-node sandbox work on
2026-10-15/16. Audited 2026-10-16.

Task source: ContextNest `/api/v1/inbox` (500 rows, filtered on
`session_id == f5671eaa-8c0c-4051-9477-10aadbf837fd`) plus
`/api/v1/features?since=24h` (77 rows). The inbox attributes ~40 rows to this
session, accumulating since 2026-10-15T13:08Z — far more than the task the
session was actively carrying when it was audited.

**Scope honesty:** this audit verified only the tasks with a concrete artifact
in the engine, git, or CI. The large inherited set (SDE-\*, VG-\*, hr-findings,
buddy-interactive, the `user_action` rows) is listed as *recorded, not verified
by this session* — several name epics other sessions own, and asserting their
status from here would be guessing. Spot-checks that did land are recorded with
shas.

## (a) Task table

| id | description | recorded | VERIFIED | evidence |
|---|---|---|---|---|
| `per-node-isolation-design` | Per-node sandbox isolation + artifact transport design | in_progress | **SPLIT — isolation SHIPPED, artifact transport DESIGN ONLY** | see gap G1 |
| `per-node-sandboxes` | per-node sandbox isolation | completed | **DONE** | `dd15418f` is ancestor of HEAD; `mini_ork/remote/providers/run_nodes.py` +319 lines from that commit; live run `run-1792137016-90593` → `verdict: pass` |
| `refresh-local-main` | Refresh local main (blocked by another session's dirty tree) | failed | **SUPERSEDED, then RE-DRIFTED** | FF completed this session; primary is stale again now |
| `primary-sync` | Fast-forward primary checkout to origin/main | pending | **STILL OPEN** | `git rev-list --left-right --count origin/main...HEAD` → `0  1` (primary `beca73ef`, origin `63fc80a0`) |
| `ci-verify` | Confirm CI run 38786840034 green | in_progress | **VERIFIED GREEN, but the head is stale** | `gh run view 38786840034` → `"conclusion":"success"`, `headSha f1a076b245dc9754f8820c958ede93b95c2d8e22` — a head now many commits behind `main` |
| `sde-3b2` | SDE-3b2 diagnosis ledger + calibrated bar | in_progress | **DONE (spot-check)** | `6f60a89c feat(recovery): diagnosis ledger + calibrated escalation bar (SDE-3b2)`, ON-MAIN; `mini_ork/stores/diagnosis_ledger.py`, `mini_ork/recovery/escalation.py` |
| `sde-4` / `sde-3b1` | SDE supervisor / model diagnoser | pending / in_progress | **PARTIAL (spot-check)** | `mini_ork/recovery/escalation_policy.py` (`e76c13ab`, ON-MAIN), `mini_ork/recovery/escalation.py` (`6f60a89c`), `mini_ork/recovery/auto_repair.py`, `mini_ork/scheduler.py` (`d70f94cf`, 2026-10-16) |
| `hr-findings`, `hr-findings-confirm`, `relaunch-cn-ui-doc`, `vg-1c`, `vg-3`, `buddy-interactive`, and all `user_action` rows | — | in_progress / pending / none | **NOT VERIFIED BY THIS SESSION** | names epics other sessions own; asserting status from here would be guessing |

## (b) Missing features & gaps

**G1 — Artifact transport is a design document, not a feature. (Highest value.)**
The task this session owned reads "Per-node sandbox isolation **+ artifact
transport design**", and only the first half shipped.
`docs/architecture/artifact-transport.md` exists (15,205 bytes, committed
`0792dc43 restore(sdd-p2): artifact-transport doc + kickoff + roadmap epic`) but
it is a *design + roadmap epic*: `git log -S artifact_transport -- mini_ork/`
matches no implementing commit, and the only string match inside the engine is
the pointer comment at `mini_ork/remote/providers/run_nodes.py:20`
("`docs/architecture/artifact-transport.md` — per-node isolation does not
[solve this]").

The transport that does exist is pre-existing `mini_ork/remote/tree_sync.py`
(git-bundle snapshot / `apply_delta` / `SyncConflictError`). And the gap the doc
names is still open: a recipe that declares a *path* requirement
(`write the report to docs/research/<dir>/`) is unsatisfiable across isolated
machines, because no two nodes share a filesystem. The doc's own evidence is
survey run `run-1791749486-16681` (recipe `audit-judge-panel-3l`): the required
`docs/research/20261012-langgraph-parity-survey/` was never created, reports
stranded in the run dir, a rollback node ran, nothing published.

This is a **spec-vs-shipped mismatch against the user's stated requirement**.
The user asked for artifacts to be shared between nodes "using a shared file
system on the cloud", and said "it should be like that". What ships instead is a
*local-disk relay*: `docs/architecture/per-node-sandboxes.md:45` — "The local
worktree is the handoff between nodes. A node's replica syncs down into the
local worktree at teardown (`RemoteWorkspace._sync_down`) and the next node
syncs up from it (`RemoteWorkspace._sync_up`)." That satisfies a *sequential*
recipe (and is why `run-1792137016-90593` passed), but it is a host-mediated
relay, not a cloud shared filesystem, and it does not give a node a home for a
recipe-declared `path`.

**G2 — Per-node isolation boots N+1 sandboxes, not N.**
`MO_NODE_ISOLATION=1` still boots one run-level sandbox for the whole run, in
addition to one per node. Live evidence, run `run-1792137016-90593`: six live
sandboxes for five nodes, the extra being `mo-run-1792137016-90593` with
`mo.node=None`. This is **deliberate, not a defect**: `run_nodes.isolated_node`
returns `""` for a process with no node to name, so that a between-nodes process
is not stranded, and `_provision_remote_session` runs before the first node so it
has no node identity. Disclosed in `docs/architecture/per-node-sandboxes.md`
(*Verified* / *Cost*). Flagged here only because it does not match the user's
"always one node per sandbox" framing literally, and a reader who takes that
framing at face value will expect N.

**G3 — The merge gate for this work was scoped, not the full suite.**
The change merged on a scoped pytest gate (23 files, 328 passed / 1 skipped).
`dd15418f` has not been validated against the full `python3 -m pytest -q` suite
in this session. GitHub CI is the only full-suite coverage; note that `main` CI
shows `cancelled` on every superseded head, so CI status is a weak signal here.
Low risk (the touched surface was covered) but it is an unverified claim.

**G4 — Primary checkout drift recurs faster than a manual sync.**
`refresh-local-main` was recorded `failed` on 2026-10-15 (blocked by another
session's dirty tree). This session did fast-forward the primary
(`2d786fec..dd15418f`), and it has already re-drifted: primary `beca73ef` vs
`origin/main` `63fc80a0`, 0 ahead / 1 behind. With 51 commits landing on `main`
between `dd15418f` and the current HEAD, a one-shot FF does not hold. `primary-sync`
is therefore open again.

## (c) Blocked on

- **G1 (artifact transport)** — blocked on a design decision the user explicitly
  deferred: whether cross-node artifacts move through a cloud shared filesystem
  (the user's stated preference) or stay on the local-worktree relay with
  explicit `path`-requirement handling. The roadmap epic is committed; the
  implementation is not started.
- **G4 (primary-sync)** — blocked on other sessions' concurrent writes to the
  primary working tree; the revert-safe move is a `--ff-only` when it is clean,
  which cannot be scheduled from here.
- **Inherited task set** — not blocked, but not verifiable from this session;
  needs the owning sessions' confirmation (the peer aggregating this report is
  the right place to reconcile them).

## Commands actually run

```
git log --oneline -S "MO_NODE_ISOLATION" -- mini_ork/          → dd15418f (only hit)
git merge-base --is-ancestor dd15418f HEAD                     → ancestor
git show --stat dd15418f -- mini_ork/remote/providers/run_nodes.py → +319 lines
git rev-parse HEAD origin/main                                 → beca73ef / 63fc80a0
git rev-list --left-right --count origin/main...HEAD           → 0	1
gh run view 38786840034 --json status,conclusion,headSha       → success, f1a076b2
git log --oneline -1 -- mini_ork/stores/diagnosis_ledger.py    → 6f60a89c (SDE-3b2)
curl -s localhost:28080/api/v1/inbox (filtered by session_id)  → ~40 attributed rows
curl -s localhost:28080/api/v1/features?since=24h              → 2 features from this session
```
