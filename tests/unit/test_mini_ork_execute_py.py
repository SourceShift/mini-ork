"""Standalone golden contract for the Python-owned executor.

The pre-retirement migration verifier captured byte parity against Bash. These
tests preserve that verified public/helper surface without reading or executing
a retired implementation.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml
import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import execute as ex


@pytest.fixture(autouse=True)
def _isolate_run_environment(monkeypatch):
    for name in (
        "MINI_ORK_RUN_DIR", "MINI_ORK_RECIPE", "MINI_ORK_PLAN_PATH",
        "MINI_ORK_RUN_ID", "MO_TARGET_CWD", "MO_APPLY_IMPL_OUTPUT",
    ):
        monkeypatch.delenv(name, raising=False)

def test_reward_from_status_parity():
    cases = {
        ("", "approve"): "1.0", ("", "needs_revision"): "0.5",
        ("published", ""): "1.0", ("failed", ""): "0.0",
        ("reviewing", ""): "0.5", ("success", "pass"): "1.0",
        ("", "ESCALATE"): "0.5", ("PUBLISHED", ""): "1.0",
        ("weird", "weird"): "0.5",
    }
    for args, expected in cases.items():
        assert ex.reward_from_status(*args) == expected


# --- Anti-Goodhart reward anchor: status is primary, verdict only vetoes ---
# These tests encode the truth table from kickoffs/reward-execution-anchor.md.
# The prior judge-anchored wiring let a self-improving loop learn to GAME the
# reviewer instead of writing correct code; the fix re-anchors on verified
# execution status so a happy verdict cannot fabricate a positive on failed exec.


def test_failed_execution_beats_happy_judge():
    # Anti-Goodhart: judge approval cannot fabricate a positive on failed exec.
    for s in ("failure", "failed", "rolled_back", "blocked", "crash",
              "escalated", "reject"):
        assert ex.reward_from_status(s, "approve") == "0.0", \
            f"status={s!r} verdict='approve' must be 0.0 (exec failed)"
        assert ex.reward_from_status(s, "") == "0.0", \
            f"status={s!r} verdict='' must be 0.0 (exec failed)"


def test_judge_veto_zeros_pass():
    # Verifier veto downgrades a passed exec.
    for s in ("success", "published", "done", "pass"):
        for v in ("reject", "needs_revision", "request_changes", "escalate"):
            assert ex.reward_from_status(s, v) == "0.0", \
                f"status={s!r} verdict={v!r} must be 0.0 (veto)"


def test_passed_exec_no_verdict_one():
    # Verified success without a veto is the canonical positive reward.
    for s in ("success", "published", "done", "pass"):
        assert ex.reward_from_status(s, "") == "1.0", \
            f"status={s!r} verdict='' must be 1.0 (exec passed, no veto)"
        assert ex.reward_from_status(s, "approve") == "1.0", \
            f"status={s!r} verdict='approve' must be 1.0 (exec passed, approve)"
        assert ex.reward_from_status(s, "ok") == "1.0", \
            f"status={s!r} verdict='ok' must be 1.0 (exec passed, ok)"


def test_review_only_falls_back_to_verdict():
    # Empty status means "no execution signal" (review-only node); the verdict
    # decides. An approving verdict still yields 1.0 — the anti-Goodhart rule
    # forbids fabrication of positives, not the legitimate review-only path.
    for v in ("approve", "approved", "pass", "passed", "success", "ok"):
        assert ex.reward_from_status("", v) == "1.0", \
            f"status='' verdict={v!r} must be 1.0 (review-only fallback)"


def test_no_signal_half():
    # Both empty: neither execution nor reviewer verdict — half-credit.
    assert ex.reward_from_status("", "") == "0.5"
    # Empty status + veto-only verdict has no exec signal to confirm the
    # negative — collapse to 0.5, NOT 0.0 (the veto cannot fabricate either).
    for v in ("reject", "needs_revision", "request_changes", "escalate"):
        assert ex.reward_from_status("", v) == "0.5", \
            f"status='' verdict={v!r} must be 0.5 (no exec to confirm veto)"


def test_bash_python_parity():
    # Preserve the full pre-retirement truth table as a standalone golden.
    statuses = ("", "success", "published", "done", "pass",
                "failure", "failed", "rolled_back", "blocked", "crash",
                "escalated", "reject", "weird", "approve", "approved",
                "passed", "rejected", "PUBLISHED", "FAILED", "Approve")
    verdicts = ("", "approve", "approved", "pass", "passed", "success", "ok",
                "reject", "rejected", "needs_revision", "request_changes",
                "escalate", "fail", "failed", "weird")
    for s in statuses:
        for v in verdicts:
            sl, vl = s.lower(), v.lower()
            if sl in {"failure", "failed", "rolled_back", "blocked", "crash", "escalated", "reject"}:
                expected = "0.0"
            elif sl in {"success", "published", "done", "pass"}:
                expected = "0.0" if vl in {"reject", "needs_revision", "request_changes", "escalate"} else "1.0"
            elif sl == "":
                expected = "1.0" if vl in {"approve", "approved", "pass", "passed", "success", "ok"} else "0.5"
            else:
                expected = "0.5"
            assert ex.reward_from_status(s, v) == expected


def test_dispatch_chain_parity():
    old = dict(os.environ)
    os.environ.update({"MO_FALLBACK_CODING": "minimax,codex,sonnet",
                       "MO_FALLBACK_REVIEW": "opus,kimi,sonnet"})
    cases = {
        ("implementer", "minimax"): "minimax,codex,sonnet",
        ("implementer", "glm"): "glm,minimax,codex,sonnet",
        ("reviewer", "opus"): "opus,kimi,sonnet",
        ("reviewer", "sonnet"): "sonnet,opus,kimi",
        ("verifier", "kimi"): "kimi,opus,sonnet",
        ("unknown_role", "codex"): "codex",
        ("planner", "codex"): "codex,minimax,sonnet",
    }
    try:
        for args, expected in cases.items():
            assert ex.dispatch_chain(*args) == expected
    finally:
        os.environ.clear(); os.environ.update(old)


def test_default_dispatch_preserves_resolved_fallback_chain(monkeypatch):
    from mini_ork.dispatch import llm_dispatch

    captured = {}

    def fake_dispatch(argv, *, root):
        captured["argv"] = argv
        captured["root"] = root
        return 0

    monkeypatch.setattr(llm_dispatch, "llm_dispatch", fake_dispatch)
    monkeypatch.setenv("MO_DISPATCH_CHAIN", "glm,codex,kimi")

    rc, output = ex._default_llm_dispatch("/engine")(
        "code_fix", "glm", "repair the target"
    )

    assert rc == 0
    assert output == ""
    assert captured["root"] == "/engine"
    assert captured["argv"] == [
        "--task-class", "code_fix",
        "--node-type", "glm",
        "--model", "glm,codex,kimi",
        "--prompt-text", "repair the target",
    ]


def test_router_respects_pins_no_monoculture():
    # Router-monoculture fix (bash + py parity): under learning_governed (the default),
    # recipe-pinned panel lenses keep their DISTINCT lanes instead of the governed router
    # collapsing all 4 same-node-type researchers onto one global-slice winner.
    for lens in ("glm_lens", "kimi_lens", "codex_lens", "opus_lens"):
        rp = ex.policy_route_lane("researcher", lens)
        assert rp == lens, f"pinned {lens} must be preserved"


def test_learning_static_lane_parity():
    cases = {
        ("reviewer", "reviewer"): "opus_lens",
        ("researcher", "researcher"): "kimi_lens",
        ("implementer", "implementer"): "kimi_lens",
        ("planner", "planner"): "planner",
        ("researcher", "glm_lens"): "glm_lens",
        ("reviewer", "custom_lane"): "custom_lane",
    }
    for args, expected in cases.items():
        assert ex.learning_static_lane(*args) == expected


def test_finish_reason_parity():
    cases = [((124, ""), "timeout"), ((43, ""), "error"),
             ((1, "lane_fuse_open here"), "error"),
             ((1, "cost_circuit_open spent"), "cost_limit"),
             ((2, "generic error"), "error"), ((0, ""), "error")]
    for args, expected in cases:
        assert ex.finish_reason_for_failure(*args) == expected


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
    expected = ["src", "(root)", "", "lib", "pkg", "", ""]
    for p, golden in zip(payloads, expected):
        old = dict(os.environ); os.environ.clear(); os.environ.update(env)
        cwd = os.getcwd(); os.chdir(tmp_path)
        try:
            rp = ex.infer_trace_code_region(p)
        finally:
            os.chdir(cwd); os.environ.clear(); os.environ.update(old)
        assert rp == golden, f"{p!r}: expected={golden!r} py={rp!r}"


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
    db_p = _seed_db(tmp_path, "gp")
    _seed_traces(db_p)
    # halflife=0 preserves the deterministic pre-retirement golden.
    old = dict(os.environ)
    os.environ.update({"MO_LEARNING_HALFLIFE_DAYS": "0"})
    try:
        np_ = ex.write_grpo_advantages(db_p)
    finally:
        os.environ.clear(); os.environ.update(old)
    assert np_ == 2
    assert _apm_rows(db_p), "agent_performance_memory must be populated"


def test_grpo_sigma_zero_tiebreak_parity(tmp_path):
    # all-success same-reward group → std==0 → cost tiebreak path
    db_p = _seed_db(tmp_path, "sp")
    for tid, av, cost in [("a", "opus_lens", 2.0), ("b", "kimi_lens", 0.1)]:
        _sql(db_p, "INSERT INTO execution_traces (trace_id,run_id,workflow_version_id,"
             "agent_version_id,task_class,prompt_version_hash,context_bundle_hash,tool_calls,"
             "files_read,files_written,verifier_output,reviewer_verdict,cost_usd,duration_ms,"
             "final_artifact_ref,status,created_at) VALUES "
             f"('{tid}','r','w','{av}','code_fix','p','c','[]','[]','[]',"
             f"'{{\"node_type\":\"impl\"}}','',{cost},100,'','success','2026-07-01T00:00:00Z');")
    old = dict(os.environ); os.environ.update({"MO_LEARNING_HALFLIFE_DAYS": "0"})
    try:
        ex.write_grpo_advantages(db_p)
    finally:
        os.environ.clear(); os.environ.update(old)
    # cheaper lane (kimi) got the positive bump
    kimi = _sql(db_p, "SELECT relative_advantage FROM agent_performance_memory "
                      "WHERE agent_version_id='kimi_lens';").stdout.strip()
    assert float(kimi) > 0


def test_conductor_outcomes_parity(tmp_path):
    db_p = _seed_db(tmp_path, "cp")
    _sql(db_p, "INSERT INTO epics (id,title,status) VALUES ('e1','E1','done'),('e2','E2','escalated');")
    _sql(db_p, "INSERT INTO conductor_decisions (decided_at,epic_id,task_class,outcome) VALUES "
         "(1,'e1','x','pending'),(1,'e2','x','pending');")
    np_ = ex.learning_update_conductor_outcomes(db_p)
    assert np_ == 2
    q = "SELECT epic_id,outcome,realized_score FROM conductor_decisions ORDER BY epic_id;"
    assert _sql(db_p, q).stdout == "e1|success|1.0\ne2|failure|0.0\n"


# ── live-path support helpers (increment 4) ──

def _seed_task_run(db, rid="r1", status="planned", cost=0.0):
    _sql(db, f"INSERT INTO task_runs (id,task_class,workflow_version,kickoff_path,status,"
             f"cost_usd,created_at,updated_at) VALUES ('{rid}','x','v1','k.md','{status}',"
             f"{cost},strftime('%s','now','-60 seconds'),strftime('%s','now'));")


def test_set_status_parity(tmp_path):
    db_p = _seed_db(tmp_path, "ssp")
    _seed_task_run(db_p)
    ex.set_status(db_p, "r1", "published")
    q = "SELECT status, ended_at IS NOT NULL AND ended_at>0, duration_ms>0 FROM task_runs WHERE id='r1';"
    assert _sql(db_p, q).stdout.strip() == "published|1|1"   # terminal stamps ended_at + duration
    # non-terminal transition: no ended_at
    _seed_task_run(db_p, rid="r2")
    ex.set_status(db_p, "r2", "reviewing")
    q2 = "SELECT status, ended_at FROM task_runs WHERE id='r2';"
    assert _sql(db_p, q2).stdout.strip() == "reviewing|"


def test_charge_node_cost_parity(tmp_path):
    db_p = _seed_db(tmp_path, "ccp")
    _seed_task_run(db_p, cost=0.10)
    cost_file = tmp_path / "cost"; cost_file.write_text("0.037")
    ex.charge_node_cost(db_p, "r1", str(cost_file), root=str(tmp_path))
    q = "SELECT printf('%.4f', cost_usd) FROM task_runs WHERE id='r1';"
    assert _sql(db_p, q).stdout.strip() == "0.1370"   # 0.10 + 0.037
    # invalid cost file → $0.01 placeholder on both
    _seed_task_run(db_p, rid="r3", cost=0)
    bad = tmp_path / "bad"; bad.write_text("not-a-number")
    ex.charge_node_cost(db_p, "r3", str(bad))
    q3 = "SELECT printf('%.4f', cost_usd) FROM task_runs WHERE id='r3';"
    assert _sql(db_p, q3).stdout == "0.0100\n"


def _git_repo(d):
    d.mkdir(parents=True)
    for a in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e"],
              ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", "-C", str(d), *a], capture_output=True)
    (d / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", str(d), "commit", "-qm", "base"], capture_output=True)
    return d


_DIFF_LOG = """The implementer emitted a diff instead of applying it:

