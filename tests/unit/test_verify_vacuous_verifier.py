"""A verifier that exits 0 but declares it measured nothing is unmeasured, not a pass.

The metamorphic verifier is on by default in code-fix. With no spec it prints
``{"verdict": "vacuous"}`` and exits 0; before this rule verify counted that as a
pass, which would have lifted every code-fix run by one free green, and turned a
run with no real verifier into ``pass``.
"""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import verify as ver


def _run(tmp_path, scripts: dict, refs: list, *, root=None, env=None):
    home = tmp_path / ".mini-ork"
    vdir = home / "verifiers"
    vdir.mkdir(parents=True)
    for name, body in scripts.items():
        (vdir / name).write_text(body)
    plan = home / "plan.json"
    plan.write_text(json.dumps({"task_class": "code_fix",
                                "artifact_contract": {"success_verifiers": refs}}))
    out, err = io.StringIO(), io.StringIO()
    old = dict(os.environ)
    os.environ.update({"MINI_ORK_HOME": str(home), "MO_MUTATION_ADVERSARY": "0"})
    for k in ("MINI_ORK_DRY_RUN", "MINI_ORK_PLAN_PATH", "MINI_ORK_TASK_CLASS",
              "MINI_ORK_RUN_DIR", "MINI_ORK_RECIPE", "MINI_ORK_ROOT",
              "MO_METAMORPHIC_SPEC", "MO_METAMORPHIC_SPEC_JSON"):
        os.environ.pop(k, None)
    os.environ.update(env or {})
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = ver.main(["artifact.bin", "--plan", str(plan)],
                          db=str(home / "state.db"), root=str(root or tmp_path))
    finally:
        os.environ.clear()
        os.environ.update(old)
    s = out.getvalue()
    return json.loads(s[s.index("{"):]), rc


VACUOUS = 'import json\nprint(json.dumps({"verdict": "vacuous", "note": "no metamorphic spec"}))\n'
PASSING = 'print("suite green")\n'


def test_declared_unmeasured_reads_the_envelope_not_the_log():
    assert ver._declared_unmeasured(b'{"verdict": "vacuous", "note": "no spec"}\n') == "no spec"
    assert ver._declared_unmeasured(b'[test] log line\n{"verifier": "x", "pass": null}\n')
    assert ver._declared_unmeasured(b'{"verifier": "test", "pass": true}\n') == ""
    assert ver._declared_unmeasured(b'{"pass": false}\n') == ""
    assert ver._declared_unmeasured(b"plain text, exit 0\n") == ""
    # only the LAST JSON object counts: a later real verdict overrides an earlier note
    assert ver._declared_unmeasured(b'{"verdict": "vacuous"}\n{"pass": true}\n') == ""


def test_lone_vacuous_verifier_is_vacuous_not_pass(tmp_path):
    d, _ = _run(tmp_path, {"meta.py": VACUOUS}, ["verifiers/meta.py"])
    assert d["verdict"] == "vacuous"
    assert d["pass_count"] == 0 and d["fail_count"] == 0
    res = [r for r in d["results"] if r["verifier"] == "verifiers/meta.py"]
    assert res and res[0]["pass"] is None and "no metamorphic spec" in res[0]["detail"]


def test_vacuous_verifier_does_not_add_a_pass(tmp_path):
    d, rc = _run(tmp_path, {"meta.py": VACUOUS, "real.py": PASSING},
                 ["verifiers/real.py", "verifiers/meta.py"])
    assert rc == 0
    assert d["verdict"] == "pass"
    assert d["pass_count"] == 1


def test_code_fix_contract_runs_metamorphic_by_default():
    contract = yaml.safe_load((REPO / "recipes/code-fix/artifact_contract.yaml").read_text())
    assert "verifiers/metamorphic.py" in contract["success_verifiers"]


def test_run_dir_spec_catches_a_cheat_through_verify(tmp_path):
    """End to end on the real code-fix verifier: a spec dropped in the run dir is
    picked up with no env var, and commutativity exposes a hardcoded add."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = run_dir / "patched.py"
    target.write_text("def add(x):\n    a, b = x\n    return 5 if (a, b) == (2, 3) else 0\n")
    (run_dir / "metamorphic-spec.json").write_text(json.dumps({
        "target": {"module": str(target), "function": "add"},
        "seed_inputs": [[2, 3]],
        "relations": ["commutativity"],
    }))
    d, rc = _run(tmp_path, {}, ["verifiers/metamorphic.py"], root=REPO,
                 env={"MINI_ORK_RECIPE": "code-fix", "MINI_ORK_RUN_DIR": str(run_dir),
                      "PYTHONPATH": str(REPO)})
    assert rc == 1
    assert d["verdict"] == "fail"
    res = [r for r in d["results"] if r["verifier"] == "verifiers/metamorphic.py"]
    assert res and res[0]["pass"] is False
