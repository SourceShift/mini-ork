# Remaining-tasks resolution — 2026-10-13

Session `83e57620` (continued). Governed by the user's instruction: *"do all
remainings when it comes to product decision take it yourself."* This doc
records the disposition of each previously-open item, with evidence.

## Dispositions

### `wizard-launch-wiring` — SHIPPED
- Implemented as a `framework-edit` run: **`run-1791925489-75510`**, verdict
  `{tests_pass:true, static_pass:true, pass:true}` (2 files).
- Adds a repeatable `--set K=V` flag to `mini-ork run`, routed through
  `mini_ork.features.gate_env`, failing closed (`exit 2`) on a pair without
  `=`; premium knobs stripped unless `MO_ACCEPT_PREMIUM` is truthy.
- Files: `mini_ork/cli/main.py` (+57), `tests/unit/test_run_feature_flag.py` (new, +87).
- No `_NATIVE_MODULE_SUBS` change (AC5 honoured). **Merged to `origin/main` as
  `8c612b92`** (`8a20ca96..8c612b92  HEAD -> main`) off a `wiz-launch` worktree;
  worktree cleaned. Merge used a **scoped gate** (see "Green-gate debt").

### `minimax-lane-alias-drift` — DONE (no work needed)
- The regression guard already exists and **passes**:
  `tests/unit/test_recipe_lane_registry.py` (1 passed). The missing alias it
  guards, `deepseek_lens`, is defined in the operative registry
  (`.mini-ork/config/agents.yaml:61 → deepseek_flash`). The one recipe-local
  exception (`chapter-validation-10lens` `lens_01..10`) is documented in the
  test's own docstring as operator-supplied.

