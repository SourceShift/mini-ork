# Synthesis — Recursive Self-Improvement, iter 20

## Ranked patch plan

| Rank | Bottleneck | Category | Patch summary | Evidence | Confidence |
|---|---|---|---|---|---|
| 1 | #1 Worktree base-branch drift (iter-20 reproduces it under its own foot) | arch | Resolve an explicit `MINI_ORK_SELF_IMPROVE_BASE_REF` (default `main`, verified via `git rev-parse`) before `git worktree add`; log the resolved SHA into `self_improve_runs.notes`; loud-warn fallback to ambient branch | `lens-bottleneck.md:36`; `bin/mini-ork-self-improve:111,251`; arXiv 2605.07062, 2603.25697 (`lens-arxiv.md:8,15`) | 0.91 |
| 2 | #3 `duration_ms` never captured at `llm_dispatch` call sites (129/131 traces empty, 19 iters stale) | perf | Wrap each of 3 `RESULT=$(llm_dispatch …)` capture sites in `bin/mini-ork-execute` with `T0/T1=$(date +%s%3N)` and propagate `duration_ms` into `_trace_write_node_rich` payload — consumers already typed | `lens-perf.md:24-30`; `bin/mini-ork-execute:473,510,566`; `lib/trace_store.sh:77`; `lib/utility_function.sh:94-95`; `learning_record.id=3` open since iter=0 | 0.84 |
| 3 | #4 Unfiltered `deferred → superseded` UPDATE | correctness | Filter the supersede UPDATE by `evidence_paths ∩ git diff --name-only` overlap (and/or matching category); keep current behavior reachable via `MINI_ORK_BROAD_SUPERSEDE=1` rollback flag | `lens-correctness.md:22-29`; `bin/mini-ork-self-improve:171-182`; arXiv 2604.07877 (`lens-arxiv.md:87`) | 0.78 |
| 4 | Envelope leak — `★ Insight` / `` block; optionally divert z-insight payload to a `.z-insight.json` sidecar | `lens-correctness.md:48-66`; iter-18/19/20 stdout artifacts reproduce the leak | 0.71 |
| 5 | #2 Synthesis → `learning_record` promotion gap (Patches 2-5 of iter-19 still live as prose only) | arch (new infra) | Add `_self_improve_promote_synthesis_patches` invoked after `_self_improve_record_success`; parse `## Ranked patch plan` table; INSERT non-landed ranked rows with `outcome='open'` keyed by `title_hash = sha1(category + normalized_title)`; one schema migration adds `learning_record.title_hash` unique index | `lens-arch.md:74-82`; `bin/mini-ork-self-improve:144`; arXiv 2511.06179, 2605.26252 (`lens-arxiv.md:73-85`) | 0.74 |

## Top patch — detailed plan

### Patch 1: Explicit base-branch resolution for self-improve worktrees

