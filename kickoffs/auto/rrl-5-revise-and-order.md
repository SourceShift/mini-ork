# RRL-5: revise a no-gain attempt, and order research techniques by family credit

## Goal

After RRL-4b, a `technique_unadopted` candidate is measured by gate G12 (sealed,
paired impact test). Two behaviours from the research
(`docs/research/2026-10-29-research-driven-rsi-loop.md`) are still missing:

1. **Revise instead of giving up.** When the ONLY failing deterministic gate is G12 and
   its rejection is a measured no-gain (not an error, not an exhausted budget), the
   cycle re-dispatches the child up to `impact.max_revisions` times with
   reflective feedback built from the **dev split only** (GEPA-style feedback,
   rejection-status list, SpecForge evidence, Paper2Code fidelity check). The sealed
   split is never shown beyond accepted/rejected.
2. **Try the most promising technique family first.** Technique candidates inside the
   queue's selected list are re-ordered by clade Thompson sampling over family credit
   (HGM), with a collapse override that forces an untried family when recent attempts
   plateau on one idea (principle-level local optima).

All paths are relative to the repository root. Read `mini_ork/rsi/cycle.py`,
`mini_ork/rsi/impact.py` and `mini_ork/rsi/config.py` first.

## Mechanism (exact spec)

### 1. `mini_ork/rsi/config.py`

- `ImpactConfig` gains `max_revisions: int = 2` (an `int ≥ 0`, `bool` refused, else
  `ConfigError("impact.max_revisions: …")`); add it to the section's known keys.
- `ResearchConfig` gains `first: bool = False` (through `_flag`); add it to the
  section's known keys.

### 2. `config/rsi-cycle.example.yaml`

Under `research:` add `first: false    # true = technique candidates ahead of gap candidates`.
Under `impact:` add `max_revisions: 2   # re-dispatches after a measured no-gain (dev feedback only)`.

### 3. `mini_ork/rsi/cycle.py`

**a. `_dispatch_child(req, wt, kickoff, child_home, *, attempt: int = 0)`** —
`run_id` is `f"{req.cycle_id}-fe"` when `attempt == 0`, else
`f"{req.cycle_id}-fe-r{attempt}"`; the kickoff file is `child_home / "kickoff.md"`
when `attempt == 0`, else `child_home / f"kickoff-r{attempt}.md"`. Nothing else changes.

**b. `_default_implement(req, cand, selection, *, attempt: int = 0)`** passes
`attempt` to `_dispatch_child`. The `Runtime.implement` hook keeps its 3-argument call
for `attempt == 0`; for `attempt >= 1` the cycle calls
`rt.implement(req, cand, selection)` too when an override is installed (tests inject
fakes that ignore attempts), and `_default_implement(req, cand, selection, attempt=attempt)`
otherwise.

**c. The revise loop** in `_body`, replacing the single IMPLEMENT → VALIDATE pass up to
(but not including) the critic panel:

```text
attempt = 0; total_cost = 0.0; history = []
loop:
    impl = implement(attempt)
    (empty / None impl → the existing implement_empty rejection; when attempt > 0 the
     halt_reason detail is prefixed with f"revision {attempt}: ")
    total_cost += impl.cost_usd; impl = replace(impl, cost_usd=total_cost)
    measurement = measure(...); evidence = _evidence(...); first = evaluate(evidence)
    deterministic = report without G10
    if deterministic.passed: break                      # → critics, decide, merge as today
    if not _revisable(req, deterministic, measurement, attempt, total_cost):
        return _quarantine(..., "gate_failed")          # exactly as today
    history.append(_attempt_record(attempt, impl, measurement))
    _archive_impact(req, attempt)
    clean the worktree (rt.clean or _default_clean); a cleanup exception →
        return _quarantine(..., "revise_cleanup_failed", detail=str(exc))
    attempt += 1
    selection = _revise_selection(req, selection, history)
    print(f"[rsi] revision {attempt}/{max_revisions}: {reason}", file=sys.stderr)
```

Everything after the loop (critics, DECIDE, merge, ledger rows) is unchanged and uses
the last `impl`, `evidence`, `first`.

**d. `_revisable(req, report, measurement, attempt, total_cost) -> bool`** — True only
when ALL hold:
- `attempt < req.config.impact.max_revisions`;
- the failing gates in `report.results` are exactly `["G12"]`;
- `measurement.impact` is a mapping with `status == "rejected"` and
  `reason != "sealed_budget_exhausted"`;
