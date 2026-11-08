# RRL-5b: `research.first` must queue techniques, not just reorder the selected few

## Goal

`research.first: true` (RRL-5) is meant to make a research-driven loop work on ranked
techniques before harvested gaps. A live dry run on 2026-10-29 showed it does nothing
when the domain also has gaps: `build_queue` caps each domain's pool at
`queue.per_domain_max` (5) by severity, verification gaps (severity 60–86) fill the
pool, and every `technique_unadopted` candidate (severity 49–61) is dropped with
`domain_cap` before `_order_research` runs — which only re-orders `plan.selected`.
The dry run picked `db:V3`.

Fix: when `research.first` is on, build the queue from the technique candidates alone;
fall back to the full candidate set only when no technique is eligible.

All paths are relative to the repository root. Read `mini_ork/rsi/cycle.py`
(`_body`, `_build_queue`, `_order_research`) first.

## Mechanism (exact spec)

### `mini_ork/rsi/cycle.py`

New helper `_queue_for(req, con, report, now) -> QueuePlan`:

```python
def _queue_for(req, con, report, now):
    if req.config.research.first:
        tech = tuple(c for c in report.candidates if c.kind == "technique_unadopted")
        if tech:
            plan = _build_queue(req, con, replace(report, candidates=tech), now)
            if plan.top is not None:
                return plan
    return _build_queue(req, con, report, now)
```

In `_body`, the QUEUE step's `plan = _build_queue(req, con, report, rt.clock())`
becomes `plan = _queue_for(req, con, report, rt.clock())` (the following
`_order_research` call is unchanged). No other call site changes — `plan`/dry-run
reporting elsewhere keeps calling `_build_queue` directly.

`replace` is `dataclasses.replace` (already imported in the module, or import it).

## Files in scope (touch ONLY these)

- `mini_ork/rsi/cycle.py`
- `tests/unit/test_rsi_research_first_py.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_rsi_research_first_py.py`, hermetic)

Reuse the patterns of `tests/unit/test_rsi_revise_py.py` / `tests/unit/test_rsi_cycle_py.py`
(`_cfg`, a migrated temp DB, `Candidate.build`). Build a report with six verification
gap candidates (`kind="data_path_dead"`, distinct evidence keys, high severity) and two
`technique_unadopted` candidates (`source="research:G01"` / `"research:G02"`,
`success_command="bench:verifier-replay"`, `files=("recipes/code-fix/verifiers/test.py",)`).

- `research.first: false` → `_queue_for(...).top` is a gap candidate (today's behaviour).
- `research.first: true` → `_queue_for(...).top.kind == "technique_unadopted"` and
  every item in `plan.selected` is a technique.
- `research.first: true` with NO technique candidates → the top is a gap candidate.
- `research.first: true` where both techniques are ineligible (insert ledger rows that
  mark their signatures `merged`, the same way `tests/unit/test_rsi_queue_py.py` /
  `test_rsi_cycle_py.py` seed avoidance) → falls back: the top is a gap candidate.
- `_body` uses `_queue_for`: run a dry-run cycle (`dry_run=True`, `Runtime` fakes for
  `probes`/`harvest` returning the report above, `research.first: true`) and assert the
  outcome's `candidate_id` is one of the two techniques.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_rsi_research_first_py.py tests/unit/test_rsi_revise_py.py`
- `pytest -q -p no:asyncio tests/unit/test_rsi_cycle_py.py tests/unit/test_rsi_impact_gate_py.py tests/unit/test_rsi_monitor_cycle_py.py`
- `ruff check --select F,E9 mini_ork/rsi/cycle.py tests/unit/test_rsi_research_first_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_rsi_research_first_py.py --junitxml="${MINI_ORK_RUN_DIR}/rrl-5b-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rrl-5b-junit.xml`

## Success criteria

- All verification commands pass with every pre-existing test unchanged.
- With `research.first: false` (the default) the queue is exactly today's.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
