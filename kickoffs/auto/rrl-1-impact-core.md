# RRL-1: `mini_ork/rsi/impact.py` — the statistics core of the G12 impact gate

## Goal

The research-driven RSI loop (`docs/plans/2026-10-29-research-driven-rsi-loop.md`)
needs one pure module that decides whether a candidate change really improved a
benchmark, and that orders and monitors the loop. This kickoff builds ONLY that
module and its tests. It touches no other code: nothing calls it yet (RRL-4 wires it).

The module is **pure**: no file, DB, network, subprocess or model access, no
`os.environ` reads, no module-level side effects. Only the standard library
(`dataclasses`, `hashlib`, `hmac`, `math`, `random`, `typing`, `collections.abc`).

Scores are per-item floats; an item **passes** when `score >= threshold`
(default `0.5`). Item ids are strings.

All paths are relative to the repository root.

## Mechanism (exact spec)

### Constants

- `SPEND_Z = 3.39`
- `SPENDING_MODES = ("horizon_free", "per_decision")`
- `STRATA_DEFAULT = ""`

### `@dataclass(frozen=True, slots=True) class PairedTable`

Fields, in order: `wins: int`, `losses: int`, `both_pass: int`, `both_fail: int`,
`missing: int`. Properties:

- `n` → `wins + losses + both_pass + both_fail`
- `base_passed` → `losses + both_pass` (items the base passed)
- `discordant` → `wins + losses`
- `to_dict()` → a dict of the five fields plus `n`, `base_passed`, `discordant`.

### `paired_counts(base, head, *, items=None, threshold=0.5) -> PairedTable`

- `base` and `head` are `Mapping[str, float]`.
- The universe is `items` (any iterable of str, de-duplicated) when given, else the
  sorted union of both mappings' keys.
- For each id in the universe: if it is missing from either mapping, count it in
  `missing` and skip it. Otherwise `b = base[id] >= threshold`,
  `h = head[id] >= threshold`:
  - `not b and h` → `wins`; `b and not h` → `losses`;
  - `b and h` → `both_pass`; neither → `both_fail`.

### `binom_tail(k: int, n: int, q: float) -> float`

Exact `P[Bin(n, q) >= k]` using `math.comb`:

- `n < 0` or `not 0.0 <= q <= 1.0` → `ValueError`.
- `k <= 0` → `1.0`; `k > n` → `0.0`.
- Otherwise `sum(comb(n, i) * q**i * (1-q)**(n-i) for i in range(k, n+1))`,
  clamped to `[0.0, 1.0]`.

### `look_alpha(alpha0: float, look: int, *, spending: str = "horizon_free") -> float`

- `not 0 < alpha0 < 1` → `ValueError`; `look < 1` → `ValueError`;
  `spending not in SPENDING_MODES` → `ValueError`.
- `per_decision` → `alpha0`.
- `horizon_free` → `alpha0 / (SPEND_Z * look * math.log2(look + 1))`.

(`look = 1` gives `alpha0 / 3.39`; the sum over all looks never exceeds `alpha0`.)

### `sage_decision(table, *, alpha, lam=1.0, max_regression_rate=0.0) -> dict`

- `lam <= 0` → `ValueError`; `not 0 < alpha < 1` → `ValueError`;
  `max_regression_rate < 0` → `ValueError`.
- `q = lam / (1 + lam)`; `delta = table.wins - lam * table.losses`.
- `p_value = binom_tail(table.wins, table.discordant, q)` when
  `table.discordant > 0`, else `1.0`.
- `regression_rate = table.losses / table.base_passed` when `table.base_passed > 0`,
  else `0.0` if `table.losses == 0` else `1.0`.
- `reason` is the FIRST that applies, in this order:
  1. `"no_discordant_pairs"` — `table.discordant == 0`;
  2. `"regression_budget"` — `regression_rate > max_regression_rate`;
  3. `"net_loss"` — `delta <= 0`;
  4. `"not_significant"` — `p_value >= alpha`;
  5. otherwise `"accepted"`.
- `accept = reason == "accepted"`.
- Returns exactly the keys: `accept, reason, wins, losses, delta, p_value, alpha,
  lam, q, regression_rate, n, base_passed`. Floats are rounded with
  `round(x, 6)` (`p_value`, `alpha`, `q`, `delta`, `regression_rate`).

### `stable_items(runs, *, threshold=0.5) -> tuple[frozenset[str], frozenset[str]]`

