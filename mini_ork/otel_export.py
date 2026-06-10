"""Minimal Langfuse OTLP exporter (docs/architecture/otel-langfuse.md).

Builds an OTel span tree for one task_run from state.db and POSTs it as
OTLP/JSON to a Langfuse OTLP endpoint. Stdlib only — no opentelemetry-sdk.

Span tree:  task_run (root) -> agent (one per execution_trace) -> llm_call.

Env gating (telemetry never leaves the host without opt-in):
  LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY   required for live export
  LANGFUSE_OTLP_ENDPOINT                      default: Langfuse Cloud

CLI:
  python3 -m mini_ork.otel_export <task_run_id> [--db PATH] [--dry-run]

--dry-run prints the OTLP payload to stdout and never opens a socket.
Without credentials, live mode is a silent no-op (exit 0).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone

DEFAULT_ENDPOINT = "https://cloud.langfuse.com/api/public/otel/v1/traces"

OK, ERROR = 1, 2  # OTLP status codes


def _hex_id(seed: str, nbytes: int) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[: nbytes * 2]


def _iso_to_ns(iso: str | None) -> int:
    if not iso:
        return 0
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1e9)
    except ValueError:
        return 0


def _attr(key: str, value):
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": str(value)}
    return {"key": key, "value": v}


def _span(trace_id, span_id, name, start_ns, end_ns, attrs, status_code, parent=None):
    s = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": 1,  # INTERNAL
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": attrs,
        "status": {"code": status_code},
    }
    if parent:
        s["parentSpanId"] = parent
    return s


def build_payload(db_path: str, task_run_id: str) -> dict:
    """Build an OTLP/JSON ExportTraceServiceRequest for one task_run."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        run = con.execute(
            "SELECT * FROM task_runs WHERE id = ?", (task_run_id,)
        ).fetchone()
        if run is None:
            raise SystemExit(f"otel_export: task_run not found: {task_run_id}")

        traces = con.execute(
            "SELECT * FROM execution_traces WHERE run_id = ? ORDER BY created_at",
            (task_run_id,),
        ).fetchall()
        calls = con.execute(
            "SELECT * FROM llm_calls WHERE run_id = ? ORDER BY id",
            (task_run_id,),
        ).fetchall()
    finally:
        con.close()

    trace_id = _hex_id(task_run_id, 16)  # proper 32-hex for external tracers
    root_id = _hex_id(f"{task_run_id}/root", 8)

    start_ns = int((run["created_at"] or 0) * 1e9)
    end_ns = int((run["ended_at"] or run["updated_at"] or 0) * 1e9)
    root_attrs = [
        _attr("mini-ork.task_run_id", task_run_id),
        _attr("mini-ork.task_class", run["task_class"]),
        _attr("mini-ork.recipe", run["recipe"] or ""),
        _attr("llm.usage.total_cost_usd", float(run["cost_usd"] or 0)),
    ]
    spans = [
        _span(
            trace_id, root_id, f"task_run {task_run_id}", start_ns, end_ns,
            root_attrs,
            ERROR if run["status"] in ("failed", "rolled_back") else OK,
        )
    ]

    # agent spans, keyed by node name (tr-<node>-<ts>-<pid>) so llm_calls
    # can parent onto them via feature_name suffix
    agent_by_node: dict[str, str] = {}
    for t in traces:
        tid = t["trace_id"]
        span_id = _hex_id(f"{task_run_id}/{tid}", 8)
        node = tid.split("-")[1] if tid.count("-") >= 3 else tid
        agent_by_node.setdefault(node, span_id)
        t_end = _iso_to_ns(t["created_at"])
        t_start = t_end - int(t["duration_ms"] or 0) * 1_000_000
        spans.append(
            _span(
                trace_id, span_id, f"agent {node}", t_start, t_end,
                [
                    _attr("mini-ork.node_id", node),
                    _attr("mini-ork.trace_id", tid),
                    _attr("mini-ork.task_class", t["task_class"]),
                    _attr("llm.usage.total_cost_usd", float(t["cost_usd"] or 0)),
                ],
                ERROR if t["status"] in ("failure",) else OK,
                parent=root_id,
            )
        )

    for c in calls:
        node = (c["feature_name"] or "").split(":")[-1]
        parent = agent_by_node.get(node, root_id)
        c_end = _iso_to_ns(c["ts"])
        c_start = c_end - int(c["duration_ms"] or 0) * 1_000_000
        attrs = [
            _attr("llm.model_name", c["model_id"]),
            _attr("llm.token_count.prompt", int(c["input_tokens"] or 0)),
            _attr("llm.token_count.completion", int(c["output_tokens"] or 0)),
            _attr("llm.usage.total_cost_usd", float(c["cost_usd"] or 0)),
            _attr("mini-ork.task_run_id", task_run_id),
            _attr("mini-ork.node_id", node),
        ]
        if c["session_id"]:
            attrs.append(_attr("langfuse.session.id", c["session_id"]))
        spans.append(
            _span(
                trace_id, _hex_id(f"{task_run_id}/llm/{c['id']}", 8),
                f"llm_call {node}", c_start, c_end, attrs,
                ERROR if c["status"] == "failed" else OK,
                parent=parent,
            )
        )

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [_attr("service.name", "mini-ork")]
                },
                "scopeSpans": [{"scope": {"name": "mini-ork"}, "spans": spans}],
            }
        ]
    }


def export(task_run_id: str, db_path: str, dry_run: bool = False) -> int:
    payload = build_payload(db_path, task_run_id)
    if dry_run:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not pk or not sk:
        print("otel_export: LANGFUSE_PUBLIC_KEY/SECRET_KEY unset — no-op", file=sys.stderr)
        return 0

    endpoint = os.environ.get("LANGFUSE_OTLP_ENDPOINT", DEFAULT_ENDPOINT)
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        n = sum(
            len(ss["spans"])
            for rs in payload["resourceSpans"]
            for ss in rs["scopeSpans"]
        )
        print(f"otel_export: {n} spans -> {endpoint} ({resp.status})", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mini_ork.otel_export")
    p.add_argument("task_run_id")
    p.add_argument("--db", default=os.environ.get("MINI_ORK_DB", ".mini-ork/state.db"))
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    return export(a.task_run_id, a.db, dry_run=a.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
