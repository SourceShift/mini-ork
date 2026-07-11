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


# ── live-path support helpers (increment 4) ──

def _seed_task_run(db, rid="r1", status="planned", cost=0.0):
    _sql(db, f"INSERT INTO task_runs (id,task_class,workflow_version,kickoff_path,status,"
             f"cost_usd,created_at,updated_at) VALUES ('{rid}','x','v1','k.md','{status}',"
             f"{cost},strftime('%s','now','-60 seconds'),strftime('%s','now'));")


def test_set_status_parity(tmp_path):
    db_b, db_p = _seed_db(tmp_path, "ssb"), _seed_db(tmp_path, "ssp")
    for db in (db_b, db_p):
        _seed_task_run(db)
    fn = _extract("_d021_set_status")
    subprocess.run(["bash", "-c", f'DRY_RUN=0\nMINI_ORK_DB="{db_b}"\nMINI_ORK_RUN_ID="r1"\n{fn}\n'
                    '_d021_set_status "published"'], capture_output=True, text=True)
    ex.set_status(db_p, "r1", "published")
    q = "SELECT status, ended_at IS NOT NULL AND ended_at>0, duration_ms>0 FROM task_runs WHERE id='r1';"
    assert _sql(db_b, q).stdout.strip() == _sql(db_p, q).stdout.strip()
    assert _sql(db_p, q).stdout.strip() == "published|1|1"   # terminal stamps ended_at + duration
    # non-terminal transition: no ended_at
    for db in (db_b, db_p):
        _seed_task_run(db, rid="r2")
    subprocess.run(["bash", "-c", f'DRY_RUN=0\nMINI_ORK_DB="{db_b}"\nMINI_ORK_RUN_ID="r2"\n{fn}\n'
                    '_d021_set_status "reviewing"'], capture_output=True, text=True)
    ex.set_status(db_p, "r2", "reviewing")
    q2 = "SELECT status, ended_at FROM task_runs WHERE id='r2';"
    assert _sql(db_b, q2).stdout == _sql(db_p, q2).stdout
    assert _sql(db_p, q2).stdout.strip() == "reviewing|"


def test_charge_node_cost_parity(tmp_path):
    db_b, db_p = _seed_db(tmp_path, "ccb"), _seed_db(tmp_path, "ccp")
    for db in (db_b, db_p):
        _seed_task_run(db, cost=0.10)
    cost_file = tmp_path / "cost"; cost_file.write_text("0.037")
    fn = _extract("_d022_charge_node_cost")
    subprocess.run(["bash", "-c", f'DRY_RUN=0\nMINI_ORK_DB="{db_b}"\nMINI_ORK_RUN_ID="r1"\n'
                    f'MINI_ORK_ROOT="{tmp_path}"\n{fn}\n_d022_charge_node_cost "{cost_file}"'],
                   capture_output=True, text=True)
    ex.charge_node_cost(db_p, "r1", str(cost_file), root=str(tmp_path))
    q = "SELECT printf('%.4f', cost_usd) FROM task_runs WHERE id='r1';"
    assert _sql(db_b, q).stdout == _sql(db_p, q).stdout
    assert _sql(db_p, q).stdout.strip() == "0.1370"   # 0.10 + 0.037
    # invalid cost file → $0.01 placeholder on both
    for db in (db_b, db_p):
        _seed_task_run(db, rid="r3", cost=0)
    bad = tmp_path / "bad"; bad.write_text("not-a-number")
    subprocess.run(["bash", "-c", f'DRY_RUN=0\nMINI_ORK_DB="{db_b}"\nMINI_ORK_RUN_ID="r3"\n'
                    f'{fn}\n_d022_charge_node_cost "{bad}"'], capture_output=True, text=True)
    ex.charge_node_cost(db_p, "r3", str(bad))
    q3 = "SELECT printf('%.4f', cost_usd) FROM task_runs WHERE id='r3';"
    assert _sql(db_b, q3).stdout == _sql(db_p, q3).stdout == "0.0100\n"


def _git_repo(d):
    d.mkdir(parents=True)
    for a in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e"],
              ["config", "user.name", "t"]):
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


def _run_bash_apply(impl_log, target):
    fn = _extract("mo_apply_impl_output")
    subprocess.run(["bash", "-c", f'MO_APPLY_IMPL_OUTPUT=1\n{fn}\n'
                    f'mo_apply_impl_output "{impl_log}" "{target}"'], capture_output=True, text=True)


