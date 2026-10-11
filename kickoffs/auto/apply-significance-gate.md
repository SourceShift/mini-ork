# Apply gate: exact significance test on per-task flips (opt-in enforcement, always recorded)

## Goal

`mini_ork/cli/apply.py::evaluate_gate` promotes a measured candidate when it solves more
held-out probe tasks than the baseline with no per-task regression. With the default
probe set (`MO_APPLY_PROBE_MAX_TASKS=2`) one task flipping fail→pass is enough. That is
indistinguishable from run-to-run noise: an exact one-sided McNemar (sign) test on
1 discordant pair gives p = 0.5. Record the evidence strength on every measured decision,
and let an operator require it.

## Files in scope

- `mini_ork/cli/apply.py` — `evaluate_gate` only (plus one small pure helper next to it).
- `tests/unit/test_apply_significance.py` — NEW.

No other file changes.

## Mechanism (exact spec)

1. New pure helper `mcnemar_exact_p(gains: int, losses: int) -> float`:
   one-sided exact binomial p-value of seeing `>= gains` successes in `n = gains + losses`
   fair coin flips: `sum(comb(n, k) for k in range(gains, n + 1)) / 2**n`. `n == 0` → `1.0`.
   Use `math.comb`; no scipy.
2. In `evaluate_gate`, when per-task vectors are present (`measured` is True and both
   vectors have equal length): `gains` = count of `after[i] and not before[i]`,
   `losses` = count of `before[i] and not after[i]`; `p = mcnemar_exact_p(gains, losses)`.
   Add to the result dict: `"sig_gains": gains, "sig_losses": losses, "sig_p": round(p, 6)`.
   When not measured (scalar path) add `"sig_p": None` and no gains/losses keys.
3. `alpha = os.environ.get("MO_APPLY_SIG_ALPHA")` — unset or empty → enforcement OFF
   (every decision byte-identical to today apart from the added keys). When set, parse as
   float in (0, 1]; an invalid value → treat as unset and add
   `"sig_alpha_error": "<raw value>"` to the result.
4. With enforcement ON, a decision that would be `"promoted"` on the measured path
   becomes `"quarantined"` when `p > alpha`, with rationale
   `insufficient evidence: <gains> gain(s) vs <losses> loss(es) over <n_tasks> held-out task(s), one-sided exact McNemar p=<p:.4f> > alpha=<alpha>`.
   Add `"sig_alpha": alpha` to the result. Promotions on the scalar path (no vectors) are
   not touched — there is nothing to test.
5. When a promotion does clear (or enforcement is off), append
   `; McNemar p=<p:.4f>` to the existing promoted rationale on the measured path.
6. Order of checks is unchanged: per-task regression gate first, then the strict-superset
   control gate, then improvement, then (new) the significance check on what would
   otherwise promote.

## Tests (`tests/unit/test_apply_significance.py`)

- `mcnemar_exact_p(0, 0) == 1.0`; `(1, 0) == 0.5`; `(4, 0) == 0.0625`; `(5, 0) == 0.03125`;
  `(3, 1) == 0.3125`; monotone: more gains at fixed n → smaller p.
- enforcement off: `before=[0,0]`, `after=[1,0]` → `decision == "promoted"`, `sig_p == 0.5`,
  `sig_gains == 1`, `sig_losses == 0`; rationale ends with `McNemar p=0.5000`.
- enforcement on (`MO_APPLY_SIG_ALPHA=0.1`, monkeypatch env), same vectors →
  `"quarantined"`, rationale starts with `insufficient evidence`, `sig_alpha == 0.1`.
- enforcement on, `before=[0]*6`, `after=[1,1,1,1,1,0]` → p=0.03125 ≤ 0.1 → `"promoted"`.
- enforcement on, a per-task regression still wins first (quarantined with the
  no-regression rationale, not the significance one).
- scalar path (no `pertask_json`) with enforcement on → unchanged decision, `sig_p is None`.
- `MO_APPLY_SIG_ALPHA=banana` → behaves as off, `sig_alpha_error == "banana"`.
- the existing `tests/unit/test_probe_scorer_control_arm.py` and every test that calls
  `evaluate_gate` (`grep -rl evaluate_gate tests/`) pass unchanged.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_apply_significance.py tests/unit/test_probe_scorer_control_arm.py tests/unit/test_cli_apply_py.py` passes.
- `ruff check mini_ork/cli/apply.py tests/unit/test_apply_significance.py` clean.
- Diff touches only the two files in scope.
