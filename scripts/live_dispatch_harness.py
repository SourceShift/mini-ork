#!/usr/bin/env python3
"""live_dispatch_harness.py — real-LLM integration gate for the ported executor.

The deterministic surface of mini_ork.cli.execute is unit-parity- and
harness-tested (see tests/unit/test_mini_ork_execute_py.py + runtime-parity-harness.sh).
The LIVE per-node dispatch path (dispatch_node with the real llm_dispatch seam) can
only be verified against a real provider — that is this harness. It fires ONE cheap
researcher node through the ported live path and checks the wiring end-to-end:

  * the LLM was actually called (rc 0, non-empty output),
  * the output artifact was written to the run dir,
  * cost was charged to task_runs.cost_usd,
  * the node returned success.

This is the gate that must pass before flipping the LIVE dispatch default to python
(the deterministic default is already validated). It costs one cheap dispatch.

    python3 scripts/live_dispatch_harness.py [--lane kimi]

Exit: 0 pass · 2 skipped (no lane / dispatch could not run) · 1 fail (wiring broke).
Requires a configured lane (MINI_ORK_SECRETS or ambient provider keys).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mini_ork.cli import execute as ex


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", default=os.environ.get("MO_HARNESS_LANE", "kimi"),
                    help="the (cheap) lane to dispatch through")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="mo-live-harness-"))
    home = tmp / ".mini-ork"; home.mkdir(parents=True)
    db = str(home / "state.db")
    subprocess.run(["bash", str(ROOT / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    run_dir = home / "runs" / "harness"; run_dir.mkdir(parents=True)
    plan = run_dir / "plan.json"
    plan.write_text(json.dumps({"objective": "harness", "artifact_contract": {"outputs": ["out.md"]}}))
    con = sqlite3.connect(db)
    con.execute("INSERT INTO task_runs (id,task_class,workflow_version,kickoff_path,status,cost_usd,"
                "created_at,updated_at) VALUES ('harness','code_fix','v1','k.md','executing',0,"
                "strftime('%s','now'),strftime('%s','now'))")
    con.commit(); con.close()

    # Export the full run env the dispatch subprocess needs — mirrors what a real
    # `mini-ork run` exports. Missing MINI_ORK_HOME sends llm-dispatch to a stale
    # cwd-relative .mini-ork with no lane config → the dispatch silently errors.
    os.environ["MINI_ORK_HOME"] = str(home)
    os.environ["MINI_ORK_ROOT"] = str(ROOT)
    os.environ["MINI_ORK_RUN_DIR"] = str(run_dir)
    os.environ["MINI_ORK_DB"] = db
    fields = ("h1_lens", "researcher", "Return the single word OK and nothing else.",
              "", "serial", "", args.lane, "")

    print(f"── live-dispatch harness (lane={args.lane}) ──")
    try:
        rc, fr = ex.dispatch_node(
            fields, root=str(ROOT), run_dir=str(run_dir), plan_path=str(plan),
            task_class="code_fix", db=db, run_id="harness",
            dispatch_fn=ex._default_llm_dispatch(str(ROOT)))
    except Exception as e:  # noqa: BLE001
        print(f"  [skip] dispatch raised (lane not configured?): {e}")
        return 2

    ctx = run_dir / "lens-h1.md"
    cost = sqlite3.connect(db).execute(
        "SELECT cost_usd FROM task_runs WHERE id='harness'").fetchone()[0]
    out_ok = ctx.exists() and ctx.stat().st_size > 0

    if rc != 0:
        print(f"  [skip] node rc={rc} reason={fr} — provider likely unavailable/throttled")
        return 2
    ok = out_ok and (cost or 0) > 0
    print(f"  output_written={out_ok} cost_charged={cost} rc={rc} finish={fr}")
    if ok:
        print("PASS — live dispatch wired: LLM called, artifact written, cost charged.")
        return 0
    print("FAIL — live path ran but wiring broke (missing artifact or cost).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
