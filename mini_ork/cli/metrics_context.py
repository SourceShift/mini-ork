"""`mini-ork metrics context` — is context v2 delivered, acknowledged, and does it help?

Read-only. Scans run dirs that hold a ``context-pack.v2.json`` and reports, per
arm (``shadow`` | ``v2`` | ``holdout``):

- **delivery**: did the planner receive the v2 block (``learned/planner.json``),
  and how many LLM nodes did (``learned/<node>.json`` sources of kind
  ``context_v2``);
- **acknowledgement**: ids in ``plan.json`` ``context_used`` that exist in the
  pack vs ids the planner made up;
- **scope**: files the delivered diff touched outside the kickoff's scope, and
  deleted test files (``context_v2.check_diff``);
- **recurrence** — the outcome that matters: of the recurring problems the pack
  selected for a run, how many did that run's own reviewers report again
  (``code_findings`` for the run, once harvested). ``lift`` is the holdout
  recurrence rate minus the v2 rate; positive means fewer repeats with v2.

A run must be harvested (``code_findings_runs``) before its recurrence counts.
``lift`` is reported as insufficient until both arms have >= 30 harvested runs.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from mini_ork import context_v2

MIN_N = 30
_DIFF_NAMES = ("review-diff.patch", "framework-edit.diff")


def _default_db() -> str:
    if os.environ.get("MINI_ORK_DB"):
        return os.environ["MINI_ORK_DB"]
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    return os.path.join(home, "state.db")


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _harvested(db: str) -> set[str]:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return set()
    try:
        return {r[0] for r in con.execute("SELECT run_id FROM code_findings_runs")}
    except sqlite3.Error:
        return set()
    finally:
        con.close()


def run_record(run_dir: Path, *, db: str, harvested: set[str]) -> dict | None:
    pack = context_v2.load_pack(str(run_dir))
    if not pack:
        return None
    run_id = pack.get("run_id") or run_dir.name
    planner = _read_json(run_dir / "learned" / "planner.json").get("context_v2") or {}
    nodes_llm = nodes_v2 = 0
    learned = run_dir / "learned"
    if learned.is_dir():
        for f in sorted(learned.glob("*.json")):
            if f.stem == "planner":
                continue
            nodes_llm += 1
            sources = _read_json(f).get("sources") or []
            if any(isinstance(s, dict) and s.get("kind") == "context_v2" for s in sources):
                nodes_v2 += 1
    ack = context_v2.acknowledgements(_read_json(run_dir / "plan.json"), pack)
    diff_text = ""
    for name in _DIFF_NAMES:
        p = run_dir / name
        if p.is_file():
            diff_text = p.read_text(encoding="utf-8", errors="replace")
            break
    scope = context_v2.check_diff(diff_text, pack.get("contract") or {})
    clusters = pack.get("file_findings") or []
    is_harvested = run_id in harvested
    recurred: list[str] = []
    if is_harvested and clusters:
        hits = context_v2.recurrence(clusters, context_v2.run_findings(run_id, db=db))
        recurred = [cid for cid, hit in hits.items() if hit]
    return {
        "run_id": run_id,
        "arm": pack.get("arm") or pack.get("mode") or "shadow",
        "n_items": len(pack.get("item_ids") or []),
        "n_groups": len(clusters),
        "planner_v2_injected": bool(planner.get("injected")),
        "nodes_llm": nodes_llm,
        "nodes_v2": nodes_v2,
        "ack_valid": len(ack["valid"]),
        "ack_invalid": len(ack["invalid"]),
        "diff_seen": bool(diff_text),
        "outside_scope": scope["outside_scope"],
        "deleted_tests": scope["deleted_tests"],
        "harvested": is_harvested,
        "recurred": recurred,
    }


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def summarize(records: list[dict]) -> dict:
    arms: dict[str, dict] = {}
    for arm in sorted({r["arm"] for r in records}):
        rs = [r for r in records if r["arm"] == arm]
        harvested = [r for r in rs if r["harvested"] and r["n_groups"]]
        groups = sum(r["n_groups"] for r in harvested)
        repeats = sum(len(r["recurred"]) for r in harvested)
        with_diff = [r for r in rs if r["diff_seen"]]
        arms[arm] = {
            "runs": len(rs),
            "planner_v2_rate": _rate(sum(r["planner_v2_injected"] for r in rs), len(rs)),
            "node_v2_rate": _rate(sum(r["nodes_v2"] for r in rs), sum(r["nodes_llm"] for r in rs)),
            "ack_rate": _rate(sum(1 for r in rs if r["ack_valid"]), len(rs)),
            "invalid_ids": sum(r["ack_invalid"] for r in rs),
            "outside_scope_rate": _rate(sum(1 for r in with_diff if r["outside_scope"]),
                                        len(with_diff)),
            "deleted_tests_runs": sum(1 for r in rs if r["deleted_tests"]),
            "harvested_runs": len(harvested),
            "groups_checked": groups,
            "recurrence_rate": _rate(repeats, groups),
        }
    v2, hold = arms.get("v2", {}), arms.get("holdout", {})
    lift: dict = {"value": None, "status": "insufficient",
                  "n_v2": v2.get("harvested_runs", 0), "n_holdout": hold.get("harvested_runs", 0)}
    if (lift["n_v2"] >= MIN_N and lift["n_holdout"] >= MIN_N
            and v2.get("recurrence_rate") is not None
            and hold.get("recurrence_rate") is not None):
        lift["value"] = round(hold["recurrence_rate"] - v2["recurrence_rate"], 3)
        lift["status"] = "measured"
    return {"arms": arms, "lift": lift, "min_n": MIN_N}


def collect(runs_dir: Path, db: str) -> list[dict]:
    harvested = _harvested(db)
    out = []
    if runs_dir.is_dir():
        for d in sorted(runs_dir.iterdir()):
            if d.is_dir():
                rec = run_record(d, db=db, harvested=harvested)
                if rec:
                    out.append(rec)
    return out


def _print_human(result: dict, out) -> None:
    out.write("Context v2 — delivery, acknowledgement, scope, recurrence\n")
    if not result["arms"]:
        out.write("  No run has a context-pack.v2.json yet (MO_CONTEXT_V2=off, or no run since).\n")
        return
    cols = ("runs", "planner_v2_rate", "node_v2_rate", "ack_rate", "invalid_ids",
            "outside_scope_rate", "harvested_runs", "groups_checked", "recurrence_rate")
    for arm, m in result["arms"].items():
        out.write(f"  [{arm}] " + "  ".join(f"{c}={m[c]}" for c in cols) + "\n")
    lift = result["lift"]
    if lift["status"] == "measured":
        out.write(f"  lift (holdout - v2 recurrence): {lift['value']}\n")
    else:
        out.write(f"  lift: insufficient (harvested runs v2={lift['n_v2']}, "
                  f"holdout={lift['n_holdout']}; need {result['min_n']} each)\n")


def main(argv: list[str] | None = None, *, stdout=None, stderr=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    p = argparse.ArgumentParser(prog="mini-ork metrics context",
                                description="Context v2 delivery and outcome, read-only.")
    p.add_argument("--db", default=None, help="state.db (default: $MINI_ORK_DB, else <home>/state.db)")
    p.add_argument("--runs-dir", default=None, help="run dirs to scan (default: next to the DB)")
    p.add_argument("--runs", action="store_true", help="include the per-run records")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    db = a.db or _default_db()
    runs_dir = Path(a.runs_dir) if a.runs_dir else Path(db).resolve().parent / "runs"
    records = collect(runs_dir, db)
    result = summarize(records)
    if a.runs:
        result["runs"] = records
    if a.json:
        out.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    else:
        _print_human(result, out)
    return 0
