# RRL-6: canary monitor + adoption halt, research refresh, `research` / `impact` verbs, operator docs

## Goal

The research-driven RSI loop can now adopt a technique only when gate G12 proves a
sealed, paired gain (RRL-4b) and revise a no-gain attempt (RRL-5). Three things make it
safe to leave running unattended:

1. **Canary monitor (running-peak ratchet, arXiv 2606.21090).** Score `main` on the
   benchmark whenever its tree changes; keep the score history; when the score has
   declined more than `monitor.drop` below the best-ever score for
   `monitor.consecutive` canaries in a row, **halt technique adoption** and queue a
   fix-forward `regression` candidate (measured by G12 against current main). The repo
   rule is fix forward — the monitor never reverts `main`.
2. **Research refresh.** When the newest technique review for a domain is older than
   `research.refresh_days`, launch the domain's review recipe (detached) at most once a
   day, so the loop keeps reading new papers.
3. **Operator surface:** `mini-ork rsi-cycle research` and `mini-ork rsi-cycle impact`,
   plus the operator doc.

All paths are relative to the repository root. Read `mini_ork/rsi/cycle.py`,
`mini_ork/rsi/impact.py`, `mini_ork/rsi/impact_state.py`, `mini_ork/rsi/benchmarks.py`,
`mini_ork/rsi/research.py`, `mini_ork/rsi/config.py` and `mini_ork/cli/rsi_cycle.py`
first.

## Mechanism (exact spec)

### 1. `mini_ork/rsi/config.py`

- New `MonitorConfig` (frozen, slots): `enabled: bool = True`, `drop: float = 0.02`
  (`0 <= drop < 1`), `consecutive: int = 3` (`_positive`); `_only("monitor", …)`.
- `ResearchConfig` gains `auto_refresh: bool = False` (`_flag`; add to known keys).
- `_TOP_KEYS` gains `"monitor"`; `CycleConfig` gains
  `monitor: MonitorConfig = field(default_factory=MonitorConfig)`; `from_mapping` parses it.

### 2. `config/rsi-cycle.example.yaml`

Under `research:` add `auto_refresh: false   # true = relaunch the review recipe when older than refresh_days`.
Append:

```yaml
# Post-merge canary (running-peak ratchet). Never reverts main: on a sustained
# decline it halts technique adoption and queues a fix-forward regression candidate.
monitor:
  enabled: true
  drop: 0.02
  consecutive: 3
```

### 3. `mini_ork/rsi/monitor.py` (new)

- `HALT_MARKER = "ADOPTION_HALTED"`; `halt_path(home) -> Path(home) / "rsi" / HALT_MARKER`.
- `adoption_halted(home) -> dict | None` — the marker's JSON, or `None` when absent
  (an unparseable marker returns `{"reason": "unreadable marker"}` — still halted).
- `bench_domain(bench) -> str` — the `domain` of the first
  `research.DEFAULT_SPECS` value whose `benchmark == bench`, else `"verification"`.
- `canary_tree(root, home, sha) -> Path` — `home/"rsi"/f"canary-{sha[:12]}"`; when the
  directory does not exist, `git -C root worktree add --detach <dir> <sha>`; raise
  `RuntimeError` on a non-zero rc. `remove_canary_tree(root, path)` →
  `git -C root worktree remove --force <path>` (errors ignored).
