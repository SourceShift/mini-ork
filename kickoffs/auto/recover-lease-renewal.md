# Recover must own the lease it re-dispatches under — no fenced-limp recovered runs

Natural home: worktree `recover-revival-fixes` (already claims `mini_ork/cli/execute.py` +
`mini_ork/recovery/planner.py`). Possibly the same family as its stale-heartbeat/recover scope.

## Why (live evidence, 2026-10-08)

Run `run-orchestrator-scaffold-20261008124718` (framework-edit) was recovered with
`mini-ork recover <id> --from-node implementer` at 13:01 and kept executing — degraded:

1. **The lease row.** `run_leases` for the run: `acquired_at=13:01:12`,
   `renewed_at=13:01:12` (never renewed), `expires_at=13:16:12`. TTL is 900 s
   (`mini_ork/stores/lease.py:58`); `refresh_lease` exists (lease.py:193) but nothing on the
   recovered path called it.
2. **The fence fired mid-run.** After expiry the run's own checkpoint writes were rejected:
   `write_checkpoint: fence rejected — token '9de1…' is not the live lease holder … refusing to
   publish 'implementer'` (`write_checkpoint` consults `is_lease_holder`, checkpoints.py:203 ←
   lease.py:239). The run kept going but could not persist node checkpoints — a fenced limp that
   silently drops resume points.
3. **The seam — verified.** Recover DOES acquire and hand off: `planner.py:1002-1012` calls
   `acquire_lease` and exports `MINI_ORK_LEASE_TOKEN` for the resumed execution (that is the
   13:01:12 row). The gap is renewal: `grep -rn refresh_lease mini_ork/recovery/ mini_ork/cli/execute.py`
   → ZERO calls. No path — recovered or initial — ever refreshes, so any run outliving TTL 900 s
   fences its own checkpoint writes. `refresh_lease` (lease.py:193) exists and is never wired.

## Files in scope

- `mini_ork/recovery/planner.py` (or wherever recover's dispatch seam mints/uses the token — trace it)
- `mini_ork/cli/execute.py` — run-loop lease refresh only
- `tests/unit/test_recover_lease_renewal.py` (new); existing lease/recovery tests only if an
  assertion must change

Do NOT modify `mini_ork/stores/lease.py` semantics (acquire/refresh/fence are correct); wire them.

## Changes (exact)

1. **(Already done — verify, don't rebuild.)** Recover acquires/steals before re-dispatch and
   hands `MINI_ORK_LEASE_TOKEN` to the resumed execution (planner.py:1002-1012), refusing on a
   live foreign lease. Add a regression test if none covers it; no new code expected here.
2. **The run loop refreshes while alive — the actual fix.** The executing run (initial AND
   recovered — one seam) refreshes its lease well before TTL expiry (e.g. every TTL/3, via the
   `MINI_ORK_LEASE_TOKEN` it already carries) so a >15 min run never outlives its own lease.
   `refresh_lease` returning False (lost ownership) must stop writes and fail the run honestly —
   never the current fenced limp.
3. **Observable.** One stderr line on recover: acquired/refreshed/stolen + token prefix +
   expires_at, so the operator can see ownership transfer.

## Tests (`tests/unit/test_recover_lease_renewal.py`, temp home/DB, no network)

- Recover into a run with an EXPIRED lease → re-dispatched writes carry the new live token;
  `write_checkpoint` succeeds during the recovered run (no fence rejection).
- Recover into a run with a LIVE foreign lease → recover refuses (rc != 0, clear message).
- A fake long run (> TTL) refreshes: after simulated TTL elapse (`now=` injection),
  `is_lease_holder` still true for the run's token; checkpoints still write.
- `refresh_lease` False (superseded token) → run marks itself failed/stop, no further
  checkpoint writes attempted.
- Regression: normal (never-recovered) run behavior unchanged.

## Verification command

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_recover_lease_renewal.py tests/test_lease_fencing.py tests/unit/test_retry_hint.py   # must exit 0
```

## Done when

- Verification command → 0 failed. Paste the summary line.
- `uvx ruff check` clean on touched files.
- Read-only proof: re-run the recover flow shape from the live run (or point at
  `run-orchestrator-scaffold-20261008124718`'s log) and show the acquire/refresh stderr lines
  plus a checkpoint write that is NOT fence-rejected. Paste both.
- `git diff --stat` touches only files in scope.
