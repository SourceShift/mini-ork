"""A/B parity: native oracle-gate evaluators vs the gates/*.sh bash shims (WS4).

For each of the 5 oracle gates (coalition, liveness, panel-health, stability,
synthesis-promote) this suite drives the SAME fixture through

  (A) the live bash shim ``gates/<name>.sh <context_json>`` via subprocess
      (rc 0 → pass, 2 → defer, else fail — the registry's contract), and
  (B) the native in-process evaluator through
      ``gate_registry.gate_register`` + ``gate_evaluate``, once with the new
      ``native:<name>`` sentinel condition and once with the legacy
      ``<root>/gates/<name>.sh`` script-path condition (the shape live DBs
      still hold — those rows must evaluate natively WITHOUT executing the
      shim).

Verdicts must be identical across A and B for every fixture.

Fixtures use a real state.db seeded by ``db/init.sh`` (plus hand-seeded
execution_traces / task_runs rows), and real verdict JSON files for the
file-driven gates. Env knobs are pinned to defaults on BOTH sides
(monkeypatch.delenv for the in-process side, a scrubbed env for the
subprocess side) except the one advisory-mode case that sets them
deliberately on both sides.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.gates import gate_registry as gr  # noqa: E402
from mini_ork.gates.native_gates import native_condition  # noqa: E402

DB_INIT = REPO / "db" / "init.sh"

# Env knobs the bash libs read at function entry. Pinned to defaults on both
# sides so the A/B comparison is deterministic regardless of ambient env.
KNOB_ENVS = [
    "MO_RHO_THRESHOLD", "MO_FAMILY_DIVERSITY_GATE",
    "MO_CB_ARTIFACT_WINDOW", "MO_CB_VERDICT_WINDOW", "MO_CB_COST_THRESHOLD",
    "MO_CB_POLICY", "MO_CB_COOLDOWN_S", "MO_CB_DISABLE",
    "MO_PANEL_STABILITY_THRESHOLD", "MO_PANEL_MIN_ROUNDS", "MO_PANEL_MAX_ROUNDS",
    "MO_CW_POR_THRESHOLD", "MO_PROMOTE_SCORE_THRESHOLD",
    "MO_MIN_CITATION_DENSITY", "MO_MIN_FINDING_CARDINALITY",
    "MO_DETERMINISTIC_TASK_CLASSES",
]


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Fresh state.db via db/init.sh + a runs row (execution_traces FK)."""
    home = tmp_path / "home"
    home.mkdir()
    db_path = str(home / "state.db")
    subprocess.run(
        ["bash", str(DB_INIT)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db_path},
        capture_output=True, text=True, timeout=60, check=True,
    )
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT OR IGNORE INTO runs (id, agent, final_verdict) "
        "VALUES (1, 'test', 'APPROVE')"
    )
    con.commit()
    con.close()
    monkeypatch.setenv("MINI_ORK_DB", db_path)
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    for k in KNOB_ENVS:
        monkeypatch.delenv(k, raising=False)
    return db_path


# ── A/B drivers ───────────────────────────────────────────────────────────────


def _bash_verdict(gate_name: str, context_json: str, db: str,
                  extra_env: dict | None = None) -> str:
    """Run the live bash shim; map rc → registry verdict contract."""
    script = REPO / "gates" / f"{gate_name}.sh"
    env = {k: v for k, v in os.environ.items() if k not in KNOB_ENVS}
    env["MINI_ORK_DB"] = db
    env["MINI_ORK_ROOT"] = str(REPO)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [str(script), context_json],
        capture_output=True, text=True, timeout=60, env=env,
    )
    rc = proc.returncode
    if rc == 0:
        return "pass"
    if rc == 2:
        return "defer"
    return "fail"


def _py_verdict(gate_name: str, context_json: str, db: str,
                condition_form: str = "sentinel") -> str:
    """Evaluate via the native path: register a custom gate whose condition is
    either the ``native:<name>`` sentinel or the legacy script path, then
    gate_evaluate it."""
    if condition_form == "sentinel":
        condition = native_condition(gate_name)
    elif condition_form == "script-path":
        condition = str(REPO / "gates" / f"{gate_name}.sh")
    else:
        raise AssertionError(f"unknown form {condition_form}")
    gid = gr.gate_register(db, "custom", condition)
    assert gid, "gate_register returned empty gate_id"
    return gr.gate_evaluate(db, gid, context_json, mini_ork_root=str(REPO))


