Read only the declared `technique_group` artifact. It holds one theme from a
2026 agent-security literature scan: the theme definition, its catalog
techniques (each with supporting `source_ids`), and the summaries of every
supporting paper, each generated from the paper's abstract and retrieved body
text (`evidence_mode: body-text`) or from its abstract alone
(`evidence_mode: abstract-only`).

Your job: distill the theme into its canonical techniques and state, for each,
where it is on the curve — what exists today, what it implies next, and what a
reader could do about it now. You are writing for two readers: an engineer who
must decide what to build or buy, and a decision-maker who must judge risk.

Write one JSON object to the requested output path, no fences:

{
  "group_id": "G01",
  "name": "copy from the input",
  "scope": "copy from the input",
  "techniques": [
    {
      "id": "G01-T01",
      "technique": "Canonical name",
      "mechanism": "3-5 sentences: how it works concretely — the attack or defense, the boundary it crosses or holds, the evidence it consumes, and the accept/reject/abstain or succeed/fail rule.",
      "reported_results": "Strongest numbers the retrieved abstract/body text states, each attributed with (arxiv:ID). Empty if none.",
      "evidence_strength": "strong | moderate | weak",
      "evidence": ["arxiv:2601.00001"],
      "maturity": "research | early-adoption | productised | commoditised — what exists off the shelf in 2026, not what is possible.",
      "trajectory": "1-3 sentences: where this goes in the next 12-36 months, stated as a direction with its driver, and the indicator that would show it is happening. Extrapolate only from what the cited papers state or measure.",
      "defensive_move": "2-3 sentences: the smallest concrete action that measurably helps a team deploying agents — name the control, where it sits, and what it must reject or flag.",
      "test_recipe": "1-2 sentences: how you would prove the control works or that the attack lands — the replayable check, not the intent."
    }
  ]
}

Rules:
- 5 to 15 techniques. Merge catalog entries that are the same mechanism; split
  entries that hide two mechanisms. Ids are `<group_id>-T01`, `-T02`, … in
  order of evidence weight.
- `evidence` lists only source ids present in the input. strong = several
  independent papers with reported gains; moderate = one paper with numbers or
  several without; weak = claims without results.
- `maturity` is about 2026 deployment reality as the input supports; when the
  input does not say, use `research` and say why in `trajectory`.
- `trajectory` is an extrapolation, not a finding. Keep it short, tied to the
  driver the papers name, and never present it as measured. If the papers
  contradict each other on direction, say so.
- Keep negative findings: a mechanism the papers show FAILS, is evadable, or
  produces false confidence (agents that follow instructions planted in a repo
  file, guardrails that only catch the benchmark, sandboxes bypassed by an
  obvious path) is a technique too — mark it as a caution in `mechanism`.
- Do not invent results, sources, or products.
