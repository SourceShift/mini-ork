# Retrieval/Hydration Lens Prompt

You are the retrieval-hydration lens for the researcher-qdrant-contract recipe.

Task: Fix retrieval allowlists to cover all canonical retrievable `node_type` values and define degraded-preview semantics when PG hydration fails.

Inputs:
- Planner JSON plan.
- Contract architect lens output (canonical payload schema).
- Researcher repo retrieval and hydration code.

## Required output format

Emit a JSON envelope with this strict schema:

```json
{
  "lens": "retrieval_hydration",
  "canonical_retrievable_node_types": [
    "text_chunk",
    "highlight",
    "ai_annotation",
    "generated_content",
    "book_chapter_content",
    "arxiv_import",
    "markdown_import"
  ],
  "allowlist_audit": {
    "current_allowlist": ["string[] — node_types currently in retrieval allowlist"],
    "missing_node_types": ["string[] — canonical types absent from allowlist"],
    "extra_node_types": ["string[] — types in allowlist that are not canonical"]
  },
  "degraded_preview_semantics": {
    "enabled": true,
    "fields_from_payload": ["title", "text_preview", "source_kind", "node_type", "document_uuid", "page", "chapter"],
    "stale_warning": "string — text to include when returning degraded preview",
    "safety_guard": "degraded preview is returned ONLY when PG hydration fails AND the Qdrant hit has the required payload keys"
  },
  "hydration_failure_handling": {
    "silent_drop": false,
    "fallback_to_payload": true,
    "log_telemetry": true
  },
  "findings": ["string[] — retrieval/hydration contract gaps"]
}
```

Rules:
- The allowlist MUST include every canonical retrievable node_type.
- Degraded preview MUST require `text_preview` and `source_kind` in payload.
- PG hydration failure MUST NOT silently drop a Qdrant hit.
- Do NOT emit `<z-insight>` blocks.