def test_apply_impl_output_diff_parity(tmp_path):
    logf = tmp_path / "impl.log"; logf.write_text(_DIFF_LOG)
    rb = _git_repo(tmp_path / "rb"); rp = _git_repo(tmp_path / "rp")
    _run_bash_apply(str(logf), str(rb))
    ex.apply_impl_output(str(logf), str(rp))
    assert (rb / "app.py").read_text() == (rp / "app.py").read_text() == "x = 1\ny = 2\n"


def test_apply_impl_output_fenced_parity(tmp_path):
    logf = tmp_path / "impl.log"; logf.write_text(_FENCED_LOG)
    rb = _git_repo(tmp_path / "rb"); rp = _git_repo(tmp_path / "rp")
    _run_bash_apply(str(logf), str(rb))
    ex.apply_impl_output(str(logf), str(rp))
    assert (rb / "newmod.py").exists() and (rp / "newmod.py").exists()
    assert (rb / "newmod.py").read_text() == (rp / "newmod.py").read_text()
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


def test_live_researcher_writes_context(tmp_path):
    db = _seed_db(tmp_path, "r"); _seed_task_run(db)
    rd = tmp_path / "run"; rd.mkdir()
    rc, fr = ex.dispatch_node(_fields("res1_lens", "researcher", "kimi_lens"),
                              root=str(REPO), run_dir=str(rd), plan_path=_plan(tmp_path),
                              task_class="code_fix", db=db, run_id="r1",
                              dispatch_fn=_fake("finding: X is slow"))
    assert rc == 0 and fr == "done"
    assert (rd / "lens-res1.md").read_text() == "finding: X is slow"
    # cost charged
    assert float(_sql(db, "SELECT cost_usd FROM task_runs WHERE id='r1';").stdout) > 0


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
    # a synth reviewer never gates → always success
    rc_synth, _ = ex.dispatch_node(_fields("synth_node", "reviewer", "opus"),
                                   dispatch_fn=_fake("# Synthesis\ntop findings..."), **common)
    assert rc_synth == 0


def test_run_verifier_ref(tmp_path):
    ok = tmp_path / "ok.sh"; ok.write_text('#!/usr/bin/env bash\necho \'{"pass": true}\'\n')
    bad = tmp_path / "bad.sh"; bad.write_text('#!/usr/bin/env bash\necho \'{"pass": false}\'\n')
    empty = tmp_path / "empty.sh"; empty.write_text('#!/usr/bin/env bash\nexit 0\n')
    assert ex._run_verifier_ref(str(ok), str(tmp_path / "e1"), cwd=str(tmp_path)) == 0
    assert ex._run_verifier_ref(str(bad), str(tmp_path / "e2"), cwd=str(tmp_path)) == 1
    assert ex._run_verifier_ref(str(empty), str(tmp_path / "e3"), cwd=str(tmp_path)) == 1  # vacuous


def test_live_publisher_and_rollback_status(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_ORACLE_GATES_AUTO", "0")  # isolate from the oracle-gate shell-out
    db = _seed_db(tmp_path, "pub"); _seed_task_run(db)
    rd = tmp_path / "run"; rd.mkdir()
    common = dict(root=str(REPO), run_dir=str(rd), plan_path=_plan(tmp_path),
                  task_class="code_fix", db=db, run_id="r1", dispatch_fn=_fake(""))
    # No recipe → no artifact_contract.yaml. Bash returns 0 WITHOUT publishing
    # (bin/mini-ork-execute:3017-3019). The old port stub wrongly always marked
    # 'published' (panel finding 2); the faithful port leaves status unchanged.
    rc, _ = ex.dispatch_node(_fields("pub", "publisher"), **common)
    assert rc == 0 and _sql(db, "SELECT status FROM task_runs WHERE id='r1';").stdout.strip() != "published"
    # F4: rollback is best-effort (bash :3205-3223) — returns 0/done regardless of
    # whether a prior version exists, and does NOT set task_runs.status. The upstream
    # failure already fails the run. Was wrongly (1,'rolled_back') + status mutation.
    rc_rb, fr_rb = ex.dispatch_node(_fields("rb", "rollback"), **common)
    assert rc_rb == 0 and fr_rb == "done"
    assert _sql(db, "SELECT status FROM task_runs WHERE id='r1';").stdout.strip() != "rolled_back"


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