--- a/app.py
+++ b/app.py
@@ -1 +1,2 @@
 x = 1
+y = 2
"""

_FENCED_LOG = """I created the file:

### FILE: `newmod.py`
```python
def hello():
    return "hi"
```
done.
"""


def test_apply_impl_output_diff_parity(tmp_path):
    logf = tmp_path / "impl.log"; logf.write_text(_DIFF_LOG)
    rp = _git_repo(tmp_path / "rp")
    ex.apply_impl_output(str(logf), str(rp))
    assert (rp / "app.py").read_text() == "x = 1\ny = 2\n"


def test_apply_impl_output_fenced_parity(tmp_path):
    logf = tmp_path / "impl.log"; logf.write_text(_FENCED_LOG)
    rp = _git_repo(tmp_path / "rp")
    ex.apply_impl_output(str(logf), str(rp))
    assert (rp / "newmod.py").exists()
    assert 'def hello():' in (rp / "newmod.py").read_text()


def test_apply_impl_output_skips_dirty_tree(tmp_path):
    # implementer already changed the tree → applier must NO-OP (both)
    logf = tmp_path / "impl.log"; logf.write_text(_DIFF_LOG)
    rp = _git_repo(tmp_path / "rp")
    (rp / "app.py").write_text("x = 999\n")   # dirty
    ex.apply_impl_output(str(logf), str(rp))
    assert (rp / "app.py").read_text() == "x = 999\n"   # untouched


# ── live per-node routing (increment 5) ──
# The LLM is an injectable seam, so these are FUNCTIONAL tests (fake dispatch →
# correct wiring of the ported helpers), not bash-parity (LLM output can't be
# parity-tested). They verify dispatch_node writes the right files, applies
# output, gates on the verdict, runs verifiers, and sets status/cost.

def _fake(response, rc=0):
    def d(_task_class, _node_type, _prompt):
        return rc, response
    return d


def _fields(node_id, node_type, lane="", vref=""):
    return (node_id, node_type, f"do {node_id}", "", "serial", vref, lane or node_type, "")


def _plan(tmp, outputs=("out.md",)):
    p = tmp / "plan.json"
    p.write_text(json.dumps({"objective": "o", "artifact_contract": {"outputs": list(outputs)}}))
    return str(p)


def test_live_researcher_writes_context(tmp_path, monkeypatch):
    db = _seed_db(tmp_path, "r"); _seed_task_run(db)
    rd = tmp_path / "run"; rd.mkdir()
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    def dispatch_with_run_dir(_task_class, _lane, _prompt):
        assert os.environ["MINI_ORK_RUN_DIR"] == str(rd)
        return 0, "finding: X is slow"

    rc, fr = ex.dispatch_node(_fields("res1_lens", "researcher", "kimi_lens"),
                              root=str(REPO), run_dir=str(rd), plan_path=_plan(tmp_path),
                              task_class="code_fix", db=db, run_id="r1",
                              dispatch_fn=dispatch_with_run_dir)
    assert rc == 0 and fr == "done"
    assert (rd / "lens-res1.md").read_text() == "finding: X is slow"
    # cost charged
    assert float(_sql(db, "SELECT cost_usd FROM task_runs WHERE id='r1';").stdout) > 0


def test_self_migrate_researcher_artifact_names(tmp_path):
    assert ex._researcher_output_file(str(tmp_path), "self-migrate", "seam_mapper") == str(
        tmp_path / "integration-map.json"
    )
    assert ex._researcher_output_file(
        str(tmp_path), "self-migrate", "static_feature_ledger"
    ) == str(tmp_path / "static-feature-ledger.json")
    assert ex._researcher_output_file(
        str(tmp_path), "self-migrate", "cost_verifiability_lens"
    ) == str(tmp_path / "cost-verifiability-lens.md")


def test_live_implementer_applies_diff(tmp_path, monkeypatch):
    db = _seed_db(tmp_path, "i"); _seed_task_run(db)
    rd = tmp_path / "run"; rd.mkdir()
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    rc, fr = ex.dispatch_node(_fields("impl1", "implementer", "codex"),
                              root=str(REPO), run_dir=str(rd), plan_path=_plan(tmp_path),
                              task_class="code_fix", db=db, run_id="r1",
                              dispatch_fn=_fake(_DIFF_LOG))
    assert rc == 0 and fr == "done"
    assert (rd / "impl-impl1.log").read_text() == _DIFF_LOG
    assert (repo / "app.py").read_text() == "x = 1\ny = 2\n"   # diff applied to clean tree


def test_live_reviewer_verdict_gate(tmp_path):
    db = _seed_db(tmp_path, "rev"); _seed_task_run(db)
    rd = tmp_path / "run"; rd.mkdir()
    common = dict(root=str(REPO), run_dir=str(rd), plan_path=_plan(tmp_path),
                  task_class="code_fix", db=db, run_id="r1")
    rc_pass, _ = ex.dispatch_node(_fields("rev1", "reviewer", "opus"),
                                  dispatch_fn=_fake('{"verdict": "pass"}'), **common)
    assert rc_pass == 0
    rc_fail, fr_fail = ex.dispatch_node(_fields("rev2", "reviewer", "opus"),
                                        dispatch_fn=_fake('{"verdict": "fail"}'), **common)
    assert rc_fail == 1 and fr_fail == "verdict_fail"
    rc_rev, fr_rev = ex.dispatch_node(_fields("rev3", "reviewer", "opus"),
                                      dispatch_fn=_fake('{"verdict": "needs_revision"}'), **common)
    assert rc_rev == 1 and fr_rev == "verdict_revise"
    # unknown/unparseable verdict on a gating (non-synth) node → FAIL, not neutral.
    rc_unk, fr_unk = ex.dispatch_node(_fields("rev4", "reviewer", "opus"),
                                      dispatch_fn=_fake("I think this looks fine overall."), **common)
    assert rc_unk == 1 and fr_unk == "verdict_fail"
    # a synth reviewer never gates → always success
    rc_synth, _ = ex.dispatch_node(_fields("synth_node", "reviewer", "opus"),
                                   dispatch_fn=_fake("# Synthesis\ntop findings..."), **common)
    assert rc_synth == 0


def test_live_verifier_hollow_artifact_fails(tmp_path):
    # Hollow-run guard: a plan that requires a concrete absolute run-local artifact
    # which is missing → the verifier node fails before any verifier runs. A real,
    # non-empty artifact at that path passes the guard.
    db = _seed_db(tmp_path, "vh"); _seed_task_run(db)
    rd = tmp_path / "run"; rd.mkdir()
    art = rd / "framework-edit.diff"
    plan = tmp_path / "plan-req.json"
    plan.write_text(json.dumps({"objective": "o",
                                "artifact_contract": {"required_artifacts": [str(art)]}}))
    common = dict(root=str(REPO), run_dir=str(rd), plan_path=str(plan),
                  task_class="framework_edit", db=db, run_id="r1", dispatch_fn=_fake(""))
    rc_missing, fr_missing = ex.dispatch_node(_fields("verify1", "verifier"), **common)
    assert rc_missing == 1 and fr_missing == "error"
    # Now materialise a real, non-empty artifact → guard passes (no outputs → the
    # node returns 0 with an informational finish_reason, bash parity).
    art.write_text("diff --git a/x b/x\n+real change\n")
    rc_ok, _ = ex.dispatch_node(_fields("verify2", "verifier"), **common)
    assert rc_ok == 0


def test_pre_implementer_verifier_does_not_require_final_artifact(tmp_path, monkeypatch):
    db = _seed_db(tmp_path, "pre-vh"); _seed_task_run(db)
    rd = tmp_path / "run"; rd.mkdir()
    missing_final_artifact = rd / "self-migrate.diff"
    plan = tmp_path / "plan-pre.json"
    plan.write_text(json.dumps({
        "objective": "capture the legacy oracle before migration",
        "artifact_contract": {"outputs": [str(missing_final_artifact)]},
    }))
    verifier_called = []

    def passing_baseline_verifier(script, evidence_path, **_kwargs):
        verifier_called.append(script)
        Path(evidence_path).write_text('{"pass":true}\n')
        return 0

    monkeypatch.setattr(ex, "_run_verifier_ref", passing_baseline_verifier)
    workflow = REPO / "recipes" / "self-migrate" / "workflow.yaml"
    rc, fr = ex.dispatch_node(
        _fields("pre_retirement_parity", "verifier", vref="verifiers/pre-retirement-parity.sh"),
        root=str(REPO), run_dir=str(rd), plan_path=str(plan), task_class="self_migrate",
        db=db, run_id="r1", dispatch_fn=_fake(""), recipe="self-migrate",
        workflow=str(workflow),
    )

    assert rc == 0 and fr == "done"
    assert verifier_called
    assert not missing_final_artifact.exists()


def test_verifier_only_workflow_is_pre_implementation(tmp_path):
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        """\
nodes:
  - name: collect
    type: verifier
  - name: synthesize
    type: researcher
  - name: verify
    type: verifier
