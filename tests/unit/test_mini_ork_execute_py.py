"""Parity gate: mini_ork.ported.mini_ork_execute helper layer vs bin/mini-ork-execute.

bin/mini-ork-execute is a CLI (runs setup at top level), so we EXTRACT each pure
helper's definition by name, source that in isolation, and compare its output to
the port. Covers the deterministic helper layer (reward/lane/chain/finish-reason/
code-region); the orchestration core (_dispatch_node + DAG loop) is a separate,
harsh-critic-gated increment.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_execute as ex  # noqa: E402

BIN = REPO / "bin" / "mini-ork-execute"
_SRC = BIN.read_text().splitlines()


def _extract(name: str) -> str:
    """Pull `<name>() { ... }` (closing brace at column 0) from the CLI."""
    start = None
    for i, ln in enumerate(_SRC):
        if re.match(rf"^{re.escape(name)}\(\) *\{{", ln):
            start = i
            break
    assert start is not None, f"function {name} not found"
    body = [_SRC[start]]
    for ln in _SRC[start + 1:]:
        body.append(ln)
        if ln == "}":
            break
    return "\n".join(body)


def _call(name, *args, env=None):
    fn = _extract(name)
    script = f'{fn}\n{name} "$@"'
    return subprocess.run(["bash", "-c", script, "_", *map(str, args)],
                          capture_output=True, text=True,
                          env={**os.environ, **(env or {})}).stdout.strip()


def test_reward_from_status_parity():
    cases = [("", "approve"), ("", "needs_revision"), ("published", ""), ("failed", ""),
             ("reviewing", ""), ("success", "pass"), ("", "ESCALATE"), ("PUBLISHED", ""),
             ("weird", "weird")]
    for status, verdict in cases:
        rb = _call("_mo_reward_from_status", status, verdict)
        rp = ex.reward_from_status(status, verdict)
        assert rb == rp, f"({status!r},{verdict!r}): bash={rb!r} py={rp!r}"


def test_dispatch_chain_parity():
    env = {"MO_FALLBACK_CODING": "minimax,codex,sonnet", "MO_FALLBACK_REVIEW": "opus,kimi,sonnet"}
    for nt, lead in [("implementer", "minimax"), ("implementer", "glm"), ("reviewer", "opus"),
                     ("reviewer", "sonnet"), ("verifier", "kimi"), ("unknown_role", "codex"),
                     ("planner", "codex")]:
        rb = _call("_mo_dispatch_chain", nt, lead, env=env)
        rp = ex.dispatch_chain(nt, lead)
        assert rb == rp, f"({nt},{lead}): bash={rb!r} py={rp!r}"


def test_learning_static_lane_parity():
    env = {"MO_FRONTIER_LANE": "opus_lens", "MO_CHEAP_LANE": "kimi_lens"}
    for nt, lane in [("reviewer", "reviewer"), ("researcher", "researcher"),
                     ("implementer", "implementer"), ("planner", "planner"),
                     ("researcher", "glm_lens"), ("reviewer", "custom_lane")]:
        rb = _call("_mo_learning_static_lane", nt, lane, env=env)
        rp = ex.learning_static_lane(nt, lane)
        assert rb == rp, f"({nt},{lane}): bash={rb!r} py={rp!r}"


def test_finish_reason_parity():
    for rc, text in [(124, ""), (43, ""), (1, "lane_fuse_open here"),
                     (1, "cost_circuit_open spent"), (2, "generic error"), (0, "")]:
        rb = _call("_mo_finish_reason_for_failure", rc, text)
        rp = ex.finish_reason_for_failure(rc, text)
        assert rb == rp, f"({rc},{text!r}): bash={rb!r} py={rp!r}"


def test_infer_code_region_parity(tmp_path):
    import json
    payloads = [
        json.dumps({"files_written": ["src/foo.py", "src/bar.py"]}),
        json.dumps({"files_written": ["README.md"]}),
        json.dumps({"files_written": []}),
        json.dumps({"files_written": "[\"lib/x.sh\"]"}),          # json-string form
        json.dumps({"files_written": ["./pkg/mod.py"]}),
        json.dumps({"other": 1}),
        "not json",
    ]
    # run both with MINI_ORK_RUN_DIR unset + a shared cwd so relative paths match
    env = {k: v for k, v in os.environ.items() if k not in ("MINI_ORK_RUN_DIR", "RUN_DIR")}
    for p in payloads:
        rb = subprocess.run(["bash", "-c", f'{_extract("_mo_infer_trace_code_region")}\n'
                             '_mo_infer_trace_code_region "$1"', "_", p],
                            capture_output=True, text=True, cwd=tmp_path, env=env).stdout.strip()
        old = dict(os.environ); os.environ.clear(); os.environ.update(env)
        cwd = os.getcwd(); os.chdir(tmp_path)
        try:
            rp = ex.infer_trace_code_region(p)
        finally:
            os.chdir(cwd); os.environ.clear(); os.environ.update(old)
        assert rb == rp, f"{p!r}: bash={rb!r} py={rp!r}"


# ── GRPO writeback parity (DB-deterministic) ──

def _seed_db(tmp, name):
    home = tmp / name / ".mini-ork"; home.mkdir(parents=True)
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    return db


def _sql(db, s):
    return subprocess.run(["sqlite3", db, s], capture_output=True, text=True)


def _seed_traces(db):
    traces = [
        ("t1", "opus_lens", "success", 1.0, 1000), ("t2", "minimax_lens", "failed", 0.5, 800),
        ("t3", "kimi_lens", "success", 0.2, 500), ("t4", "opus_lens", "failed", 1.1, 900),
    ]
    for tid, av, status, cost, dur in traces:
        _sql(db, "INSERT INTO execution_traces (trace_id,run_id,workflow_version_id,agent_version_id,"
                 "task_class,prompt_version_hash,context_bundle_hash,tool_calls,files_read,"
                 "files_written,verifier_output,reviewer_verdict,cost_usd,duration_ms,"
                 "final_artifact_ref,status,created_at) VALUES "
                 f"('{tid}','r1','wf1','{av}','code_fix','ph','ch','[]','[]','[]',"
                 f"'{{\"node_type\":\"researcher\"}}','',{cost},{dur},'','{status}','2026-07-01T00:00:00Z');")


def _apm_rows(db):
    return _sql(db, "SELECT agent_version_id,role,task_class,runs_count,success_count,"
                    "printf('%.6f',avg_cost_usd),printf('%.6f',avg_duration_ms),"
                    "printf('%.6f',relative_advantage) FROM agent_performance_memory "
                    "ORDER BY agent_version_id;").stdout.strip()


def test_grpo_advantages_parity(tmp_path):
    db_b = _seed_db(tmp_path, "gb"); db_p = _seed_db(tmp_path, "gp")
    _seed_traces(db_b); _seed_traces(db_p)
    # halflife=0 disables recency drift so bash/py compute identical weights
    env = {"MO_LEARNING_HALFLIFE_DAYS": "0", "MINI_ORK_DB": db_b}
    nb = subprocess.run(["bash", "-c", f'{_extract("mo_learning_write_grpo_advantages")}\n'
                         'mo_learning_write_grpo_advantages', "_"],
                        capture_output=True, text=True, env={**os.environ, **env}).stdout.strip()
    old = dict(os.environ)
    os.environ.update({"MO_LEARNING_HALFLIFE_DAYS": "0"})
    try:
        np_ = ex.write_grpo_advantages(db_p)
    finally:
        os.environ.clear(); os.environ.update(old)
    assert nb == str(np_), f"count bash={nb} py={np_}"
    assert _apm_rows(db_b) == _apm_rows(db_p) and _apm_rows(db_p), "agent_performance_memory differs"


def test_grpo_sigma_zero_tiebreak_parity(tmp_path):
    # all-success same-reward group → std==0 → cost tiebreak path
    db_b = _seed_db(tmp_path, "sb"); db_p = _seed_db(tmp_path, "sp")
    for db in (db_b, db_p):
        for tid, av, cost in [("a", "opus_lens", 2.0), ("b", "kimi_lens", 0.1)]:
            _sql(db, "INSERT INTO execution_traces (trace_id,run_id,workflow_version_id,"
                     "agent_version_id,task_class,prompt_version_hash,context_bundle_hash,tool_calls,"
                     "files_read,files_written,verifier_output,reviewer_verdict,cost_usd,duration_ms,"
                     "final_artifact_ref,status,created_at) VALUES "
                     f"('{tid}','r','w','{av}','code_fix','p','c','[]','[]','[]',"
                     f"'{{\"node_type\":\"impl\"}}','',{cost},100,'','success','2026-07-01T00:00:00Z');")
    env = {"MO_LEARNING_HALFLIFE_DAYS": "0", "MINI_ORK_DB": db_b}
    subprocess.run(["bash", "-c", f'{_extract("mo_learning_write_grpo_advantages")}\n'
                    'mo_learning_write_grpo_advantages', "_"],
                   capture_output=True, text=True, env={**os.environ, **env})
    old = dict(os.environ); os.environ.update({"MO_LEARNING_HALFLIFE_DAYS": "0"})
    try:
        ex.write_grpo_advantages(db_p)
    finally:
        os.environ.clear(); os.environ.update(old)
    assert _apm_rows(db_b) == _apm_rows(db_p)
    # cheaper lane (kimi) got the positive bump
    kimi = _sql(db_p, "SELECT relative_advantage FROM agent_performance_memory "
                      "WHERE agent_version_id='kimi_lens';").stdout.strip()
    assert float(kimi) > 0


def test_conductor_outcomes_parity(tmp_path):
    db_b = _seed_db(tmp_path, "cb"); db_p = _seed_db(tmp_path, "cp")
    for db in (db_b, db_p):
        _sql(db, "INSERT INTO epics (id,title,status) VALUES ('e1','E1','done'),('e2','E2','escalated');")
        _sql(db, "INSERT INTO conductor_decisions (decided_at,epic_id,task_class,outcome) VALUES "
                 "(1,'e1','x','pending'),(1,'e2','x','pending');")
    nb = subprocess.run(["bash", "-c", f'{_extract("mo_learning_update_conductor_outcomes")}\n'
                         'mo_learning_update_conductor_outcomes', "_"],
                        capture_output=True, text=True,
                        env={**os.environ, "MINI_ORK_DB": db_b}).stdout.strip()
    np_ = ex.learning_update_conductor_outcomes(db_p)
    assert nb == str(np_) == "2"
    q = "SELECT epic_id,outcome,realized_score FROM conductor_decisions ORDER BY epic_id;"
    assert _sql(db_b, q).stdout == _sql(db_p, q).stdout


# ── orchestration backbone parity (NODE_IDS + --dry-run) ──

def _py_blocks():
    lines = BIN.read_text().splitlines()
    blocks, cur = [], None
    for ln in lines:
        if cur is None and re.search(r"<<'PY'", ln):
            cur = []
        elif cur is not None and ln.strip() == "PY":
            blocks.append("\n".join(cur)); cur = None
        elif cur is not None:
            cur.append(ln)
    wf = next(b for b in blocks if "requires_capabilities" in b and 'wf.get("nodes"' in b)
    plan = next(b for b in blocks if "decomposition" in b and "wf_by_name" in b)
    return wf, plan


_WF = """dispatch_mode: serial
nodes:
  - name: plan1
    type: planner
    description: make a plan
    model_lane: opus_lens
  - name: res1
    type: researcher
    description: research it
    model_lane: kimi_lens
    dispatch_mode: parallel
  - name: impl1
    type: implementer
    description: build it
  - name: rev1
    type: reviewer
    description: review it
    verifier_ref: verifiers/check.sh
  - name: rb1
    type: rollback
    description: undo
