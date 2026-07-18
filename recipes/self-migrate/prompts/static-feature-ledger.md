# Static-feature ledger — the migration's strategic payload

This is not bookkeeping. It is the cost/verifiability audit that makes the
migration worth more than a port. mini-ork's moat is **cost-down at constant
verified correctness**, and those two goals share one root cause:

| class | cost | verifiability |
|---|---|---|
| **static** (deterministic logic) | ~0 tokens | un-gameable — byte-parity vs an oracle |
| **agentic** (LLM call) | tokens per call | weak — an LLM-judge is gameable |

## Produce `${MINI_ORK_RUN_DIR}/static-feature-ledger.json`

For **every** function/behavior in the fork being migrated (from
`integration-map.json` + the module source), emit one row. Classify
deliberately — this is a decision, not a description:

```json
{
  "fork": "verify",
  "features": [
    {
      "feature": "aggregate_win_rates",
      "class": "static | agentic | integration",
      "verifiability": "byte-parity | test | llm-judge | none",
      "cost": "zero | tokens-per-call",
      "decision": "keep-static | make-static | must-stay-agentic | plumbing",
      "opportunity": "<if agentic: can it become deterministic, or be gated by a deterministic check? if so, how. else null>"
    }
  ],
  "summary": {"static": 0, "agentic": 0, "integration": 0, "cost_down_candidates": 0}
}
```

## Rules
- **static** confirmed → a unit of the moat proven. Keep it static.
- **agentic** flagged → a cost AND verifiability liability. Fill `opportunity`:
  is there a deterministic replacement, or a deterministic gate that makes the
  agentic step verifiable? Pure static→agentic is almost never right — say so if
  the code drifted that way.
- **integration** → a seam; note whether it carries an LLM or is pure plumbing.
- Every behavior the `migrator` will touch MUST appear here — the
  `ledger_verifier` fails the run if a changed function has no ledger row.
- Ground each row in a real file:line. Do not invent features.
