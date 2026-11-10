# ADR-2026-11-10 — Disposition of the cycle-15 evidence and review worktrees

> Status: accepted. Closes the audit row `quarantine-cleanup`
> (`docs/audits/2026-11-04-task-status-audit/A-git-mo.md`), which asked for a
> decision rather than more machinery. Supersedes no prior ADR.

## Context

Between 2026-10-25 and 2026-10-31 the RSI cycle and its review loop parked seven
checkouts under `$MINI_ORK_WORKTREES_DIR` (default
`/Volumes/docker-ssd/ps/mini-ork-worktrees/`): five `review-*` trees holding the
exact commit a review panel had just judged, and two branch trees whose commits
had already reached `main` (one re-landed under a new SHA, one a plain
ancestor). Nothing in the workflow ever reads them — a review tree is a frozen
snapshot for a human to look at, and once the reviewed change is on `main` the
snapshot is a second copy of a file you can read from `git show`. The
`quarantine release` verb was fixed (`9f1a93a5`, it now drops the file claims a
quarantined run took) but the trees themselves were never dispositioned, so they
sat in every worktree listing (`ls` showed 128 entries at audit time) and each
one keeps its commits from being garbage-collected.

The question this decision answers: **what marks a parked evidence tree as
moot, and what is the evidence we keep before deleting it?**

## Decision

A parked tree is moot when its HEAD content is already on `main`, and that is
decidable without reading the diff — two probes cover every case seen:

- **duplicate of a landed commit**: `git patch-id --stable` of `HEAD` equals the
  patch-id of a commit on `origin/main`. The reviewed change landed under a new
  SHA (mini-ork merges by rebase), so the tree's SHA is not an ancestor even
  though its patch is byte-identical.
- **already an ancestor**: `git rev-list --count origin/main..HEAD` is `0`. The
  tree adds nothing at all.

A tree that neither probe clears is **not** moot, whatever its name suggests,
and is left alone: `w2-review-resume-from-waiting` looked like the rest and
turned out to hold a real unmerged fix (see "Found while closing").

Before removing a moot tree, record in this ADR: the tree, its HEAD SHA, the
landed SHA it duplicates (or "ancestor of main" for the second case), and the
patch-id. That is the whole evidence trail — a SHA in a document outlives a
reflog, and the content itself is reachable from the landed commit.

## The trees this decision removes

Probed 2026-11-10 with the two probes above (`git patch-id --stable` over
`git show <sha>`, and `rev-list --count origin/main..HEAD`):

| worktree | HEAD (parked) | landed as | patch-id | verdict |
|---|---|---|---|---|
| `review-f2-fresh-cache` | `5c731459` | `4b58cc68` | `4820eca5` | duplicate |
| `review-t0f-installer` | `d5a7b87e` | `90e08e75` | `73246a39` | duplicate |
| `review-t1-certificate` | `4ff3a0aa` | `56c6dacf` | `e95c99a7` | duplicate |
| `review-t1-collapse-floor-breaker` | `0f4778e7` | `10b73fd6` | `d4fc1b6a` | duplicate |
| `review-t1-kernel-hole-3` | `e178d24e` | `bcc98e10` | `142809e5` | duplicate |
| `t1-certificate` (branch `wt/t1-certificate`) | `a290f315` | `56c6dacf` | `e95c99a7` | duplicate |
| `review-verdict-evidence` (branch `wt/review-verdict-evidence`) | `d6430b1b` | — | — | ancestor of `main`, 0 ahead |

All seven were clean (`git status --porcelain` empty), so nothing uncommitted
was at stake.

## Found while closing

`w2-review-resume-from-waiting` (branch `wt/w2-review-resume-from-waiting`,
`b1afcaa2`, 2026-10-31) was **not** moot: approving a parked node spawned
`recover --strategy resume` with no entry point, and because verifier nodes
write no `node_checkpoints` the planner restarted at `specdir_ingest` and
re-dispatched the node a human had just approved — a $4.29 loop in the live SDD
smoke that produced it. It is the same failure the audit's "A block closed by a
missing decision" cluster keeps surfacing, so it was merged rather than
dispositioned; the SHA it lands under is recorded in the audit's correction log.
The general lesson is the reason this ADR probes instead of trusting names: a
`review-*` tree can hold real work.

## Consequences

- The worktree directory loses seven entries (128 → ~121 at audit time, less the
  trees created since); the same probe can be re-run any time:
  `git worktree list --porcelain` → `rev-list --count origin/main..HEAD` → for
  `ahead=1` trees, compare `git patch-id --stable`.
- The parked SHAs above stay reachable through this document and the reflog even
  after the branches are force-deleted; nothing here is lost content.
- A wide sweep of every `ahead=0`/clean tree is **not** part of this decision.
  The probe at 2026-11-10 found dozens (e.g. `vt9b-oracle-consistency` 593
  behind, `zed-acp-slice0` 2798 behind); deleting a tree another session is
  using is a different risk from deleting a review snapshot, so that sweep needs
  its own check-and-act pass and its own record.
- The leftover population, measured 2026-11-10 after the seven removals
  (`ls -1 $MINI_ORK_WORKTREES_DIR`, cross-checked against
  `git worktree list --porcelain`): **95 directories, 50 of them worktrees
  registered to this repo** (a 51st registration sits nested inside a scratch
  tree), so **45 are unregistered leftovers** — the directory half of a checkout
  whose git registration went away in an earlier clean. They are out of scope
  here: a leftover directory can still hold untracked run output, so each needs
  its own content check before deletion. Recorded so the next sweep does not
  mistake them for this ADR's unfinished business.
