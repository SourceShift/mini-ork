# OpenTelemetry + Langfuse integration

> Status: **partially implemented**. `mini_ork/otel_export.py` builds the
> task_run → agent → llm_call span tree from state.db and exports it as
> OTLP/JSON (env-gated on `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`,
> `--dry-run` prints the payload; tests in `tests/test_otel_export.py`).
> Live in-process span emission (`lib/mo_otel.sh` wiring points below)
> remains design-only.

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

### 2. Span emitter (`lib/mo_otel.sh`)

Pure-python helper invoked from bash. Buffers spans as JSONL at
`$MINI_ORK_RUN_DIR/.otel-spans.jsonl`, then flushes to OTLP at run end.

### 3. Wiring points

| Where | Span emitted |
|---|---|
| `bin/mini-ork-execute` startup | root span "task_run" |
| `_dispatch_node` entry | child span "agent" |
| `_mo_llm_write_llm_calls_row` | grandchild span "llm_call" |
| dispatcher exit trap | flush + end root span |

### 4. Configuration

```bash
export LANGFUSE_OTLP_ENDPOINT="https://cloud.langfuse.com/api/public/otel/v1/traces"
export LANGFUSE_PUBLIC_KEY="pk-lf-..."
export LANGFUSE_SECRET_KEY="sk-lf-..."
```

## What's already in place

- `llm_calls.session_id` column (migration 0018) ← **done**
- `metadata_json.turn_index`, `session_id`, `cache_*` per-turn ← **done**
- `traceparent` LIKE matching in obs UI ← **done** (string trace_id today)
- W3C traceparent shape `00-{traceid}-{spanid}-01` ← **done** (hex TBD)

## What needs building (~1 day)

1. **0019 migration**: add `task_runs.otel_trace_id TEXT` (32-hex)
2. **`lib/mo_otel.sh`** helper (~150 LOC bash + small python module)
3. **`mini_ork/otel.py`** OTLP exporter (~100 LOC using
   `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-http`)
4. **Wire** into `bin/mini-ork-execute` at 3 points
5. **UI**: Langfuse deep-link from task_run header

## Failure modes & guards

- OTLP endpoint unreachable → JSONL on disk survives; `mini-ork otel-resync` re-POSTs
- Credentials missing → emitter is a no-op (telemetry never leaves the host without opt-in)
- Schema drift → namespace attributes (`mini-ork.*`, `langfuse.*`, `llm.*`)