def assert_ab(gate_name: str, context_json: str, db: str, expected: str,
              extra_env: dict | None = None) -> None:
    """A/B assertion: bash shim verdict == native verdict == expected, for
    BOTH condition forms (sentinel + legacy script path)."""
    bash_v = _bash_verdict(gate_name, context_json, db, extra_env=extra_env)
    assert bash_v == expected, (
        f"[{gate_name}] bash shim verdict {bash_v!r} != expected {expected!r}"
    )
    for form in ("sentinel", "script-path"):
        py_v = _py_verdict(gate_name, context_json, db, condition_form=form)
        assert py_v == bash_v, (
            f"[{gate_name}] native({form}) verdict {py_v!r} "
            f"!= bash shim {bash_v!r}"
        )


# ── seeding helpers ───────────────────────────────────────────────────────────


def _seed_traces(db: str, rows: list[tuple]) -> None:
    """rows: (trace_id, agent_version_id, reviewer_verdict, cost_usd, files_written)."""
    con = sqlite3.connect(db)
    for tid, av, verdict, cost, fw in rows:
        con.execute(
            "INSERT INTO execution_traces "
            "(trace_id, agent_version_id, run_id, task_class, status, "
            " reviewer_verdict, cost_usd, files_written) "
            "VALUES (?,?,1,'refactor_audit','success',?,?,?)",
            (tid, av, verdict, cost, fw),
        )
    con.commit()
    con.close()


def _seed_task_runs(db: str, rows: list[tuple]) -> None:
    """rows: (id, task_class, recipe, artifact_hash, created_at)."""
    con = sqlite3.connect(db)
    for rid, tc, recipe, ah, ts in rows:
        con.execute(
            "INSERT INTO task_runs "
            "(id, task_class, recipe, kickoff_path, artifact_hash, "
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (rid, tc, recipe, "kickoff.md", ah, ts, ts),
        )
    con.commit()
    con.close()


def _write_verdict(tmp_path: Path, name: str, payload: dict) -> str:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


# ═════════════════════════════════════════════════════════════════════════════
# coalition (gates/coalition.sh → coalition_gate.py + topology measure_rho)
# ═════════════════════════════════════════════════════════════════════════════


def test_coalition_same_family_panel_aborts(db):
    prun = "run-ab-coal-collision"
    _seed_traces(db, [
        (f"tr-1-{prun}", "sonnet", "APPROVE", 0.0, "[]"),
        (f"tr-2-{prun}", "opus", "APPROVE", 0.0, "[]"),
        (f"tr-3-{prun}", "sonnet", "APPROVE", 0.0, "[]"),
        (f"tr-4-{prun}", "opus", "APPROVE", 0.0, "[]"),
    ])
    ctx = json.dumps({"panel_run_id": prun, "recipe": "refactor-audit"})
    assert_ab("coalition", ctx, db, "fail")


def test_coalition_diverse_panel_passes(db):
    prun = "run-ab-coal-diverse"
    # Varying verdicts keep ρ < threshold so only family diversity decides.
    _seed_traces(db, [
        (f"tr-1-{prun}", "glm", "APPROVE: findings cluster A", 0.0, "[]"),
        (f"tr-2-{prun}", "kimi", "REQUEST_CHANGES: missed B", 0.0, "[]"),
        (f"tr-3-{prun}", "codex", "APPROVE: focus on perf", 0.0, "[]"),
        (f"tr-4-{prun}", "minimax", "ESCALATE: security gap C", 0.0, "[]"),
    ])
    ctx = json.dumps({"panel_run_id": prun, "recipe": "refactor-audit"})
    assert_ab("coalition", ctx, db, "pass")


