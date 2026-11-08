# RRL-6b: call the canary monitor and the research refresh from the RSI cycle

## Goal

`mini_ork/rsi/monitor.py` (RRL-6a) can score `main` on the benchmark, keep the
running-peak history, write an `ADOPTION_HALTED` marker on a sustained decline, build a
fix-forward `regression` candidate, and relaunch a stale technique review. Nothing in
the cycle calls it yet. This kickoff wires it into `_body`, between HARVEST and QUEUE,
only when `research.enabled` — so a cycle with the default config behaves exactly as
before.

All paths are relative to the repository root. Read `mini_ork/rsi/cycle.py` and
`mini_ork/rsi/monitor.py` first; do not change `monitor.py`'s API.

## Mechanism (exact spec)

### `mini_ork/rsi/cycle.py`

In `_body`, right after the HARVEST block (after `_upsert` and `ledger.reconcile`)
and before `plan = _build_queue(...)`, when `cfg.research.enabled and not req.dry_run`:

1. **Refresh.** `monitor.maybe_refresh(req.root, req.home, cfg, now=rt.clock(), domains=req.domains)`;
   print each entry whose `action == "launched"` to stderr as
   `[rsi] research refresh: <domain> launched (<log>)`. Any exception is printed as
   `[rsi] research refresh failed: <Type>: <msg>` and ignored.
2. **Canary.** When `cfg.monitor.enabled`: for each distinct benchmark name among
   `research.DEFAULT_SPECS[d].benchmark` for `d` in `cfg.research.domains`
   (∩ `req.domains` when set, and only domains present in `DEFAULT_SPECS`), call
   `monitor.run_canary(req.root, req.home, cfg, bench, now=rt.clock())` and print
   `[rsi] canary <bench>: <status> score=<score> peak=<peak>` to stderr. An exception
   is printed as `[rsi] canary <bench> failed: <Type>: <msg>` and ignored — a canary
   failure never halts and never crashes a cycle.
3. **Adoption halt.** `halt = monitor.adoption_halted(req.home)`; when not `None`:
   - `report = replace(report, candidates=tuple(c for c in report.candidates if c.kind != "technique_unadopted"))`;
   - print `[rsi] adoption halted: <halt.get('bench', '?')> canary below its peak`;
   - when `halt` has a `bench` key: `reg = monitor.regression_candidate(halt, now=rt.clock())`,
     `ledger.upsert_candidate(con, reg, now=rt.clock())`, and
     `report = replace(report, candidates=(*report.candidates, reg))`.
4. Continue to QUEUE with the possibly-modified `report`.

This block must sit in a small helper, `_research_preflight(req, rt, con, report) -> HarvestReport`,
called from `_body`; nothing else in the cycle changes.

## Files in scope (touch ONLY these)

- `mini_ork/rsi/cycle.py`
- `tests/unit/test_rsi_monitor_cycle_py.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_rsi_monitor_cycle_py.py`, hermetic)

Reuse the patterns of `tests/unit/test_rsi_cycle_py.py` (`_cfg`, a migrated temp DB,
`Runtime` fakes). Monkeypatch `monitor.run_canary`, `monitor.maybe_refresh` and (where
needed) `monitor.adoption_halted` / `monitor.regression_candidate` on the
`mini_ork.rsi.monitor` module.

- `research.enabled: false` → `_research_preflight` returns the report unchanged and
  neither `run_canary` nor `maybe_refresh` is called.
- `research.enabled: true`, no halt → `run_canary` called once with
  `"verifier-replay"`, `maybe_refresh` called once, report unchanged.
- a halt marker present (`{"bench": "verifier-replay", "peak": 0.6, "peak_sha": "a"*40, "latest": 0.5, "latest_sha": "b"*40, "declines": 3, "at": 1}`)
  and a report holding one `technique_unadopted` candidate and one gap candidate →
  the returned report holds the gap candidate and one `regression` candidate whose
  `source == "monitor:verifier-replay"`, and that candidate is in `rsi_candidates`.
- `run_canary` raising → no exception escapes; the report is unchanged.
- `maybe_refresh` raising → no exception escapes.
- `req.dry_run` → nothing is called.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_rsi_monitor_cycle_py.py tests/unit/test_rsi_monitor_py.py`
- `pytest -q -p no:asyncio tests/unit/test_rsi_cycle_py.py tests/unit/test_rsi_revise_py.py tests/unit/test_rsi_impact_gate_py.py`
- `ruff check --select F,E9 mini_ork/rsi/cycle.py tests/unit/test_rsi_monitor_cycle_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_rsi_monitor_cycle_py.py --junitxml="${MINI_ORK_RUN_DIR}/rrl-6b-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rrl-6b-junit.xml`

## Success criteria

- All verification commands pass with every pre-existing test unchanged.
- With `research.enabled: false` (the default) a cycle never runs a canary, never
  launches a refresh and never reads the halt marker.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
