# RRL-4b: wire gate G12 (sealed impact) and the research harvest into the RSI cycle

## Goal

The pieces of the research-driven RSI loop exist on `main`:
`mini_ork/rsi/impact.py` (statistics), `mini_ork/rsi/research.py` (technique review →
`technique_unadopted` candidates), `mini_ork/rsi/benchmarks.py` (registry +
`verifier-replay`), `mini_ork/rsi/impact_state.py` (secret, epochs, looks) and the
`research:` / `impact:` config sections. This kickoff connects them to the cycle:

1. the harvest adds research candidates when `research.enabled`;
2. a candidate whose success command is `bench:<name>` is measured by a new
   twelfth gate, **G12 `impact`**: dev split first (no error budget spent), then one
   sealed look with SAGE + no-regression + guard metrics at the spent α;
3. children may never edit the evaluator; research proposals must touch the
   benchmark's surface.

All paths are relative to the repository root. Read the five modules above before
editing; their public APIs are the contract and must not be changed here.

## Mechanism (exact spec)

### 1. `mini_ork/rsi/gates.py`

- `GATE_ORDER` gains a twelfth row, last: `("G12", "impact", "no_measured_gain")`.
- `GateEvidence` gains, after the G11 fields, `# G12` and
  `impact: Mapping[str, Any] | None = None`.
- New constant `BENCH_PREFIX = "bench:"` (add to `__all__` if the module has one).
- `_g8`: as its FIRST statement, when `ev.metric_command.startswith(BENCH_PREFIX)`
  return `GateResult("G8", "success_metric", True, detail="measured by G12")` (use the
  `GateResult` field name the module already uses for details).
- New `_g12(ev)`:
  - not a bench command → `GateResult("G12", "impact", True)`;
  - `ev.impact is None` → fail `no_measured_gain`, detail
    `"impact was not measured"`;
  - `ev.impact.get("status") == "accepted"` → pass;
  - otherwise fail `no_measured_gain`, detail
    `f"{ev.impact.get('stage') or 'setup'}: {ev.impact.get('reason') or 'unknown'}"`.
- `_GATE_FNS` gains `"G12": _g12`. Update the module docstring's gate count words
  ("eleven" → "twelve") and add one sentence describing G12.

### 2. `mini_ork/rsi/select.py`

- `DENY_PREFIXES` becomes
  `("mini_ork/rsi/", "evals/", "scripts/verifier_replay.py", "scripts/run_heldout.py")`.
- `registered_success_commands()` also includes
  `f"bench:{name}"` for every name in `mini_ork.rsi.benchmarks.benchmark_names()`
  (lazy import inside the function).
- New select code `"off_surface"` appended to `SELECT_CODES`. In `select()`, right
  after the `self_edit_of_gate` check: when `cand.kind == "technique_unadopted"` and
  `cand.success_command` starts with `"bench:"`, look up the benchmark
  (`benchmarks.get_benchmark(name)`; unknown → reject `missing_success_command` with
  detail `f"unknown benchmark {name!r}"`). If the benchmark has a non-empty `surface`
  and NO proposal file is inside it (same `_allowlisted` prefix semantics), reject
  `off_surface` with detail
  `f"no proposed file is inside the {name} benchmark surface: {', '.join(surface)}"`.
- `build_kickoff`: when `verification_command` starts with `"bench:"`:
  - the second constraint becomes
    `f"Impact is measured by the harness on a held-out benchmark (`{verification_command}`) that this run cannot see or run. Add or update unit tests for the new behaviour; they are the run's own check."`;
  - append the constraint
    `"Do not edit `evals/`, `scripts/verifier_replay.py`, `scripts/run_heldout.py`, `mini_ork/rsi/`, or any test that pins existing behaviour."`;
  - the first acceptance line becomes
    `f"- {ac_ids[0]} The change implements the mechanism described under Expected Edit, inside Files in scope."`.
  Every other byte of the kickoff is unchanged, and non-bench kickoffs are byte-for-byte
  unchanged.

### 3. `mini_ork/rsi/cycle.py`

**a. Research harvest.** `_default_harvest` keeps its current call and, when
`req.config.research.enabled`, appends research candidates:

```python
from dataclasses import replace as _replace
from . import research
rcfg = req.config.research
domains = [d for d in rcfg.domains if not req.domains or d in req.domains]
specs = {d: _replace(research.DEFAULT_SPECS[d],
                     review_glob=rcfg.review_globs.get(d, research.DEFAULT_SPECS[d].review_glob))
         for d in domains if d in research.DEFAULT_SPECS}
harvests = research.harvest_all(req.home, specs, now=now, max_n=rcfg.max_candidates,
                                verdicts=rcfg.verdicts,
                                skip_disagreement=rcfg.skip_disagreement,
                                skip_single_rater=rcfg.skip_single_rater)
```

Print each harvest's notes and error to stderr as `[rsi] research <domain>: <text>`.
Return `replace(report, candidates=tuple(report.candidates) + <all research candidates>)`.
A research error never changes `harvest_ok`.

**b. Research blindness.** `_blind_sources(report, con=None)` gains an optional
`con`. When `con` is given and the report contains NO candidate with
`kind == "technique_unadopted"`, it also returns every distinct `source` from
`SELECT DISTINCT source FROM rsi_candidates WHERE status = 'open' AND kind = 'technique_unadopted'`
(an `sqlite3.Error` adds nothing). The one call site in `_body` passes `con`.
(Research switched off, or a missing review, must not close open technique rows as
`fixed_externally`.)

**c. Evaluator tamper guard.** In `_evidence`, `frozen_hits` becomes the existing hits
plus every changed file inside `select.DENY_PREFIXES` (prefix semantics as in
`select._allowlisted`), de-duplicated, existing hits first.

**d. Measurement.** In `_default_measure`:
- for a bench candidate (`cand.success_command.startswith("bench:")`), skip `_gap`
  and use `gap_before, gap_after = None, None`;
- pass `impact=_impact(req, cand, impl)` into the returned `GateEvidence`.

**e. `_impact(req, cand, impl, *, base: Path | None = None) -> dict | None`** — new.
Returns `None` for a non-bench candidate. Otherwise builds and returns `out`, after
writing it as JSON to `req.home / "rsi" / req.cycle_id / "impact.json"` (parents
created). `out` starts as
`{"benchmark": name, "status": "error", "stage": "setup", "reason": "", "epoch": None, "look": None, "alpha": None, "dev": None, "sealed": None, "guards": None, "flaky": []}`.
Steps (`icfg = req.config.impact`, `wt = Path(getattr(impl.worktree, "path", req.root))`):

1. `bench = benchmarks.get_benchmark(name)`; `KeyError` → `reason="unknown_benchmark"`.
2. `ctx = benchmarks.BenchContext(home=req.home, repo=req.root, workers=icfg.workers, timeout_s=icfg.timeout_s)`.
3. `secret = impact_state.load_secret(req.home, secure_store=req.config.secure.secret_store, secret_name=icfg.secret_name)`.
4. `items = dict(bench.items(ctx))`; empty → `reason="empty_corpus"`.
5. `state = impact_state.load_state(req.home)`;
   `entry = impact_state.bench_state(state, name, impact_state.corpus_hash(items))`;
   `out["epoch"] = entry["epoch"]`.
6. `split = impact.split_items(items, secret=secret, epoch=entry["epoch"], dev_fraction=icfg.dev_fraction, strata=items)`.
7. Base tree: `base_tree = base or _detached_base(req, wt)`; `None` → `reason="no_base"`.
   In a `try/finally` that calls `_remove_detached(req, base_tree)` only when `base`
   was not given:
   - `first = benchmarks.run_benchmark(name, base_tree, ctx=ctx)`;
   - stable set: file `impact_state.impact_dir(req.home) / f"stable-{name}-{first.tree}.json"`
     (only when `first.tree` is not `None`). If it exists, `stable` = its JSON list.
     Otherwise run `icfg.base_repeats - 1` more times with `use_cache=False`,
     `stable, flaky = impact.stable_items([first.scores, *repeat scores])`, write the
     sorted stable list there (when `first.tree` is not `None`), and set
     `out["flaky"] = sorted(flaky)`.
