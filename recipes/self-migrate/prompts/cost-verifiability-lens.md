# Cost/verifiability lens (non-critical, cheap first pass)

You run on a cheap lane (GLM), in parallel, as a NON-critical pre-pass for the
authoritative `static_feature_ledger`. The panel tolerates your failure; if your
lane throttles (GLM 429 "Fair Usage"), the run backs off to codex and proceeds
without you. So be fast and cheap, not exhaustive.

## Job
Skim the fork's module + `integration-map.json` and flag the OBVIOUS
cost/verifiability signals, so the opus ledger node can focus its judgment:

- Which functions clearly call an LLM (grep for `claude`, `llm_dispatch`,
  `cl_*`, `subprocess`→a model) → **agentic candidates**.
- Which are clearly pure sqlite/string/math logic → **static candidates**.
- Any function that is agentic today but looks mechanically deterministic
  (fixed transform, no genuine judgment) → **cost-down candidate** worth the
  ledger node's attention.

## Output
A short list to `${MINI_ORK_RUN_DIR}/cost-verifiability-lens.md`:

```
agentic:   <fn> — <why (file:line)>
static:    <fn> — <why>
cost-down: <fn> — agentic now, looks deterministic because <...>
```

Keep it under ~20 lines. You are a hint generator, not the authority — the
opus ledger node makes the final call and its `static-feature-ledger.json` is
what the `ledger_verifier` checks.
