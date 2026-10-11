# Three-lane audit judge panel — learning-loop defect findings (2026-10-11)

Read-only THREE-lane judge panel (recipe `audit-judge-panel-3l`) over an
audit of mini-ork's learning data. An external auditor (Claude session,
2026-10-11) examined recent runs (Oct 7–11) plus the learning tables in the
live state DB and reported six findings F1–F6 with six proposed fixes
S1–S6 below.

Lane roles: `opus_lens` and `minimax_lens` verify against LIVE evidence
(state DB + source) — re-derive every number, do not trust the auditor.
`laguna_lens` (free OpenRouter, tool-less) adjudicates from THIS BRIEFING
alone: everything it needs — claims, numbers, SQL, file anchors — is
inline below, and its reply is captured verbatim as its report.

## Scope (binding)

- Repo (read-only): `/Volumes/docker-ssd/ps/mini-ork` (current working tree).
- State DB (read-only): open with
  `sqlite3 "file:/Volumes/docker-ssd/ps/mini-ork/.mini-ork/state.db?mode=ro"`.
- All file:line anchors refer to the CURRENT tree. Line numbers may drift a
  few lines; verify by symbol name if the line is off.
- No file under the repo may be modified. Run-dir reports only.
- "Last 3 days" in the findings means `created_at >= '2026-10-08'`
  (execution_traces.created_at is ISO-8601 text).

## Findings

### F1 — Node reward is exit-code rubber-stamping, not verification (claimed severity: critical)

Claim: `reward_from_status()` (`mini_ork/learning/writeback.py:34`) returns
1.0 for any node whose dispatch status is `success` unless a veto verdict is
present, and the trace writer hardcodes `verifier_output` to
`{"node_type", "finish_reason"}` (`mini_ork/cli/execute.py:3033`), so real
verifier results (`verifier_test.json`, `run-verdict.json`) never reach
node-level rewards. Auditor measured, last 3 days: 617 traces total;
`verifier@v1` = 601 rows, avg reward 0.815, 447 rows (74%) at ≥0.99, 101 at
zero; 397 perfect-reward rows carry `verifier_output` containing
`finish_reason` but NO `verdict` key. Example run where implementer,
reviewer, AND researcher all got reward_g=1.0 / validity=valid on
`{"finish_reason":"done"}` alone: `run-1791665283-39803`.

Reproduce:
```sql
SELECT reward_source, COUNT(*) n, ROUND(AVG(reward_value),3),
       SUM(reward_value>=0.99), SUM(reward_value<=0.01)
FROM execution_traces WHERE created_at >= '2026-10-08' GROUP BY 1;

SELECT COUNT(*) FROM execution_traces
WHERE created_at >= '2026-10-08' AND reward_value >= 0.99
  AND verifier_output LIKE '%finish_reason%'
  AND verifier_output NOT LIKE '%verdict%';
```
Also judge whether downstream consumers (GRPO writeback
`mini_ork/learning/writeback.py`, `lane_region_advantage`) actually read
`reward_value` — i.e. is the corruption load-bearing?

### F2 — Reflect pipeline stalled: 89 gradients unprocessed since Oct 9 (claimed severity: high)

Claim: `mini-ork reflect` is the only producer of gradient→theme assignment
(`themes.assign_new`, called from `mini_ork/cli/reflect.py:383`) and nothing
schedules it: `bin/mini-ork automations list` is empty, and BOTH launchd
tick jobs (`ai.mini-ork.automations.45c708802f`,
`ai.mini-ork.automations.f7e68c830f`) exit 78 (EX_CONFIG) every fire — one
targets a worktree engine (`mini-ork-worktrees/ide-pages`) with a foreign
home (`/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork`).
Auditor: last theme assignment 2026-10-09 10:14; 89 gradient_records created
after that have no gradient_theme row (0 assigned).

Reproduce:
```sql
SELECT COUNT(*) FROM gradient_records g
WHERE NOT EXISTS (SELECT 1 FROM gradient_theme gt
                  WHERE gt.gradient_id = g.gradient_id);
SELECT MAX(last_seen) FROM lesson_themes;  -- unix ts
```
External: `launchctl list | grep mini-ork` (exit codes), `bin/mini-ork
automations list`, `cat` both plists in `~/Library/LaunchAgents/`.

### F3 — Theme promotion funnel degenerate: 1 of 5306 themes verified (claimed severity: high)

Claim: `themes.promote()` (`mini_ork/learning/themes.py:922`) only selects
`kind='task'` candidates (line ~978), so all 3277 `kind='framework'`
candidates are unpromotable by construction. Only 1 theme is `verified`
ever (`th-1e42cc51c68c`, authored 2026-10-09); it was injected 128 times
across 31 runs since — every LLM node gets the same single theme lesson.
Auditor: 103 task candidates meet the promotion bar (n_runs ≥ 3 AND ≥ 3
distinct member traces) but only 1 was promoted on the single promote pass;
skip reasons were not persisted anywhere.

