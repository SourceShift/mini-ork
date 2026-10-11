# Price non-Anthropic models on claude-CLI lanes at their real list price

## Goal

Every `anthropic-compat` lane (minimax, glm, deepseek, kimi) runs through the `claude`
CLI, and `claude_cost` in `mini_ork/dispatch/providers.py` trusts the CLI's
`total_cost_usd`. The CLI prices models it does not know at Anthropic rates. Measured on
the 40-task held-out baseline (llm_calls rows, run_id `heldout-mo-%`):

- MiniMax-M3: reported $85.35, real list price $6.59 (13.0× overstated)
- GLM-5.3:    reported $38.35, real list price $10.24 (3.7× overstated)

Both lanes come out at the same ~$10.8 per million uncached tokens, so the router's cost
tie-break (`decision_service.py`, `lane_bonus` from `cost_usd`) cannot tell a cheap
provider from an expensive one, and the daily budget breaker trips on spend that never
happened. Make the meter report real list-price spend.

## Files in scope

- `mini_ork/dispatch/providers.py` — `claude_cost`.
- `.mini-ork/config/pricing.yaml` — add rows (this tracked file is the shipped table).
- `tests/unit/test_claude_cost_list_price.py` — NEW.

No other file changes.

## Mechanism (exact spec)

The claude result envelope (`_claude_envelope(stdout)`) carries per-model usage:

```json
"modelUsage": {"GLM-5.3": {"inputTokens": 40442, "outputTokens": 15103,
  "cacheReadInputTokens": 168960, "cacheCreationInputTokens": 0, "costUSD": 0.664}}
```

New `claude_cost(stdout, usage)` behaviour:

1. Read `modelUsage` from the envelope. If it is missing or not a dict → return
   `total_cost_usd` exactly as today.
2. Load the pricing table: path = `MO_PRICING_YAML` env, else
   `$MINI_ORK_HOME/config/pricing.yaml`, else `.mini-ork/config/pricing.yaml`
   (same rule as `codex_transport._pricing_rate`; read env through `os.environ`).
   Unreadable/missing/malformed file → treat as empty table. Cache the parsed table per
   path for the process (a module-level dict keyed by path + mtime is fine).
3. For each `(model_name, mu)` in `modelUsage`:
   - Normalize the name: strip a trailing `[...]` suffix (e.g. `deepseek-v4-pro[1m]` →
     `deepseek-v4-pro`), compare case-insensitively.
   - If the normalized name starts with `claude` → add `mu["costUSD"]` (Anthropic's own
     price is correct for Anthropic models).
   - Else search every provider block in the table for a model key equal to the
     normalized name (case-insensitive). If found and it has `input` and `output` rates →
     add `(inputTokens*input + cacheReadInputTokens*cache_read + cacheCreationInputTokens*cache_write + outputTokens*output) / 1e6`,
     where an absent `cache_read`/`cache_write` rate counts as 0 (the file's documented rule).
     Use `pricing_strategy.lookup` for the reads.
   - Else (unknown model, no rate) → add `mu["costUSD"]` (today's behaviour; never guess).
4. Return the sum as `float`. Any exception inside steps 2–3 → fall back to
   `total_cost_usd` (the meter must never crash a dispatch).

Update the docstring: Anthropic models keep the CLI's billed figure; other models are
priced from `pricing.yaml`.

### `pricing.yaml` rows to add (USD per 1M tokens, official list prices, 2026-10-01)

Keep every existing row. Add:

```yaml
  minimax:
    MiniMax-M3:          # platform.minimax.io pay-as-you-go, <=512k input
      input:       0.30
      output:      1.20
      cache_read:  0.06
  zhipu:
    GLM-5.3:             # z.ai API
      input:       1.40
      output:      4.40
      cache_read:  0.26
  deepseek:
    deepseek-v4-pro:     # api-docs.deepseek.com, PEAK rates (conservative)
      input:       1.32
      output:      3.96
      cache_read:  0.044
    deepseek-flash:
      input:       0.30
      output:      1.20
      cache_read:  0.006
    deepseek-v4-flash:   # legacy name, billed as deepseek-flash
      input:       0.30
      output:      1.20
      cache_read:  0.006
  moonshot:
    kimi-k2.7-code:
      input:       0.95
      output:      4.00
      cache_read:  0.30
```

(Merge into the existing `minimax`/`zhipu`/`deepseek`/`moonshot` blocks — do not create
duplicate provider keys.)

## Tests (`tests/unit/test_claude_cost_list_price.py`)

Write a temp pricing.yaml and point `MO_PRICING_YAML` at it (monkeypatch). Cases:

- GLM-5.3 envelope above → `(40442*1.40 + 168960*0.26 + 15103*4.40)/1e6` (pytest.approx).
- `deepseek-v4-pro[1m]` key → priced with the `deepseek-v4-pro` row.
- Two models in one envelope (a non-Anthropic model + `claude-haiku-…` with costUSD 0.01)
  → list price of the first + 0.01.
- Unknown model `foo-1` with costUSD 0.5 → 0.5.
- No `modelUsage` → `total_cost_usd`.
- Missing pricing file → sum of `costUSD` per model == today's behaviour.
- `stream-json` stdout (multiple lines, a `type=="result"` event last) works the same.
- The shipped `.mini-ork/config/pricing.yaml` parses and has rows for MiniMax-M3, GLM-5.3,
  deepseek-v4-pro, deepseek-flash, kimi-k2.7-code with input < 2.0 (guards against a
  copy-paste of Anthropic rates).

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_claude_cost_list_price.py tests/unit/test_pricing_strategy_parity.py` passes, plus every existing test that imports `claude_cost` (`grep -rl claude_cost tests/`).
- `ruff check mini_ork/dispatch/providers.py tests/unit/test_claude_cost_list_price.py` clean.
- Diff touches only the three files in scope.
