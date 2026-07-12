#!/usr/bin/env python3
"""Pure JSON-driven held-out eval scorer.

Reads a manifest and a per-task results JSON, walks tasks in manifest order,
computes resolve_rate and resolve_per_dollar with an optional budget cap, and
emits a single JSON line on stdout (and to --out when provided).

No network. No subprocess. No state. Deterministic by manifest order.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="heldout_eval",
        description="JSON-driven held-out eval scorer (no LLM, no subprocess).",
    )
    p.add_argument("--manifest", required=True, type=Path,
                   help="Path to manifest.json (list of {id, dir, gold_test_cmd, note}).")
    p.add_argument("--results", required=True, type=Path,
                   help="Path to results.json ({task_id: {passed: bool, cost_usd: float}}).")
    p.add_argument("--out", type=Path, default=None,
                   help="Optional output path; JSON line is also printed to stdout.")
    p.add_argument("--arm", required=True, choices=["on", "off"],
                   help="Arm label passed through to the output line.")
    p.add_argument("--epoch", required=True, type=int,
                   help="Epoch index passed through to the output line.")
    p.add_argument("--budget-usd", type=float, default=None,
                   help="Optional cap on cumulative cost; tasks whose inclusion "
                        "would exceed the cap are skipped.")
    return p.parse_args(argv)


def score(
    manifest: Any,
    results: Any,
    *,
    arm: str,
    epoch: int,
    budget_usd: float | None,
) -> dict[str, Any]:
    if not isinstance(manifest, list):
        raise ValueError("manifest must be a JSON list")
    if not isinstance(results, dict):
        raise ValueError("results must be a JSON object keyed by task id")
    tasks_scored = 0
    passed = 0
    cost_usd = 0.0
    for entry in manifest:
        if not isinstance(entry, dict):
            raise ValueError("each manifest entry must be an object")
        tid = entry.get("id")
        if not isinstance(tid, str):
            raise ValueError("manifest entry missing string 'id'")
        row = results.get(tid)
        if row is None:
            continue
        try:
            task_cost = float(row.get("cost_usd", 0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"results[{tid!r}].cost_usd is not numeric") from exc
        if budget_usd is not None and (cost_usd + task_cost) > budget_usd:
            break
        cost_usd += task_cost
        tasks_scored += 1
        if bool(row.get("passed", False)):
            passed += 1
    resolve_rate = (passed / tasks_scored) if tasks_scored else 0.0
    if cost_usd == 0:
        resolve_per_dollar = 0.0
    else:
        resolve_per_dollar = resolve_rate / cost_usd
    return {
        "arm": arm,
        "epoch": epoch,
        "resolve_rate": resolve_rate,
        "cost_usd": cost_usd,
        "resolve_per_dollar": resolve_per_dollar,
        "tasks_scored": tasks_scored,
        "budget_usd": budget_usd,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    manifest = json.loads(args.manifest.read_text())
    results = json.loads(args.results.read_text())
    out = score(
        manifest,
        results,
        arm=args.arm,
        epoch=args.epoch,
        budget_usd=args.budget_usd,
    )
    line = json.dumps(out)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
