# REVIVE-set base-drift triage (2026-10-10)

Closing out the 10 failed REVIVE targets from the kickoff triage. Method is
evidence-first (git ancestry + committed-HEAD probes + run artifacts), not
keyword grep.

## Verdict: all 10 closed as superseded — none revivable in place

| run | base commit | ancestor of HEAD? | +lines in committed HEAD | call |
|---|---|---|---|---|
| `rsi-build-rsi-i1-control-arm-20260929-115150` | `a15b052b` | **no** (orphaned) | 2/5 (coincidental) | superseded |
| `rsi-build-rsi-i2-verifier-audit-20260929-131940` | `fc8bc1a0` | **no** | 3/5 | superseded |
| `rsi-build-rsi-i2b-audit-sensitivity-20260929-193126` | `85a1b980` | **no** | 0/5 | superseded |
| `rsi-build-rsi-i3-bsg-va-wiring-20260929-122109` | `f7ec8efd` | **no** | 0/5 | superseded |
| `rsi-build-rsi-i3-bsg-va-wiring-20260929-131925` | `165613a1` | **no** | 0/5 | superseded |
| `rsi-build-rsi-i4-collapse-halt-20260929-115155` | `6ea2e78e` | **no** | 0/5 | superseded |
| `rsi-build-rsi-i4-collapse-halt-20260929-122018` | `ea7752b0` | **no** | 0/5 | superseded |
| `recover-verify-20261007103743` | (none) | — | **5/5** | superseded (landed) |
| `retry-notify-20261007123316` | (none) | — | **5/5** | superseded (landed) |

## Why they cannot be revived in place

All seven `rsi-i*` runs were created 2026-09-29 with a `framework-edit` base
recorded in `<run_dir>/pre-implementer-ref`. `main` has since been rewritten
(diverged-merge / squash hazard); every one of those base commits **still
exists as an object but is no longer an ancestor of HEAD** (23 commits
diverged, `git merge-base --is-ancestor` = false).

`recover --strategy resume` re-runs the implementer when the artifact hash
mismatches (`hash_mismatch_or_artifact_corrupt`). The post-implementer
ground-truth guard (`mini_ork/cli/execute_handlers.py::_implementer_moved_base`)
then compares HEAD to that baseline and correctly refuses:

```
[ground-truth] FAIL: HEAD 32beebc5 is not the run's starting commit a15b052b:
  the tree delta would carry unrelated commits.
```

The guard is right: harvesting `baseline..HEAD` would diff across two diverged
histories. There is nothing to preserve — the run's original context is gone.

### Why two siblings DID publish earlier today

`rsi-i1-control-arm-…122013` and `rsi-i4b-collapse-writer-…131943` published
because their implementer artifact was **intact**, so `resume` reused it and
ran only a 3-node closure (`dispatched:3`, `failed_nodes:0`) — the implementer
never re-ran, so the guard never fired. The guard only bites when the closure
re-includes the implementer.

## Why supersede rather than re-launch

Per the standing revive rule #3 (“check the change still fits current work and
has not already landed; if it has, close as superseded rather than reviving”):

- **`recover-verify`, `retry-notify`** — their added lines are 5/5 present in
  committed HEAD. Verbatim landed.
- **The `rsi-i*` set** — not landed *verbatim*, but they are members of the RSI
  i-series, whose surfaces the current tree already carries: the working tree
  (peer WIP) modifies `mini_ork/learning/verifier_audit.py`,
  `mini_ork/recovery/circuit_breaker.py`, and `mini_ork/cli/board_cmd.py` —
  exactly the i2/i3/i4 targets. The final implementations diverged from the
  proposed diffs, which is why the old diffs do not match HEAD. The series
  shipped by a different path.

Re-launching 7 full `framework-edit` runs is expensive and unverifiable against
current work (11-day-old kickoffs, Orca IDE redesign in flight). The re-launch
escape hatch is recorded below.

## Escape hatch

If any of the 7 are still wanted, dispatch **fresh** at current `main` (a new
run has a correct baseline by construction):

```
MO_TARGET_CWD=$PWD MO_ALLOW_FRAMEWORK_CWD=1 \
MINI_ORK_AGENTS=$PWD/.mini-ork/config/agents.local.yaml \
  bin/mini-ork run kickoffs/auto/<slug>.md
```

Do not attempt `recover` on these — the base is orphaned; it will fail the
ground-truth guard every time.

## Gates closed

The exit-75 pending-fix gates (`retry-gate.json → inbox_id`) are rejected with a
superseded note so the IDE is no longer blocked:

| inbox_id | run |
|---:|---|
| 16 | `retry-notify` |
| 17 | `rsi-i1-control-arm` |
| 19 | `rsi-i2-verifier-audit` |
| 20 | `rsi-i2b-audit-sensitivity` |
| 21 | `rsi-i3-bsg-va-wiring` |
| 22 | `rsi-i3-bsg-va-wiring` (repair) |
| 23 | `rsi-i4-collapse-halt` |
| 24 | `rsi-i4-collapse-halt` (repair) |

(`recover-verify` carries no gate file.)

## At-source fix landed (2026-10-10)

Dispatched through mini-ork (`framework-edit`,
`kickoffs/auto/recover-refresh-pre-implementer-baseline.md`, run
`run-1791665283-39803`, verdict `pass`, 9 nodes / 0 failed) → committed
**`7e113de9`**:

- `mini_ork/cli/execute.py` (+112), `mini_ork/recovery/planner.py` (+55),
  `mini_ork/cli/execute_handlers.py` (+1),
  `tests/unit/test_resume_refresh_pre_impl_baseline.py` (+259, 8 tests — 8 pass
  under python3.11).
- On a resume that will (re-)run the implementer, the baseline is refreshed to
  the current target HEAD; within-attempt retries do **not** re-snapshot; the
  guard still fires on a within-attempt move.

**Residual (conservative by design).** The laundering refusal keys on the run's
ledger recording a prior `impl_moved_base`, so a run that *already* tripped the
guard is refused even when its base is orphaned by an external history rewrite
(verified live on `rsi-i1-control-arm-…115150`: `[recover] refusing to refresh
pre-implementer-ref …` → still `[ground-truth] FAIL`). The sharper discriminator
would be ancestry — a base that is **not** an ancestor of HEAD cannot carry the
implementer's prior change into HEAD — but a base that IS a forward ancestor is
ambiguous with a laundered commit, so the current code fails closed. Net effect:
the fix safely unblocks resumes with **no** prior recorded move; the 174
orphaned-base runs that already recorded `impl_moved_base` are still best handled
by a fresh re-launch (see *Escape hatch* above).

## Second fix landed — orphaned baselines refresh unconditionally (2026-10-11)

The first fix refused to refresh when the run's ledger recorded a prior
`impl_moved_base`, which left the whole orphaned class blocked (proved live on
`rsi-i1-control-arm`). Committed **`bf5a02e9`** via mini-ork `framework-edit`
run `run-1791723941-11171` (verdict `pass`, 9 nodes / 0 failed, rubric 8/8):

- `mini_ork/cli/execute.py` — test ancestry FIRST: a baseline that is **not** an
  ancestor of HEAD (merge-base exit 1, or 128 when the object is gone) is
  dropped unconditionally, skipping the ledger refusal; only a forward-ancestor
  baseline reaches it. Factored `_drop_pre_impl_baseline`.
- `tests/unit/test_resume_refresh_pre_impl_baseline.py` — +2 tests (orphaned +
  recorded ledger → refresh; forward-ancestor + recorded → still refused).
  10 passed in the green gate.

**Live smoke** (`run-1791541963-98230`, real run dir + real ledger + real
target `/Volumes/docker-ssd/ps/mini-ork`):

| engine | `_refresh_stale_pre_impl_baseline` | guard after refresh |
|---|---|---|
| fixed (`bf5a02e9`) | **True** (`[recover] refreshed stale pre-implementer-ref …`) | **pass** (baseline dropped) |
| old | `False` (`refusing … ledger already records impl_moved_base`) | FAIL |

## Orphaned-run census + disposition (2026-10-11)

Full split of the 807 failed runs: **580** no-base / **59** ancestor / **91**
orphaned / **1** missing / **76** other-repo. Of the 91 orphaned, **43** carry
their change on `origin/main` (landed → superseded), **20** are not-landed
(first rounds of a series whose later round landed), **28** have no
`framework-edit.diff` (reviews / migrations / child runs). Detail:
`2026-10-11-orphaned-run-census.md`.

The 4 still-open orphan gates were closed: **13**, **26** (rsi-i2-verifier-audit
repair — the i2 series landed via `…135337`), **28** (`rsi-i1` — surfaces in the
tree), **27** (`needs-change-reasons-subullets` — not-landed, 2-day-old stale;
re-launch fresh if wanted).

**Do NOT revive an orphaned `framework-edit` run against the PRIMARY while a
peer run is live**: a live framework-edit stashes the launching tree, and these
runs target `/Volumes/docker-ssd/ps/mini-ork`. Route any fresh re-launch through
a worktree.