- `run_canary(root, home, cfg, bench, *, now) -> dict`:
  1. `sha = git -C root rev-parse HEAD` (stripped).
  2. `tree = canary_tree(root, home, sha)`; in `try/finally` remove it.
  3. `ctx = benchmarks.BenchContext(home=home, repo=root, workers=cfg.impact.workers, timeout_s=cfg.impact.timeout_s)`;
     `result = benchmarks.run_benchmark(bench, tree, ctx=ctx)`.
  4. `score = result.meta.get("accuracy")`; when `None`, the mean of `result.scores`
     values; when there are no scores → return `{"status": "error", "bench": bench, "sha": sha, "reason": "no scores"}`.
  5. `state = impact_state.load_state(home)`; when `bench` has no entry, create it with
     `impact_state.bench_state(state, bench, impact_state.corpus_hash(get_benchmark(bench).items(ctx)))`;
     `impact_state.record_canary(state, bench, sha=sha, score=score, at=now)`;
     `impact_state.save_state(home, state)`.
  6. `verdict = impact.ratchet(impact_state.canary_scores(state, bench), drop=cfg.monitor.drop, consecutive=cfg.monitor.consecutive)`;
     `history = state["benchmarks"][bench]["canary"]`;
     `peak_sha = history[verdict["peak_index"]]["sha"]` when `peak_index` is not `None`.
  7. `verdict["revert"]` → write `halt_path(home)` (parents created) with
     `json.dumps({"bench": bench, "peak": verdict["peak"], "peak_sha": peak_sha, "latest": score, "latest_sha": sha, "declines": verdict["declines"], "at": now})`
     and return `{"status": "regressed", "bench": bench, "sha": sha, "score": score, **verdict, "peak_sha": peak_sha}`.
  8. Otherwise, when the marker exists for this bench and `score >= verdict["peak"] - cfg.monitor.drop`,
     delete the marker and return status `"recovered"`; else status `"ok"` (same keys).
- `regression_candidate(halt: Mapping, *, now) -> Candidate` — `Candidate.build(
  domain=bench_domain(bench), source=f"monitor:{bench}", kind="regression",
  title=f"{bench} canary fell below its best-ever score",
  evidence_key=f"canary:{bench}:{str(peak_sha)[:12]}",
  files=<that domain's research.DEFAULT_SPECS files, else ()>,
  detail=f"peak {peak} at {peak_sha[:12]}, now {latest} at {latest_sha[:12]}",
  fix_hint=f"main's {bench} score fell from {peak} (at {peak_sha[:12]}) to {latest} (at {latest_sha[:12]}) over {declines} consecutive canaries. Find the change since {peak_sha[:12]} that caused it and restore the score. Fix forward; do not revert main.",
  success_metric=f"{bench}: sealed paired gain (G12)", success_command=f"bench:{bench}",
  evidence=dict(halt), now=now)`.
- `review_age_days(home, spec, *, now) -> int | None` — via `research.latest_review`;
  `None` when there is no review.
- `maybe_refresh(root, home, cfg, *, now, force=False, domains=None) -> list[dict]` —
  for each domain in `cfg.research.domains` (∩ `domains` when given) that has a
  `research.DEFAULT_SPECS` entry with a non-empty `recipe`:
  - skip unless `force`, or (`cfg.research.auto_refresh` and the age is `None` or
    `>= cfg.research.refresh_days`);
  - skip when `home/"rsi"/f"research-refresh-{domain}.json"` records a launch within the
    last 86400 s (unless `force`);
  - launch `bash <root>/recipes/<recipe>/launch.sh verify-rrl-<YYYYmmdd-HHMMSS>` with
    `subprocess.Popen(..., cwd=root, stdout=<log>, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, env={**os.environ, "MINI_ORK_HOME": str(home)})`,
    log at `home/"rsi"/f"research-refresh-{domain}-{ts}.log"`; write the
    `research-refresh-{domain}.json` record `{"at": now, "run_id": ..., "log": ..., "pid": ...}`;
  - return one dict per domain: `{"domain", "action": "launched" | "skipped", "reason", "age_days", "log"}`.
  The run id prefix `verify-` keeps the default review glob matching. Only the
  `verification` spec has a recipe today.

### 4. `mini_ork/rsi/cycle.py`

In `_body`, right after the HARVEST block (after `_upsert`/`reconcile`) and before
QUEUE, when `cfg.research.enabled and not req.dry_run`:

1. `maybe_refresh(req.root, req.home, cfg, now=rt.clock(), domains=req.domains)`; print
   each `launched` entry to stderr as `[rsi] research refresh: …`; any exception is
   printed and ignored (a refresh never blocks a cycle).