def test_coalition_advisory_mode_passes_on_collision(db, monkeypatch):
    """MO_FAMILY_DIVERSITY_GATE=advisory: collision emits a warning but the
    shim exits 0 on both sides."""
    monkeypatch.setenv("MO_FAMILY_DIVERSITY_GATE", "advisory")
    prun = "run-ab-coal-advisory"
    _seed_traces(db, [
        (f"tr-1-{prun}", "sonnet", "APPROVE", 0.0, "[]"),
        (f"tr-2-{prun}", "opus", "APPROVE", 0.0, "[]"),
        (f"tr-3-{prun}", "sonnet", "APPROVE", 0.0, "[]"),
        (f"tr-4-{prun}", "opus", "APPROVE", 0.0, "[]"),
    ])
    ctx = json.dumps({"panel_run_id": prun, "recipe": "refactor-audit"})
    assert_ab("coalition", ctx, db, "pass",
              extra_env={"MO_FAMILY_DIVERSITY_GATE": "advisory"})


def test_coalition_single_agent_fail_open(db):
    prun = "run-ab-coal-single"
    _seed_traces(db, [(f"tr-1-{prun}", "sonnet", "APPROVE", 0.0, "[]")])
    ctx = json.dumps({"panel_run_id": prun, "recipe": "code-fix"})
    assert_ab("coalition", ctx, db, "pass")


def test_coalition_missing_context_defers(db):
    assert_ab("coalition", "{}", db, "defer")
    assert_ab("coalition", json.dumps({"panel_run_id": "x"}), db, "defer")


# ═════════════════════════════════════════════════════════════════════════════
# liveness (gates/liveness.sh → recovery/circuit_breaker.py)
# ═════════════════════════════════════════════════════════════════════════════


def test_liveness_unknown_run_proceeds(db):
    assert_ab("liveness", json.dumps({"run_id": "run-not-in-db"}), db, "pass")


def test_liveness_panel_run_id_backcompat_key(db):
    # The shim accepts panel_run_id as a fallback key for run_id.
    assert_ab("liveness", json.dumps({"panel_run_id": "run-not-in-db"}), db, "pass")


def test_liveness_missing_run_id_defers(db):
    assert_ab("liveness", "{}", db, "defer")


def test_liveness_trip_fails(db):
    """3-of-3 stagnation signals → LIVENESS_TRIP on both sides.

    artifact_invariant: last 3 task_runs in scope share one artifact_hash;
    verdict_stuck: last 3 traces all REQUEST_CHANGES; cost_burn_no_write:
    Σcost=1.5 > 1.0 with zero unique files_written. Running A then B against
    the same DB keeps the breaker OPEN (cooldown not elapsed), so both sides
    report the trip.
    """
    rid = "run-ab-cb-trip"
    _seed_task_runs(db, [
        (rid, "tc", "r", "aaaaaaaahash", 100),
        ("run-ab-cb-trip-prev1", "tc", "r", "aaaaaaaahash", 90),
        ("run-ab-cb-trip-prev2", "tc", "r", "aaaaaaaahash", 80),
    ])
    _seed_traces(db, [
        (f"tr-1-{rid}", "sonnet", "REQUEST_CHANGES", 0.5, "[]"),
        (f"tr-2-{rid}", "sonnet", "REQUEST_CHANGES", 0.5, "[]"),
        (f"tr-3-{rid}", "sonnet", "REQUEST_CHANGES", 0.5, "[]"),
    ])
    assert_ab("liveness", json.dumps({"run_id": rid}), db, "fail")


def test_liveness_productive_run_proceeds(db):
    rid = "run-ab-cb-ok"
    _seed_task_runs(db, [
        (rid, "tc2", "r", "hash-one", 100),
        ("run-ab-cb-ok-prev1", "tc2", "r", "hash-two", 90),
        ("run-ab-cb-ok-prev2", "tc2", "r", "hash-three", 80),
    ])
    _seed_traces(db, [
        (f"tr-1-{rid}", "sonnet", "APPROVE", 0.1, '[{"path":"a.py"}]'),
        (f"tr-2-{rid}", "sonnet", "REQUEST_CHANGES", 0.1, '[{"path":"b.py"}]'),
        (f"tr-3-{rid}", "sonnet", "APPROVE", 0.1, '[{"path":"c.py"}]'),
    ])
    assert_ab("liveness", json.dumps({"run_id": rid}), db, "pass")


