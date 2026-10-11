# Cost meter: fall back to the engine's shipped pricing table

## Goal

`claude_cost` (`mini_ork/dispatch/providers.py`) prices non-Anthropic models from
`pricing.yaml`, resolved as `MO_PRICING_YAML` → `$MINI_ORK_HOME/config/pricing.yaml` →
`.mini-ork/config/pricing.yaml` (relative to the cwd), taking the FIRST candidate even
when that file does not exist. `mini-ork init` does not scaffold a `pricing.yaml`, so in
any user repository (and inside `mini-ork certify`, which runs in the user's repo) the
table is missing, the meter silently falls back to the claude CLI's Anthropic-rate
figure, and the list-price fix is inert. Measured: the certify demo still reports
$0.53 / $0.66 per certificate after the fix landed.

## Files in scope

- `mini_ork/dispatch/providers.py` — `_load_pricing_table` only.
- `tests/unit/test_claude_cost_list_price.py` — extend.

No other file changes.

## Mechanism (exact spec)

`_load_pricing_table()` resolves its path as the first candidate that **exists as a
file**, in this order:

1. `MO_PRICING_YAML` (if set — used even if missing, so an operator typo is not silently
   replaced: missing → empty table, as today);
2. `$MINI_ORK_HOME/config/pricing.yaml` (if `MINI_ORK_HOME` is set);
3. `.mini-ork/config/pricing.yaml` relative to the cwd;
4. the engine's shipped table: `Path(__file__).resolve().parents[2] / ".mini-ork" / "config" / "pricing.yaml"`
   (providers.py lives at `mini_ork/dispatch/providers.py`, so `parents[2]` is the engine root).

None exists → empty table (today's fallback). Keep the per-path mtime cache. Update the
`claude_cost` docstring's resolution sentence to list the four steps.

## Tests (extend `tests/unit/test_claude_cost_list_price.py`)

- no `MO_PRICING_YAML`, `MINI_ORK_HOME` pointing at an empty temp dir, cwd = another empty
  temp dir (monkeypatch.chdir) → a GLM-5.3 envelope is priced from the ENGINE table
  (`(40442*1.40 + 168960*0.26 + 15103*4.40)/1e6`), not the CLI's costUSD.
- `MINI_ORK_HOME` with its own pricing.yaml carrying a different GLM-5.3 rate → the home
  table wins over the engine table.
- `MO_PRICING_YAML` set to a missing path → empty table → CLI costUSD (unchanged rule).
- all existing tests in the file still pass.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_claude_cost_list_price.py` passes.
- `ruff check mini_ork/dispatch/providers.py tests/unit/test_claude_cost_list_price.py` clean.
- Diff touches only the two files in scope.