- `runs` is a sequence of `Mapping[str, float]` (repeated runs of the SAME tree).
- Empty `runs` → `(frozenset(), frozenset())`.
- Consider only ids present in **every** run. An id is **stable** when its pass/fail
  is identical in every run, else **flaky**. Return `(stable, flaky)`.

### Split assignment

`split_items(items, *, secret: bytes, epoch: int, dev_fraction=0.5, strata=None) -> dict[str, list[str]]`

- `not secret` → `ValueError`; `not 0 < dev_fraction < 1` → `ValueError`.
- `strata` is an optional `Mapping[str, str]` (item → stratum name); an item absent
  from it is in stratum `STRATA_DEFAULT`.
- Each item's key is
  `u(item) = int.from_bytes(hmac.new(secret, f"{epoch}:{item}".encode(), hashlib.sha256).digest()[:8], "big")`.
- Group the de-duplicated items by stratum. Within each stratum, sort by
  `(u(item), item)`; the first `round(len(group) * dev_fraction)` go to `"dev"`, the
  rest to `"sealed"`.
- Return `{"dev": sorted(dev), "sealed": sorted(sealed)}`.

Deterministic for a fixed `(secret, epoch)`; a different epoch re-draws.

### `guard_check(base, head, *, tolerances, lower_is_better=()) -> dict`

- `base`, `head`: `Mapping[str, float]` of guard metrics; `tolerances`:
  `Mapping[str, float]` (every key is a guarded metric, value ≥ 0, else
  `ValueError`); `lower_is_better`: iterable of metric names.
- For each metric in `sorted(tolerances)`:
  - missing from `base` or `head` → violation `{"metric": m, "reason": "missing"}`;
  - `worse = head - base` if `m` in `lower_is_better` else `base - head`;
    `worse > tolerances[m]` → violation `{"metric": m, "reason": "worse",
    "base": base[m], "head": head[m], "tolerance": tolerances[m]}`.
- Returns `{"ok": not violations, "violations": violations}`.

### `ratchet(history, *, drop=0.02, consecutive=3) -> dict`

- `history`: canary scores of main, oldest first. `consecutive < 1` → `ValueError`.
- Empty → `{"peak": None, "peak_index": None, "declines": 0, "revert": False, "n": 0}`.
- `peak = max(history)`; `peak_index` = the FIRST index holding `peak`.
- `declines` = the number of TRAILING entries (from the end, walking back, stopping
  at the first entry that is not a decline, and never past `peak_index`) with
  `value < peak - drop` (strict).
- `revert = declines >= consecutive`. Returns `peak, peak_index, declines, revert, n`.

### Family diversity

- `effective_families(labels) -> float`: `0.0` for no labels; otherwise
  `exp(-sum(p * ln p))` over the label frequencies, rounded to 6 places.
- `family_collapse(labels, accepted, *, window=6, n_eff_max=1.5) -> dict`:
  - `len(labels) != len(accepted)` → `ValueError`.
  - `recent_labels = labels[-window:]`, `recent_accepted = accepted[-window:]`.
  - `plateau = len(labels) >= window and not any(recent_accepted)`.
  - `n_eff = effective_families(recent_labels)`; `narrow = n_eff <= n_eff_max`.
  - Returns `{"collapse": plateau and narrow, "plateau": plateau, "narrow": narrow,
    "n_eff": n_eff, "window": window, "recent_families": sorted(set(recent_labels))}`.

### Clade Thompson ordering

`thompson_order(options, attempts, *, rng, tau=1.0, prior_weight=2.0) -> list[str]`

- `options`: sequence of mappings `{"id": str, "family": str, "prior": float}`;
  `prior` is clamped to `[0, 1]`. `attempts`: sequence of mappings
  `{"family": str, "technique": str, "success": bool}` (`technique` matches an
  option `id`). `tau <= 0` or `prior_weight < 0` → `ValueError`.
- For each option: `s` = successes over attempts with the same `family` (the clade)
  plus successes with `technique == id`; `f` likewise for failures. (A technique's
  own attempt therefore counts twice: once in its clade, once as itself.)
- `a = tau * (1 + s + prior_weight * prior)`,
  `b = tau * (1 + f + prior_weight * (1 - prior))`, `draw = rng.betavariate(a, b)`.
- Draw in the order the options are given. Return the ids sorted by `(-draw, id)`.
- Empty options → `[]`.

### `__all__`

Every public name above, sorted.

## Files in scope (touch ONLY these)

- `mini_ork/rsi/impact.py` (new)
- `tests/unit/test_rsi_impact_py.py` (new)