# ═════════════════════════════════════════════════════════════════════════════
# panel-health (gates/panel-health.sh → cw_por.py)
# ═════════════════════════════════════════════════════════════════════════════

_CAPTURE_VOTERS = [
    {"voter_id": "w1", "vote": "reject", "confidence": 0.95,
     "ground_truth_match": False},
    {"voter_id": "w2", "vote": "reject", "confidence": 0.90,
     "ground_truth_match": False},
    {"voter_id": "c1", "vote": "approve", "confidence": 0.60,
     "ground_truth_match": True},
]

_HEALTHY_VOTERS = [
    {"voter_id": "c1", "vote": "approve", "confidence": 0.90,
     "ground_truth_match": True},
    {"voter_id": "c2", "vote": "approve", "confidence": 0.85,
     "ground_truth_match": True},
    {"voter_id": "w1", "vote": "reject", "confidence": 0.60,
     "ground_truth_match": False},
]


def test_panel_health_authority_capture_fails(db, tmp_path):
    vf = _write_verdict(tmp_path, "capture.json", {"voters": _CAPTURE_VOTERS})
    assert_ab("panel-health", json.dumps({"verdict_file": vf}), db, "fail")


def test_panel_health_healthy_passes(db, tmp_path):
    vf = _write_verdict(tmp_path, "healthy.json", {"voters": _HEALTHY_VOTERS})
    assert_ab("panel-health", json.dumps({"verdict_file": vf}), db, "pass")


def test_panel_health_indeterminate_passes(db, tmp_path):
    vf = _write_verdict(tmp_path, "indet.json", {"voters": [
        {"voter_id": "a", "vote": "approve", "confidence": 0.9},
        {"voter_id": "b", "vote": "reject", "confidence": 0.8},
    ]})
    assert_ab("panel-health", json.dumps({"verdict_file": vf}), db, "pass")


def test_panel_health_missing_input_defers(db, tmp_path):
    assert_ab("panel-health", "{}", db, "defer")
    missing = str(tmp_path / "no-such-file.json")
    assert_ab("panel-health", json.dumps({"verdict_file": missing}), db, "defer")


# ═════════════════════════════════════════════════════════════════════════════
# stability (gates/stability.sh → adaptive_stability.py)
# ═════════════════════════════════════════════════════════════════════════════


def _seed_stability_rounds(db: str, prun: str, verdicts_r1: dict,
                           verdicts_r2: dict) -> None:
    rows = []
    for agent, v in verdicts_r1.items():
        rows.append((f"tr-{agent}-r1-{prun}", agent, v, 0.0, "[]"))
    for agent, v in verdicts_r2.items():
        rows.append((f"tr-{agent}-r2-{prun}", agent, v, 0.0, "[]"))
    _seed_traces(db, rows)


def test_stability_stabilized_panel_halts(db):
    prun = "run-ab-stab-halt"
    same = {"glm": "approve: a", "kimi": "reject: b"}
    _seed_stability_rounds(db, prun, same, dict(same))
    ctx = json.dumps({"panel_run_id": prun, "current_round": 2})
    assert_ab("stability", ctx, db, "fail")  # HALT → rc 1 → fail


def test_stability_moving_panel_continues(db):
    prun = "run-ab-stab-move"
    _seed_stability_rounds(
        db, prun,
        {"glm": "approve: a", "kimi": "reject: b"},
        {"glm": "reject: c", "kimi": "approve: d"},
    )
    ctx = json.dumps({"panel_run_id": prun, "current_round": 2})
    assert_ab("stability", ctx, db, "pass")


def test_stability_below_min_rounds_continues(db):
    prun = "run-ab-stab-min"
    same = {"glm": "approve: a", "kimi": "reject: b"}
    _seed_stability_rounds(db, prun, same, dict(same))
    ctx = json.dumps({"panel_run_id": prun, "current_round": 1})
    assert_ab("stability", ctx, db, "pass")


def test_stability_no_traces_fail_open(db):
    ctx = json.dumps({"panel_run_id": "run-ab-stab-none", "current_round": 3})
    assert_ab("stability", ctx, db, "pass")


