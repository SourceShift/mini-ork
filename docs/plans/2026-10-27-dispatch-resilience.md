# Dispatch resilience (bif), 2026-10-27

Follow-up to the Bifrost study (`maximhq/bifrost`, an LLM gateway), and the
rest of the 2026-10-26 session's open items. The user chose: Bifrost items 1–4
plus the mock provider; build the in-lock review fix for their approval; wire
or remove `MINI_ORK_ON_EVENT`.

## Measured starting point

- **`llm_calls` failures:** 2,675 failed rows, **1,881 `unknown`**. The
  unknowns include 488 timeouts, 128 spawn failures, 93 cwd-confusion
  guards and 64 shape rejects. Unknown means no retry, no fuse, no diagnosis.
- **The fallback walk moves on after any failure;** nothing reads
  `Retry-After` / "try again at".
- **A hung lead lane burns the whole node timeout before fallback:** 3,000 s
  on `sde-3b1`, where two lanes timed out at 1,500 s each.
- **16 of 127 multi-attempt framework-edit runs changed lanes between
  rounds.** Five were fallback-walk effects that lose the provider prompt
  cache (11,109 rows carry cached input).
- **`throttle_guard.py` is unwired by its own docstring.** The lane fuse has
  no per-class cooldown and no probe.
- **`MINI_ORK_ON_EVENT` is documented and shown in the IDE, but nothing calls
  it.** The emitter also blocks up to 5 s per event and can raise.

## Decisions

| # | Change | Kickoff |
|---|---|---|
| R2e | The merge driver tells the push hook which patch it already reviewed, so the in-lock push never runs the LLM panel; also fixes three open gate findings (a crashed panel, secure-tier block findings, zero-row cache hits) | `bif-r2e-review-outside-lock.md` (high tier: the user approves the merge) |
| E1 | `MINI_ORK_ON_EVENT` goes live as the no-server tier of event push (SSE on `serve` is the served tier): non-blocking, a command with arguments (no shell), heartbeats opt-in, plus a `run_finished` event | `bif-e1-event-hook-live.md` |
| B6 | `kind: mock` lanes with rule files (match prompt / lane / attempt → response or preset failure; seeded probability; latency), so dispatch changes are testable | `bif-b6-mock-provider.md` |
| B3 | Lane affinity: a node's later rounds try first the lane that served its first success, while the routing lead is unchanged; an operator remap always wins | `bif-b3-lane-affinity.md` |
| B1 | Typed failure classes and one action table (retry the chain / next lane / stop the walk / wait hint), stored in `metadata_json` (the `error_category` CHECK is unchanged) | `bif-b1-failure-classes.md` (after B6, B3) |
| B2 | A first-output deadline (default 300 s) on every streaming lane but the chain's last | `bif-b2-first-output-deadline.md` (after B1) |
| B4 | Held lanes: per-class cooldowns from B1's classes and wait hints, plus a single probe on re-admission | written after B1 lands |

Not adopted (from the study): a semantic response cache, virtual keys and
multi-tenant governance, Starlark code mode. A per-lane `gateway:` setting to
use Bifrost itself as an optional secure-tier transport stays a later option.

## Notes

- The quarantined RSI cycle `rsi-c1792992296-8a0044f3` held stale claims on
  `llm_dispatch.py`, `telemetry.py` and `db/migrations/0085_llm_calls_lane.sql`.
  The claims were released (the branch and worktree are kept). Its migration
  number collides with `0085_review_findings.sql` on main if it is ever
  revived.
- The rev6 runs R2a, R4, R5a and R5b were closed `superseded` with their
  landed commit (f9a73858, 36715db8, 547d0522, 1487a237). The supervisor's
  rule only closes `failed` rows whose diff landed unchanged.
- Anomaly: run `rev6-r2d-gate-cache-degraded-20261026-192210` completed (14
  `llm_calls`, 10 `node_attempts`) but has no `task_runs` row. Nothing in
  `mini_ork/` deletes or replaces `task_runs` rows; cause not found.