8. `head = benchmarks.run_benchmark(name, wt, ctx=ctx)`.
9. `dev_items = [i for i in split["dev"] if i in stable]`, `sealed_items` likewise.
10. Dev stage: `dev_table = impact.paired_counts(first.scores, head.scores, items=dev_items)`,
    `dev_dec = impact.sage_decision(dev_table, alpha=0.5, lam=icfg.lam, max_regression_rate=icfg.max_regression_rate)`;
    `out["dev"] = {**dev_table.to_dict(), "reason": dev_dec["reason"], "items": _dev_feedback(dev_items, first, head)}`.
    When `dev_dec["reason"] not in ("accepted", "not_significant")` →
    `status="rejected", stage="dev", reason=f"dev_{dev_dec['reason']}"`, return (no look
    spent, state not saved).
11. `entry["looks"] >= icfg.max_looks` → `status="rejected", stage="sealed", reason="sealed_budget_exhausted"`, return.
12. Sealed look: `look = entry["looks"] + 1`,
    `alpha_k = impact.look_alpha(icfg.alpha, look, spending=icfg.spending)`,
    `sealed_table = impact.paired_counts(first.scores, head.scores, items=sealed_items)`,
    `dec = impact.sage_decision(sealed_table, alpha=alpha_k, lam=icfg.lam, max_regression_rate=icfg.max_regression_rate)`,
    `guards = impact.guard_check(first.guards, head.guards, tolerances=icfg.guard_tolerances, lower_is_better=bench.lower_is_better)`,
    `accepted = dec["accept"] and guards["ok"]`;
    `impact_state.record_look(state, name, accepted=accepted)`;
    `impact_state.save_state(req.home, state)`.
    `out.update(status="accepted" if accepted else "rejected", stage="sealed", look=look, alpha=alpha_k, reason="accepted" if accepted else (dec["reason"] if not dec["accept"] else "guard_violation"), sealed={**sealed_table.to_dict(), "p_value": dec["p_value"], "delta": dec["delta"], "regression_rate": dec["regression_rate"]}, guards=guards)`.
13. `impact_state.ImpactStateError` anywhere → `reason=f"state_error: {exc}"`; any other
    exception → `reason=f"measure_error: {type(exc).__name__}: {exc}"` truncated to 300
    characters. Both keep `status="error"`.

`_dev_feedback(items, base, head) -> list[dict]` — new: for each id in `items` (sorted)
where `base.scores.get(id) != head.scores.get(id)` or `head.scores.get(id, 0) < 0.5`,
the dict `{"item": id, "stratum": head.strata.get(id, ""), "base": base.scores.get(id), "head": head.scores.get(id), "reason": str((head.meta.get("reasons") or {}).get(id, ""))[:200]}`;
at most 40 entries.

`public_impact(out: Mapping | None) -> dict | None` — new: `None` → `None`; else a copy
with `sealed` and `guards` replaced by `None` and `flaky` dropped. (The revise loop,
RRL-5, may only ever read this view: the sealed split returns one bit.)

## Files in scope (touch ONLY these)

- `mini_ork/rsi/gates.py`
- `mini_ork/rsi/select.py`
- `mini_ork/rsi/cycle.py`
- `tests/unit/test_rsi_gates_py.py` — change `assert len(report.results) == 11` to
  `== 12`, update "eleven" wording in its docstrings, and append G12/G8 tests
- `tests/unit/test_rsi_select_py.py` — append tests only
- `tests/unit/test_rsi_impact_gate_py.py` (new)

Do NOT modify `impact.py`, `research.py`, `benchmarks.py`, `impact_state.py`,
`config.py`, `harvest.py`, `queue.py`, `learn.py` or any other file.

## Tests

**`tests/unit/test_rsi_gates_py.py` additions:** G12 passes for a non-bench command;
fails `no_measured_gain` with `impact=None` and a `bench:verifier-replay` command;
passes with `{"status": "accepted"}`; fails with detail `"dev: dev_net_loss"` for
`{"status": "rejected", "stage": "dev", "reason": "dev_net_loss"}`; G8 passes with
detail `measured by G12` for a bench command even when `gap_after is True`.