- `total_cost < req.config.budget.per_cycle_cap_usd`.

**e. `_attempt_record(attempt, impl, measurement) -> dict`** —
`{"attempt": attempt + 1, "impact": public_impact(measurement.impact), "changed": list(impl.changed_files), "diff": (impl.diff or "")[:6000]}`.
It must use `public_impact` (from RRL-4b) so no sealed count can reach a prompt.

**f. `_archive_impact(req, attempt)`** — when `req.home / "rsi" / req.cycle_id / "impact.json"`
exists, copy it to `impact-attempt-{attempt + 1}.json` in the same directory.

**g. `_revise_selection(req, selection, history) -> Selection`** — returns
`replace(selection, kickoff=new, kickoff_sha=sha256(new).hexdigest())` where
`new = selection.kickoff.rstrip() + "\n\n" + _revise_section(req, history) + "\n"`.
(`selection.kickoff` is the ORIGINAL kickoff on every revision: keep a reference to the
first selection and always append to it, so sections do not stack.)

**h. `_revise_section(req, history) -> str`** — exactly this structure (`n` = the next
attempt's number = `len(history) + 1`, `m` = `max_revisions + 1`; `last = history[-1]`;
`imp = last["impact"] or {}`; `dev = imp.get("dev") or {}`):

~~~
## Revision {n} of {m} — the previous attempt showed no measured gain

The harness measured each attempt on a held-out benchmark. You see only its dev half; the sealed half reports a single bit.

### Previous attempts
- attempt {k}: stopped at {stage}: {reason}. Dev split: {wins} fixed, {losses} broken, {both_fail} still wrong.
  (one line per history entry; missing numbers print as 0)

### Dev items the last attempt still gets wrong or broke
- `{item}` ({stratum}): base {base}, now {head}. Verifier said: {reason}
  (at most 15 lines from dev["items"]; "- (none)" when empty)

### What the last attempt changed
- `{path}` (one per changed file)

```diff
{last["diff"]}
```

### Rules for this revision
- These attempts were tried and rejected: do not resubmit the same change.
- Generalize. A change that special-cases the item ids above will fail the sealed half, which holds different items.
- First check that the change implements the Mechanism under Expected Edit. A no-gain result often means the mechanism was not implemented as described.
- If the dev split shows 0 fixed and 0 broken, the change altered no verdict: it is probably outside the code path the benchmark exercises.
~~~

**i. `_order_research(req, con, plan) -> QueuePlan`** — called in `_body` right after
`plan = _build_queue(...)`.
- `positions` = indexes in `plan.selected` whose candidate has
  `kind == "technique_unadopted"`; none → return `plan` unchanged.
- `attempts` = `[{"family": source.split(":", 1)[1], "technique": candidate_id, "success": outcome == "merged"} for (candidate_id, source, outcome) in SELECT candidate_id, source, outcome FROM rsi_credit WHERE source LIKE 'research:%' ORDER BY created_at, id]`
  (`sqlite3.Error` → `[]`; a NULL `candidate_id` becomes `""`).
- `pmax = max(float(c.evidence.get("priority") or 0) for the technique candidates) or 1.0`;
  options = `{"id": cand.candidate_id, "family": cand.source.split(":", 1)[1] if ":" in cand.source else cand.source, "prior": float(cand.evidence.get("priority") or 0) / pmax}`.
- `rng = random.Random(int(hashlib.sha256(req.cycle_id.encode()).hexdigest()[:8], 16))`;
  `order = impact.thompson_order(options, attempts, rng=rng)`.
- `col = impact.family_collapse([a["family"] for a in attempts], [a["success"] for a in attempts])`;
  when `col["collapse"]`, stable-partition `order` so ids whose family is NOT in
  `col["recent_families"]` come first.
- When `req.config.research.first`: `selected` = the re-ordered technique items, then
  the non-technique items in their original order. Otherwise the technique items fill
  `positions` in the new order and every other item keeps its index.
- Re-number `rank` as `index + 1` (`dataclasses.replace` on each `QueueItem`) and return
  `replace(plan, selected=tuple(new_selected))`.
- Print `[rsi] research order: {comma-joined ids} (collapse: {col['collapse']})` to stderr.
- A pinned candidate (`req.candidate_id`) still resolves through `plan.find`; this
  function never drops or adds items.

## Files in scope (touch ONLY these)

