"""Golden contract tests for the sole Python verifier implementation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import verify as ver

def _env(home, db):
    return {
        **os.environ,
        "MINI_ORK_ENGINE_ROOT": str(REPO),
        "MINI_ORK_PROJECT_HOME": str(home),
        "MINI_ORK_TARGET_REPO": str(home.parent),
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_DB": db,
    }


def _scenario(tmp_path, verifiers):
    home = tmp_path / ".mini-ork"; home.mkdir()
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")], env=_env(home, db),
                   capture_output=True, text=True, check=True)
    vdir = home / "verifiers"; vdir.mkdir()
    (vdir / "goodv.py").write_text('print("ok")\n')
    (vdir / "badv.py").write_text('import sys\nprint("nope")\nsys.exit(1)\n')
    plan = home / "plan.json"
    plan.write_text(json.dumps({"task_class": "code_fix",
                                "artifact_contract": {"success_verifiers": verifiers}}))
    return home, db, str(plan)


def _norm(js: str):
    d = json.loads(js)
    return (d["verdict"], d["pass_count"], d["fail_count"],
            [(r["verifier"], r["pass"]) for r in d["results"]])


def _verify(home, db, *args):
    import io
    from contextlib import redirect_stdout, redirect_stderr
    o, e = io.StringIO(), io.StringIO()
    old = dict(os.environ); os.environ.update({"MINI_ORK_HOME": str(home)})
    for k in ("MINI_ORK_DRY_RUN", "MINI_ORK_PLAN_PATH", "MINI_ORK_TASK_CLASS", "MINI_ORK_RUN_DIR"):
        os.environ.pop(k, None)
    try:
        with redirect_stdout(o), redirect_stderr(e):
            rc = ver.main(list(args), db=db, root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    s = o.getvalue()[o.getvalue().index("{"):]
    return s, rc


def test_help_golden(tmp_path):
    home, db, _ = _scenario(tmp_path, [])
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = ver.main(["--help"], db=db, root=str(REPO))
    assert rc == 0
    assert buf.getvalue() == ver._USAGE


def test_all_pass_golden(tmp_path):
    home, db, plan = _scenario(tmp_path, ["goodv"])
    sp, rp = _verify(home, db, "art.txt", "--plan", plan)
    assert rp == 0
    assert _norm(sp) == ("pass", 1, 0, [("goodv", True), ("__gates__", True)])
    assert json.loads(sp)["verdict"] == "pass"


def test_mixed_partial_golden(tmp_path):
    home, db, plan = _scenario(tmp_path, ["goodv", "badv"])
    sp, rp = _verify(home, db, "art.txt", "--plan", plan)
    assert rp == 0
    assert _norm(sp) == (
        "partial", 1, 1,
        [("goodv", True), ("badv", False), ("__gates__", True)],
    )
    assert json.loads(sp)["verdict"] == "partial"


def test_vacuous_golden(tmp_path):
    home, db, plan = _scenario(tmp_path, [])   # no verifiers
    sp, rp = _verify(home, db, "art.txt", "--plan", plan)
    assert rp == 0
    # The `__artifact_contract__` row carries pass=None: this plan declares no
    # artifact requirement, so nothing was asserted about the artifact. It is a
    # deferral, not a pass, and it must be visible as its own row — an empty
    # requirement set and a satisfied one used to look identical here.
    assert _norm(sp) == (
        "vacuous", 0, 0,
        [("__artifact_contract__", None), ("__gates__", True)],
    )


def test_dry_run_golden(tmp_path):
    home, db, plan = _scenario(tmp_path, ["goodv", "badv"])
    sp, rp = _verify(home, db, "art.txt", "--plan", plan, "--dry-run")
    assert rp == 0
    assert _norm(sp) == ("dry-run", 0, 0, [("goodv", None), ("badv", None)])
    assert json.loads(sp)["verdict"] == "dry-run"


def _req_plan(home, artifact_path, verifiers=None):
    """A plan whose artifact_contract requires a concrete (absolute) run-local
    artifact — the hollow-run guard target."""
    plan = home / "plan-req.json"
    plan.write_text(json.dumps({
        "task_class": "framework_edit",
        "artifact_contract": {
            "required_artifacts": [str(artifact_path)],
            "success_verifiers": verifiers or [],
        },
    }))
    return str(plan)


def test_missing_required_artifact_fails(tmp_path):
    # A required artifact that does not exist is a hard failure.
    home, db, _ = _scenario(tmp_path, [])
    missing = tmp_path / "artifact.md"          # never created
    plan = _req_plan(home, missing)
    sp, rp = _verify(home, db, str(missing), "--plan", plan)
    assert json.loads(sp)["verdict"] == "fail"
    assert rp == 1


def test_empty_required_artifact_fails(tmp_path):
    # A zero-byte required artifact is also a hard failure.
    home, db, _ = _scenario(tmp_path, [])
    empty = tmp_path / "artifact.md"; empty.write_text("")   # exists but 0 bytes
    plan = _req_plan(home, empty)
    sp, rp = _verify(home, db, str(empty), "--plan", plan)
    assert json.loads(sp)["verdict"] == "fail"
    assert rp == 1


def test_real_required_artifact_passes(tmp_path):
    # (ii) a real, non-empty required artifact → PASS (exit 0), both impls. This
    # is the 36KB-synthesis false-negative that must NOT be false-failed.
    home, db, _ = _scenario(tmp_path, [])
    real = tmp_path / "synthesis.md"
    real.write_text("# Synthesis\n" + ("evidence line with a real finding\n" * 2000))
    assert real.stat().st_size > 30_000
    plan = _req_plan(home, real)
    sp, rp = _verify(home, db, str(real), "--plan", plan)
    assert json.loads(sp)["verdict"] == "pass"
    assert rp == 0


def test_relative_output_is_exempt(tmp_path):
    # A relative canonical output (publish-target) that doesn't exist yet must NOT
    # trip the guard — only absolute run-local artifacts are enforced.
    home, db, _ = _scenario(tmp_path, [])
    plan = home / "plan-rel.json"
    plan.write_text(json.dumps({
        "task_class": "research_synthesis",
        "artifact_contract": {
            "outputs": ["docs/research/synthesis-latest.md"],  # relative → exempt
            "success_verifiers": ["goodv"],
        },
    }))
    sp, rp = _verify(home, db, "art.txt", "--plan", str(plan))
    assert json.loads(sp)["verdict"] == "pass"
    assert rp == 0


def test_rolled_back_artifact_is_attributed_to_rollback(tmp_path):
    # Post-run verify runs AFTER rollback. A required artifact the rollback
    # node itself deleted still fails the run (a reverted run must not verify
    # as pass) but is labelled rolled_back — not "missing or empty", which
    # blamed the implementer for the rollback's deletion.
    import io
    from contextlib import redirect_stdout, redirect_stderr
    home, db, _ = _scenario(tmp_path, [])
    reverted = tmp_path / "created.css"           # implementer wrote it; rollback removed it
    plan = _req_plan(home, reverted)
    run_dir = tmp_path / "run"; run_dir.mkdir()
    (run_dir / "rolled-back.json").write_text(json.dumps({"paths": [str(reverted)]}))
    o, e = io.StringIO(), io.StringIO()
    old = dict(os.environ)
    os.environ.update({"MINI_ORK_HOME": str(home), "MINI_ORK_RUN_DIR": str(run_dir)})
    try:
        with redirect_stdout(o), redirect_stderr(e):
            rc = ver.main([str(reverted), "--plan", plan], db=db, root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    doc = json.loads(o.getvalue()[o.getvalue().index("{"):])
    art = [r for r in doc["results"] if r["verifier"] == "__artifact__"]
    assert doc["verdict"] == "fail" and rc == 1
    assert art == [{"verifier": "__artifact__", "pass": False, "detail": "rolled_back",
                    "evidence_path": str(reverted)}]
    assert "reverted by rollback" in e.getvalue()
    assert "missing or empty" not in e.getvalue()


# --------------------------------------------------------------------------- #
# instrument ⑩ consumer: a measured, non-discriminating verifier's pass is not
# evidence. Unmeasured (no card, no sidecar) never blocks.
# --------------------------------------------------------------------------- #


def _card_dir(tmp_path):
    d = tmp_path / "verifiers"
    d.mkdir()
    return d


def _card_with_sidecar(d, stem, kill_score):
    card = d / f"{stem}.card.yaml"
    card.write_text("name: x\nkind: behavioral\n")
    Path(str(card) + ".mutation.json").write_text(json.dumps({"kill_score": kill_score}))
    return str(card)


def test_a_verifier_with_no_card_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_VERIFIER_MUTATION", raising=False)
    assert ver._unadmitted_verifier(None, str(tmp_path), "api_contract") == ""
    assert ver._unadmitted_verifier(str(tmp_path / "nope.py"), str(tmp_path), "api_contract") == ""


def test_an_unmeasured_verifier_is_left_alone(tmp_path, monkeypatch):
    """A card with no sidecar is unmeasured, and unmeasured never blocks."""
    monkeypatch.delenv("MO_VERIFIER_MUTATION", raising=False)
    d = _card_dir(tmp_path)
    (d / "api_contract.card.yaml").write_text("name: x\n")
    script = d / "api_contract.py"
    script.write_text("")
    assert ver._unadmitted_verifier(str(script), str(tmp_path), "api_contract") == ""


def test_a_measured_nondiscriminating_verifier_is_not_admitted(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_VERIFIER_MUTATION", raising=False)
    monkeypatch.delenv("MO_VERIFIER_MUTATION_MIN", raising=False)
    d = _card_dir(tmp_path)
    _card_with_sidecar(d, "api_contract", 0.5)
    script = d / "api_contract.py"
    script.write_text("")
    note = ver._unadmitted_verifier(str(script), str(tmp_path), "api_contract")
    assert "mutation kill score 0.50" in note
    assert "0.80" in note


def test_a_discriminating_verifier_is_admitted(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_VERIFIER_MUTATION", raising=False)
    d = _card_dir(tmp_path)
    _card_with_sidecar(d, "api_contract", 1.0)
    script = d / "api_contract.py"
    script.write_text("")
    assert ver._unadmitted_verifier(str(script), str(tmp_path), "api_contract") == ""


def test_the_master_switch_restores_the_pass(tmp_path, monkeypatch):
    d = _card_dir(tmp_path)
    _card_with_sidecar(d, "api_contract", 0.5)
    script = d / "api_contract.py"
    script.write_text("")
    monkeypatch.setenv("MO_VERIFIER_MUTATION", "off")
    assert ver._unadmitted_verifier(str(script), str(tmp_path), "api_contract") == ""


def test_a_root_verifiers_card_is_found_for_a_recipe_local_script(tmp_path, monkeypatch):
    """The card may sit at ``<root>/verifiers`` even when the script does not."""
    monkeypatch.delenv("MO_VERIFIER_MUTATION", raising=False)
    root_cards = _card_dir(tmp_path)
    _card_with_sidecar(root_cards, "api_contract", 0.2)
    recipe = tmp_path / "recipes" / "x" / "verifiers"
    recipe.mkdir(parents=True)
    script = recipe / "api_contract.py"
    script.write_text("")
    assert "not admitted" in ver._unadmitted_verifier(
        str(script), str(tmp_path), "api_contract")
