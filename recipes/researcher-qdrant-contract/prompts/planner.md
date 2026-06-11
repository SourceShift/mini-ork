# Planner Prompt — researcher-qdrant-contract

You are planning a PG/Qdrant contract remediation in the researcher repo.

Inputs:
- Target repo path (via env: RESEARCHER_REPO or inferred from kickoff).
- Operator-supplied markdown audit summary of known PG/Qdrant contract gaps.
- Optional exact document UUID / user UUID for live validation.
- Optional command allowlist for targeted tests.

Kickoff content:
```text
{{KICKOFF_CONTENT}}
```

Produce a concise JSON plan with this strict schema:

```json
{
  "outcome": "string — one sentence describing the remediation goal",
  "candidate_globs": ["string[] — files or globs to inspect in researcher repo"],
  "out_of_scope": ["string[] — files or operations explicitly excluded"],
  "verifier_commands": ["string[] — expected deterministic verifier invocations"],
  "artifact_manifest": {
    "qdrant-contract-remediation-plan.md": "phased implementation plan, owned file/module list, risk table, rollback/backfill strategy",
    "qdrant-contract-findings.json": "machine-readable list of contract gaps by flow",
    "qdrant-contract-patch-summary.md": "what was changed or exact patch queue in plan-only mode",
    "qdrant-contract-verification.md": "commands run, DB/Qdrant dry-run evidence, remaining blockers"
  },
  "lens_scope": {
    "contract_architect": "canonical payload schema + embedding-text contract",
    "creation_flow_auditor": "map every content creation path to post-persist sync point",
    "retrieval_hydration_lens": "fix retrieval allowlists + degraded-preview fallback",
    "migration_backfill_lens": "reconciliation + scoped repair with mandatory --dry-run"
  }
}
```

Constraints:
- Do NOT include blind full Qdrant reindexing in the plan.
- Do NOT touch the mini-ork framework core.
- Do NOT run the researcher frontend or backend servers.
- Preserve the invariant: PostgreSQL `blocks` is canonical; Qdrant `knowledge_nodes_unified` is derived.