**`tests/unit/test_rsi_select_py.py` additions:**
`"bench:verifier-replay" in registered_success_commands()`; a proposal touching
`evals/x.json` → `self_edit_of_gate`; a `technique_unadopted` candidate with
`success_command="bench:verifier-replay"` proposing only `mini_ork/dispatch/routing.py`
→ `off_surface`; proposing `recipes/code-fix/verifiers/test.py` → accepted, and its
kickoff contains `held-out benchmark` and does NOT contain
`reports success on the fixed tree`; a non-bench kickoff is byte-identical to
`build_kickoff` output computed before this change for the same inputs (pin one
existing fixture's kickoff text by recomputing it with a `db:` command and asserting
`"The gap must stop reproducing"` is present).

**`tests/unit/test_rsi_impact_gate_py.py` (new, hermetic):** build a request with the
`_cfg` pattern from `tests/unit/test_rsi_cycle_py.py`
(`from_mapping({... "budget", "critic", "gates", "decide" ...}, env={}, strict=True)`),
`db = tmp_path / "home" / "state.db"` (so `req.home` is `tmp_path / "home"`), and a fake
benchmark registered with `benchmarks.register_benchmark(..., replace=True)` (restore
in a `finally`) whose `items()` returns 40 ids (`h00..h15` → `hard_negative`,
`c00..c23` → `correct`) and whose `runner(root, items, ctx)` returns scores from a dict
keyed by `root.name` (`"base"` / `"head"`), `tree=root.name`, guards
`{"false_reject": 0.0, "no_verdict": 0.0}`. Pass `base=tmp_path / "base"` explicitly and
an `impl` whose `worktree.path` is `tmp_path / "head"` (both plain dirs).

- non-bench candidate → `_impact(...) is None`;
- unknown benchmark name → `status == "error"`, `reason == "unknown_benchmark"`;
- head identical to base → `stage == "dev"`, `reason == "dev_no_discordant_pairs"`,
  state file shows `looks == 0`;
- head fixes every hard negative, breaks nothing → `status == "accepted"`,
  `stage == "sealed"`, `look == 1`, `alpha == round(0.05 / 3.39, 6)` within 1e-6,
  `impact.json` written under `home/rsi/<cycle_id>/`, state `looks == 1, accepted == 1`;
- head fixes every hard negative but breaks one sealed correct item → rejected with
  `reason == "regression_budget"` (or `"dev_regression_budget"` if the broken item
  fell in dev — construct the case by breaking an item you read from the computed
  sealed split);
- guard violation (head `false_reject` 1.0) with otherwise accepted scores → `reason == "guard_violation"`;
- `max_looks` exhausted (pre-seed state with `looks = 30`) → `sealed_budget_exhausted`;
- `public_impact` hides `sealed` / `guards`;
- the secret file `home/rsi/impact/secret` was created with mode `0o600`;
- `_default_harvest` with `research.enabled: true` and a `runs/verify-x/merged-review.json`
  under the home (one eligible row) returns the probe/rule candidates plus one
  `technique_unadopted` candidate (monkeypatch `harvest.harvest` to return an empty
  `HarvestReport` so no rule runs);
- `_blind_sources(report, con)` returns an open `research:G01` technique row's source
  when the report has no technique candidates, and not when it has one.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_rsi_impact_gate_py.py tests/unit/test_rsi_gates_py.py tests/unit/test_rsi_select_py.py`
- `pytest -q -p no:asyncio tests/unit/test_rsi_cycle_py.py tests/unit/test_rsi_cycle_cli_py.py tests/unit/test_rsi_gates_python.py tests/unit/test_rsi_harvest_py.py`
- `ruff check --select F,E9 mini_ork/rsi/gates.py mini_ork/rsi/select.py mini_ork/rsi/cycle.py tests/unit/test_rsi_impact_gate_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_rsi_impact_gate_py.py tests/unit/test_rsi_gates_py.py --junitxml="${MINI_ORK_RUN_DIR}/rrl-4b-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rrl-4b-junit.xml`

## Success criteria

- All verification commands pass; every pre-existing test is unchanged except the
  one `== 11` pin and its wording.
- The revise path cannot read sealed counts: nothing outside `_impact` and the
  operator artifact reads `out["sealed"]`.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