"""

_PLAN = json.dumps({"decomposition": [
    {"id": "res1", "node_type": "researcher", "description": "research"},
    {"id": "impl1", "node_type": "implementer", "description": "build"},
    {"id": "bad", "description": "no type defaults to implementer"},
]})


def _write(tmp, name, content):
    p = tmp / name; p.write_text(content); return str(p)


def test_nodes_from_workflow_parity(tmp_path):
    wf = _write(tmp_path, "wf.yaml", _WF)
    blk = tmp_path / "wfblk.py"; blk.write_text(_py_blocks()[0])
    rb = subprocess.run(["python3", str(blk), wf], capture_output=True, text=True).stdout
    rp = "\n".join(ex.nodes_from_workflow(wf)) + ("\n" if ex.nodes_from_workflow(wf) else "")
    assert rb == rp, f"BASH:{rb!r}\nPY:{rp!r}"


def test_nodes_from_plan_parity(tmp_path):
    wf = _write(tmp_path, "wf.yaml", _WF)
    plan = _write(tmp_path, "plan.json", _PLAN)
    blk = tmp_path / "planblk.py"; blk.write_text(_py_blocks()[1])
    rb = subprocess.run(["python3", str(blk), plan], capture_output=True, text=True,
                        env={**os.environ, "WORKFLOW_PATH": wf}).stdout
    rp = "\n".join(ex.nodes_from_plan(plan, wf)) + ("\n" if ex.nodes_from_plan(plan, wf) else "")
    assert rb == rp, f"BASH:{rb!r}\nPY:{rp!r}"


def _dispatch_lines(text):
    return [ln for ln in text.splitlines()
            if ln.startswith("[dry-run] would dispatch") or "[skip] rollback" in ln]


def test_dry_run_end_to_end_parity(tmp_path):
    wf = _write(tmp_path, "wf.yaml", _WF)
    home = tmp_path / ".mini-ork"; home.mkdir()
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    run_dir = home / "runs" / "run-x"; run_dir.mkdir(parents=True)
    plan = _write(tmp_path, str(run_dir / "plan.json").replace(str(tmp_path) + "/", ""), _PLAN) \
        if False else str(run_dir / "plan.json")
    Path(plan).write_text(_PLAN)
    env = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_WORKFLOW": wf,
           "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db, "MINI_ORK_PLAN_PATH": plan,
           "MINI_ORK_TASK_CLASS": "code_fix", "MINI_ORK_DRY_RUN": "1"}
    cb = subprocess.run(["bash", str(BIN), "--dry-run"], capture_output=True, text=True, env=env)
    old = dict(os.environ); os.environ.clear(); os.environ.update(env)
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = ex.main(["--dry-run"], root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    assert cb.returncode == rc == 0, cb.stderr
    assert _dispatch_lines(cb.stdout) == _dispatch_lines(buf.getvalue()), \
        f"BASH:{_dispatch_lines(cb.stdout)}\nPY:{_dispatch_lines(buf.getvalue())}"
    # workflow source has 5 nodes; 4 dispatch + 1 rollback skip
    assert len(_dispatch_lines(buf.getvalue())) == 5
    assert any("rollback" in ln for ln in _dispatch_lines(buf.getvalue()))


def test_dry_run_node_type_filter(tmp_path):
    wf = _write(tmp_path, "wf.yaml", _WF)
    home = tmp_path / ".mini-ork"; home.mkdir()
    plan = str(home / "plan.json"); Path(plan).write_text(_PLAN)
    env = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_WORKFLOW": wf,
           "MINI_ORK_HOME": str(home), "MINI_ORK_PLAN_PATH": plan, "MINI_ORK_DRY_RUN": "1"}
    cb = subprocess.run(["bash", str(BIN), "--dry-run", "--node-type", "researcher"],
                        capture_output=True, text=True, env=env)
    old = dict(os.environ); os.environ.clear(); os.environ.update(env)
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            ex.main(["--dry-run", "--node-type", "researcher"], root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    assert _dispatch_lines(cb.stdout) == _dispatch_lines(buf.getvalue())
    assert len(_dispatch_lines(buf.getvalue())) == 1   # only res1
