# Learning-loop integrity & loop-closure — fix scope

**Date:** 2026-10-15
**Owner:** dispatched to a mini-ork peer session (from mini-ork-7c, 2026-10-15)
**Origin:** learning-loop audit (2026-10-11) + three-lane judge panel
(`run-1791725633-31802`, panel gate pass 100/100). All findings below were
independently confirmed by three judge lanes; F1/F2/F3/F4/F6 confirmed, F5
confirmed, F6 counts confirmed with mechanism corrected.

## Why this matters (product framing)

mini-ork's headline claim is *"it improves over time"* (classify → plan →
execute → verify → reflect → improve). Right now that claim is **false by
construction**: F1 makes every node reward `1.0` unless a veto verdict fires,
F2 stops reflect from running, F3 blocks theme promotion even when it runs.
Cost governance, the verification stack, and heterogeneous routing are all
sellable today — the *learning* story is not.

Goal: make the claims true, in dependency order. Do not build new surface
until the loop is honest.

## Findings (evidence-backed)

### F1 — Node reward is exit-code rubber-stamping (severity: critical)
`reward_from_status()` (`mini_ork/learning/writeback.py:34`) returns `1.0` for
any `success` dispatch unless a veto verdict is present, and the trace writer
hardcodes `verifier_output` to `{"node_type","finish_reason"}`
(`mini_ork/cli/execute.py:3033`), so real verifier results
(`verifier_test.json`, `run-verdict.json`) never reach node-level rewards.
Measured last 3 days (from `created_at >= '2026-10-08'`): 617 traces; 601
`verifier@v1` rows, avg reward 0.815, 447 (74%) at ≥0.99, 101 at zero; 397
perfect-reward rows carry `finish_reason` but no `verdict`.

```sql
SELECT reward_source, COUNT(*) n, ROUND(AVG(reward_value),3),
       SUM(reward_value>=0.99), SUM(reward_value<=0.01)
FROM execution_traces WHERE created_at >= '2026-10-08' GROUP BY 1;
SELECT COUNT(*) FROM execution_traces
WHERE created_at >= '2026-10-08' AND reward_value >= 0.99
  AND verifier_output LIKE '%finish_reason%'
  AND verifier_output NOT LIKE '%verdict%';
```

### F2 — Reflect pipeline stalled; 89 gradients unprocessed (high)
`mini-ork reflect` is the only producer of gradient→theme assignment
(`themes.assign_new`, called from `mini_ork/cli/reflect.py:383`); nothing
schedules it. `bin/mini-ork automations list` is empty; both launchd ticks
(`ai.mini-ork.automations.45c708802f`, `ai.mini-ork.automations.f7e68c830f`)
exit 78 (EX_CONFIG) every fire — one targets a worktree engine
(`mini-ork-worktrees/ide-pages`) with a foreign home
(`/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork`). Last theme
assignment 2026-10-09 10:14; 89 gradients created after have no
`gradient_theme` row.

### F3 — Theme promotion funnel degenerate: 1 of 5306 (high)
`themes.promote()` (`mini_ork/learning/themes.py:922`) selects only
`kind='task'` candidates (~line 978), so 3277 `kind='framework'` candidates are
unpromotable by construction. Only 1 theme is `verified` ever
(`th-1e42cc51c68c`), injected 128× across 31 runs — every LLM node gets the
same single lesson. 103 task candidates meet the bar (n_runs ≥ 3 AND ≥ 3
distinct member traces) but only 1 promoted; skip reasons are not persisted.

### F4 — `learning_pass_stats` is dead instrumentation (medium)
`record_pass_stat()` (`mini_ork/learning/ledger.py:201`) has ZERO production
callers; the table has 0 rows; yet the IDE Learn overview
(`mini_ork/ide_pages/learn/overview.py:129,377`) reads it — pass-health renders
empty and F2/F3 skip reasons are unobservable.

### F5 — Production DB polluted by test/eval fixtures (medium)
`lesson_injections` holds ~200 fixture rows (`run_id='run'`, node ids `rev1`,
`impl1`, `alpha_lens`, `tier4_synth`) plus four Oct-7 kickoff-eval runs
(`learn-ledger-*`, `eng-path-rules-*`, `verified-only-inject-*`,
`verified-only-prompts-*`) that injected gradient id `g1` / steering id `17` —
5 gradient injections reference ids absent from `gradient_records` (orphans).
Skews `ledger.injection_counts()` and pack-accuracy analytics.

### F6 — Dead learning stores (medium)
`lessons_bank` 0 rows ever (healer reads it at `mini_ork/recovery/healer.py:299`
and always misses → every failure re-classifies from scratch via LLM);
`reflection_log` 0 rows; `learning_record` last written 2026-06-05.

## Engine defects measured while running the panel (same session)

- **N1 — silent lane substitution.** `dispatch_with_fallback`
  (`mini_ork/dispatch/providers.py:2273`) has no 429 retry: a failing lane
  falls silently to the next. Only `.last-llm-lane` (dotfile) records the
  winner; node artifacts, panel reports, and the verifier all keep the
  *requested* lane id. Round 3 captured the `laguna_free failed → served by
  minimax` notice itself as the node's output. For a heterogeneous-router
  product this is an integrity failure, not a nit.