Reproduce:
```sql
SELECT status, kind, COUNT(*) FROM lesson_themes GROUP BY 1,2;
SELECT COUNT(*) FROM lesson_themes lt
WHERE lt.kind='task' AND lt.status='candidate' AND lt.n_runs>=3
  AND (SELECT COUNT(DISTINCT g.evidence) FROM gradient_theme gt
       JOIN gradient_records g ON g.gradient_id=gt.gradient_id
       WHERE gt.theme_id=lt.theme_id AND COALESCE(g.evidence,'')<>'') >= 3;
SELECT source_kind, COUNT(*) FROM lesson_injections
WHERE source_kind='theme' GROUP BY 1;
```

### F4 — learning_pass_stats is dead instrumentation (claimed severity: medium)

Claim: `record_pass_stat()` (`mini_ork/learning/ledger.py:201`) has ZERO
callers in production code (grep it), the table has 0 rows, yet the IDE
Learn overview (`mini_ork/ide_pages/learn/overview.py:129,377`) gates/reads
it — so pass-health renders empty and the F2/F3 skip reasons are
unobservable.

Reproduce: `grep -rn "record_pass_stat" mini_ork/ --include="*.py"` and
`SELECT COUNT(*) FROM learning_pass_stats;`

### F5 — Production DB polluted by test/eval fixture rows (claimed severity: medium)

Claim: `lesson_injections` contains ~200 rows from test fixtures:
`run_id='run'` (literal placeholder; node ids `rev1`, `impl1`,
`alpha_lens`, `tier4_synth`, dated Oct 8 and Oct 10) plus four Oct 7
kickoff-eval runs (`learn-ledger-*`, `eng-path-rules-*`,
`verified-only-inject-*`, `verified-only-prompts-*`) that injected gradient
id `g1` / steering id `17` — 5 gradient injections reference ids that do
NOT exist in `gradient_records` (orphans). This skews
`ledger.injection_counts()` and any pack-accuracy analytics.

Reproduce:
```sql
SELECT COUNT(*) FROM lesson_injections WHERE run_id='run';
SELECT run_id, source_id, datetime(ts,'unixepoch') FROM lesson_injections
WHERE source_kind='gradient' AND source_id NOT IN
  (SELECT gradient_id FROM gradient_records);
```

### F6 — Dead learning stores: lessons_bank / reflection_log / learning_record (claimed severity: medium)

Claim: `lessons_bank` has 0 rows ever (the healer reads it at
`mini_ork/recovery/healer.py:299` and always misses, so every failure
re-classifies from scratch via LLM); `reflection_log` has 0 rows;
`learning_record` last written 2026-06-05 (stale 4 months). These are
either unwired producers or abandoned tables still read by live code.

Reproduce:
```sql
SELECT (SELECT COUNT(*) FROM lessons_bank),
       (SELECT COUNT(*) FROM reflection_log),
       (SELECT COUNT(*) FROM learning_record),
       (SELECT datetime(MAX(iter_ts),'unixepoch') FROM learning_record);
```
(schema of learning_record may name the ts column differently — inspect
first). Also read `healer.py` around the lessons_bank query to confirm the
miss path is live.

## Proposed fixes

- **S1 (fixes F1):** At the trace-write source in
  `mini_ork/cli/execute.py` (the `_make_trace_fn` closure), populate the
  node's reward from the run's ACTUAL verifier artifacts for that node
  (verifier JSON sidecars / reviewer verdict) instead of
  `reward_from_status(status)` alone; keep exit-code as fallback only when
  no artifact exists, and record which path stamped the reward.
- **S2 (fixes F2):** Repair or replace the two exit-78 launchd automations
  ticks (point at the primary engine + primary home), and register a
  scheduled automation (or extend the tick) that runs
  `mini-ork reflect` on a fixed cadence.
- **S3 (fixes F3):** Policy decision + change: either extend
  `themes.promote()` to author framework-kind lessons scoped to
  framework-facing roles, or stop clustering framework gradients into
  lesson candidates at all (route them to bug_reports only). Needs a human
  choice between the two; implement the chosen one.
- **S4 (fixes F4):** Wire `ledger.record_pass_stat()` into the reflect
  side-channels (pattern_miner, pattern_induct, themes.assign_new/promote)
  in `mini_ork/cli/reflect.py`, one row per pass with inputs/outputs/
  failures/lane/last_error.
- **S5 (fixes F5):** One-shot purge of fixture rows (run_id='run', the
  orphan g1/17 injections) from `lesson_injections`, plus isolate eval
  kickoffs so they run against a scratch DB (env pin or ledger db= arg).
- **S6 (fixes F6):** Populate `lessons_bank` from the failure classifier on
  each classified failure (writer beside `failure_classifier`), or retire
  the healer's fingerprint-match branch; either way the always-miss path
  must stop being the default.

## Success

Per `recipes/audit-judge-panel-3l/artifact_contract.yaml`: all three judge
reports (`judge-opus-audit.md`, `judge-minimax-audit.md`, `lens-laguna.md`
with its embedded json sidecar) + `synthesis.md` under the run dir,
structured sidecars with F1–F6 and S1–S6 in order, verdict/severity
vocabularies respected, read-only attested.
