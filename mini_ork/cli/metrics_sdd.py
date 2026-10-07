"""``mini-ork metrics sdd`` — the SDD plan's baselines, read-only.

Every later SDD epic (docs/plans/2026-10-07-sdd-mechanisms-for-mini-ork.md)
reports these before and after it lands. Definitions match the plan's
baseline queries:

- publish rate, $/published, share of spend on unpublished runs (task_runs)
- vacuous verify: ``execution_traces`` rows with ``trace_id LIKE 'tr-verify-%'``
  whose status is ``vacuous``, overall and per month
- reviewer failures: ``trace_id LIKE 'tr-reviewer-%'`` rows with status ``failure``
- verify passed but run failed, and published with a vacuous verify (distinct runs)
- non-terminal runs
- run dirs: planner failure (``plan-failure-*``), ``needs_answers``
  (``run_profile.json`` → ``profile_status``), empty ``review-diff.patch``,
  ``implementer-summary.json`` claiming ``implemented`` with 0 files

Runs the reaper or a kill marked crashed (verdict CRASH, or ``reaped:`` /
``killed-by-user`` / ``dispatcher exited rc=`` notes) are their own bucket and
never count as verify-passed-run-failed: otherwise before/after numbers drift
as the reaper heals old rows.

Run dirs are not frozen by a DB snapshot (old dirs get garbage-collected), so
``--write-run-dir-baseline FILE`` freezes counts + run-id lists, and
``--run-dir-baseline FILE`` reports from that file instead of the live dirs.

The DB is never written: it is opened ``immutable=1`` when no ``-wal`` file is
next to it (a snapshot), else ``mode=ro``.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA_ID = "mini-ork.metrics.sdd/v1"
RUN_DIR_SCHEMA_ID = "mini-ork.metrics.sdd.run-dirs/v1"
TERMINAL = ("published", "failed", "rolled_back")
# NULL-safe on purpose: verdict/notes are NULL on most rows, and NOT (NULL OR ...)
# is NULL, which would drop every ordinary run from a `NOT crashed` filter.
_CRASH_SQL = ("(coalesce(t.verdict,'') = 'CRASH' OR coalesce(t.notes,'') LIKE 'reaped:%' "
              "OR coalesce(t.notes,'') LIKE '%; reaped:%' OR coalesce(t.notes,'') LIKE 'killed-by-user%' "
              "OR coalesce(t.notes,'') LIKE 'dispatcher exited rc=%')")


def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 4) if den else None


def connect_readonly(db: str) -> sqlite3.Connection:
    mode = "mode=ro" if os.path.exists(f"{db}-wal") else "immutable=1"
    con = sqlite3.connect(f"file:{db}?{mode}", uri=True)
    con.row_factory = sqlite3.Row
    return con


def db_metrics(con: sqlite3.Connection) -> dict[str, Any]:
    r = con.execute(
        "SELECT count(*) n, coalesce(sum(status='published'),0) pub, coalesce(sum(cost_usd),0) cost, "
        "coalesce(sum(CASE WHEN status<>'published' THEN cost_usd ELSE 0 END),0) unpub, "
        "coalesce(sum(CASE WHEN status='published' THEN cost_usd ELSE 0 END),0) pubcost, "
        f"coalesce(sum(status NOT IN {TERMINAL}),0) nonterm FROM task_runs").fetchone()
    crash = con.execute(f"SELECT count(*) n, coalesce(sum(cost_usd),0) cost FROM task_runs t WHERE {_CRASH_SQL}").fetchone()
    v = con.execute("SELECT count(*) n, coalesce(sum(status='vacuous'),0) vac FROM execution_traces "
                    "WHERE trace_id LIKE 'tr-verify-%'").fetchone()
    monthly = [{"month": m["month"], "traces": m["n"], "vacuous": m["vac"], "vacuous_rate": _ratio(m["vac"], m["n"])}
               for m in con.execute("SELECT substr(created_at,1,7) month, count(*) n, sum(status='vacuous') vac "
                                    "FROM execution_traces WHERE trace_id LIKE 'tr-verify-%' GROUP BY 1 ORDER BY 1")]
    rv = con.execute("SELECT count(*) n, coalesce(sum(status='failure'),0) fail FROM execution_traces "
                     "WHERE trace_id LIKE 'tr-reviewer-%'").fetchone()
    vprf = {row["recipe"]: row["n"] for row in con.execute(
        "SELECT coalesce(t.recipe,'(none)') recipe, count(DISTINCT t.id) n FROM task_runs t "
        "JOIN execution_traces e ON e.run_id = t.id AND e.trace_id LIKE 'tr-verify-%' AND e.status = 'success' "
        f"WHERE t.status IN ('failed','rolled_back') AND NOT {_CRASH_SQL} GROUP BY 1 ORDER BY 2 DESC, 1")}
    pvac = {row["recipe"]: row["n"] for row in con.execute(
        "SELECT coalesce(t.recipe,'(none)') recipe, count(DISTINCT t.id) n FROM task_runs t "
        "JOIN execution_traces e ON e.run_id = t.id AND e.trace_id LIKE 'tr-verify-%' AND e.status = 'vacuous' "
        "WHERE t.status = 'published' GROUP BY 1 ORDER BY 2 DESC, 1")}
    per_recipe = [{
        "recipe": p["recipe"], "runs": p["n"], "published": p["pub"], "publish_rate": _ratio(p["pub"], p["n"]),
        "cost_usd": round(p["cost"], 2), "unpublished_cost_share": _ratio(p["unpub"], p["cost"]),
        "cost_per_published": round(p["cost"] / p["pub"], 2) if p["pub"] else None,
    } for p in con.execute(
        "SELECT coalesce(recipe,'(none)') recipe, count(*) n, sum(status='published') pub, coalesce(sum(cost_usd),0) cost, "
        "coalesce(sum(CASE WHEN status<>'published' THEN cost_usd ELSE 0 END),0) unpub "
        "FROM task_runs GROUP BY 1 ORDER BY cost DESC, recipe")]
    return {
        "runs": {
            "total": r["n"], "published": r["pub"], "publish_rate": _ratio(r["pub"], r["n"]),
            "non_terminal": r["nonterm"], "cost_usd": round(r["cost"], 2),
            "unpublished_cost_usd": round(r["unpub"], 2), "unpublished_cost_share": _ratio(r["unpub"], r["cost"]),
            "cost_per_published": round(r["pubcost"] / r["pub"], 2) if r["pub"] else None,
        },
        "crashed": {"runs": crash["n"], "cost_usd": round(crash["cost"], 2)},
        "verify": {
            "traces": v["n"], "vacuous": v["vac"], "vacuous_rate": _ratio(v["vac"], v["n"]), "monthly": monthly,
            "passed_run_failed": {"runs": sum(vprf.values()), "by_recipe": vprf},
            "published_with_vacuous": {"runs": sum(pvac.values()), "by_recipe": pvac},
        },
        "reviewer": {"traces": rv["n"], "failures": rv["fail"], "failure_rate": _ratio(rv["fail"], rv["n"])},
        "per_recipe": per_recipe,
    }


def scan_run_dirs(runs_dir: Path) -> dict[str, Any]:
    """Counts + run-id lists from the live run dirs."""
    ids: dict[str, list[str]] = {k: [] for k in (
        "dirs", "plan_failure", "run_profile", "needs_answers", "review_diff", "empty_diff",
        "implementer_summary", "implemented_zero_files")}
    for d in sorted(p for p in runs_dir.iterdir() if p.is_dir()) if runs_dir.is_dir() else []:
        rid = d.name
        ids["dirs"].append(rid)
        if any(d.glob("plan-failure-*")):
            ids["plan_failure"].append(rid)
        prof = d / "run_profile.json"
        if prof.is_file():
            ids["run_profile"].append(rid)
            if (_load(prof) or {}).get("profile_status") == "needs_answers":
                ids["needs_answers"].append(rid)
        diff = d / "review-diff.patch"
        if diff.is_file():
            ids["review_diff"].append(rid)
            if diff.stat().st_size == 0:
                ids["empty_diff"].append(rid)
        summ = d / "implementer-summary.json"
        if summ.is_file():
            ids["implementer_summary"].append(rid)
            s = _load(summ) or {}
            if s.get("status") == "implemented" and not s.get("files_changed"):
                ids["implemented_zero_files"].append(rid)
    return {"schema": RUN_DIR_SCHEMA_ID, "runs_dir": str(runs_dir), "frozen_at": int(time.time()),
            "counts": {k: len(v) for k, v in ids.items()}, "ids": ids}


def run_dir_metrics(snapshot: dict[str, Any], source: str) -> dict[str, Any]:
    c = snapshot["counts"]
    return {
        "source": source, "dirs": c["dirs"],
        "plan_failure": c["plan_failure"], "plan_failure_rate": _ratio(c["plan_failure"], c["dirs"]),
        "run_profiles": c["run_profile"], "needs_answers": c["needs_answers"],
        "needs_answers_rate": _ratio(c["needs_answers"], c["run_profile"]),
        "review_diffs": c["review_diff"], "empty_diffs": c["empty_diff"],
        "empty_diff_rate": _ratio(c["empty_diff"], c["review_diff"]),
        "implementer_summaries": c["implementer_summary"], "implemented_zero_files": c["implemented_zero_files"],
        "implemented_zero_files_rate": _ratio(c["implemented_zero_files"], c["implementer_summary"]),
    }


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def collect(db: str, *, runs_dir: Path | None, run_dir_baseline: Path | None) -> dict[str, Any]:
    con = connect_readonly(db)
    try:
        out: dict[str, Any] = {"schema": SCHEMA_ID, "db": db, **db_metrics(con)}
    finally:
        con.close()
    if run_dir_baseline is not None:
        frozen = json.loads(run_dir_baseline.read_text(encoding="utf-8"))
        if frozen.get("schema") != RUN_DIR_SCHEMA_ID:
            raise ValueError(f"{run_dir_baseline} is not a {RUN_DIR_SCHEMA_ID} file")
        out["run_dirs"] = run_dir_metrics(frozen, f"frozen:{run_dir_baseline}")
    elif runs_dir is not None:
        out["run_dirs"] = run_dir_metrics(scan_run_dirs(runs_dir), f"live:{runs_dir}")
    else:
        out["run_dirs"] = None
    return out


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def render_markdown(m: dict[str, Any]) -> str:
    r, v, rv, c = m["runs"], m["verify"], m["reviewer"], m["crashed"]
    cpp = "—" if r["cost_per_published"] is None else f"${r['cost_per_published']:.2f}"
    lines = [
        f"# SDD baselines — `{m['db']}`", "",
        "| Metric | Value |", "|---|---|",
        f"| Runs published | {r['published']}/{r['total']} ({_pct(r['publish_rate'])}) |",
        f"| Spend on unpublished runs | {_pct(r['unpublished_cost_share'])} of ${r['cost_usd']:.2f} |",
        f"| $ per published run | {cpp} |",
        f"| Non-terminal runs | {r['non_terminal']} |",
        f"| Crashed / reaped (own bucket) | {c['runs']} runs, ${c['cost_usd']:.2f} |",
        f"| Verify `vacuous` | {v['vacuous']}/{v['traces']} ({_pct(v['vacuous_rate'])}) |",
        f"| Verify passed, run failed (excl. crashed) | {v['passed_run_failed']['runs']} runs |",
        f"| Published with a vacuous verify | {v['published_with_vacuous']['runs']} runs |",
        f"| Reviewer-stage failures | {rv['failures']}/{rv['traces']} ({_pct(rv['failure_rate'])}) |",
    ]
    rd = m.get("run_dirs")
    if rd:
        lines += [
            f"| Planner failure (run dirs) | {rd['plan_failure']}/{rd['dirs']} ({_pct(rd['plan_failure_rate'])}) |",
            f"| `needs_answers` | {rd['needs_answers']}/{rd['run_profiles']} ({_pct(rd['needs_answers_rate'])}) |",
            f"| Empty review diff | {rd['empty_diffs']}/{rd['review_diffs']} ({_pct(rd['empty_diff_rate'])}) |",
            f"| \"Implemented\" with 0 files | {rd['implemented_zero_files']}/{rd['implementer_summaries']} "
            f"({_pct(rd['implemented_zero_files_rate'])}) |",
            "", f"Run dirs: {rd['source']}",
        ]
    lines += ["", "| Month | Verify traces | Vacuous |", "|---|---:|---:|"]
    lines += [f"| {x['month']} | {x['traces']} | {x['vacuous']} ({_pct(x['vacuous_rate'])}) |" for x in v["monthly"]]
    return "\n".join(lines) + "\n"


def _default_db() -> str:
    if os.environ.get("MINI_ORK_DB"):
        return os.environ["MINI_ORK_DB"]
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    return os.path.join(home, "state.db")


def main(argv: list[str] | None = None, *, stdout=None, stderr=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    p = argparse.ArgumentParser(prog="mini-ork metrics sdd",
                                description="The SDD plan's baselines, read-only.")
    p.add_argument("--db", default=None, help="state.db (default: $MINI_ORK_DB, else <home>/state.db)")
    p.add_argument("--runs-dir", default=None, help="run dirs to scan (default: next to the DB)")
    p.add_argument("--run-dir-baseline", default=None, help="report run-dir metrics from this frozen file")
    p.add_argument("--write-run-dir-baseline", default=None, help="freeze the live run-dir scan to this file")
    p.add_argument("--no-run-dirs", action="store_true", help="DB metrics only")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    db = a.db or _default_db()
    if not os.path.isfile(db):
        err.write(f"mini-ork metrics sdd: no state.db at {db}\n")
        return 1
    runs_dir = Path(a.runs_dir) if a.runs_dir else Path(db).resolve().parent / "runs"
    if a.write_run_dir_baseline:
        frozen = scan_run_dirs(runs_dir)
        Path(a.write_run_dir_baseline).write_text(json.dumps(frozen, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        err.write(f"froze {frozen['counts']['dirs']} run dirs → {a.write_run_dir_baseline}\n")
    m = collect(db, runs_dir=None if a.no_run_dirs else runs_dir,
                run_dir_baseline=Path(a.run_dir_baseline) if a.run_dir_baseline else None)
    out.write(json.dumps(m, sort_keys=True) + "\n" if a.json else render_markdown(m))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