""",
        encoding="utf-8",
    )

    assert ex._verifier_runs_before_implementer(str(workflow), "collect")
    assert ex._verifier_runs_before_implementer(str(workflow), "verify")


def test_run_verifier_ref(tmp_path):
    ok = tmp_path / "ok.sh"; ok.write_text('#!/usr/bin/env bash\necho \'{"pass": true}\'\n')
    bad = tmp_path / "bad.sh"; bad.write_text('#!/usr/bin/env bash\necho \'{"pass": false}\'\n')
    empty = tmp_path / "empty.sh"; empty.write_text('#!/usr/bin/env bash\nexit 0\n')
    env_ok = tmp_path / "env-ok.sh"
    env_ok.write_text(
        '#!/usr/bin/env bash\n'
        '[ "$MINI_ORK_RUN_DIR" = "$PWD" ]\n'
        'echo \'{"pass": true}\'\n'
    )
    assert ex._run_verifier_ref(str(ok), str(tmp_path / "e1"), cwd=str(tmp_path)) == 0
    assert ex._run_verifier_ref(str(bad), str(tmp_path / "e2"), cwd=str(tmp_path)) == 1
    assert ex._run_verifier_ref(str(empty), str(tmp_path / "e3"), cwd=str(tmp_path)) == 1  # vacuous
    assert ex._run_verifier_ref(str(env_ok), str(tmp_path / "e4"), cwd=str(tmp_path)) == 0


def test_live_publisher_and_rollback_status(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_ORACLE_GATES_AUTO", "0")  # isolate from the oracle-gate shell-out
    db = _seed_db(tmp_path, "pub"); _seed_task_run(db)
    rd = tmp_path / "run"; rd.mkdir()
    common = dict(root=str(REPO), run_dir=str(rd), plan_path=_plan(tmp_path),
                  task_class="code_fix", db=db, run_id="r1", dispatch_fn=_fake(""))
    # No recipe → no artifact_contract.yaml. Bash returns 0 WITHOUT publishing
    # (mini_ork/cli/execute.py:3017-3019). The old port stub wrongly always marked
    # 'published' (panel finding 2); the faithful port leaves status unchanged.
    rc, _ = ex.dispatch_node(_fields("pub", "publisher"), **common)
    assert rc == 0 and _sql(db, "SELECT status FROM task_runs WHERE id='r1';").stdout.strip() != "published"
    # F4: rollback is best-effort (bash :3205-3223) — returns 0/done regardless of
    # whether a prior version exists, and does NOT set task_runs.status. The upstream
    # failure already fails the run. Was wrongly (1,'rolled_back') + status mutation.
    rc_rb, fr_rb = ex.dispatch_node(_fields("rb", "rollback"), **common)
    assert rc_rb == 0 and fr_rb == "done"
    assert _sql(db, "SELECT status FROM task_runs WHERE id='r1';").stdout.strip() != "rolled_back"


def test_publisher_preserves_heterogeneous_run_local_artifacts(tmp_path, monkeypatch):
    """A diff source must never overwrite sibling JSON outputs in a composite contract."""
    root = tmp_path / "engine"
    recipe_dir = root / "recipes" / "self-migrate"
    recipe_dir.mkdir(parents=True)
    run_dir = root / ".mini-ork" / "runs" / "run-publish"
    run_dir.mkdir(parents=True)
    diff = run_dir / "self-migrate.diff"
    ledger = run_dir / "static-feature-ledger.json"
    verdict = run_dir / "verdict.json"
    diff.write_text("diff --git a/a b/a\n")
    ledger.write_text('{"features": []}\n')
    verdict.write_text('{"pass": false}\n')
    (recipe_dir / "artifact_contract.yaml").write_text(
        "source_artifact: ${MINI_ORK_RUN_DIR}/self-migrate.diff\n"
        "outputs:\n"
        "  - ${MINI_ORK_RUN_DIR}/self-migrate.diff\n"
        "  - ${MINI_ORK_RUN_DIR}/static-feature-ledger.json\n"
        "  - ${MINI_ORK_RUN_DIR}/verdict.json\n"
    )
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    statuses = []
    monkeypatch.setattr(ex, "set_status", lambda _db, _rid, status: statuses.append(status))

    rc, reason = ex.publisher_node(
        str(root), str(run_dir), "", "run-publish", "self-migrate", "self_migrate"
    )

    assert (rc, reason) == (0, "done")
    assert diff.read_text() == "diff --git a/a b/a\n"
    assert json.loads(ledger.read_text()) == {"features": []}
    assert json.loads(verdict.read_text()) == {"pass": False}
    assert statuses == ["published"]


def test_publisher_commit_stages_only_reviewed_files(tmp_path):
    repo = _git_repo(tmp_path / "publish-repo")
    reviewed = repo / "app.py"
    reviewed.write_text("x = 2\n")
    unrelated = repo / "local-only.txt"
    unrelated.write_text("must not enter the commit\n")
    run_dir = tmp_path / "publish-run"
    run_dir.mkdir()
    review = run_dir / "review-verdict.json"
    review.write_text('{"verdict":"approve"}\n')
    (run_dir / "implementer-summary.json").write_text(json.dumps({
        "files_changed": [str(reviewed)],
        "worktree_path": str(repo),
    }))

    assert ex._publisher_try_commit_files(
        str(REPO), str(repo), str(run_dir), str(review), "approve",
        "code-fix", "implementer", "run-publish",
    ) is True
    committed = subprocess.check_output(
        ["git", "-C", str(repo), "show", "--name-only", "--format=", "HEAD"],
        text=True,
    ).splitlines()
    assert committed == ["app.py"]
    assert unrelated.exists()
    assert "local-only.txt" in subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain"], text=True
    )


def test_publisher_commit_rejects_unapproved_or_outside_paths(tmp_path):
    repo = _git_repo(tmp_path / "reject-repo")
    base_head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    run_dir = tmp_path / "reject-run"
    run_dir.mkdir()
    review = run_dir / "review-verdict.json"
    review.write_text('{"verdict":"needs_revision","pass":false}\n')
    (run_dir / "implementer-summary.json").write_text(json.dumps({
        "files_changed": [str(repo / "app.py")],
    }))
    assert ex._publisher_try_commit_files(
        str(REPO), str(repo), str(run_dir), str(review), "needs_revision",
        "code-fix", "implementer", "run-reject",
    ) is False

    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    review.write_text('{"verdict":"approve"}\n')
    (run_dir / "implementer-summary.json").write_text(json.dumps({
        "files_changed": [str(outside)],
    }))
    assert ex._publisher_try_commit_files(
        str(REPO), str(repo), str(run_dir), str(review), "approve",
        "code-fix", "implementer", "run-outside",
    ) is False
    assert subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip() == base_head


def test_reviewer_input_assembly_preserves_summary_verifiers_and_diff(tmp_path):
    repo = _git_repo(tmp_path / "review-repo")
    (repo / "app.py").write_text("x = 2\n")
    run_dir = tmp_path / "review-run"
    run_dir.mkdir()
    (run_dir / "implementer-summary.json").write_text(json.dumps({
        "files_changed": [str(repo / "app.py")],
        "worktree_path": str(repo),
        "rationale": "review this change",
    }))
    (run_dir / "verifier_typecheck.json").write_text(
        '{"verifier":"typecheck","pass":true}\n'
    )
    (run_dir / "verifier_test.json").write_text(
        '{"verifier":"test","pass":true}\n'
    )

    block = ex._assemble_reviewer_inputs(str(run_dir))
    assert "review this change" in block
    assert "# verifier_typecheck.json" in block
    assert "# verifier_test.json" in block
    assert "x = 2" in block
    assert "app.py" in (run_dir / "review-diff.patch").read_text()


def test_resolve_target_cwd_prefers_explicit_worktree_over_external_kickoff(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "target")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    kickoff = tmp_path / "external-kickoff.md"
    kickoff.write_text("# kickoff\n")
    (run_dir / "run_profile.json").write_text(json.dumps({"kickoff_path": str(kickoff)}))
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    assert ex._resolve_target_cwd(str(run_dir)) == str(repo)


def test_self_migrate_harvests_target_run_mirror_before_review(tmp_path):
    target = tmp_path / "target"
    run_dir = tmp_path / "engine" / ".mini-ork" / "runs" / "run-harvest"
    mirror = target / ".mini-ork" / "runs" / "run-harvest"
    run_dir.mkdir(parents=True)
    mirror.mkdir(parents=True)
    (mirror / "self-migrate.diff").write_text("diff --git a/a b/a\n")
    (mirror / "static-feature-ledger.json").write_text('{"features":[{"feature":"f"}]}\n')
    (mirror / "verdict.json").write_text('{"pass":true}\n')
    (mirror / "verifier_fork-closure.json").write_text('{"pass":true}\n')
    (mirror / "agent-migrator.stream.jsonl").write_text("large operational transcript\n")

    copied = ex._harvest_self_migrate_artifacts(str(run_dir), str(target))

    assert copied == ["self-migrate.diff", "static-feature-ledger.json", "verdict.json",
                      "verifier_fork-closure.json"]
    assert json.loads((run_dir / "verdict.json").read_text()) == {"pass": True}
    assert not (run_dir / "agent-migrator.stream.jsonl").exists()


def test_run_verdict_preserves_recipe_detailed_verdict(tmp_path):
    run_dir = tmp_path / "run"; run_dir.mkdir()
    detailed = {"pass": True, "parity_pass": True}
    (run_dir / "verdict.json").write_text(json.dumps(detailed))

    ex._emit_run_verdict(str(run_dir), fail_count=2, dispatched=12)

    assert json.loads((run_dir / "verdict.json").read_text()) == detailed
    run_verdict = json.loads((run_dir / "run-verdict.json").read_text())
    assert run_verdict == {
        "verdict": "fail", "failed_nodes": 2, "dispatched": 12,
        "source": "execute@run-level",
    }


def test_envsubst_blanks_unset_vars(monkeypatch):
    # B2-C: envsubst-equivalent blanks unset vars (not literal like os.path.expandvars),
    # else the publisher commits garbage ${VAR}-in-path files.
    monkeypatch.setenv("MINI_ORK_DERIVED_RECIPE_NAME", "my-recipe")
    monkeypatch.delenv("NOPE", raising=False)
    assert ex._envsubst("docs/${MINI_ORK_DERIVED_RECIPE_NAME}/out.md") == "docs/my-recipe/out.md"
    assert ex._envsubst("a/${NOPE}/b") == "a//b"  # blanked, not left literal


def test_classic_reviewer_prompt_has_inputs_and_json_envelope(tmp_path):
    # F2-B: the classic reviewer prompt must carry the assembled inputs block AND the
    # JSON verdict envelope — without the envelope the LLM emits prose → unknown verdict
    # → false rollback of a good run.
    db = _seed_db(tmp_path, "rv"); _seed_task_run(db)
    rd = tmp_path / "run"; rd.mkdir()
    captured = {}

    def cap(tc, nt, prompt):
        captured["p"] = prompt
        return 0, '{"verdict":"pass"}'

    ex.dispatch_node(_fields("rev1", "reviewer"), root=str(REPO), run_dir=str(rd),
                     plan_path=_plan(tmp_path), task_class="code_fix", db=db, run_id="r1",
                     dispatch_fn=cap)
    assert "Respond with JSON" in captured["p"]
    assert "Reviewer inputs" in captured["p"]


def test_classic_reviewer_prompt_includes_recipe_specific_evidence(tmp_path):
    db = _seed_db(tmp_path, "rve"); _seed_task_run(db)
    rd = tmp_path / "run"; rd.mkdir()
    (rd / "implementer-summary.json").write_text('{"status":"implemented"}\n')
    (rd / "pre-retirement-parity.json").write_text('{"pass":true}\n')
    (rd / "verifier_parity.json").write_text('{"pass":true}\n')
    (rd / "verifier_fork-closure.json").write_text('{"pass":true}\n')
    (rd / "integration-map.json").write_text('{"fork":"verify"}\n')
    (rd / "static-feature-ledger.json").write_text('{"features":[]}\n')
    (rd / "verdict.json").write_text('{"pass":true}\n')
    (rd / "self-migrate.diff").write_text("diff --git a/a b/a\n")
    captured = {}

    def cap(tc, nt, prompt):
        captured["p"] = prompt
        return 0, '{"verdict":"pass"}'

    ex.dispatch_node(_fields("reviewer", "reviewer"), root=str(REPO), run_dir=str(rd),
                     plan_path=_plan(tmp_path), task_class="self_migrate", db=db,
                     run_id="r1", recipe="self-migrate", dispatch_fn=cap)

    for name in ("pre-retirement-parity.json", "verifier_parity.json",
                 "verifier_fork-closure.json",
                 "integration-map.json", "static-feature-ledger.json",
                 "verdict.json", "self-migrate.diff"):
        assert f"# {name}" in captured["p"]


def test_researcher_recipe_specific_output_files(tmp_path):
    # F1-B: recursive-validate-impl tier4_* and schema-judge-panel *_lens researcher
    # nodes must write the exact tier4-*.md / judge-*.md the panel synthesizer globs,
    # not context-<id>.json. Otherwise the panel gate sees zero lens inputs.
    db = _seed_db(tmp_path, "rf"); _seed_task_run(db)
    rd = tmp_path / "run"; rd.mkdir()
    common = dict(root=str(REPO), run_dir=str(rd), plan_path=_plan(tmp_path),
                  task_class="code_fix", db=db, run_id="r1", dispatch_fn=_fake("panel body"))
    ex.dispatch_node(_fields("tier4_glm", "researcher"), recipe="recursive-validate-impl", **common)
    assert (rd / "tier4-glm.md").is_file()
    ex.dispatch_node(_fields("kimi_correctness_lens", "researcher"), recipe="schema-judge-panel", **common)
    assert (rd / "judge-kimi-correctness.md").is_file()


def test_verifier_no_artifact_does_not_fail_run(tmp_path):
    # NEW-1: bash (:2899-2902) warns + does NOT return 1 when artifact_contract has
    # no outputs — the run passes. The port previously returned (1,'error').
    db = _seed_db(tmp_path, "vf"); _seed_task_run(db)
    rd = tmp_path / "run"; rd.mkdir()
    plan = tmp_path / "noout.json"; plan.write_text('{"artifact_contract": {"outputs": []}}')
    rc, _ = ex.dispatch_node(_fields("v1", "verifier"), root=str(REPO), run_dir=str(rd),
                             plan_path=str(plan), task_class="code_fix", db=db, run_id="r1",
                             dispatch_fn=_fake(""))
    assert rc == 0


def test_publisher_panel_gate_blocks_without_approval(tmp_path, monkeypatch):
    # F2/F3: the recursive-validate-impl publisher MUST block when panel-verdict.json
    # is missing or not approved (bash :2986-3013). The old stub shipped regardless.
    monkeypatch.setenv("MO_ORACLE_GATES_AUTO", "0")
    db = _seed_db(tmp_path, "pg"); _seed_task_run(db)
    rd = tmp_path / "run"; rd.mkdir()
    common = dict(root=str(REPO), run_dir=str(rd), plan_path=_plan(tmp_path),
                  task_class="code_fix", db=db, run_id="r1", dispatch_fn=_fake(""),
                  recipe="recursive-validate-impl")
    # missing panel verdict → block
    rc, fr = ex.dispatch_node(_fields("pub", "publisher"), **common)
    assert rc == 1 and fr == "verdict_fail"
    # present but rejecting → still block
    (rd / "panel-verdict.json").write_text('{"verdict":"reject"}')
    rc2, fr2 = ex.dispatch_node(_fields("pub", "publisher"), **common)
    assert rc2 == 1 and fr2 == "verdict_fail"
    # approved → clears the panel gate (the recursive-validate-impl contract then
    # errors on the absent source artifact in this minimal run dir — that's a
    # downstream delivery error, NOT a gate block, so fr is not verdict_fail).
    (rd / "panel-verdict.json").write_text('{"verdict":"approve"}')
    _, fr3 = ex.dispatch_node(_fields("pub", "publisher"), **common)
    assert fr3 != "verdict_fail"


def test_reviewer_panel_gate_not_treated_as_synth(tmp_path, monkeypatch):
    # F3: recursive-validate-impl/tier4_synth is a panel GATE, not an ungated synth —
    # a reject verdict must fail the node (old code: "synth" in node_id → ungated pass).
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path / "run"))
    db = _seed_db(tmp_path, "tg"); _seed_task_run(db)
    rd = tmp_path / "run"; rd.mkdir()
    common = dict(root=str(REPO), run_dir=str(rd), plan_path=_plan(tmp_path),
                  task_class="code_fix", db=db, run_id="r1",
                  recipe="recursive-validate-impl",
                  dispatch_fn=_fake('{"verdict":"fail"}'))
    rc, fr = ex.dispatch_node(_fields("tier4_synth", "reviewer"), **common)
    assert rc == 1 and fr == "verdict_fail"
    assert (rd / "panel-verdict.json").is_file()  # writes panel-verdict.json, not synthesis.md


def test_main_live_run_wired(tmp_path, monkeypatch):
    # a small workflow-sourced run driven by a fake LLM end-to-end through main()
    wf = tmp_path / "wf.yaml"
    wf.write_text("dispatch_mode: serial\nnodes:\n"
                  "  - {name: res1, type: researcher, description: research}\n"
                  "  - {name: rev1, type: reviewer, description: review}\n")
    home = tmp_path / ".mini-ork"; home.mkdir()
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    rd = home / "runs" / "run-x"; rd.mkdir(parents=True)
    plan = rd / "plan.json"; plan.write_text(json.dumps({"objective": "o", "decomposition": []}))
    _seed_task_run(db, rid="run-x")
    for k, v in {"MINI_ORK_ROOT": str(REPO), "MINI_ORK_WORKFLOW": str(wf), "MINI_ORK_HOME": str(home),
                 "MINI_ORK_DB": db, "MINI_ORK_PLAN_PATH": str(plan), "MINI_ORK_RUN_DIR": str(rd),
                 "MINI_ORK_RUN_ID": "run-x", "MINI_ORK_TASK_CLASS": "code_fix"}.items():
        monkeypatch.setenv(k, v)
    rc = ex.main([], root=str(REPO), dispatch_fn=_fake('{"verdict": "pass"}'))  # plan via env
    assert rc == 0
    assert (rd / "context-res1.json").exists()          # researcher wrote its output
    assert (rd / "review-rev1.json").exists()            # reviewer wrote its output
    assert (rd / "verdict.json").exists()                # run-level verdict emitted
    assert _sql(db, "SELECT status FROM task_runs WHERE id='run-x';").stdout.strip() in ("executing", "reviewing", "published")
    # F3: the live path must now write reward-stamped execution_traces rows — the
    # GRPO/reflect learning-loop signal that was ZERO under python before the trace_fn
    # wiring. researcher + reviewer each stamp a row with a non-null reward_value.
    n = _sql(db, "SELECT COUNT(*) FROM execution_traces "
                 "WHERE run_id='run-x' AND reward_value IS NOT NULL;").stdout.strip()
    assert int(n) >= 2

    # Exercise the production process-isolated branch without a provider call:
    # planner nodes are handled locally by dispatch_node and therefore cost $0.
    parallel_wf = tmp_path / "parallel-wf.yaml"
    parallel_wf.write_text(
        "dispatch_mode: parallel\nnodes:\n" +
        "".join(f"  - {{name: plan{i}, type: planner, description: plan {i}}}\n"
                for i in range(4))
    )
    parallel_rd = home / "runs" / "run-parallel"
    parallel_rd.mkdir()
    parallel_plan = parallel_rd / "plan.json"
    parallel_plan.write_text(json.dumps({"objective": "parallel", "decomposition": []}))
    _seed_task_run(db, rid="run-parallel")
    for key, value in {
        "MINI_ORK_WORKFLOW": str(parallel_wf),
        "MINI_ORK_PLAN_PATH": str(parallel_plan),
        "MINI_ORK_RUN_DIR": str(parallel_rd),
        "MINI_ORK_RUN_ID": "run-parallel",
        "MINI_ORK_MAX_PARALLEL": "2",
    }.items():
        monkeypatch.setenv(key, value)
    assert ex._max_parallel() == 2
    assert ex.main([], root=str(REPO)) == 0
    assert (parallel_rd / "verdict.json").is_file()


def test_parallel_graph_dispatches_artifact_dependencies_in_readiness_waves(tmp_path, monkeypatch):
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        """\
version: "0.2.0"
task_class: artifact_test
dispatch_mode: parallel
nodes:
  - { name: producer, type: researcher, model_lane: producer, dispatch_mode: parallel, outputs: [{ name: report, path: producer.md }] }
  - { name: consumer, type: researcher, model_lane: consumer, dispatch_mode: parallel, inputs: { report: { required: true } }, outputs: [{ name: summary, path: consumer.md }] }