2. When `cfg.monitor.enabled`: for each distinct benchmark of the enabled research
   domains (`research.DEFAULT_SPECS[d].benchmark`), call `run_canary(...)`; an
   exception is printed to stderr and treated as status `"error"` (never a halt by
   itself, never a crash).
3. `halt = adoption_halted(req.home)`; when not `None`:
   - drop every `kind == "technique_unadopted"` candidate from `report.candidates`
     (`dataclasses.replace`), print `[rsi] adoption halted: <bench> canary below peak`;
   - add `regression_candidate(halt, now=rt.clock())` to `report.candidates` and upsert
     it with `ledger.upsert_candidate`.
4. Continue to QUEUE with the possibly-modified `report`.

Nothing else in the cycle changes.

### 5. `mini_ork/cli/rsi_cycle.py`

- `_VERBS` gains `"research"` and `"impact"`; `_USAGE` gains
  `  mini-ork rsi-cycle research [--domain D] [--refresh] [--json]` and
  `  mini-ork rsi-cycle impact [status | rotate <benchmark> | canary] [--json]`, and its
  "all eleven gates" wording becomes "all twelve gates".
- New flag `--refresh` (store_true, help `research: launch the domain's review recipe now`).
- `_cmd_research(args, root, db)`: load the config (same loader `_cmd_run` uses; a
  `ConfigError` → stderr + return 2). For each domain in `research.DEFAULT_SPECS`
  (filtered by `--domain`): newest review path, `review_id`, `age_days`, ranked count,
  eligible count (via `research.eligible` with the config's flags), and from the DB the
  counts of `technique_unadopted` rows by `status`
  (`SELECT status, COUNT(*) FROM rsi_candidates WHERE kind='technique_unadopted' AND domain=? GROUP BY status`;
  a missing table → `{}`). With `--refresh`, call
  `monitor.maybe_refresh(root, home, cfg, now=int(time.time()), force=True, domains=[d])`
  and include its result. Print JSON with `--json`, else one readable block per domain.
  Return 0.
- `_cmd_impact(args, root, db)`: `home = Path(db).parent`.
  - `status` (default): print `impact_state.load_state(home)` per benchmark — epoch,
    looks, accepted, rejected, number of canaries, peak and latest score — plus
    `monitor.adoption_halted(home)`. `ImpactStateError` → stderr + return 1.
  - `rotate <benchmark>`: `rotate` + `save_state`; print the new epoch; unknown
    benchmark → stderr + return 1.
  - `canary`: load the config and run `monitor.run_canary` for each enabled research
    domain's benchmark; print the results.
  - Any other sub-verb → usage on stderr, return 2.

### 6. `docs/operator/rsi-cycle.md`

- Rename the heading `## The eleven gates` to `## The twelve gates`, add the table row
  `| G12 | \`impact\` | a \`bench:\` candidate showed no sealed, paired gain (dev split first; one sealed look at the spent α) |`,
  and replace other "eleven gates" wording with "twelve gates".
- Add a section `## Research-driven adoption` before `## What it does not do yet`,
  covering in plain words: how to turn it on (`research.enabled: true`, `--domain
  verification`); where techniques come from (the technique-review recipe →
  `merged-review.json` → `technique_unadopted` candidates); what G12 checks (sealed
  split keyed by a secret, SAGE/McNemar, no-regression, guards, α spending per look,
  `max_looks`, epochs); the revise loop (`impact.max_revisions`, dev-only feedback);
  family ordering; the canary + adoption halt; `rsi-cycle research` / `rsi-cycle impact`;
  the guarantee in one sentence ("never merges a measured regression, halts rather
  than ships when it cannot tell, promises nothing about what no benchmark measures");
  and the two tiers (simple: a 0600 secret file under the RSI home; secure:
  `secure.secret_store` + the vault, fail closed). Link
  `docs/plans/2026-10-29-research-driven-rsi-loop.md`.

## Files in scope (touch ONLY these)

- `mini_ork/rsi/monitor.py` (new)
- `mini_ork/rsi/config.py`
- `mini_ork/rsi/cycle.py`
- `mini_ork/cli/rsi_cycle.py`
- `config/rsi-cycle.example.yaml`
- `docs/operator/rsi-cycle.md`
- `tests/unit/test_rsi_monitor_py.py` (new)
- `tests/unit/test_rsi_cycle_config_py.py` — append tests; add `"monitor"` to the
  unknown-key parametrize list
- `tests/unit/test_rsi_cycle_cli_py.py` — append tests; update any assertion that pins
  the "eleven gates" usage wording

Do NOT modify `gates.py`, `select.py`, `impact.py`, `research.py`, `benchmarks.py`,
`impact_state.py` or any other file.

## Tests

**`tests/unit/test_rsi_monitor_py.py`** (hermetic; a temp git repo with one commit as
`root`; a fake benchmark registered with `replace=True` and restored in `finally`, whose
runner returns scores from a list the test controls):

- `run_canary` records one canary per call, keyed by HEAD sha (a second call at the same
  sha replaces, not appends); the canary worktree is removed afterwards
  (`git worktree list` shows only the repo);
- scores `0.6, 0.6, 0.55, 0.55, 0.55` over five new commits (drop 0.02, consecutive 3)
  → the fifth call returns `"regressed"`, the marker exists with `peak == 0.6`;
  a later commit scoring `0.6` → `"recovered"` and the marker is gone;
- a runner returning no scores → `"error"`, no canary recorded;
- `adoption_halted` on garbage marker text → `{"reason": "unreadable marker"}`;
- `regression_candidate` → `kind == "regression"`, `source == "monitor:<bench>"`,
  `success_command == "bench:<bench>"`, and `"do not revert main"` in `fix_hint`;
- `maybe_refresh`: `auto_refresh` false → skipped; true with no review → launched
  (monkeypatch `subprocess.Popen`, assert argv ends with a `verify-rrl-` run id and
  `start_new_session=True`); a second call within a day → skipped; `force=True` →
  launched; a review newer than `refresh_days` → skipped.
- cycle integration: with `research.enabled` and a halt marker present, a cycle's queue
  contains no `technique_unadopted` candidate and does contain the regression candidate
  (use the `Runtime` fakes from `tests/unit/test_rsi_cycle_py.py`, `--dry-run`-free
  `plan`-level check via `_body` or the smallest seam that reaches QUEUE; monkeypatch
  `monitor.run_canary` and `monitor.maybe_refresh` to no-ops).

**Config additions:** `monitor` defaults; `monitor.drop: 1` and `monitor.consecutive: 0`
raise; `research.auto_refresh` defaults to `False`; the example parses strict.

**CLI additions:** `main(["research", "--json", "--home", <tmp home>, "--root", <repo>])`
returns 0 with an empty home; `main(["impact", "--home", <tmp home>])` returns 0 and
prints no benchmarks on a fresh home; `main(["impact", "rotate", "nope", "--home", …])`
returns 1; `main(["impact", "bogus", …])` returns 2.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_rsi_monitor_py.py tests/unit/test_rsi_cycle_config_py.py tests/unit/test_rsi_cycle_cli_py.py`
- `pytest -q -p no:asyncio tests/unit/test_rsi_cycle_py.py tests/unit/test_rsi_revise_py.py tests/unit/test_rsi_impact_gate_py.py`
- `ruff check --select F,E9 mini_ork/rsi/monitor.py mini_ork/rsi/config.py mini_ork/rsi/cycle.py mini_ork/cli/rsi_cycle.py tests/unit/test_rsi_monitor_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_rsi_monitor_py.py --junitxml="${MINI_ORK_RUN_DIR}/rrl-6-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rrl-6-junit.xml`

## Success criteria

- All verification commands pass with every pre-existing test unchanged except the
  named wording pins.
- With `research.enabled: false` (the default) a cycle never runs a canary, never
  launches a refresh and never reads the halt marker.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