- `mini_ork/rsi/cycle.py`
- `mini_ork/rsi/config.py`
- `config/rsi-cycle.example.yaml`
- `tests/unit/test_rsi_cycle_config_py.py` — append tests only
- `tests/unit/test_rsi_revise_py.py` (new)

Do NOT modify `gates.py`, `select.py`, `impact.py`, `research.py`, `benchmarks.py`,
`impact_state.py`, `queue.py`, `learn.py`, or any other file.

## Tests

**Config additions:** defaults `max_revisions == 2`, `first is False`;
`max_revisions: -1` and `max_revisions: true` raise `impact.max_revisions`; the example
still parses strict.

**`tests/unit/test_rsi_revise_py.py`** (hermetic; reuse the patterns of
`tests/unit/test_rsi_cycle_py.py`: `_cfg`, a migrated temp DB, a `Runtime` with fake
`probes`, `harvest`, `propose`, `implement`, `measure`, `critics`, `merge`, `clean`):

- **revise then accept:** a technique candidate (`kind="technique_unadopted"`,
  `success_command="bench:verifier-replay"`), `measure` returns a `GateEvidence` where
  every gate passes except G12 on the first call (`impact={"status": "rejected", "stage": "dev", "reason": "dev_net_loss", "dev": {"wins": 0, "losses": 1, "both_fail": 3, "items": [{"item": "h01", "stratum": "hard_negative", "base": 0.0, "head": 0.0, "reason": "accepted"}]}}`)
  and `impact={"status": "accepted"}` on the second → the cycle merges; `implement`
  was called twice; `clean` was called once before the second implement; the second
  selection's kickoff contains `## Revision 2 of 3`, `h01`, and the first attempt's
  diff text; it contains the original kickoff exactly once.
- **no sealed leak:** with a first impact that also carries
  `"sealed": {"wins": 9, "losses": 0}` and `"guards": {...}`, the revised kickoff
  contains neither `"wins": 9` nor the word `sealed:` followed by digits (assert that the
  string `9 fixed` is absent).
- **cap:** `max_revisions: 1` and G12 rejected on every call → `implement` called twice,
  then `rejected`/quarantined with code `no_measured_gain`.
- **not revisable:** G6 failing together with G12 → no revision (implement called once);
  `reason == "sealed_budget_exhausted"` → no revision; `status == "error"` → no revision;
  `total_cost` at or above `per_cycle_cap_usd` after attempt 1 → no revision.
- **cumulative cost:** two attempts costing 1.5 and 2.0 → the evidence's `cycle_cost`
  on the second evaluation is 3.5.
- **dispatch ids:** `_dispatch_child` with `attempt=0` writes `kickoff.md` and uses run
  id `<cycle>-fe`; with `attempt=2` writes `kickoff-r2.md` and `<cycle>-fe-r2`
  (monkeypatch `subprocess.run` to capture argv and env).
- **_archive_impact:** copies `impact.json` to `impact-attempt-1.json`; no file → no-op.
- **_order_research:**
  - a plan with selected `[gap, techA(G01), gap2, techB(G02)]`, credit rows giving
    `research:G02` 5 merges and `research:G01` 5 rejections → `techB` takes index 1 and
    `techA` index 3 (positions preserved), gaps unchanged, ranks renumbered `1..4`;
  - `research.first: true` → `[techB, techA, gap, gap2]`;
  - collapse: 6 credit rows all `research:G01` failures, techniques from G01 and G03 →
    the G03 technique is first among technique positions;
  - same `cycle_id` → same order twice; a plan without technique items is returned
    unchanged (identity).

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_rsi_revise_py.py tests/unit/test_rsi_cycle_config_py.py`
- `pytest -q -p no:asyncio tests/unit/test_rsi_cycle_py.py tests/unit/test_rsi_impact_gate_py.py tests/unit/test_rsi_cycle_cli_py.py`
- `ruff check --select F,E9 mini_ork/rsi/cycle.py mini_ork/rsi/config.py tests/unit/test_rsi_revise_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_rsi_revise_py.py --junitxml="${MINI_ORK_RUN_DIR}/rrl-5-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rrl-5-junit.xml`

## Success criteria

- All verification commands pass with every pre-existing test unchanged.
- A cycle with no technique candidate and no `bench:` command behaves exactly as before
  (the existing `tests/unit/test_rsi_cycle_py.py` passes untouched).

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