edges:
  - { from: producer, to: consumer, edge_type: supplies_context_to, from_output: report, to_input: report }
""",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    plan = run_dir / "plan.json"
    plan.write_text(json.dumps({"objective": "o", "task_class": "artifact_test"}))
    batches: list[list[str]] = []

    def fake_parallel(fields, **_kwargs):
        batches.append([field[0] for field in fields])
        return [(field, 0, "done") for field in fields]

    monkeypatch.setattr(ex, "_run_parallel_batch", fake_parallel)
    for name, value in {
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_WORKFLOW": str(workflow),
        "MINI_ORK_PLAN_PATH": str(plan),
        "MINI_ORK_RUN_DIR": str(run_dir),
        "MINI_ORK_RUN_ID": "graph-waves",
        "MINI_ORK_DB": str(tmp_path / "state.db"),
        "MINI_ORK_DRY_RUN": "0",
        "MINI_ORK_EXECUTE_GATE": "0",
        "MO_GRADE_RUN_REWARD": "0",
        "MO_LEARNING_WRITEBACK": "0",
    }.items():
        monkeypatch.setenv(name, value)

    assert ex.main([], root=str(REPO)) == 0
    assert batches == [["producer"], ["consumer"]]


def test_failed_parent_blocks_dependent_node(tmp_path, monkeypatch):
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        """\
version: "0.2.0"
task_class: artifact_test
dispatch_mode: serial
nodes:
  - { name: producer, type: researcher, model_lane: producer, dispatch_mode: serial }
  - { name: publisher, type: researcher, model_lane: publisher, dispatch_mode: serial }
