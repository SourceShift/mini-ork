#!/usr/bin/env python3
"""Analyze mini-ork routing-policy experiments for the arXiv draft.

The script is intentionally read-only. It does not dispatch agents or mutate
state.db; it converts task_runs, llm_calls, run_events, and verifier sidecars
into paper-ready CSV/Markdown summaries.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import math
import re
import statistics
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TERMINAL_SUCCESS = {"published"}
DEFAULT_EXPENSIVE_PATTERNS = ("opus", "sonnet", "fable", "mythos", "claude")
DEFAULT_TARGET_POLICY = "trace_governed"
DEFAULT_BASELINE_POLICIES = ("frontier_only", "static_hybrid", "cheap_only")
DEFAULT_MIN_RUNS_PER_POLICY = 12
DEFAULT_MIN_TASK_CLASSES = 3
DEFAULT_MAX_VERIFIER_PASS_LOSS = 0.05
DEFAULT_MIN_COST_REDUCTION = 0.20


@dataclass(frozen=True)
class ManifestRun:
    policy: str
    task_id: str
    replicate: int
    run_id: str


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def load_manifest(path: Path) -> tuple[list[ManifestRun], tuple[str, ...], str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    patterns = tuple(
        str(x).lower()
        for x in data.get("expensive_model_patterns", DEFAULT_EXPENSIVE_PATTERNS)
    )
    runs = []
    for item in data.get("runs", []):
        runs.append(
            ManifestRun(
                policy=str(item["policy"]),
                task_id=str(item["task_id"]),
                replicate=int(item.get("replicate", 1)),
                run_id=str(item["run_id"]),
            )
        )
    return runs, patterns, str(data.get("experiment_id") or path.stem)


def load_auto_runs(con: sqlite3.Connection, recipe: str, policy: str) -> tuple[list[ManifestRun], str]:
    rows = con.execute(
        """
        SELECT id
          FROM task_runs
         WHERE recipe = ?
         ORDER BY created_at ASC
        """,
        (recipe,),
    ).fetchall()
    runs = [
        ManifestRun(policy=policy, task_id=f"{recipe}-{i + 1:03d}", replicate=1, run_id=row["id"])
        for i, row in enumerate(rows)
    ]
    return runs, f"historical-{recipe}"


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def task_run(con: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM task_runs WHERE id=? LIMIT 1", (run_id,)).fetchone()


def llm_calls(con: sqlite3.Connection, run: sqlite3.Row | None, run_id: str) -> list[sqlite3.Row]:
    if not table_exists(con, "llm_calls"):
        return []
    trace_id = (run["trace_id"] if run and "trace_id" in run.keys() else "") or ""
    clauses = ["CAST(run_id AS TEXT) = ?"]
    params: list[Any] = [run_id]
    if trace_id:
        clauses.append("traceparent LIKE ?")
        params.append(f"%{trace_id}%")
    return con.execute(
        f"""
        SELECT *
          FROM llm_calls
         WHERE {" OR ".join(clauses)}
         ORDER BY ts ASC, id ASC
        """,
        params,
    ).fetchall()


def run_events(con: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    if not table_exists(con, "run_events"):
        return []
    return con.execute(
        "SELECT * FROM run_events WHERE run_id=? ORDER BY created_at ASC",
        (run_id,),
    ).fetchall()


def verifier_sidecars(mini_ork_home: Path, run_id: str) -> tuple[int, int, list[str]]:
    run_dir = mini_ork_home / "runs" / run_id
    sidecars = sorted(run_dir.glob("verifier-result-*.json"))
    results: dict[str, bool] = {}
    reasons: list[str] = []
    for sidecar in sidecars:
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - diagnostic collector
            reasons.append(f"{sidecar.name}: invalid-json: {exc}")
            continue
        name = str(data.get("verifier") or sidecar.stem)
        passed = data.get("pass") is True or data.get("status") == "pass"
        results[name] = passed
        if not passed:
            reasons.append(f"{sidecar.name}: pass={data.get('pass')!r}")

    # Recipe verifiers often write JSON summaries as the final line of
    # verifier_*.log instead of verifier-result-*.json sidecars.
    for log_path in sorted(run_dir.glob("verifier_*.log")):
        data = last_json_line(log_path)
        if not data:
            continue
        name = str(data.get("verifier") or log_path.stem)
        if data.get("status") == "skipped":
            continue
        passed = data.get("pass") is True or data.get("status") == "pass"
        results[name] = passed
        if not passed:
            reasons.append(f"{log_path.name}: pass={data.get('pass')!r} status={data.get('status')!r}")

    for log_path in sorted((run_dir / "evidence").glob("*.log")):
        data = last_json_line(log_path)
        if not data or not data.get("verifier"):
            continue
        name = str(data.get("verifier") or log_path.stem)
        if data.get("status") == "skipped":
            continue
        passed = data.get("pass") is True or data.get("status") == "pass"
        results[name] = passed
        if not passed:
            reasons.append(f"{log_path.name}: pass={data.get('pass')!r} status={data.get('status')!r}")

    pass_count = sum(1 for passed in results.values() if passed)
    return pass_count, len(results), reasons


def last_json_line(path: Path) -> dict[str, Any] | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def first_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            data, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def reviewer_diagnostics(mini_ork_home: Path, run_id: str) -> tuple[str, int]:
    run_dir = mini_ork_home / "runs" / run_id
    reviews = sorted(run_dir.glob("review-*.json"))
    if not reviews:
        return "", 0
    verdicts = []
    accepted = 0
    for review in reviews:
        try:
            text = review.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        data = first_json_object(text)
        verdict = str((data or {}).get("verdict") or "").strip()
        if not verdict:
            verdict = "unknown"
        verdicts.append(verdict)
        if verdict.lower() in {"pass", "approve", "approved"}:
            accepted += 1
    return ",".join(verdicts), int(bool(verdicts) and accepted == len(verdicts))


def filesystem_diagnostics(mini_ork_home: Path, run_id: str) -> dict[str, int]:
    run_dir = mini_ork_home / "runs" / run_id
    plan_fallback = int(any(run_dir.glob("plan-failure-*")))
    zero_stdout_artifacts = sum(
        1
        for path in run_dir.glob("*.stdout.md")
        if path.is_file() and path.stat().st_size == 0
    )
    return {
        "plan_fallback": plan_fallback,
        "zero_stdout_artifact_count": zero_stdout_artifacts,
    }


def is_expensive_call(row: sqlite3.Row, patterns: tuple[str, ...]) -> bool:
    text = " ".join(
        str(row[key] or "")
        for key in ("provider", "model_id", "tier", "feature_name", "actor")
        if key in row.keys()
    ).lower()
    return any(pattern in text for pattern in patterns)


def duration_seconds(run: sqlite3.Row | None) -> float:
    if not run:
        return 0.0
    if run["duration_ms"]:
        return float(run["duration_ms"]) / 1000.0
    if run["ended_at"] and run["created_at"]:
        return max(0.0, float(run["ended_at"]) - float(run["created_at"]))
    return 0.0


def retry_count_from_events(events: list[sqlite3.Row]) -> int:
    count = 0
    for event in events:
        payload = event["payload_json"] if "payload_json" in event.keys() else ""
        if not payload:
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        text = json.dumps(data).lower()
        if "retry" in text or "request_changes" in text:
            count += 1
    return count


def analyze_run(
    con: sqlite3.Connection,
    mini_ork_home: Path,
    item: ManifestRun,
    expensive_patterns: tuple[str, ...],
) -> dict[str, Any]:
    run = task_run(con, item.run_id)
    calls = llm_calls(con, run, item.run_id)
    events = run_events(con, item.run_id)
    verifier_passes, verifier_total, verifier_reasons = verifier_sidecars(mini_ork_home, item.run_id)
    fs_diag = filesystem_diagnostics(mini_ork_home, item.run_id)
    reviewer_verdicts, reviewer_accept = reviewer_diagnostics(mini_ork_home, item.run_id)

    status = run["status"] if run else "missing"
    success = status in TERMINAL_SUCCESS
    total_cost = float(run["cost_usd"] or 0.0) if run else 0.0
    llm_cost = sum(float(call["cost_usd"] or 0.0) for call in calls)
    expensive_calls = sum(1 for call in calls if is_expensive_call(call, expensive_patterns))
    failed_llm_calls = sum(1 for call in calls if str(call["status"]).lower() != "success")
    providers = sorted({str(call["provider"] or "unknown") for call in calls})

    return {
        "policy": item.policy,
        "task_id": item.task_id,
        "replicate": item.replicate,
        "run_id": item.run_id,
        "recipe": run["recipe"] if run else "",
        "task_class": run["task_class"] if run else "",
        "status": status,
        "success": int(success),
        "verifier_pass": int(verifier_total > 0 and verifier_passes == verifier_total),
        "verifier_passes": verifier_passes,
        "verifier_total": verifier_total,
        "verifier_reasons": "; ".join(verifier_reasons),
        "reviewer_verdicts": reviewer_verdicts,
        "reviewer_accept": reviewer_accept,
        "task_cost_usd": round(total_cost, 6),
        "llm_cost_usd": round(llm_cost, 6),
        "duration_s": round(duration_seconds(run), 3),
        "llm_call_count": len(calls),
        "failed_llm_call_count": failed_llm_calls,
        "input_tokens": sum(int(call["input_tokens"] or 0) for call in calls),
        "output_tokens": sum(int(call["output_tokens"] or 0) for call in calls),
        "expensive_call_count": expensive_calls,
        "distinct_provider_count": len(providers),
        "providers": ",".join(providers),
        "retry_event_count": retry_count_from_events(events),
        "plan_fallback": fs_diag["plan_fallback"],
        "zero_stdout_artifact_count": fs_diag["zero_stdout_artifact_count"],
    }


def aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["policy"], []).append(row)

    out = []
    for policy, items in sorted(groups.items()):
        runs = len(items)
        successes = sum(int(x["success"]) for x in items)
        verifier_passes = sum(int(x["verifier_pass"]) for x in items)
        reviewer_total = sum(1 for x in items if x["reviewer_verdicts"])
        reviewer_accepts = sum(int(x["reviewer_accept"]) for x in items if x["reviewer_verdicts"])
        total_cost = sum(float(x["task_cost_usd"]) for x in items)
        durations = [float(x["duration_s"]) for x in items if float(x["duration_s"]) > 0]
        out.append(
            {
                "policy": policy,
                "runs": runs,
                "successful_runs": successes,
                "success_rate": round(successes / runs, 4) if runs else 0.0,
                "verifier_pass_rate": round(verifier_passes / runs, 4) if runs else 0.0,
                "reviewer_accept_rate": (
                    round(reviewer_accepts / reviewer_total, 4)
                    if reviewer_total
                    else ""
                ),
                "total_cost_usd": round(total_cost, 6),
                "cost_per_success_usd": round(total_cost / successes, 6) if successes else "",
                "median_duration_s": round(statistics.median(durations), 3) if durations else "",
                "llm_call_count": sum(int(x["llm_call_count"]) for x in items),
                "failed_llm_call_count": sum(int(x["failed_llm_call_count"]) for x in items),
                "input_tokens": sum(int(x["input_tokens"]) for x in items),
                "output_tokens": sum(int(x["output_tokens"]) for x in items),
                "expensive_call_count": sum(int(x["expensive_call_count"]) for x in items),
                "retry_event_count": sum(int(x["retry_event_count"]) for x in items),
                "reviewer_verdict_count": reviewer_total,
                "reviewer_accept_count": reviewer_accepts,
                "plan_fallback_count": sum(int(x["plan_fallback"]) for x in items),
                "zero_stdout_artifact_count": sum(
                    int(x["zero_stdout_artifact_count"]) for x in items
                ),
                "distinct_provider_count_max": max(int(x["distinct_provider_count"]) for x in items),
            }
        )
    return out


def wilson_interval(successes: int, runs: int, z: float = 1.96) -> tuple[float, float]:
    """Return a Wilson score interval for a binomial proportion."""
    if runs <= 0:
        return (0.0, 0.0)
    p = successes / runs
    denom = 1 + z**2 / runs
    center = (p + z**2 / (2 * runs)) / denom
    spread = z * math.sqrt((p * (1 - p) / runs) + (z**2 / (4 * runs**2))) / denom
    return (round(max(0.0, center - spread), 4), round(min(1.0, center + spread), 4))


def add_intervals(summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in summary:
        runs = int(row["runs"])
        successes = int(row["successful_runs"])
        verifier_passes = int(round(float(row["verifier_pass_rate"]) * runs))
        success_lo, success_hi = wilson_interval(successes, runs)
        verifier_lo, verifier_hi = wilson_interval(verifier_passes, runs)
        enriched = dict(row)
        enriched["success_rate_ci95"] = f"[{success_lo}, {success_hi}]"
        enriched["verifier_pass_rate_ci95"] = f"[{verifier_lo}, {verifier_hi}]"
        out.append(enriched)
    return out


def numeric_or_none(value: Any) -> float | None:
    if value == "" or value is None:
        return None
    return float(value)


def policy_comparisons(
    summary: list[dict[str, Any]],
    target_policy: str,
    baseline_policies: tuple[str, ...],
) -> list[dict[str, Any]]:
    by_policy = {str(row["policy"]): row for row in summary}
    target = by_policy.get(target_policy)
    if not target:
        return []

    target_cost = numeric_or_none(target.get("cost_per_success_usd"))
    target_success = float(target["success_rate"])
    target_verifier = float(target["verifier_pass_rate"])
    rows = []
    for baseline_policy in baseline_policies:
        baseline = by_policy.get(baseline_policy)
        if not baseline:
            continue
        baseline_cost = numeric_or_none(baseline.get("cost_per_success_usd"))
        if baseline_cost and target_cost is not None:
            cost_reduction = (baseline_cost - target_cost) / baseline_cost
        else:
            cost_reduction = None
        rows.append(
            {
                "target_policy": target_policy,
                "baseline_policy": baseline_policy,
                "target_runs": target["runs"],
                "baseline_runs": baseline["runs"],
                "success_rate_delta": round(target_success - float(baseline["success_rate"]), 4),
                "verifier_pass_rate_delta": round(
                    target_verifier - float(baseline["verifier_pass_rate"]),
                    4,
                ),
                "cost_per_success_delta_usd": (
                    round(target_cost - baseline_cost, 6)
                    if target_cost is not None and baseline_cost is not None
                    else ""
                ),
                "cost_reduction_vs_baseline": (
                    round(cost_reduction, 4)
                    if cost_reduction is not None
                    else ""
                ),
            }
        )
    return rows


def claim_gate(
    summary: list[dict[str, Any]],
    comparisons: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    target_policy: str,
    baseline_policy: str,
    min_runs_per_policy: int,
    min_task_classes: int,
    min_cost_reduction: float,
    max_verifier_pass_loss: float,
) -> dict[str, Any]:
    by_policy = {str(row["policy"]): row for row in summary}
    task_classes = sorted({str(row["task_class"]) for row in rows if row.get("task_class")})
    policies_with_low_n = [
        policy for policy, row in sorted(by_policy.items()) if int(row["runs"]) < min_runs_per_policy
    ]
    comparison = next(
        (row for row in comparisons if row["baseline_policy"] == baseline_policy),
        None,
    )
    reasons = []
    if target_policy not in by_policy:
        reasons.append(f"missing target policy: {target_policy}")
    if baseline_policy not in by_policy:
        reasons.append(f"missing baseline policy: {baseline_policy}")
    if policies_with_low_n:
        reasons.append(
            "insufficient per-policy sample size: "
            + ", ".join(f"{policy}<n{min_runs_per_policy}" for policy in policies_with_low_n)
        )
    if len(task_classes) < min_task_classes:
        reasons.append(
            f"insufficient task-class diversity: observed={len(task_classes)} "
            f"({', '.join(task_classes) or 'none'}), required>={min_task_classes}"
        )
    if not comparison:
        reasons.append(f"missing comparison against {baseline_policy}")
    else:
        cost_reduction = numeric_or_none(comparison["cost_reduction_vs_baseline"])
        verifier_delta = float(comparison["verifier_pass_rate_delta"])
        if cost_reduction is None or cost_reduction < min_cost_reduction:
            reasons.append(
                f"cost reduction below threshold: observed={cost_reduction}, "
                f"required>={min_cost_reduction}"
            )
        if verifier_delta < -max_verifier_pass_loss:
            reasons.append(
                f"verifier pass loss too large: observed={verifier_delta}, "
                f"allowed>=-{max_verifier_pass_loss}"
            )

    return {
        "target_policy": target_policy,
        "primary_baseline_policy": baseline_policy,
        "min_runs_per_policy": min_runs_per_policy,
        "min_task_classes": min_task_classes,
        "observed_task_classes": task_classes,
        "min_cost_reduction": min_cost_reduction,
        "max_verifier_pass_loss": max_verifier_pass_loss,
        "supported": not reasons,
        "reasons": reasons,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "_No rows._\n"
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(col, "")) for col in columns) + " |")
    return "\n".join(lines) + "\n"


def write_summary(
    path: Path,
    experiment_id: str,
    summary: list[dict[str, Any]],
    comparisons: list[dict[str, Any]],
    gate: dict[str, Any],
    runs: list[dict[str, Any]],
) -> None:
    primary_cols = [
        "policy",
        "runs",
        "success_rate",
        "verifier_pass_rate",
        "reviewer_accept_rate",
        "total_cost_usd",
        "cost_per_success_usd",
        "median_duration_s",
        "expensive_call_count",
        "plan_fallback_count",
        "zero_stdout_artifact_count",
        "success_rate_ci95",
        "verifier_pass_rate_ci95",
    ]
    comparison_cols = [
        "target_policy",
        "baseline_policy",
        "target_runs",
        "baseline_runs",
        "success_rate_delta",
        "verifier_pass_rate_delta",
        "cost_per_success_delta_usd",
        "cost_reduction_vs_baseline",
    ]
    run_cols = [
        "policy",
        "task_id",
        "replicate",
        "run_id",
        "status",
        "success",
        "verifier_pass",
        "reviewer_verdicts",
        "reviewer_accept",
        "task_cost_usd",
        "duration_s",
        "llm_call_count",
        "expensive_call_count",
        "plan_fallback",
        "zero_stdout_artifact_count",
    ]
    gate_reason_lines = [f"- {reason}" for reason in gate["reasons"]] or ["- none"]
    text = [
        f"# Trace-Budget Experiment Summary: {experiment_id}",
        "",
        "## Policy Summary",
        "",
        markdown_table(summary, primary_cols),
        "",
        "## Target Policy Comparisons",
        "",
        markdown_table(comparisons, comparison_cols),
        "",
        "## Claim Gate",
        "",
        f"- Target policy: `{gate['target_policy']}`",
        f"- Primary baseline: `{gate['primary_baseline_policy']}`",
        f"- Minimum runs per policy: {gate['min_runs_per_policy']}",
        f"- Minimum task classes: {gate['min_task_classes']}",
        f"- Observed task classes: {', '.join(gate['observed_task_classes']) or 'none'}",
        f"- Minimum cost reduction: {gate['min_cost_reduction']}",
        f"- Maximum verifier-pass loss: {gate['max_verifier_pass_loss']}",
        f"- Supported by this batch: **{str(gate['supported']).lower()}**",
        "",
        "Reasons:",
        "",
        *gate_reason_lines,
        "",
        "## Run-Level Data",
        "",
        markdown_table(runs, run_cols),
        "",
        "## Caveats",
        "",
        "- A policy with zero runs is not represented in the summary.",
        "- Historical auto-recipe mode is pilot evidence only; use a manifest for controlled experiments.",
        "- `verifier_pass` requires at least one `verifier-result-*.json` sidecar in the run directory.",
    ]
    path.write_text("\n".join(text) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(".mini-ork/state.db"))
    parser.add_argument("--mini-ork-home", type=Path, default=Path(".mini-ork"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--auto-recipe", help="Build a pilot manifest from all task_runs for this recipe.")
    parser.add_argument("--policy", default="historical")
    parser.add_argument("--target-policy", default=DEFAULT_TARGET_POLICY)
    parser.add_argument("--primary-baseline-policy", default="frontier_only")
    parser.add_argument("--min-runs-per-policy", type=int, default=DEFAULT_MIN_RUNS_PER_POLICY)
    parser.add_argument("--min-task-classes", type=int, default=DEFAULT_MIN_TASK_CLASSES)
    parser.add_argument("--min-cost-reduction", type=float, default=DEFAULT_MIN_COST_REDUCTION)
    parser.add_argument("--max-verifier-pass-loss", type=float, default=DEFAULT_MAX_VERIFIER_PASS_LOSS)
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.db.is_file():
        raise SystemExit(f"state db not found: {args.db}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    con = connect(args.db)
    try:
        if args.manifest:
            manifest_runs, patterns, experiment_id = load_manifest(args.manifest)
        elif args.auto_recipe:
            manifest_runs, experiment_id = load_auto_runs(con, args.auto_recipe, args.policy)
            patterns = DEFAULT_EXPENSIVE_PATTERNS
        else:
            raise SystemExit("Provide --manifest or --auto-recipe.")

        runs = [
            analyze_run(con, args.mini_ork_home, item, patterns)
            for item in manifest_runs
        ]
    finally:
        con.close()

    summary = add_intervals(aggregate(runs))
    comparisons = policy_comparisons(
        summary,
        args.target_policy,
        DEFAULT_BASELINE_POLICIES,
    )
    gate = claim_gate(
        summary,
        comparisons,
        runs,
        args.target_policy,
        args.primary_baseline_policy,
        args.min_runs_per_policy,
        args.min_task_classes,
        args.min_cost_reduction,
        args.max_verifier_pass_loss,
    )
    write_csv(args.out_dir / "runs.csv", runs)
    write_csv(args.out_dir / "summary.csv", summary)
    write_csv(args.out_dir / "comparisons.csv", comparisons)
    write_summary(args.out_dir / "summary.md", experiment_id, summary, comparisons, gate, runs)
    (args.out_dir / "summary.json").write_text(
        json.dumps(
            {
                "experiment_id": experiment_id,
                "summary": summary,
                "comparisons": comparisons,
                "claim_gate": gate,
                "runs": runs,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(args.out_dir / "summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