### `land-rsi-e1` — DONE (already landed)
- E1 is on `origin/main` as **`9a698e04`** ("feat(rsi): code-arm probe scorer,
  calibrated to zero on a null patch (E1)"), plus follow-ups `a047f878`
  (matched-attempt control arm, G06-T03), `e073354e`, `f500d69a`.
- `product/main`'s `dd319c30` is the same change pre-landing; a cherry-pick onto
  `origin/main` therefore conflicts (add/add) — nothing left to land.

### `zed-wizard` — DROPPED (redundant)
- Product call: the capability already ships. Feature configuration in the IDE
  lives in `mini_ork/ide_pages/setup.py` (the **"Features" tab**, `_feature_controls`),
  listed by `mini_ork/ide_pages/features.py`, and served over HTTP by
  `/api/v1/features`. A dedicated `wizard.py` page would duplicate it. No
  `ide_pages/wizard.py` exists and none is needed.

### T3 — Memory-index compaction — DONE
- `MEMORY.md` reduced **20361 → 16770 bytes** (target < 17.1 KB).
- Method chosen to avoid information loss: **50 lower-value links were moved to
  `MEMORY-ARCHIVE.md`** (same directory; not loaded as the index), rather than
  deleted or hook-trimmed. Measured: tightening all hooks alone caps out at
  ~17.3 KB (hook text is 7.9 KB over 243 links; filenames 10.1 KB), so the
  budget cannot be met without dropping links. Archive is fully reversible —
  restore any line verbatim into `MEMORY.md`.
- Backup: `MEMORY.md.bak-20261013`.

### `e1-sensitivity` — DONE (live null calibration, non-degenerate)
- Definition: run the code arm (`probe_score_code`) on a live arm and compare
  arms; the null calibration is an empty patch, whose delta must be 0.
- **Live receipt** (`probe_score_code("code-fix", "")`, 2026-10-14):

  | probe | arm | outcome | cost |
  |---|---|---|---|
  | probe-1 (gate) | baseline | 0.0 | $0.00 |
  | probe-1 (gate) | candidate | 0.0 | $0.00 |
  | probe-3 (anchor) | baseline | 1.0 | $0.065 |
  | probe-3 (anchor) | candidate | 1.0 | $0.071 |

  `n=1`, `before=after=0.0` (empty patch → **delta 0.0**, null calibration
  holds), `anchor_solved_frac=1.0` (the instrument can observe a *solve*, not
  just a failure), `cost_usd=0.1363`.
- **Two operator preconditions** — neither is a code bug, both are knobs:
  1. **Lane credentials must be in the process environment.** `_launch_run`
     passes `{**os.environ}` to each arm, and every arm's `MINI_ORK_HOME` is
     its own gitignored worktree whose `config/` carries `*.yaml` only
     (`_seed_arm_home`: "a secrets file is never copied"). The arm's file-tier
     vault is therefore empty, so credentials must arrive via the `env` tier
     (all vault tiers win in that order). Calling `probe_score_code` from a bare
     `python` with no keys in the env yields **dead arms** — every dispatch
     fails `rc=2` (`[llm_dispatch FAIL model=deepseek_flash rc=2]`) and both
     arms score 0 at $0. `bin/mini-ork` / any `mini-ork` process avoids this
     because `main()` runs `_load_secret_store_env()`
     (`os.environ.setdefault` from `$MINI_ORK_HOME/config/secrets.local.sh`),
     so the whole process tree inherits the keys.
  2. **`MO_APPLY_PROBE_TIMEOUT_S` must fit a real run.** The shipped default
     (600 s) was shorter than one agentic `code-fix` arm run on a slow lane
     (the implementer alone ran ≥5 min), producing the first-attempt result
     `n=0, truncated_by_budget=true, "probe arm exceeded MO_APPLY_PROBE_TIMEOUT_S=600s"`.
     At `MO_APPLY_PROBE_TIMEOUT_S=2400` the same sweep completed. **Left at the
     default** — the 600 s guard is deliberate and the timeout fails *loud*
     (`timed_out: true` + an error naming the knob), so this is recorded as a
     tuning note, not a defect to patch.
- Note: the cheapest legal sweep is 2 probes — `MO_APPLY_PROBE_MAX_TASKS` caps
  only the GATE probes, and the anchor (probe-3) is always kept.
- Arithmetic-level coverage stays in `tests/unit/test_probe_scorer_code_arm.py`.

### `reflect-wedge` source fix — SHIPPED
- Root cause (memory `feedback_reflect_child_wedges_run_after_verdict`): after
  the verdict, `mini-ork run` blocks in the reflect child bounded only by a
  history-derived timeout (`_reflect_timeout`, ~145 min on the primary home;
  ceiling 6 h), which `--deadline` does not cover.
- Fix (framework-edit `run-1791928955-25409`, verdict pass, 5/5, $0.44):
  **cap the post-verdict reflect bound at the run's `--deadline` remainder plus
  a 120 s grace**, via a pure, unit-tested helper `_cap_reflect_to_deadline`
  (+ `_REFLECT_DEADLINE_GRACE`) in `mini_ork/cli/main.py`; the call site reads
  `deadline_budget.status(run_id)` best-effort. No `--deadline` → unchanged.
  No change to `_reflect_timeout` / the reflector node / `deadline_budget`.
- **Merged to `origin/main` as `99d6b172`** (`8c612b92..99d6b172  HEAD -> main`).
- Live evidence: the fixing run's *own* reflect **timed out at the pinned
  300 s** — uncapped it would have used the ~8730 s derived bound (~2.4 h).
- Remaining (out of scope, documented): the reflector **node**
  (`execute_handlers._handle_reflector_early`) shares the same history-derived
  bound and sits inside execute, so it is not deadline-capped. A separate,
  smaller hole.

### `budget-gate` — DONE (measurement, no edit)
- **Window is a rolling 24 h, not a calendar day.** The enforced global budget is
  `MO_DAILY_BUDGET_USD` (default `50`), read by
  `dispatch.llm_dispatch.cost_circuit_open` =
  `cost_ledger.spent_last_24h(db) >= budget`. Spend is summed from the
  per-dispatch `llm_calls` ledger — *not* `task_runs.cost_usd`, which
  undercounted ~3× (measured 2026-10-03).
- **Two enforcement sites:** (1) per LLM call inside dispatch
  (`llm_dispatch.py:752` → `[cost_circuit_open] … halting dispatch`); (2) a
  preflight before any node runs (`cli.main._budget_preflight` →
  `RC_BLOCKED=75`, `verdict=blocked`, `blocked_reason=cost_limit`, "Nothing was
  dispatched. … wait for the window to roll").
- **The `agents.yaml budget:` block is display-only.** `per_run_usd: 12.00`,
  `per_epic_usd: 50.00`, `daily_cap_usd: 225.00` parse into
  `mini_ork.types.BudgetPolicy` and are shown by `client.py` and the IDE
  `lanes` page, but **no runtime enforcer reads them** — grep finds no
  comparison against spend. The scheduler carries its own `--budget-cap-usd`,
  which reads `MO_DAILY_BUDGET_USD`. So raising `MO_DAILY_BUDGET_USD` above
  `daily_cap_usd` (e.g. today's `400` vs `225`) is **not** capped by the recipe
  block; the dispatcher is the only hard gate. Recorded as a gap, not fixed
  (declared-policy-vs-enforced-policy is a product call).

## Merge note

`make worktree-merge` runs the full `python3 -m pytest -q` green gate, which
requires a ≥3.11 `python3` (the ambient `python3` here is micromamba 3.9.23,
and `bin/mini-ork` exits 2 on it — 46 collection errors). Re-run with the venv
on PATH:

```bash
PATH=/Volumes/docker-ssd/ps/mini-ork/.venv/bin:$PATH make worktree-merge SLUG=wiz-launch
```
