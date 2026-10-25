# OpenTelemetry + Langfuse integration

> Status: **implemented**. `mini_ork/otel_export.py` builds the
> task_run → agent → llm_call span tree from state.db and exports it as
> OTLP/JSON. After every run, `_post_run_learning` exports the run's span
> tree when both Langfuse keys resolve through the vault (simple tier:
> environment / `config/secrets.local.sh`; secure tier: a vault backend,
> see `config/vault.example.yaml`). `MO_OTEL_EXPORT=0` opts out.
> Manual: `python -m mini_ork.otel_export <run_id> [--dry-run]`.
> Tests in `tests/test_otel_export.py` and
> `tests/unit/test_eco6_k10_otel_live.py`.

## Why

Today mini-ork's LLM telemetry lives in `state.db.llm_calls` — one row per
real API turn with provider/model/tokens/cost/session_id/traceparent.
That's enough for local forensics in the obs UI, but it doesn't compose
with the broader ML-ops ecosystem:

- No span hierarchy (root → agent → llm_call) — only flat rows
- Langfuse / Honeycomb / Datadog can't ingest sqlite
- W3C traceparent is *shape-valid* (`00-{32-hex-trace_id}-{16-hex-span_id}-01`)
  but our trace_ids are `tr-classify-1781010037-28452` — not 32 hex. External
  tracers will reject them.

## Target

Each `mini-ork run` emits an **OTel span tree** to an OTLP HTTP endpoint
(Langfuse Cloud at `/api/public/otel/v1/traces` by default):

```
task_run "self-improve-iter-N-TS"  (root span)
├── agent "bottleneck_lens"        (span; parent = root)
│   └── llm_call turn=0            (span; parent = agent; usage=...)
├── agent "perf_lens"
│   ├── llm_call turn=0
│   └── llm_call turn=1
├── agent "opus_synthesizer"
│   └── llm_call turn=0
└── verifier "self-tests-pass"     (span; non-LLM)
```

Span attributes follow Langfuse / OpenInference semantic conventions:

- `llm.model_name`, `llm.token_count.prompt`, `llm.token_count.completion`,
  `llm.usage.total_cost_usd`
- `langfuse.session.id` (from claude SDK session_id)
- `mini-ork.task_run_id`, `mini-ork.node_id`, `mini-ork.recipe`, `mini-ork.family`
- `mini-ork.dispatch_mode`, `mini-ork.turn_index`

## Mechanism

### 1. Proper W3C trace_ids

Generate a 32-hex trace_id at `mini-ork run` startup; store both the
existing string trace_id (for internal correlation) AND a new
`task_runs.otel_trace_id` (32-hex) for external export.

### 2. Post-run export

`_post_run_learning` (the final step) calls
`otel_export.export_after_run(run_id, db)`. It resolves the key pair through
the same `vault_environment()` chain every other credential uses, builds the
span tree from state.db, and POSTs it. Best-effort: any failure is reported
on stderr with the exception class only and never fails the run.
`MO_OTEL_EXPORT=0` opts out entirely.

### 3. Wiring points

| Where | Effect |
|---|---|
| `mini_ork/cli/execute.py::_post_run_learning` | export the finished run |
| `mini_ork/otel_export.py::export_after_run` | resolve keys, build, POST |
| `mini_ork/otel_export.py::export` | manual CLI path |

### 4. Configuration

```bash
export LANGFUSE_OTLP_ENDPOINT="https://cloud.langfuse.com/api/public/otel/v1/traces"
export LANGFUSE_PUBLIC_KEY="pk-lf-..."
export LANGFUSE_SECRET_KEY="sk-lf-..."
```

The simple tier reads those from the environment or an owner-only
`config/secrets.local.sh`; the secure tier reads them from a sops / command /
openbao vault backend (`config/vault.example.yaml`). A half-configured pair
(one key set) fails closed with a visible stderr warning; neither key means
export is off.

## What's already in place

- `llm_calls.session_id` column (migration 0018) ← **done**
- `metadata_json.turn_index`, `session_id`, `cache_*` per-turn ← **done**
- `traceparent` LIKE matching in obs UI ← **done** (string trace_id today)
- W3C traceparent shape `00-{traceid}-{spanid}-01` ← **done** (hex TBD)

## What's built

1. **`mini_ork/otel_export.py`** — stdlib-only OTLP/JSON exporter. It reads
   one run from state.db (`<task_run_id>`), builds the span tree, and POSTs
   it; `--dry-run` prints the payload instead. 32-hex trace_ids are derived
   deterministically from task_run_id (`sha256` prefix) — no migration
   needed; re-exporting the same run collides on traceId/spanId so the
   backend dedupes. Keys resolve through the vault chain.
2. **Post-run hook** — `mini_ork/cli/execute.py::_post_run_learning` exports
   every finished run (best-effort, `MO_OTEL_EXPORT=0` opts out).
3. **IDE setup page** — the OTel / Langfuse status row reports whether each
   finished run is exported, resolved through the same credentials path.

## What's left

- **UI**: Langfuse deep-link from task_run header

## Failure modes & guards

- OTLP endpoint unreachable → the export is reported and skipped; the run is unaffected
- Credentials missing → export is a no-op (telemetry never leaves the host without opt-in)
- Only one key set → export fails closed with a visible stderr warning
- Schema drift → namespace attributes (`mini-ork.*`, `langfuse.*`, `llm.*`)
