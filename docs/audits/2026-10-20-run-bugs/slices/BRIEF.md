# mini-ork 7-day run bug audit — shared brief (2026-10-13 .. 2026-10-20)

Goal: find ENGINE BUGS (defects in mini-ork itself) evidenced by the last 7 days of runs —
their logs, traces, artifacts and DB outcomes. Not a task-quality review.

## Hard rules
- READ-ONLY. Do not edit the repo, run dirs, or DB. No git writes, no `mini-ork run/recover/resume`,
  no launching anything. Open the DB with `sqlite3 -readonly`.
- Repo: /Volumes/docker-ssd/ps/mini-ork  (engine code in `mini_ork/`, recipes in `recipes/`).
- `.codegraph/` exists — use `codegraph explore "<symbol or question>"` (shell) to locate code
  before grep/reading files.
- Evidence or omit. Every bug needs at least one concrete artifact citation (path + short excerpt
  or a SQL query + its result). Mark each bug CONFIRMED (artifact + code pointer agree) or
  PLAUSIBLE (artifact shows the symptom, cause inferred).

## Data sources
- Run list for your slice: /tmp/mo-run-audit-20261020/<your-file>.txt
  columns: id | recipe | status | verdict | cost_usd | duration_s | created_at(UTC)
- Run dir: /Volumes/docker-ssd/ps/mini-ork/.mini-ork/runs/<id>/  — typical files: execute.log,
  run-verdict.json, verdict.json, panel-verdict.json, review-*.json, verifier-*.log / *.checks.tsv,
  retry-exhausted, reflect.log, impl-*.log, agent-*.live.jsonl, sessions/, plan.json,
  run_profile.json, kickoff-guard.json, implementer-summary.json, diffstat.json.
  If a run dir is missing/empty, also look in
  /Volumes/docker-ssd/ps/mini-ork-worktrees/*/.mini-ork/runs/<id>/ and ~/.mini-ork/runs/<id>/.
- DB: /Volumes/docker-ssd/ps/mini-ork/.mini-ork/state.db
  - task_runs (id, recipe, status, verdict, cost_usd, duration_ms, created_at/updated_at/ended_at
    unix-ts, notes, fuse_blown_lane, fuse_consecutive_failures)
  - run_events (run_id, event_type, payload_json, finish_reason, created_at)
  - node_attempts (run_id, node_id, attempt_no, node_type, result, failure_class, cost_usd, ...)
  - execution_traces, failure_memory, node_checkpoints, recovery_requests, run_leases
- Logs: .mini-ork/logs/, .mini-ork/*.log (file-reversion-guard.log, repo-integrity-guard.log).
- Known issues already recorded (tag a bug "KNOWN: <memory file>" if it matches, still list it if
  it recurred this week): /Users/admin/.claude/projects/-Volumes-docker-ssd-ps-mini-ork/memory/
  (index: MEMORY.md). Prior audits: docs/audits/2026-10-16-session-gaps/AGGREGATE.md,
  docs/audits/2026-10-19-session-goals/SESSION-GOALS.md.

## What counts as a bug (report) vs not (note briefly, low weight)
Report: engine crashes/tracebacks, wrong status/verdict bookkeeping (e.g. status=published with
verdict=CRASH, failed with no reason, never-terminal runs), verdict files that contradict each other,
gates that pass/fail wrongly, retries that loop or never fire, cost/duration accounting errors,
lost work, wrong base refs, artifacts missing that the contract requires, silent no-ops,
misleading error messages, timeouts mis-classified, recovery/resume defects, node results that
contradict logs.
Environmental (list in a short separate section): lane credentials/quota outages, provider 401/429,
operator mistakes — UNLESS the engine handled them wrongly (e.g. hung for the full timeout instead of
failing fast, or mis-attributed blame) — then it IS an engine bug.

## Already fixed?
For every bug, check whether it is fixed in current main: inspect the current code at the cited
location and `git -C /Volumes/docker-ssd/ps/mini-ork log --since=<run date> --oneline -- <file>`.
Record: FIXED-IN <sha> / OPEN / UNKNOWN.

## Output
Write your findings to /tmp/mo-run-audit-20261020/findings-<SLICE>.md using this per-bug shape:

### <SLICE>-<n>. <short title>
- Severity: P0 (data loss / wrong merge / security) | P1 (run fails or false verdict) |
  P2 (degraded, workaround exists) | P3 (cosmetic/telemetry)
- Confidence: CONFIRMED | PLAUSIBLE
- Status: OPEN | FIXED-IN <sha> | UNKNOWN
- Affected runs: <ids> (count)
- Symptom: 1-3 sentences.
- Evidence: path:line or SQL + excerpt (<= 6 lines each, max 3 items)
- Root cause: code pointer mini_ork/...py:<line> + 1-3 sentences
- Suggested fix: 1-3 sentences
- Known: <memory file or prior audit> | new

End the file with "## Environmental / non-engine" and "## Runs reviewed" (id -> one-line outcome).
Then reply with a compact summary: bug titles + severity + confidence + status + run count.
Be efficient: group runs with the same failure signature, inspect 1-2 representatives deeply,
spot-check the rest. Prioritise depth on the highest-severity signatures.
