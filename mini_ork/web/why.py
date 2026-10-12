"""Failure-evidence aggregator — answers 'why did this run fail?'

mini-ork scatters its failure evidence across:
  1. execute.log              (orchestrator stdout + verifier verdicts)
  2. verifier-result-*.json   (structured pass/fail per verifier)
  3. verifier-*.log           (per-verifier stderr / detail)
  4. .mini-ork/runs/evidence/ (evidence logs referenced by failing verifiers)
  5. self_improve_runs.notes  (loop-level bookkeeping)
  6. execution_traces         (per-node traces with reviewer_verdict)

The Overview tab can't usefully show "0 events" when all of this is sitting
on disk. This module reads from all six sources and returns a structured
diagnostic the UI can render at-a-glance.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .db import StateDB

# Patterns that signal failure in execute.log lines
FAIL_PATTERNS = [
    re.compile(r"\[fail\]", re.IGNORECASE),
    re.compile(r"\bfailed\b", re.IGNORECASE),
    re.compile(r"\berror\b", re.IGNORECASE),
    re.compile(r"escalated", re.IGNORECASE),
    re.compile(r"\d+\s+node\(s\)\s+failed"),
]

# Pattern extracting evidence paths from verifier failure lines:
#   "[fail] verifier_ref verifiers/X.sh failed → /abs/path/to/evidence.log"
EVIDENCE_REF = re.compile(
    r"verifier_ref\s+(\S+)\s+failed\s*(?:→|->)+\s*(\S+)", re.IGNORECASE
)


def aggregate(
    home: Path,
    db: StateDB,
    task_run_id: str,
) -> dict[str, Any]:
    run_dir = home / "runs" / task_run_id
    out: dict[str, Any] = {
        "task_run_id": task_run_id,
        "run_dir": str(run_dir),
        "run_dir_exists": run_dir.exists(),
        "execute_log": None,
        "verifier_results": [],
        "evidence_refs": [],
        "self_improve_notes": None,
        "trace_verdicts": [],
        "lane_failures": [],
        "summary": "no diagnostic data found",
    }

    # 1. execute.log — tail + failure lines
    log = run_dir / "execute.log"
    if log.exists():
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            # Tail: last 80 lines
            tail = lines[-80:]
            # Failure lines: anywhere matching FAIL_PATTERNS
            failure_lines = [
                {"line_no": i + 1, "text": L}
                for i, L in enumerate(lines)
                if any(p.search(L) for p in FAIL_PATTERNS)
            ][:30]
            # Evidence references
            evidence = []
            for L in lines:
                m = EVIDENCE_REF.search(L)
                if m:
                    evidence.append({"verifier": m.group(1), "evidence_path": m.group(2)})
            out["execute_log"] = {
                "size": log.stat().st_size,
                "tail": tail,
                "failure_lines": failure_lines,
                "total_lines": len(lines),
            }
            out["evidence_refs"] = evidence
        except OSError:
            pass

    # 2. + 3. verifier-result-*.json + verifier-*.log
    if run_dir.exists():
        for vr in sorted(run_dir.glob("verifier-result-*.json")):
            try:
                payload = json.loads(vr.read_text())
                # Pair with the corresponding log if present
                verifier_name = vr.stem.removeprefix("verifier-result-")
                log_path = run_dir / f"verifier-{verifier_name}.log"
                out["verifier_results"].append(
                    {
                        "verifier": verifier_name,
                        "pass": bool(payload.get("pass")),
                        "result_file": vr.name,
                        "log_file": log_path.name if log_path.exists() else None,
                        "evidence_path": payload.get("evidence_path"),
                        "payload": payload,
                    }
                )
            except (OSError, json.JSONDecodeError):
                continue

    # 4. self_improve_runs.notes
    if db.has_table("self_improve_runs"):
        row = db.row(
            "SELECT notes, outcome FROM self_improve_runs WHERE run_id = ?",
            (task_run_id,),
        )
        if row:
            out["self_improve_notes"] = {
                "raw": row.get("notes"),
                "outcome": row.get("outcome"),
            }

    # 5. execution_traces with non-success status or verdict
    if db.has_table("execution_traces"):
        out["trace_verdicts"] = db.rows(
            """
            SELECT trace_id, task_class, status, reviewer_verdict,
                   substr(verifier_output, 1, 200) AS verifier_excerpt,
                   final_artifact_ref, created_at
            FROM execution_traces
            WHERE created_at >= COALESCE(
              (SELECT created_at FROM task_runs WHERE id = ?),
              0
            )
            AND created_at <= COALESCE(
              (SELECT COALESCE(ended_at, strftime('%s','now')) FROM task_runs WHERE id = ?),
              strftime('%s','now')
            )
            AND task_class IN (
              SELECT task_class FROM task_runs WHERE id = ?
            )
            ORDER BY created_at ASC
            LIMIT 50
            """,
            (task_run_id, task_run_id, task_run_id),
        )

    # 6. llm_calls — which LANE failed, and why.
    #
    # Without this the aggregation answers "no specific failure signal found" for
    # the most common death there is: a run whose dispatch chain died at its
    # first node never reached the point of writing execute.log or a verifier
    # result, so every signal above is legitimately empty. Every lane that failed
    # is nevertheless already in llm_calls, with the provider's own classified
    # error, one row per dead lane — it was simply never read here. The terminal
    # lane is the one that ended the walk; the earlier rows are what the cascade
    # tried and abandoned.
    if db.has_table("llm_calls"):
        cols = {r["name"] for r in db.rows("PRAGMA table_info(llm_calls)")}
        if {"run_id", "status"} <= cols:
            wanted = [c for c in ("id", "ts", "provider", "model_id", "feature_name",
                                  "error_category", "error_message", "retryable", "status")
                      if c in cols]
            from mini_ork.dispatch.llm_dispatch import error_message_head

            for row in db.rows(
                f"SELECT {', '.join(wanted)} FROM llm_calls "
                "WHERE run_id = ? AND status = 'failed' ORDER BY id ASC",
                (task_run_id,),
            ):
                out["lane_failures"].append({
                    "lane": row.get("model_id"),
                    "provider": row.get("provider"),
                    "role": row.get("feature_name"),
                    "category": row.get("error_category"),
                    "message": error_message_head(row.get("error_message"), limit=300),
                })

    # 7. Summarize — pick the most user-facing line
    out["summary"] = _summarize(out)
    return out


def _lane_label(failure: dict[str, Any]) -> str:
    """``lane(category)`` for the chain summary, naming the two categories the
    ledger's vocabulary cannot.

    ``llm_calls.error_category`` is pinned by a CHECK constraint to the PROVIDER
    error vocabulary, which has no value for a mini-ork-level rejection — a reply
    of the right kind but the wrong shape. The message is the only place that
    distinction lives, so derive it here rather than printing "unknown" for the
    failure that actually happens most (two of four lanes on a live chain,
    2026-10-12).
    """
    category = str(failure.get("category") or "").strip()
    if not category or category == "unknown":
        if re.search(r"shape check", str(failure.get("message") or ""), re.IGNORECASE):
            category = "shape"
        else:
            category = "error"
    return f"{failure.get('lane') or '?'}({category})"


def _summarize(diag: dict[str, Any]) -> str:
    """Return a one-line human-readable cause."""
    verifiers = diag.get("verifier_results", [])
    failed_verifiers = [v["verifier"] for v in verifiers if not v["pass"]]
    if failed_verifiers:
        return f"verifier failure: {', '.join(failed_verifiers)}"

    log = diag.get("execute_log") or {}
    fail_lines = log.get("failure_lines", [])
    if fail_lines:
        # Prefer "N node(s) failed" if present
        for fl in fail_lines:
            if "node(s) failed" in fl["text"]:
                return fl["text"].strip()
        # Else first matching line
        return fail_lines[0]["text"].strip()

    notes = diag.get("self_improve_notes") or {}
    if notes.get("outcome") in ("failed", "rejected", "aborted", "timed_out"):
        return f"self_improve outcome: {notes['outcome']} (notes: {notes.get('raw', '')})"

    # Lane failures outrank the catch-alls below: when the dispatch chain is what
    # died, the lane ledger is the whole story, and "all verifiers passed" or
    # "no specific failure signal found" would read as if nothing had gone wrong.
    lanes = diag.get("lane_failures") or []
    if lanes:
        chain = ", ".join(_lane_label(f) for f in lanes)
        terminal = lanes[-1].get("message") or ""
        return f"dispatch chain died: {chain}" + (f" — {terminal}" if terminal else "")

    if verifiers and all(v["pass"] for v in verifiers):
        return "all verifiers passed — failure occurred at orchestrator level (check execute.log tail)"

    if not diag.get("run_dir_exists"):
        return "run directory not found on disk — likely deleted or run never started"

    return "no specific failure signal found"


def read_evidence_log(home: Path, evidence_path: str) -> dict[str, Any]:
    """Read an evidence log file by absolute path under .mini-ork/.

    Constrained to paths under .mini-ork/ to prevent escape.
    """
    target = Path(evidence_path).resolve()
    home_resolved = home.resolve()
    if home_resolved not in target.parents and target != home_resolved:
        raise PermissionError(f"path escape: {evidence_path}")
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(evidence_path)
    size = target.stat().st_size
    MAX = 256 * 1024  # 256 KiB
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise FileNotFoundError(str(e))
    truncated = False
    if len(text) > MAX:
        text = text[-MAX:]
        truncated = True
    return {
        "path": str(target),
        "relpath": str(target.relative_to(home_resolved)),
        "size": size,
        "truncated": truncated,
        "content": text,
    }