def test_stability_missing_panel_run_id_defers(db):
    assert_ab("stability", "{}", db, "defer")


# ═════════════════════════════════════════════════════════════════════════════
# synthesis-promote (gates/synthesis-promote.sh → promotion_gate.py)
# ═════════════════════════════════════════════════════════════════════════════


def test_synthesis_promote_deterministic_class_bypasses(db, tmp_path):
    vf = _write_verdict(tmp_path, "det.json",
                        {"panel_score": 0, "voters": [], "structural": {}})
    ctx = json.dumps({"verdict_file": vf, "task_class": "code_fix"})
    assert_ab("synthesis-promote", ctx, db, "pass")


def test_synthesis_promote_all_conditions_met(db, tmp_path):
    vf = _write_verdict(tmp_path, "ok.json", {
        "panel_score": 87.5,
        "voters": _HEALTHY_VOTERS,
        "structural": {"citation_density_per_lens": 5.2,
                       "file_coverage_delta": 3, "finding_cardinality": 11},
    })
    ctx = json.dumps({"verdict_file": vf, "task_class": "research_synthesis"})
    assert_ab("synthesis-promote", ctx, db, "pass")


def test_synthesis_promote_low_score_rejects(db, tmp_path):
    vf = _write_verdict(tmp_path, "low.json", {
        "panel_score": 62.0,
        "voters": [],
        "structural": {"citation_density_per_lens": 8.0,
                       "file_coverage_delta": 5, "finding_cardinality": 20},
    })
    ctx = json.dumps({"verdict_file": vf, "task_class": "refactor_audit"})
    assert_ab("synthesis-promote", ctx, db, "fail")


def test_synthesis_promote_authority_capture_rejects(db, tmp_path):
    vf = _write_verdict(tmp_path, "cap.json", {
        "panel_score": 90.0,
        "voters": _CAPTURE_VOTERS,
        "structural": {"citation_density_per_lens": 5.2,
                       "file_coverage_delta": 3, "finding_cardinality": 11},
    })
    ctx = json.dumps({"verdict_file": vf, "task_class": "research_synthesis"})
    assert_ab("synthesis-promote", ctx, db, "fail")


def test_synthesis_promote_missing_inputs_defer(db, tmp_path):
    assert_ab("synthesis-promote", "{}", db, "defer")
    vf = _write_verdict(tmp_path, "ok2.json", {"panel_score": 90.0})
    # missing task_class
    assert_ab("synthesis-promote", json.dumps({"verdict_file": vf}), db, "defer")
    # verdict_file not found
    ctx = json.dumps({"verdict_file": str(tmp_path / "nope.json"),
                      "task_class": "ui_audit"})
    assert_ab("synthesis-promote", ctx, db, "defer")


def test_synthesis_promote_bad_json_defers(db, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    ctx = json.dumps({"verdict_file": str(bad), "task_class": "ui_audit"})
    assert_ab("synthesis-promote", ctx, db, "defer")


# ═════════════════════════════════════════════════════════════════════════════
# Legacy executable contract — non-oracle script paths still dispatch via
# subprocess (the DB-facing contract for user-registered custom gates).
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("rc,expected", [(0, "pass"), (1, "fail"), (2, "defer"),
                                         (7, "fail")])
def test_unknown_script_path_uses_executable_contract(db, tmp_path,
                                                      rc, expected):
    script = tmp_path / f"custom-gate-{rc}.sh"
    script.write_text(f"#!/bin/sh\nexit {rc}\n", encoding="utf-8")
    script.chmod(0o755)
    gid = gr.gate_register(db, "custom", str(script))
    assert gr.gate_evaluate(db, gid, "{}") == expected


def test_nonexistent_and_nonexecutable_conditions_defer(db, tmp_path):
    gid = gr.gate_register(db, "custom", str(tmp_path / "missing.sh"))
    assert gr.gate_evaluate(db, gid, "{}") == "defer"
    not_exec = tmp_path / "not-exec.sh"
    not_exec.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    not_exec.chmod(0o644)
    gid2 = gr.gate_register(db, "custom", str(not_exec))
    assert gr.gate_evaluate(db, gid2, "{}") == "defer"