**Problem statement.** The outer runner derives the parent ref from `git rev-parse --abbrev-ref HEAD` of `MINI_ORK_ROOT`, so every iteration inherits whatever branch the runner happens to sit on. Iter-20 reproduces the failure mode under its own foot: this worktree is based on `36543a3` (iter-19 audit publish), while `main` is at `0a3bf1c` (iter-19's actual landed profile-gate fix). The landed fix is therefore invisible to iter-20, and the bottleneck scan correctly re-flags it. Until the base ref is an explicit, governed control-plane input, every iteration is at risk of re-spending budget on already-closed work.

**Evidence.**
- `lens-bottleneck.md:36` ranks this critical; reproduces in-band: `git rev-parse HEAD` → `36543a3`, `git rev-parse main` → `0a3bf1c`, `git log --oneline main..HEAD` empty, `git log --oneline 36543a3..main` shows the missed fix.
- `bin/mini-ork-self-improve:111` — `[ -z "$PARENT_BRANCH" ] && PARENT_BRANCH=$(git -C "$MINI_ORK_ROOT" rev-parse --abbrev-ref HEAD)`.
- `bin/mini-ork-self-improve:251` — `git worktree add -b "$branch" "$wt_path" "$PARENT_BRANCH"`.
- `lens-arch.md:57-69` (Candidate 1) — same prescription with worked example.
- arXiv 2605.07062 (Barnes 2026, control-plane authority in CI/CD pipelines): base-ref is a control-plane action that must be explicit and logged (`lens-arxiv.md:8-13`, confidence 0.84).
- arXiv 2603.25697 (Roy 2026, Kitchen Loop drift control): each iteration must prove it starts from current product state before mutating it (`lens-arxiv.md:15-20`, confidence 0.72).
- Cross-iteration carry-forward: iter-19 Patch 2 specified this exact fix; it did not land.

**Proposed change.** In `bin/mini-ork-self-improve`:

1. At `:111` (parent-branch resolution), replace the ambient-fallback default with an explicit policy:
   ```bash
   : "${MINI_ORK_SELF_IMPROVE_BASE_REF:=main}"
   if [ -z "$PARENT_BRANCH" ]; then
     # Prefer the configured base ref; fetch opportunistically.
     git -C "$MINI_ORK_ROOT" remote get-url origin >/dev/null 2>&1 \
       && git -C "$MINI_ORK_ROOT" fetch --quiet origin "$MINI_ORK_SELF_IMPROVE_BASE_REF" 2>/dev/null || true
     if git -C "$MINI_ORK_ROOT" rev-parse --verify --quiet "$MINI_ORK_SELF_IMPROVE_BASE_REF" >/dev/null; then
       PARENT_BRANCH="$MINI_ORK_SELF_IMPROVE_BASE_REF"
     else
       PARENT_BRANCH=$(git -C "$MINI_ORK_ROOT" rev-parse --abbrev-ref HEAD)
       printf '[mini-ork-self-improve] WARN: base ref %q unavailable; falling back to ambient branch %q (drift risk)\n' \
         "$MINI_ORK_SELF_IMPROVE_BASE_REF" "$PARENT_BRANCH" >&2
     fi
   fi
   RESOLVED_BASE_SHA=$(git -C "$MINI_ORK_ROOT" rev-parse --verify "$PARENT_BRANCH" 2>/dev/null || echo unknown)
   ```

2. At `:251` (`git worktree add`), leave the call signature unchanged but write the resolved base SHA into the run's notes column after `_self_improve_record_*` is invoked. Add to the success/failure recorders:
   ```bash
   con.execute("UPDATE self_improve_runs SET notes = COALESCE(notes,'') || ? WHERE run_id = ?",
               (f"base_ref={PARENT_BRANCH}@{RESOLVED_BASE_SHA};", run_id))
   ```
   (or equivalent shell SQL — match the recorder's existing style).

3. Export `MINI_ORK_SELF_IMPROVE_BASE_REF` and the resolved SHA into the run-dir's `run_profile.json` so downstream lenses can see the base their worktree forked from.

Compatibility shim: `MINI_ORK_SELF_IMPROVE_BASE_REF="$(git rev-parse --abbrev-ref HEAD)"` reproduces today's behavior — the operator override path is preserved.

**Regression test.** Add `tests/self_improve/test_base_ref_resolution.bats` (or shell test under existing convention) with at least these three assertions:

- *Assertion 1 (happy path):* `MINI_ORK_SELF_IMPROVE_BASE_REF=main` with `main` present → resolved base equals `git rev-parse main`. Assertion text: `assert_equal "$resolved_sha" "$(git rev-parse main)"`.
- *Assertion 2 (drift guard):* runner sits on an audit branch ahead of `main`'s tip — confirm the new worktree forks from `main`, not from the runner's HEAD. Assertion text: `assert "git merge-base --is-ancestor "$(git rev-parse main)" "$wt_sha""`.
- *Assertion 3 (loud fallback):* `MINI_ORK_SELF_IMPROVE_BASE_REF=nonexistent` → stderr contains `"WARN: base ref"` and `"falling back to ambient branch"`, and the run completes (not aborted). Assertion text: `assert_output --partial "WARN: base ref \"nonexistent\""`.

**Verification.**
- Existing `tests/self_improve/*` shell tests must continue green.
- `bin/mini-ork-self-improve --help` exit code unchanged.
- `recipes/recursive-self-improve/verifiers/no-regression.sh` continues to report at most `benchmark_inconclusive=true` (gate dormancy is Bottleneck #5, out of scope for this patch).
- Expected utility-delta sign: **non-negative**, magnitude small (~+0.02 to +0.05). The patch is pure control-plane and adds no LLM dispatches; the gain is structural — future iters will dedupe against the correct base, eliminating wasted iterations on already-fixed work. Iter 21's bottleneck scan should show ≥1 fewer `learning_record`-resurfacing row vs the iter-19→iter-20 transition.
- `self_improve_runs` rows produced after this patch must contain `base_ref=` in `notes` (manual spot-check in addition to the bats test).

**Rollback criteria.** Discard the patch and reopen the bottleneck if any of:

1. The new bats test fails on a clean checkout of `main` after merge.
2. `git fetch origin "$MINI_ORK_SELF_IMPROVE_BASE_REF"` errors out non-quietly in any CI environment lacking `origin` — fetch must remain best-effort.
3. Any existing self-improve run aborts where it previously succeeded due to the base-ref check (i.e., the warn-and-fallback path is reached but execution does not continue).
4. Verifier `v8_no_anthropic_env_leak` (provider env isolation, iter-18 guard) starts failing — this patch must not touch provider env at all; any cross-talk is a rollback signal.

## Lower-ranked patches

### Patch 2 (rank 2) — `duration_ms` capture at `llm_dispatch` call sites

- **Files:** `bin/mini-ork-execute:473,510,566`; payload propagation already typed at `lib/trace_store.sh:77`.
- **Change:** `T0=$(date +%s%3N); RESULT=$(llm_dispatch …); T1=$(date +%s%3N); DUR_MS=$(( T1 - T0 < 0 ? 0 : T1 - T0 ))`; emit `duration_ms=$DUR_MS` in payload. Clamp negative on clock-step.
- **Regression test:** `tests/test_duration_ms_capture.sh` invokes a tiny recipe and asserts `SELECT COUNT(*) FROM execution_traces WHERE duration_ms > 0` ≥ 2 of 3 dispatches.
- **Why deferred to next iter:** Two LOC-equivalent patches in one iter raises rollback ambiguity; #1 is the structural root-cause unlock. Also closes `learning_record.id=3` (19 iters stale) — high signal but downstream of #1.

### Patch 3 (rank 3) — Filter `deferred → superseded` by evidence-path overlap

- **Files:** `bin/mini-ork-self-improve:171-182`.
- **Change:** Compute `git diff --name-only HEAD^..HEAD` for the success commit, intersect each deferred row's `evidence_paths` (JSON array), `UPDATE` only on non-empty intersection (or matching `category`). Preserve broad-update path behind `MINI_ORK_BROAD_SUPERSEDE=1`.
- **Regression test:** seed two deferred rows with disjoint `evidence_paths`; commit touches one path only; assert exactly one row flips to `superseded`, the other stays `deferred`.
- **Why deferred:** Dormant today (0 `deferred` rows). Blast-loaded only after Patch 5 lands. Logical ordering = land Patch 5 first OR land this concurrently — for iter 20, the safe choice is to keep both queued.

### Patch 4 (rank 4) — Strip envelope blocks from `*.stdout.md` artifacts

- **Files:** the stdout-capturing runner (likely in `bin/mini-ork-execute` or the workflow runner; needs locate-and-confirm).
- **Change:** Post-capture sed pipeline that drops `` `★ Insight ─` `` framed blocks and `` blocks before write. Optional sidecar `*.z-insight.json` if z-dashboard ingestion needs the payload.
- **Regression test:** `tests/test_stdout_sanitization.py` (already drafted in `lens-correctness.md:151-159`) — assert neither marker present in produced `.stdout.md`.
- **Why deferred:** Cross-iteration nuisance, but no current downstream parser breaks on the leak. Low blast, low urgency relative to drift fix.

### Patch 5 (rank 5) — Synthesis-to-`learning_record` promotion (requires new infra)

- **Files:** new function `_self_improve_promote_synthesis_patches` in `bin/mini-ork-self-improve` invoked after `_self_improve_record_success` at `:144`; new migration adding `learning_record.title_hash TEXT` + unique index.
- **arXiv evidence (required because new schema):** 2511.06179 (MemoriesDB, lightweight relational long-term memory), 2605.26252 (Orogat 2026, Governed Evolving Memory ingestion operators) — both cited at `lens-arxiv.md:73-85`, confidence 0.81 and 0.87.
- **Change:** Parse `## Ranked patch plan` markdown table from `${RUN_DIR}/synthesis.md`; INSERT each non-landed row with `outcome='open'`; idempotency via `title_hash = sha1(category + lower(normalized_title))`. Parse failure → `${RUN_DIR}/promotion.err`, non-fatal.
- **Regression test:** seed a synthesis.md fixture with 3 ranked rows; run promotion twice; assert `COUNT(*) FROM learning_record WHERE title_hash LIKE …` equals 3 after both runs (idempotent).
- **Why deferred:** Largest blast surface of the five (new schema migration, new parser, new failure mode). Should follow Patch 3 (so its `deferred` rows survive supersede) and Patch 1 (so the base it lands against is correct).

## Convergence assessment

**Not converged. Diminishing returns NOT reached.** The bottleneck lens explicitly states (`lens-bottleneck.md:115`): *"Bottlenecks #1 + #6 together are the structural root cause of non-convergence; until both close, the loop's memory cannot drive its behavior."* iter-20 is itself a live reproduction of bottleneck #1, which is the strongest possible signal that the loop has not converged — the drift mechanism kept iter-19's landed work invisible to iter-20.

Landing Patch 1 in iter-20 removes the *base-ref half* of the convergence blocker. The *promotion half* (manual quarantine→main cherry-pick) is iter-19 Patch 6 / iter-20 bottleneck #6; it remains open and is the natural Patch 1 candidate for iter 21 once Patch 1 here lands and is cherry-picked to `main`.

Recommendation to the outer loop: **continue iterating.** Re-evaluate convergence in iter 22, after Patch 1 (drift) + a #6-class promotion patch have both landed.

## Provenance footer

- Lenses consumed: minimax (perf), kimi (correctness), codex (arch + arxiv).
- Synthesizer family: opus.
- arXiv papers cited: 7 distinct IDs (2605.07062, 2603.25697, 2604.27148, 2602.05270, 2604.08988, 2511.06179, 2605.26252, 2604.07877) — all sourced from `lens-arxiv.md`; none invented.
- Cross-iteration learnings applied: 5 `learning_record` rows interrogated (`id=1,2,3,4,5` plus iter=19 rows); 4 iter-19 ranked patches carry-forward (`#2` drift → Patch 1, `#3` supersede → Patch 3, `#4` promotion → Patch 5, `#5` duration_ms → Patch 2); 2 novel iter-20 findings (envelope leak → Patch 4; empty `benchmark_results` → noted as gate-dormancy, out of scope this iter).
- Lens artifact placement notes: `lens-arch.md` and `lens-arxiv.md` were authored in the worktree (`/Volumes/docker-ssd/…/iter-20-20260609101845/`) because the sandbox blocked `cp` into `/Users/admin/…/runs/…`; content is identical and was read for this synthesis from the worktree path. Flag for the publisher: include both worktree and run-dir copies in the next-iter dedupe sweep.