Do NOT modify any other file. In particular do not touch `mini_ork/rsi/__init__.py`,
`mini_ork/rsi/gates.py`, `mini_ork/rsi/cycle.py` or `mini_ork/rsi/learn.py`.

## Tests (`tests/unit/test_rsi_impact_py.py`: hermetic, no I/O)

- **paired_counts:** base `{a:0,b:1,c:1,d:0}`, head `{a:1,b:0,c:1,d:0,e:1}` →
  `wins=1, losses=1, both_pass=1, both_fail=1, missing=1`, `n=4`, `base_passed=2`.
  With `items=["a","zz"]` → `wins=1, missing=1`. Threshold: score `0.5` passes,
  `0.49` fails.
- **binom_tail:** `binom_tail(7, 7, 0.5) == 0.5**7`;
  `abs(binom_tail(7, 8, 0.5) - 9/256) < 1e-12`; `binom_tail(0, 5, .3) == 1.0`;
  `binom_tail(6, 5, .3) == 0.0`; `q=1.1` raises.
- **look_alpha:** `look_alpha(0.05, 1)` ≈ `0.05/3.39`;
  `look_alpha(0.05, 1, spending="per_decision") == 0.05`; `look=0` raises; the sum of
  `look_alpha(0.05, k)` for `k` in `1..10000` is `< 0.05`.
- **sage_decision (McNemar, λ=1, α=0.0147):**
  - 7 wins / 0 losses → `accept`, `reason == "accepted"`, `p_value == round(1/128, 6)`;
  - 6 / 0 → `"not_significant"` (p = 1/64 ≥ 0.0147);
  - 7 / 1 → `"regression_budget"` with the default `max_regression_rate=0.0`;
  - 7 / 1 with `max_regression_rate=1.0` → `"not_significant"` (p = 9/256);
  - 0 / 0 → `"no_discordant_pairs"`, `p_value == 1.0`;
  - 2 / 3 with `max_regression_rate=1.0` → `"net_loss"`;
  - `lam=0` raises; returned key set equals the spec's exactly.
- **stable_items:** runs `[{a:1,b:0,c:1},{a:1,b:1,c:1,d:0}]` → stable `{a,c}`,
  flaky `{b}` (`d` absent from run 1 is neither).
- **split_items:**
  - same secret+epoch → identical result twice; a different epoch changes it for a
    40-item set; a different secret changes it;
  - 40 items, strata 17 `hard_negative` + 23 `correct`, `dev_fraction=0.5` → dev
    holds `round(17*.5)=8` hard negatives and `round(23*.5)=12` correct ones;
    `dev ∪ sealed` == all items, disjoint;
  - `secret=b""` raises; `dev_fraction=1.0` raises.
- **guard_check:** `false_accept` lower-is-better tolerance 0: base 5 → head 5 ok,
  head 6 violation `"worse"`; a metric missing from head → `"missing"`;
  higher-is-better `accuracy` 0.8 → 0.79 with tolerance 0.02 is ok.
- **ratchet:** `[]` → revert False; `[0.5, 0.6, 0.57, 0.57, 0.57]` → peak 0.6,
  `declines == 3`, `revert is True`; `[0.6, 0.59, 0.57, 0.57]` with `drop=0.02` →
  `declines == 2` (0.59 is not a decline) → False; a new peak at the end →
  `declines == 0`; `consecutive=0` raises.
- **family_collapse:** labels `["A"]*6`, accepted all False → collapse True,
  `n_eff == 1.0`; labels `["A","B","C","A","B","C"]` all False → collapse False
  (`n_eff` ≈ 3.0); one True in the window → plateau False; 5 entries with
  `window=6` → plateau False; mismatched lengths raise.
- **thompson_order:**
  - deterministic for `random.Random(7)` (two calls with fresh `Random(7)` equal);
  - a family with 20 successes vs a family with 20 failures (equal priors): over 200
    seeds the successful family's option is first in ≥ 190;
  - with no attempts, prior 1.0 vs prior 0.0 (`prior_weight=10`): the prior-1.0
    option is first in ≥ 170 of 200 seeds;
  - `tau=0` raises; empty options → `[]`.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_rsi_impact_py.py`
- `ruff check --select F,E9 mini_ork/rsi/impact.py tests/unit/test_rsi_impact_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_rsi_impact_py.py --junitxml="${MINI_ORK_RUN_DIR}/rrl-1-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rrl-1-junit.xml`

## Success criteria

- Both verification commands pass.
- `mini_ork/rsi/impact.py` imports nothing outside the standard library and reads no
  environment variable.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
