# Framework Edit: make `mini-ork run` throttle-aware with bounded inline retry

## Goal

Stop the **silent-throttle stall**: when a lane (kimi/codex/glm) gets a 429
"Fair Usage" rejection that lands as `rc≠0` with an **empty** `.out` and an
**empty** `.err.log`, mini-ork currently treats it as an unclassifiable
`unknown` failure, records nothing, and either hangs the node to its 25-minute
`MO_NODE_TIMEOUT_S` ceiling or sinks the lane outright. The throttle machinery
already exists (`lib/throttle-guard.sh`) but is only wired into
`bin/mini-ork-self-improve` — a normal `bin/mini-ork run` never consults it.

Make the **normal-run dispatch path** consult throttle-guard: classify a silent
empty-output failure as `throttled`, record a cooldown, and apply a **bounded
inline retry** with a short backoff before giving up. A throttled lane should
recover within a couple of short retries instead of stalling the whole run.

## Root cause (verified against live code + the live FE-1 stall)

1. **Silent throttle classifies as `unknown` and is dropped.**
   - `lib/throttle-guard.sh:_throttle_classify_error` matches 429 wording in the
     err log, but if the provider returns `rc≠0` with an **empty** `.err.log`
     (kimi/glm Fair-Usage often writes nothing to stderr), every pattern misses
     and it falls to the final `echo "unknown"`.
   - `lib/throttle-guard.sh:_throttle_classify_run_failures` then does
     `[ "$cls" = "unknown" ] && continue` — so the failure is **never recorded**
     and no cooldown is set.

2. **The normal-run path never calls throttle-guard at all.**
   - `lib/throttle-guard.sh` is sourced/called only by
     `bin/mini-ork-self-improve` (cooldown wait + classify at its lines ~177,
     ~440-447, ~560-561). `bin/mini-ork-execute` / `lib/llm-dispatch.sh` never
     source it, so a normal `bin/mini-ork run` has no cooldown check, no failure
     recording, and **no retry**.

3. **The single dispatch returns a bare rc with no retry.**
   - `lib/llm-dispatch.sh:782` / `:800` run `claude --print ...` in a subshell
     and on failure do `return $_rc` with a possibly-empty `err_log`.
   - The wrapping caller at `lib/llm-dispatch.sh:1434` captures `rc=$?` and at
     `:1451` only emits a diagnostic **when `.err.log` is non-empty** — a silent
     empty failure produces `rc=1` with no signal and no second attempt.
   - Live proof: FE-1 `run-1782203890-44814` `code_impact_lens` (kimi) dispatched
     at 10:38:53, produced **no stream, no `.out`, no `.err.log`, no
     llm-failures entry**, and blocked the downstream implementer
     (`code_impact_lens --supplies_context_to--> implementer`).

## Scope Hint

- `lib/throttle-guard.sh`  (`_throttle_classify_error` ~:40; treat silent
  empty-output `rc≠0` as `throttled` — pass the rc + `.out` path in, not only
  the err log)
- `lib/llm-dispatch.sh`    (the dispatch wrapper around `:1434`-`:1465`: source
  throttle-guard, check cooldown before dispatch, record failure + bounded
  inline retry after, clear-on-success)

## Expected Edit

Touch exactly these two files:

1. **Classify silent throttle.** Extend `_throttle_classify_error` (or add a
   thin companion the dispatch path calls) so that a non-zero rc with an
   **empty `.out` and empty `.err.log`** is classified `throttled` (the kimi/glm
   Fair-Usage signature), not `unknown`. Keep all existing pattern matches. The
   classifier must still return `unknown` for a genuinely empty/successful case
   (rc==0), so pass the rc through — only `rc≠0 && empty out && empty err`
   becomes `throttled`.

2. **Inline throttle-aware retry in the normal-run dispatch.** In
   `lib/llm-dispatch.sh`, source `lib/throttle-guard.sh` once (guard against
   double-source), and around the dispatch wrapper (~:1434):
   - **before** dispatch: `_throttle_check_cooldown <lane>` — if cooling, sleep
     the remaining cooldown (bounded) or skip to retry.
   - **after** a failure: classify; if `throttled|capacity|overloaded|timed_out`,
     `_throttle_record_failure` and retry with a **short bounded ladder**
     (e.g. `15 45 90` seconds, **N=2 retries**, configurable via
     `MO_DISPATCH_RETRY_MAX` default 2 and `MO_DISPATCH_RETRY_LADDER`).
   - **on success:** `_throttle_clear_on_success <lane>`.
   - `auth_failed` must **not** retry (halt the lane immediately — it is not a
     backoff candidate).

## Requirements

- Do not change the GRPO/PRM/router logic, any recipe, or any schema/migration.
- Do not touch `.mini-ork/config/**` or any provider wrapper (`lib/providers/*`).
- The short inline ladder is for the **per-node** dispatch; do NOT reuse the
  self-improve macro ladder `(0 300 600 1800 3600)` here (way too long for a
  single node under a 1500s node timeout).
- Retries must respect the existing `MO_NODE_TIMEOUT_S` budget: total
  (attempts + sleeps) must not exceed it — cap the ladder so the last retry can
  still complete.
- Backward compatible: a run with no throttling behaves exactly as today (one
  attempt, same rc). Throttle-guard sourcing must be a no-op if already loaded.
- No new dependencies; pure bash + python3 stdlib.

## Done When

- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains the proposed two-file patch.
- `${MINI_ORK_RUN_DIR}/verdict.json` contains
  `{ "files_changed": 2, "tests_pass": true, "static_pass": true, "pass": true }`.
- A proof harness in the isolated worktree demonstrates the fix:
  1. **Classifier proof:** call the classifier with `rc=1`, an empty `.out`, and
     an empty `.err.log`; assert it returns `throttled` (not `unknown`). Call it
     again with `rc=0` and assert it does **not** return `throttled`.
  2. **Retry proof:** stub a dispatch that fails `throttled` on attempt 1 and
     succeeds on attempt 2; assert the wrapper retries once, the second attempt
     succeeds, and `_throttle_clear_on_success` was invoked. Use a tiny ladder
     (`MO_DISPATCH_RETRY_LADDER="0"`) so the proof runs fast.
  Write both assertion results to `${MINI_ORK_RUN_DIR}/throttle-retry-proof.txt`.
- `bash scripts/learning-loop-closure-gate.sh` still exits 0 (no regression).

## Why this kickoff exists

This is the second half of the learning-loop live validation. FE-1 fixes the
**write half** so GRPO can crown a winner; FE-2 fixes the **dispatch half** so a
real N-run validation can actually complete — today the kimi/codex researcher
lenses silently throttle, so runs return COLD with lanes unstamped. With
throttle-aware inline retry, the 4 researcher lenses survive transient 429s,
land their traces, and accumulate the samples GRPO + RHO need to flip the router.
