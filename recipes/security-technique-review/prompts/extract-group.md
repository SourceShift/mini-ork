Read only the declared `technique_group` artifact. It holds one technique group
from a 2026 security-literature scan: the group definition, its catalog
techniques (each with supporting `source_ids`), and the summaries of every
supporting paper, each generated from the paper's abstract and retrieved body
text (`evidence_mode: body-text`) or from its abstract alone
(`evidence_mode: abstract-only`).

Your job: distill the group into its canonical, implementable SECURITY
techniques for mini-ork — a task OS for coding agents that ships code it writes
(recipe DAGs of LLM + deterministic verifier nodes, a publish gate, a worktree
merge gate, sandboxed places to run, and a secrets vault). Its security posture
is FAIL-CLOSED and EVIDENCE-ANCHORED: a guard that cannot measure blocks, an
unverified result is an abstention, and nothing is promoted on a model's
opinion.

The shipped surfaces a technique can land on (name one of these, or name a real
sibling if the technique needs it):
- `code-fix verifier set` — `recipes/code-fix/verifiers/{test,typecheck,fallback_guard,metamorphic}.py`; today it checks tests, types and lint, and has NO security check.
- `gaming scan` — `mini_ork/verify/gaming_scan.py`, gate-hackability detection.
- `publish gate` / `promotion gate` — `mini_ork/cli/publisher.py`, `mini_ork/gates/`.
- `merge risk gate and approvals` — `config/risk-tiers.yaml`, `mini_ork/gates/merge_risk.py`, `mini_ork/gates/merge_approval.py`.
- `secret scan` — `make secrets` plus the pinned-gitleaks CI job over `.gitleaks.toml`.
- `vault and per-node secret scope` — `mini_ork/dispatch/vault.py`, `mini_ork/remote/secrets_scope.py`.
- `sandbox placement and per-node sandbox` — container/remote node providers, `docs/architecture/per-node-sandboxes.md`.
- `worktree claim registry` — `scripts/mini_ork_worktree.py` (write-scope ownership, CAID claims).
- `repo integrity guard` — frozen kernel paths (`mini_ork/gates/`, `config/kernel-frozen.yaml`).
- `kickoff and intake guard` — `mini_ork/verify/kickoff_guard.py`, `mini_ork/intake/`.
- `artifact contract` — `schemas/artifact_contract.schema.json`, `docs/architecture/artifact-graph.md` (declared ports, visibility limits).
- `evidence ledger and traces` — `mini_ork/verify/evidence_ledger.py`, run transcripts.
- `lane config` — `agents.yaml` / `providers.yaml` credential and provider surface.
- `MCP / tool surface` — the tool servers and IDE actions an agent may call.

Write one JSON object to the requested output path, no fences:

{
  "group_id": "G01",
  "name": "copy from the input",
  "scope": "copy from the input",
  "techniques": [
    {
      "id": "G01-T01",
      "technique": "Canonical name",
      "mechanism": "3-5 sentences: how it works concretely — what it detects or blocks, what evidence it consumes, the accept/reject/abstain rule, and what it is anchored on.",
      "reported_results": "Strongest numbers the retrieved abstract/body text states, each attributed with (arxiv:ID). Empty if none.",
      "evidence_strength": "strong | moderate | weak",
      "evidence": ["arxiv:2601.00001"],
      "miniork_surface": "Which mini-ork surface from the list above it would change.",
      "adoption_sketch": "2-3 sentences: the smallest concrete change that would test it in mini-ork, and the measurement that would show it worked."
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
- Keep negative findings: a technique that papers show FAILS, is evadable, or
  produces false confidence (LLM scanners that hallucinate vulnerabilities,
  agents that follow instructions planted in a repo file, red-team suites that
  miss the obvious bug, security benchmarks that leak answers) is a technique
  too — mark it in `mechanism` as a caution.
- Do not invent results, sources, or mini-ork features.