edges:
  - { from: producer, to: publisher, edge_type: depends_on }
""",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    plan = run_dir / "plan.json"
    plan.write_text(json.dumps({"objective": "o", "task_class": "artifact_test"}))
    calls: list[str] = []

    def failing_producer(_task_class, lane, _prompt):
        calls.append(lane)
        return (1, "producer failed") if lane == "producer" else (0, "must not run")

    for name, value in {
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_WORKFLOW": str(workflow),
        "MINI_ORK_PLAN_PATH": str(plan),
        "MINI_ORK_RUN_DIR": str(run_dir),
        "MINI_ORK_RUN_ID": "blocked-child",
        "MINI_ORK_DB": str(tmp_path / "state.db"),
        "MINI_ORK_DRY_RUN": "0",
        "MINI_ORK_EXECUTE_GATE": "0",
        "MO_GRADE_RUN_REWARD": "0",
        "MO_LEARNING_WRITEBACK": "0",
    }.items():
        monkeypatch.setenv(name, value)

    assert ex.main([], root=str(REPO), dispatch_fn=failing_producer) == 1
    assert calls == ["producer"]
    assert json.loads((run_dir / "verdict.json").read_text())["failed_nodes"] == 2


def test_trace_fn_stamps_scoping(tmp_path, monkeypatch):
    # Scoping-stamp fix: the per-node trace_fn must land code_region (from an in-repo
    # files_written path) and objective_domain (from MINI_ORK_OBJECTIVE_DOMAIN), the
    # two feature-partition columns that were NULL/monolithic before.
    db = _seed_db(tmp_path, "scope"); _seed_task_run(db, rid="run-s", status="executing")
    repo = _git_repo(tmp_path / "repo")
    (repo / "lib").mkdir()
    edited = repo / "lib" / "healer.sh"; edited.write_text("echo hi\n")
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_ROOT", str(repo))
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    monkeypatch.setenv("MINI_ORK_OBJECTIVE_DOMAIN", "book-gen")
    tf = ex._make_trace_fn("code_fix", db, "run-s")
    tf("impl1", "success", "implementer", output_file=str(edited), finish_reason="done")
    row = _sql(db, "SELECT objective_domain, code_region, process_reward "
                   "FROM execution_traces WHERE run_id='run-s';").stdout.strip()
    objective_domain, code_region, process_reward = row.split("|")
    assert (objective_domain, code_region) == ("book-gen", "lib")
    assert float(process_reward) >= 0.5


def test_trace_fn_process_reward_opt_out(tmp_path, monkeypatch):
    db = _seed_db(tmp_path, "prm-off")
    _seed_task_run(db, rid="run-prm-off", status="executing")
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MO_PRM_SCORE", "0")
    ex._make_trace_fn("code_fix", db, "run-prm-off")(
        "r1", "success", "researcher"
    )
    assert _sql(
        db,
        "SELECT process_reward IS NULL FROM execution_traces "
        "WHERE run_id='run-prm-off';",
    ).stdout.strip() == "1"


def test_minimal_scaffold_routes_without_harness_dispatch(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mini_ork.agent import minimal

    db = _seed_db(tmp_path, "minimal")
    _seed_task_run(db)
    rd = tmp_path / "run-minimal"
    rd.mkdir()
    repo = _git_repo(tmp_path / "minimal-repo")
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    monkeypatch.setenv("MO_SCAFFOLD_TIER", "minimal")
    monkeypatch.setattr(
        minimal,
        "run_minimal",
        lambda task, cwd: SimpleNamespace(
            final_output="COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n",
            exit_status="complete",
        ),
    )

    def forbidden_dispatch(*_args):
        raise AssertionError("harness dispatcher must not run in minimal mode")

    rc, reason = ex.dispatch_node(
        _fields("impl-min", "implementer", "codex"),
        root=str(REPO),
        run_dir=str(rd),
        plan_path=_plan(tmp_path),
        task_class="code_fix",
        db=db,
        run_id="r1",
        dispatch_fn=forbidden_dispatch,
    )
    assert (rc, reason) == (0, "done")
    assert (rd / "impl-impl-min.log").read_text() == (
        "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
    )


def test_trace_fn_objective_domain_defaults(tmp_path, monkeypatch):
    # objective_domain defaults to code-delivery ONLY when the env is unset.
    db = _seed_db(tmp_path, "scopedef"); _seed_task_run(db, rid="run-d", status="executing")
    monkeypatch.delenv("MINI_ORK_OBJECTIVE_DOMAIN", raising=False)
    monkeypatch.delenv("MO_OBJECTIVE_DOMAIN", raising=False)
    monkeypatch.setenv("MINI_ORK_DB", db)
    tf = ex._make_trace_fn("code_fix", db, "run-d")
    tf("r1", "success", "researcher")
    assert _sql(db, "SELECT objective_domain FROM execution_traces "
                    "WHERE run_id='run-d';").stdout.strip() == "code-delivery"


def test_trace_fn_merges_tool_summary_sidecar_and_lane(tmp_path, monkeypatch):
    # MO_TRACE_RICH fidelity: when the "${output_file}.tool-summary" sidecar exists
    # (emitted by llm-dispatch stream-json post-process), the trace_fn must parse it
    # and merge tool_calls + files_read (+ extra files_written) into the row — mirroring
    # bash _trace_write_node_rich. And the resolved lane must land as agent_version_id.
    db = _seed_db(tmp_path, "sidecar"); _seed_task_run(db, rid="run-x", status="executing")
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)  # keep files_written deterministic (no target-repo seed)
    out = tmp_path / "impl.log"; out.write_text("implementer output\n")
    # Seed the tool-summary sidecar next to the output file, bash-shaped.
    (tmp_path / "impl.log.tool-summary").write_text(json.dumps({
        "tool_calls": [{"name": "Edit", "count": 2}, {"name": "Bash", "count": 1}],
        "files_read": ["src/a.py", "src/b.py"],
        "files_written": ["src/a.py"],
    }))
    tf = ex._make_trace_fn("code_fix", db, "run-x")
    # lane threaded via kwarg (dispatch_node binds the resolved lane into trace()).
    tf("impl1", "success", "implementer", output_file=str(out),
       finish_reason="done", lane="minimax_lens")
    row = _sql(db, "SELECT tool_calls, files_read, files_written, agent_version_id "
                   "FROM execution_traces WHERE run_id='run-x';").stdout.strip()
    tool_calls_json, files_read_json, files_written_json, agent = row.split("|")
    assert json.loads(tool_calls_json) == [
        {"name": "Edit", "count": 2}, {"name": "Bash", "count": 1}]
    assert json.loads(files_read_json) == ["src/a.py", "src/b.py"]
    # output_file first, then the sidecar's extra files_written (deduped).
    assert json.loads(files_written_json) == [str(out), "src/a.py"]
    assert agent == "minimax_lens"


def test_trace_fn_no_sidecar_leaves_tool_calls_empty(tmp_path, monkeypatch):
    # Guard: with no sidecar present, tool_calls/files_read stay empty arrays (the
    # merge is strictly best-effort and gated on sidecar existence, like bash).
    db = _seed_db(tmp_path, "nosidecar"); _seed_task_run(db, rid="run-n", status="executing")
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)  # keep files_written deterministic (no target-repo seed)
    out = tmp_path / "impl.log"; out.write_text("out\n")
    tf = ex._make_trace_fn("code_fix", db, "run-n")
    tf("impl1", "success", "implementer", output_file=str(out), lane="codex_lens")
    row = _sql(db, "SELECT tool_calls, files_read, files_written, agent_version_id "
                   "FROM execution_traces WHERE run_id='run-n';").stdout.strip()
    tc, fr, fw, agent = row.split("|")
    assert json.loads(tc) == []
    assert json.loads(fr) == []
    assert json.loads(fw) == [str(out)]
    assert agent == "codex_lens"


def test_implementer_trace_code_region_from_target_repo(tmp_path, monkeypatch):
    # Regression: an implementer node's code_region must reflect the TARGET repo's
    # edited source dir, NOT '.mini-ork'. The impl.log passed as output_file lives
    # under $MO_TARGET_CWD/.mini-ork/runs/<id>/ — with MINI_ORK_RUN_DIR unset it
    # relativizes to '.mini-ork', poisoning the GRPO code_region slice. files_written
    # must be seeded from git-visible target-repo changes first (untracked + unstaged),
    # and .mini-ork/runs artifacts must be excluded via .gitignore.
    db = _seed_db(tmp_path, "implregion"); _seed_task_run(db, rid="run-i", status="executing")
    repo = _git_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text(".mini-ork/runs/\n")   # mirror production: run artifacts ignored
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "gitignore"], check=True, capture_output=True)
    (repo / "app").mkdir()
    (repo / "app" / "service.py").write_text("x = 1\n")     # new untracked source edit (implementer output)
    run_dir = repo / ".mini-ork" / "runs" / "run-i"; run_dir.mkdir(parents=True)
    impl_log = run_dir / "impl-impl1.log"; impl_log.write_text("done\n")  # the poisoning output_file
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)   # the env that triggered the bug
    monkeypatch.delenv("RUN_DIR", raising=False)
    tf = ex._make_trace_fn("code_fix", db, "run-i")
    tf("impl1", "success", "implementer", output_file=str(impl_log), finish_reason="done")
    region = _sql(db, "SELECT code_region FROM execution_traces "
                      "WHERE run_id='run-i';").stdout.strip()
    assert region == "app", f"expected 'app' from target-repo edit, got {region!r}"


# ── orchestration backbone golden contract (NODE_IDS + --dry-run) ──


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
    assert ex.nodes_from_workflow(wf) == [
        "plan1\x1fplanner\x1fmake a plan\x1f\x1fserial\x1f\x1fopus_lens\x1f",
        "res1\x1fresearcher\x1fresearch it\x1f\x1fparallel\x1f\x1fkimi_lens\x1f",
        "impl1\x1fimplementer\x1fbuild it\x1f\x1fserial\x1f\x1fimplementer\x1f",
        "rev1\x1freviewer\x1freview it\x1f\x1fserial\x1fverifiers/check.sh\x1freviewer\x1f",
        "rb1\x1frollback\x1fundo\x1f\x1fserial\x1f\x1frollback\x1f",
    ]


def test_self_migrate_workflow_orders_pre_retirement_parity_before_migrator():
    workflow = yaml.safe_load((REPO / "recipes" / "self-migrate" / "workflow.yaml").read_text())
    names = [node["name"] for node in workflow["nodes"]]

    assert workflow["dispatch_mode"] == "serial"
    assert names.index("pre_retirement_parity") < names.index("migrator")


def test_self_migrate_verifier_exit_status_matches_false_json(tmp_path):
    """A reported migration failure must also fail the process-level gate."""
    target = tmp_path / "target"
    (target / "bin").mkdir(parents=True)
    (target / "gates").mkdir()
    failing_gate = target / "gates" / "feature_acceptance.sh"
    failing_gate.write_text("#!/usr/bin/env bash\nexit 1\n")
    failing_gate.chmod(0o755)

    cases = [
        "pre-retirement-parity.sh",
        "parity.sh",
        "feature-acceptance.sh",
        "ledger-shape.sh",
        "fork-closure.sh",
    ]
    for name in cases:
        run_dir = tmp_path / name.removesuffix(".sh")
        run_dir.mkdir()
        legacy_entrypoint = target / "bin" / f"mini-ork-{'verify'}"
        if name == "fork-closure.sh":
            legacy_entrypoint.write_text("#!/usr/bin/env bash\n")
        script = REPO / "recipes" / "self-migrate" / "verifiers" / name
        result = subprocess.run(
            ["bash", str(script)],
            cwd=target,
            env={
                **os.environ,
                "MINI_ORK_RUN_DIR": str(run_dir),
                "MINI_ORK_ROOT": str(target),
                "MO_TARGET_CWD": str(target),
                "MO_FORK": "verify",
            },
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        assert payload["pass"] is False, (name, result.stdout, result.stderr)
        assert result.returncode != 0, (name, result.stdout, result.stderr)
        legacy_entrypoint.unlink(missing_ok=True)

    ledger_run = tmp_path / "ledger-success"
    ledger_run.mkdir()
    (ledger_run / "static-feature-ledger.json").write_text(json.dumps({
        "features": [{
            "feature": "function:mini_ork.ported.example.main",
            "class": "static",
            "opportunity": "deterministic",
        }],
    }))
    (ledger_run / "self-migrate.diff").write_text(
        "diff --git a/example.py b/example.py\n"
        "+def main():\n"
        "+    return 0\n"
    )
    ledger_result = subprocess.run(
        ["bash", str(REPO / "recipes" / "self-migrate" / "verifiers" / "ledger-shape.sh")],
        env={**os.environ, "MINI_ORK_RUN_DIR": str(ledger_run)},
        capture_output=True,
        text=True,
    )
    assert ledger_result.returncode == 0, ledger_result.stdout + ledger_result.stderr
    assert json.loads(ledger_result.stdout)["pass"] is True


def test_nodes_from_plan_parity(tmp_path):
    wf = _write(tmp_path, "wf.yaml", _WF)
    plan = _write(tmp_path, "plan.json", _PLAN)
    assert ex.nodes_from_plan(plan, wf) == [
        "res1\x1fresearcher\x1fresearch\x1f\x1fparallel\x1f\x1fkimi_lens\x1f",
        "impl1\x1fimplementer\x1fbuild\x1f\x1fserial\x1f\x1fimplementer\x1f",
        "bad\x1fimplementer\x1fno type defaults to implementer\x1f\x1fserial\x1f\x1fimplementer\x1f",
    ]


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
    old = dict(os.environ); os.environ.clear(); os.environ.update(env)
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = ex.main(["--dry-run"], root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rc == 0
    # workflow source has 5 nodes; 4 dispatch + 1 rollback skip
    assert len(_dispatch_lines(buf.getvalue())) == 5
    assert any("rollback" in ln for ln in _dispatch_lines(buf.getvalue()))


def test_dry_run_node_type_filter(tmp_path):
    wf = _write(tmp_path, "wf.yaml", _WF)
    home = tmp_path / ".mini-ork"; home.mkdir()
    plan = str(home / "plan.json"); Path(plan).write_text(_PLAN)
    env = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_WORKFLOW": wf,
           "MINI_ORK_HOME": str(home), "MINI_ORK_PLAN_PATH": plan, "MINI_ORK_DRY_RUN": "1"}
    old = dict(os.environ); os.environ.clear(); os.environ.update(env)
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            ex.main(["--dry-run", "--node-type", "researcher"], root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    assert len(_dispatch_lines(buf.getvalue())) == 1   # only res1


# ── execute pre-dispatch gate (port of tests/integration/test_dispatch_telemetry_gate.sh §4) ──
# A plan_status=needs_answers plan carrying real human_questions must be REFUSED
# before any node dispatches — the run has an open question only a human can
# answer, so dispatching would burn LLM budget producing work against an
# unresolved premise. The gate fires at mini_ork_execute:946 (before node
# resolution), so these tests never call a provider.

def _needs_answers_plan(questions=("q1",)):
    return json.dumps({
        "plan_status": "needs_answers", "blocked_by": "run_profile",
        "human_questions": list(questions), "decomposition": [],
    })


def test_execute_gate_refuses_needs_answers_plan(tmp_path):
    """End-to-end: ex.main on a needs_answers plan exits 6 and leaves a
    fail-closed audit trail — blocked.json, a task_runs failed/ESCALATE row, and
    exactly one execute_blocked run_event. Nothing dispatches."""
    db = _seed_db(tmp_path, "gate")
    home = Path(db).parent
    _seed_task_run(db, rid="run-gate", status="planned")
    run_dir = home / "runs" / "run-gate"; run_dir.mkdir(parents=True)
    plan = run_dir / "plan.json"; plan.write_text(_needs_answers_plan())

    env = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": str(home),
           "MINI_ORK_DB": db, "MINI_ORK_RUN_ID": "run-gate", "MINI_ORK_DRY_RUN": "0"}
    env.pop("MINI_ORK_EXECUTE_GATE", None)  # gate defaults ON
    old = dict(os.environ); os.environ.clear(); os.environ.update(env)
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = ex.main([str(plan)], root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)

    assert rc == 6                                             # refused before dispatch
    assert "[blocked]" in buf.getvalue()                       # human-readable refusal
    assert (run_dir / "blocked.json").is_file()                # fail-closed artifact
    # verdict='ESCALATE' (schema CHECK forbids 'BLOCKED'); the "blocked" identity lives
    # in status='failed' + the execute_blocked run_event + notes + blocked.json.
    assert _sql(db, "SELECT status||'|'||COALESCE(verdict,'') FROM task_runs "
                    "WHERE id='run-gate';").stdout.strip() == "failed|ESCALATE"
    assert _sql(db, "SELECT count(*) FROM run_events "
                    "WHERE event_type='execute_blocked';").stdout.strip() == "1"


def test_execute_gate_bypasses_override_dryrun_and_zero_question(tmp_path):
    """The gate must NOT block when the operator overrides it
    (MINI_ORK_EXECUTE_GATE=0), under dry-run, or on the needs_answers-with-ZERO-
    questions contradiction — each returns False so the run proceeds. Exercised on
    _execute_gate_check directly so no node ever dispatches."""
    db = _seed_db(tmp_path, "bypass")
    home = Path(db).parent
    run_dir = home / "runs" / "run-gate"; run_dir.mkdir(parents=True)
    blocking = run_dir / "plan.json"; blocking.write_text(_needs_answers_plan())
    zero_q = run_dir / "plan_zero_q.json"; zero_q.write_text(_needs_answers_plan(questions=()))

    base = {**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db,
            "MINI_ORK_RUN_ID": "run-gate"}

    def _gate(plan_path, dry_run, gate):
        env = {**base, "MINI_ORK_EXECUTE_GATE": gate}
        old = dict(os.environ); os.environ.clear(); os.environ.update(env)
        try:
            return ex._execute_gate_check(str(plan_path), str(run_dir), dry_run)
        finally:
            os.environ.clear(); os.environ.update(old)

    assert _gate(blocking, False, "1") is True    # sanity: the gate DOES block when armed
    assert _gate(blocking, False, "0") is False   # operator override
    assert _gate(blocking, True, "1") is False    # dry-run skip
    assert _gate(zero_q, False, "1") is False     # needs_answers + 0 questions = not a real block
