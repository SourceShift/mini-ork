"""F5 regression: every completed trace must carry a reward + lane attribution.

Live-DB evidence (2026-09 audit): reward_g coverage collapsed Jun 0% -> Jul 62%
-> Aug 39% -> Sep 27% because (a) pipeline-stage writers (classify/plan/verify/
reflect) emitted bare status rows — no reward, no lane, no node_type, so both
advantage writebacks (which filter agent_version_id <> '' AND reward_g IS NOT
NULL) never saw them; (b) grade_run_reward overwrote per-node rewards with one
uniform rubric value, zeroing within-run lane differentiation (all 15 live
lane_region_advantage rows sat at exactly 0.0 / success_count 0); (c) the
region/domain advantage tables refreshed only inside `mini-ork reflect`, so
preferred_lane's first two hops rode July data while APM stayed fresh.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.trace_store import enrich_stage_trace, grade_run_reward


def _seed_db(tmp, name):
    home = tmp / name / ".mini-ork"
    home.mkdir(parents=True)
    db = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
        capture_output=True, text=True, check=True,
    )
    return db


def _sql(db, s):
    return subprocess.run(["sqlite3", db, s], capture_output=True, text=True)


def _insert(db, tid, *, run_id="r1", lane="opus_lens", status="success",
            reward_value=None, reward_g=None):
    """Insert an execution_traces row; reward cols only when given so NULL
    columns stay NULL (the fill-only rubric test depends on that)."""
    cols = ("trace_id,run_id,workflow_version_id,agent_version_id,task_class,"
            "prompt_version_hash,context_bundle_hash,tool_calls,files_read,"
            "files_written,verifier_output,reviewer_verdict,cost_usd,duration_ms,"
            "final_artifact_ref,status,created_at")
    vals = (f"'{tid}','{run_id}','wf1','{lane}','code_fix','ph','ch','[]','[]','[]',"
            f"'{{\"node_type\":\"researcher\"}}','',0,0,'','{status}',"
            f"'2026-09-01T00:00:00Z'")
    if reward_value is not None:
        cols += ",reward_value,reward_anchor,reward_g"
        vals += f",{reward_value},0.5,{reward_g}"
    r = _sql(db, f"INSERT INTO execution_traces ({cols}) VALUES ({vals});")
    assert r.returncode == 0, r.stderr


# ── enrich_stage_trace ──────────────────────────────────────────────────────


def test_enrich_stamps_node_type_reward_and_fresh_lane(tmp_path, monkeypatch):
    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    (run_dir / ".last-llm-lane").write_text("GLM-5.3")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_REWARD_STAMP", "1")
    monkeypatch.delenv("MO_DISPATCH_TIMEOUT", raising=False)

    p = enrich_stage_trace(
        {"trace_id": "t1", "task_class": "framework_edit", "status": "success"},
        node_type="planner")

    assert p["verifier_output"]["node_type"] == "planner"
    assert p["agent_version_id"] == "GLM-5.3"
    assert p["reward_value"] == 1.0
    assert p["reward_anchor"] == 0.5
    assert p["reward_direction"] == "higher_is_better"


def test_enrich_failure_status_and_verdict_veto(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.setenv("MO_REWARD_STAMP", "1")

    p = enrich_stage_trace({"status": "failure"}, node_type="verifier")
    assert p["reward_value"] == 0.0

    # Execution status is primary; the reviewer verdict can only veto a success.
    p2 = enrich_stage_trace({"status": "success"}, node_type="verifier",
                            verdict="needs_revision")
    assert p2["reward_value"] == 0.0


def test_enrich_skips_reward_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.setenv("MO_REWARD_STAMP", "0")
    p = enrich_stage_trace({"status": "success"}, node_type="classifier")
    assert "reward_value" not in p


def test_enrich_ignores_stale_lane_sidecar(tmp_path, monkeypatch):
    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    sidecar = run_dir / ".last-llm-lane"
    sidecar.write_text("GLM-5.3")
    stale = time.time() - 10_000
    os.utime(sidecar, (stale, stale))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_DISPATCH_TIMEOUT", "1500")
    monkeypatch.setenv("MO_REWARD_STAMP", "1")

    p = enrich_stage_trace({"status": "success"}, node_type="planner")
    assert "agent_version_id" not in p


def test_enrich_preserves_explicit_fields(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.setenv("MO_REWARD_STAMP", "1")
    p = enrich_stage_trace(
        {"status": "success", "agent_version_id": "opus_lens",
         "verifier_output": {"verdict": "partial"}, "reward_value": 0.42},
        node_type="verifier", verdict="partial")
    assert p["agent_version_id"] == "opus_lens"      # caller lane wins
    assert p["reward_value"] == 0.42                 # caller reward wins
    assert p["verifier_output"]["verdict"] == "partial"
    assert p["verifier_output"]["node_type"] == "verifier"  # merged, not clobbered


# ── grade_run_reward is fill-only ───────────────────────────────────────────


def test_grade_run_reward_fills_only_null_rewards(tmp_path, monkeypatch):
    db = _seed_db(tmp_path, "fillonly")
    _insert(db, "t-stamped", reward_value=0.95, reward_g=0.9)
    _insert(db, "t-blank")
    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    (run_dir / "rubric.json").write_text(json.dumps({"score": 6}))

    n = grade_run_reward(str(run_dir), "r1", db=db)

    assert n == 1  # only the reward-less row changed
    assert _sql(db, "SELECT printf('%.2f',reward_g) FROM execution_traces "
                    "WHERE trace_id='t-stamped';").stdout.strip() == "0.90"
    assert _sql(db, "SELECT printf('%.2f',reward_g) FROM execution_traces "
                    "WHERE trace_id='t-blank';").stdout.strip() == "0.50"
    assert _sql(db, "SELECT reward_source FROM execution_traces "
                    "WHERE trace_id='t-blank';").stdout.strip() == "rubric@v1"


# ── execute post-run recompute hook ─────────────────────────────────────────


def test_post_run_learning_recomputes_lane_advantages(tmp_path, monkeypatch):
    from mini_ork.cli import execute as ex
    from mini_ork import lane_router

    seen = []
    monkeypatch.setattr(lane_router, "recompute_advantages",
                        lambda since, db: seen.append((since, db)) or 0)
    monkeypatch.setenv("MO_LANE_ROUTER", "1")
    ex._post_run_learning(str(tmp_path / "no.db"), str(tmp_path), "r1")
    assert seen == [(0, str(tmp_path / "no.db"))]


def test_post_run_learning_respects_lane_router_off(tmp_path, monkeypatch):
    from mini_ork.cli import execute as ex
    from mini_ork import lane_router

    seen = []
    monkeypatch.setattr(lane_router, "recompute_advantages",
                        lambda since, db: seen.append((since, db)) or 0)
    monkeypatch.setenv("MO_LANE_ROUTER", "0")
    ex._post_run_learning(str(tmp_path / "no.db"), str(tmp_path), "r1")
    assert seen == []


# ── end-to-end writeback visibility ─────────────────────────────────────────


def test_enriched_stage_trace_is_visible_to_lane_recompute(tmp_path, monkeypatch):
    """The contract that actually starved the router: an enriched stage trace
    (lane + reward + node_type) must land in lane_region/domain advantage
    after recompute_advantages, where the old bare row was invisible."""
    from mini_ork import lane_router, trace_store

    db = _seed_db(tmp_path, "visible")
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.setenv("MO_REWARD_STAMP", "1")
    for i, (lane, status) in enumerate(
            (("glm-5.3", "success"), ("opus-4-8", "failure"),
             ("glm-5.3", "success"), ("opus-4-8", "success"))):
        payload = enrich_stage_trace(
            {"trace_id": f"tr-plan-{i}", "run_id": "r9",
             "task_class": "framework_edit", "status": status,
             "agent_version_id": lane, "objective_domain": "code-delivery"},
            node_type="planner")
        # Route through trace_write so reward_g is derived exactly as production.
        trace_store.trace_write(payload, db=db)

    upserted = lane_router.recompute_advantages(since=0, db=db)
    assert upserted >= 1
    rows = _sql(db, "SELECT COUNT(*) FROM lane_domain_advantage "
                    "WHERE node_type='planner';").stdout.strip()
    assert rows == "2"  # both lanes grouped under the planner slice


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
