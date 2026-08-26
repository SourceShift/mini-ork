#!/usr/bin/env python3
"""Shadow-metrics report for the verifiable/process-reward stack (R1-R7).

Reads the process signals the eval node already PERSISTS on every run — the
per-trace ``reward_vector_json`` in ``execution_traces`` — and reports, over a
time window, whether the new Layer-2 behaviors are firing and how much they
*would* move the training score if promoted from record-only to primary. It is
a pure read: no judge, no model, no network, so it costs nothing to run against
production history.

What it answers, per recommendation:

  R2  coherence gate — the incoherence rate (coherence < 1.0): the share of runs
      the default-ON gate can bite (an over-claimed success the execution
      backbone cannot see). If this is ~0 on healthy history, the gate is inert
      today; a non-zero rate is where it changes verdicts.
  R3  decomposed reward — the mean/max |decomposed - reward_value| spread: how
      far the equal-weight component mean sits from the shipped score, i.e. how
      much MO_EVAL_DECOMPOSED_REWARD=1 would move scores before you A/B it.
  R1/R7 process reward — coverage (% of eval traces carrying a process_reward)
      and its distribution: proof the SLM-distillation loop actually sees a
      process signal rather than a flattened verdict.

Usage::

    python3.11 scripts/vprm_shadow_report.py --window 7d
    python3.11 scripts/vprm_shadow_report.py --db /path/state.db --json

Exits 0 always on a readable DB (an empty window is a valid, reported result);
exits 2 only on an unusable DB / bad arguments.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sqlite3
import sys

# Signals the eval node writes into reward_vector_json (see execute_handlers.py
# _handle_eval: reward_vector). Kept here so the report and the writer can't
# silently drift on key names.
COHERENCE_KEY = "coherence"
DECOMPOSED_KEY = "decomposed"
PROCESS_KEY = "process_reward"
SUBPROBLEM_KEY = "subproblem_reward"
REQUIRED_COLS = ("reward_source", "reward_value", "reward_vector_json",
                 "reviewer_verdict", "created_at")


def _parse_window(spec: str) -> _dt.timedelta:
    """'7d' / '24h' / '90m' / '3600s' → timedelta. Bare integer → days."""
    s = (spec or "").strip().lower()
    if not s:
        return _dt.timedelta(days=7)
    unit = s[-1]
    mult = {"d": "days", "h": "hours", "m": "minutes", "s": "seconds"}
    if unit in mult:
        return _dt.timedelta(**{mult[unit]: float(s[:-1])})
    return _dt.timedelta(days=float(s))  # bare number → days


def _cutoff_iso(window: _dt.timedelta) -> str:
    """Now-relative cutoff, formatted to match trace_store's created_at strings
    ('%Y-%m-%dT%H:%M:%S.000Z'). Computed from NOW so the window slides with the
    clock (never a hardcoded absolute date)."""
    cut = _dt.datetime.now(_dt.UTC) - window
    return cut.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _has_reward_columns(con: sqlite3.Connection) -> bool:
    cols = {r[1] for r in con.execute("PRAGMA table_info(execution_traces)")}
    return all(c in cols for c in REQUIRED_COLS)


def fetch_rows(con: sqlite3.Connection, cutoff_iso: str, limit: int) -> list[dict]:
    """Eval-sourced traces since the cutoff, newest first. reward_vector_json is
    decoded to a dict (garbled/empty → {})."""
    con.row_factory = sqlite3.Row
    sql = ("SELECT reward_source, reward_value, reviewer_verdict, "
           "reward_vector_json, created_at, run_id, task_class "
           "FROM execution_traces "
           "WHERE reward_source LIKE 'eval%' AND created_at >= ? "
           "ORDER BY created_at DESC LIMIT ?")
    out: list[dict] = []
    for r in con.execute(sql, (cutoff_iso, limit)):
        row = dict(r)
        vec = row.get("reward_vector_json")
        try:
            row["vector"] = json.loads(vec) if isinstance(vec, str) and vec else {}
        except (ValueError, TypeError):
            row["vector"] = {}
        if not isinstance(row["vector"], dict):
            row["vector"] = {}
        out.append(row)
    return out


def _stats(xs: list[float]) -> dict | None:
    if not xs:
        return None
    return {"n": len(xs), "mean": sum(xs) / len(xs),
            "min": min(xs), "max": max(xs)}


def summarize(rows: list[dict]) -> dict:
    """Pure aggregation over fetched rows (no DB). Returns the report dict the CLI
    renders. Every rate has an explicit denominator so an empty window reads as
    'no data', never a divide-by-zero or a fake 0.0."""
    n = len(rows)
    coh_vals, proc_vals, sub_vals, spreads = [], [], [], []
    incoherent = 0
    verdicts: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for row in rows:
        vec = row["vector"]
        by_source[row.get("reward_source") or "?"] = (
            by_source.get(row.get("reward_source") or "?", 0) + 1)
        v = row.get("reviewer_verdict") or "(none)"
        verdicts[v] = verdicts.get(v, 0) + 1

        coh = vec.get(COHERENCE_KEY)
        if isinstance(coh, (int, float)):
            coh_vals.append(float(coh))
            if float(coh) < 1.0:
                incoherent += 1
        proc = vec.get(PROCESS_KEY)
        if isinstance(proc, (int, float)):
            proc_vals.append(float(proc))
        sub = vec.get(SUBPROBLEM_KEY)
        if isinstance(sub, (int, float)):
            sub_vals.append(float(sub))
        dec = vec.get(DECOMPOSED_KEY)
        rv = row.get("reward_value")
        if isinstance(dec, (int, float)) and isinstance(rv, (int, float)):
            spreads.append(abs(float(dec) - float(rv)))

    return {
        "n_eval_traces": n,
        "by_source": by_source,
        "verdicts": verdicts,
        # R2 — the default-ON coherence gate's reach.
        "coherence": {
            "stats": _stats(coh_vals),
            "n_scored": len(coh_vals),
            "incoherent": incoherent,
            "incoherence_rate": (incoherent / len(coh_vals)) if coh_vals else None,
        },
        # R3 — how much promoting decomposed-as-primary would move the score.
        "decomposed_spread": {
            "stats": _stats(spreads),
            "n_pairs": len(spreads),
        },
        # R1/R7 — is a process signal actually present for distillation?
        "process_reward": {
            "stats": _stats(proc_vals),
            "coverage": (len(proc_vals) / n) if n else None,
        },
        "subproblem_reward": {
            "stats": _stats(sub_vals),
            "coverage": (len(sub_vals) / n) if n else None,
        },
    }


def _fmt_stats(s: dict | None) -> str:
    if not s:
        return "no data"
    return (f"n={s['n']} mean={s['mean']:.3f} "
            f"min={s['min']:.3f} max={s['max']:.3f}")


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def render_text(rep: dict, window_spec: str) -> str:
    lines = [f"VPRM shadow report — eval traces in the last {window_spec}",
             f"  eval traces:         {rep['n_eval_traces']}"]
    if rep["n_eval_traces"] == 0:
        lines.append("  (no eval-sourced traces in this window — nothing to report)")
        return "\n".join(lines)
    src = ", ".join(f"{k}={v}" for k, v in sorted(rep["by_source"].items()))
    vs = ", ".join(f"{k}={v}" for k, v in sorted(rep["verdicts"].items()))
    lines += [f"  by source:           {src}",
              f"  verdicts:            {vs}",
              "",
              "  R2  coherence (default-ON gate reach)",
              f"      distribution:    {_fmt_stats(rep['coherence']['stats'])}",
              f"      incoherent:      {rep['coherence']['incoherent']}"
              f"/{rep['coherence']['n_scored']}"
              f"  → incoherence rate {_pct(rep['coherence']['incoherence_rate'])}",
              "",
              "  R3  decomposed vs backbone (score movement if promoted primary)",
              f"      |decomposed-reward_value|: {_fmt_stats(rep['decomposed_spread']['stats'])}",
              "",
              "  R1/R7  process reward (signal for SLM distillation)",
              f"      distribution:    {_fmt_stats(rep['process_reward']['stats'])}",
              f"      coverage:        {_pct(rep['process_reward']['coverage'])}"
              f" of eval traces carry a process_reward",
              f"      subproblem:      {_fmt_stats(rep['subproblem_reward']['stats'])}"
              f"  (coverage {_pct(rep['subproblem_reward']['coverage'])})"]
    return "\n".join(lines)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="vprm_shadow_report",
        description="Read-only shadow report of the R1-R7 process-reward signals.")
    p.add_argument("--db", default=os.environ.get("MINI_ORK_DB"),
                   help="SQLite DB path (default: $MINI_ORK_DB).")
    p.add_argument("--window", default="7d",
                   help="Relative window: 7d / 24h / 90m / 3600s (default 7d).")
    p.add_argument("--limit", type=int, default=5000,
                   help="Max traces to scan (default 5000).")
    p.add_argument("--json", action="store_true",
                   help="Emit the report as one JSON line instead of a table.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.db:
        print("error: no DB — pass --db or set MINI_ORK_DB", file=sys.stderr)
        return 2
    if not os.path.exists(args.db):
        print(f"error: DB not found: {args.db}", file=sys.stderr)
        return 2
    con = sqlite3.connect(args.db)
    con.execute("PRAGMA busy_timeout=5000")
    if not _has_reward_columns(con):
        con.close()
        print("error: this DB predates the 0042 reward columns "
              "(no reward_vector_json) — run `mini-ork` migrations first.",
              file=sys.stderr)
        return 2
    window = _parse_window(args.window)
    rows = fetch_rows(con, _cutoff_iso(window), args.limit)
    con.close()
    rep = summarize(rows)
    if args.json:
        print(json.dumps({"window": args.window, **rep}))
    else:
        print(render_text(rep, args.window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