- **N3 — `recover --force` ≠ re-dispatch.** `--force`
  (`mini_ork/recovery/planner.py:191`) only overrides a `retryable:false`
  hint. Node reuse is decided by `is_node_reusable`
  (`mini_ork/stores/checkpoints.py:417`): a *verifier-failed* node has a
  SUCCESS checkpoint and is reused forever. The only operator lever is deleting
  the artifact (sha256 rule fails) or pinning the manifest sha.
- **N4 — orphaned run leases.** Killing a hung recover leaves a `run_leases`
  row for its 15-min TTL; the next recover refuses ("single-writer lease held
  by another worker"). Needs lease release on SIGTERM/SIGKILL recovery or a
  `recover --take-lease` escape.
- **N5 — resume degeneracy.** Retried nodes resume via `MO_RESUME_SESSION_ID`
  (`node_attempts.provider_session_id`); a cross-family fallback cannot see the
  transcript and replies "No re-write" — silent bad output.

## Fixes

- **S1 (F1):** at the trace-write source (`_make_trace_fn` closure in
  `mini_ork/cli/execute.py`), populate the node's reward from the run's ACTUAL
  verifier artifacts for that node (verifier JSON sidecars / reviewer verdict)
  instead of `reward_from_status(status)` alone; keep exit-code as fallback
  ONLY when no artifact exists, and record which path stamped the reward
  (e.g. `reward_source` ∈ {`artifact`,`exit_code`}).
- **S2 (F2):** repair or replace the two exit-78 launchd ticks (point at the
  primary engine + primary home); register a scheduled automation that runs
  `mini-ork reflect` on a fixed cadence.
- **S3 (F3):** POLICY DECISION REQUIRED — extend `themes.promote()` to author
  framework-kind lessons scoped to framework-facing roles, OR stop clustering
  framework gradients into lesson candidates (route them to bug_reports only).
  **Do NOT implement S3 without the human's choice; flag and continue.**
- **S4 (F4):** wire `ledger.record_pass_stat()` into the reflect side-channels
  (`pattern_miner`, `pattern_induct`, `themes.assign_new/promote`) in
  `mini_ork/cli/reflect.py` — one row per pass with inputs/outputs/failures/
  lane/last_error.
- **S5 (F5):** one-shot purge of fixture rows (`run_id='run'`, orphan g1/17
  injections) from `lesson_injections`, plus isolate eval kickoffs to a scratch
  DB (env pin or ledger `db=` arg).
- **S6 (F6):** populate `lessons_bank` from the failure classifier on each
  classified failure (writer beside `failure_classifier`), OR retire the
  healer's fingerprint-match branch — either way the always-miss path stops
  being the default.
- **N1:** thread the ACTUAL winning lane (`.last-llm-lane`) into the node
  artifact + any panel/report consumer, and have the panel verifier assert the
  lane field matches the dispatched lane (fail closed on mismatch).
- **N3:** make reuse refuse to reuse an output whose DOWNSTREAM node failed
  (or add `recover --no-reuse`), so a verifier-failed closure re-dispatches.
- **N4:** release/expire the lease on unclean exit; add a `recover` escape that
  can reclaim a lease whose owner pid is dead.

## Delivery order

1. **Wave 1 — Truth (do first, surgical):** S1 + N1. Small, load-bearing;
   unblocks every downstream metric. A wrong S1 poisons all learning — do it
   directly and verify against the reproduction SQL above (447→expected drop in
   perfect-reward-without-verdict count).
2. **Wave 2 — Close the loop:** S4 → S2 → S6. Then S3 only after the human's
   policy call. Exit criterion: a run where reward trends in, a lesson is
   promoted, injected, and the next run's score moves — reproducible.
3. **Wave 3 — Durable:** N3, N4, S5.

**Cut list (do NOT build now):** full GRPO refinement, framework-theme taxonomy
design, retiring dead stores beyond the S6 hot path, new recipe surface.

## Acceptance criteria

- Repro SQL for F1 returns ~0 rows matching
  `reward_value>=0.99 AND verifier_output LIKE '%finish_reason%' AND NOT LIKE
  '%verdict%'` for new traces after the fix; `reward_source` distinguishes
  artifact-derived from exit-code rewards.
- `mini-ork reflect` runs on schedule (automations list non-empty; launchd
  ticks exit 0) and `gradient_theme` assignment lag < one cadence.
- Every LLM node artifact records the ACTUAL served lane; a panel verifier
  fails on requested≠served.
- `learning_pass_stats` has rows after a reflect pass; IDE Learn overview
  renders pass-health non-empty.
- Fixture/orphan rows gone; `ledger.injection_counts()` matches a clean count.
- Healer no longer always-misses (either `lessons_bank` populated or the
  fingerprint branch retired); the re-classify-from-scratch path is no longer
  the default.

## Constraints (binding)

- Fix at the SOURCE — no kickoff/env workarounds.
- Worktree-first: `make worktree SLUG=... OWNS="<paths>"` → green gate
  (`MINI_ORK_TEST_CMD` scoped per task is fine) → `make worktree-merge`.
  Never `git add -A`; never revert `main`.
- Two tiers for any new config surface (secure cloud-tier + simple local-tier,
  fail-closed on missing values).
- Do NOT touch `ui/` or any `mini_ork_ui` crate — a separate session owns those
  paths (SourceShift/mini-ork-ui). Other sessions are editing `recipes/`
  concurrently; claim files explicitly.
- Leave S3 unimplemented and clearly flagged for the human.
