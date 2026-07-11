"""Parity gate: mini_ork.ported.mini_ork_verify vs bin/mini-ork-verify.

A plan with pass/fail verifier scripts is run through the LIVE bash dispatcher
and the port; the parsed verdict + pass/fail counts + per-verifier pass values
must match (evidence paths carry timestamps, so those are excluded). Covers
--help, empty→vacuous, dry-run, all-pass, and mixed→partial.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_verify as ver  # noqa: E402

BIN = REPO / "bin" / "mini-ork-verify"


def _env(home, db):
    return {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db}


def _scenario(tmp_path, verifiers):
    home = tmp_path / ".mini-ork"; home.mkdir()
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")], env=_env(home, db),
                   capture_output=True, text=True, check=True)
    vdir = home / "verifiers"; vdir.mkdir()
    (vdir / "goodv.sh").write_text('echo "ok"\nexit 0\n')
    (vdir / "badv.sh").write_text('echo "nope"\nexit 1\n')
    plan = home / "plan.json"
    plan.write_text(json.dumps({"task_class": "code_fix",
                                "artifact_contract": {"success_verifiers": verifiers}}))
    return home, db, str(plan)


def _norm(js: str):
    d = json.loads(js)
    return (d["verdict"], d["pass_count"], d["fail_count"],
            [(r["verifier"], r["pass"]) for r in d["results"]])


def _bash(home, db, *args):
    r = subprocess.run(["bash", str(BIN), *args], capture_output=True, text=True, env=_env(home, db))
    # the JSON is the {...} block on stdout
    s = r.stdout[r.stdout.index("{"):] if "{" in r.stdout else r.stdout
    return s, r.returncode


def _py(home, db, *args):
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


def test_help_parity(tmp_path):
    home, db, _ = _scenario(tmp_path, [])
    ob = subprocess.run(["bash", str(BIN), "--help"], capture_output=True, text=True, env=_env(home, db))
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = ver.main(["--help"], db=db, root=str(REPO))
    assert ob.returncode == rc == 0 and ob.stdout == buf.getvalue()


def test_all_pass_parity(tmp_path):
    home, db, plan = _scenario(tmp_path, ["goodv"])
    sb, rb = _bash(home, db, "art.txt", "--plan", plan)
    sp, rp = _py(home, db, "art.txt", "--plan", plan)
    assert rb == rp == 0
    assert _norm(sb) == _norm(sp)
    assert json.loads(sp)["verdict"] == "pass"


def test_mixed_partial_parity(tmp_path):
    home, db, plan = _scenario(tmp_path, ["goodv", "badv"])
    sb, rb = _bash(home, db, "art.txt", "--plan", plan)
    sp, rp = _py(home, db, "art.txt", "--plan", plan)
    assert rb == rp == 0                       # partial → exit 0
    assert _norm(sb) == _norm(sp)
    assert json.loads(sp)["verdict"] == "partial"


def test_vacuous_parity(tmp_path):
    home, db, plan = _scenario(tmp_path, [])   # no verifiers
    sb, rb = _bash(home, db, "art.txt", "--plan", plan)
    sp, rp = _py(home, db, "art.txt", "--plan", plan)
    assert rb == rp == 0
    assert json.loads(sb)["verdict"] == json.loads(sp)["verdict"] == "vacuous"


def test_dry_run_parity(tmp_path):
    home, db, plan = _scenario(tmp_path, ["goodv", "badv"])
    sb, _ = _bash(home, db, "art.txt", "--plan", plan, "--dry-run")
    sp, _ = _py(home, db, "art.txt", "--plan", plan, "--dry-run")
    assert _norm(sb) == _norm(sp)
    assert json.loads(sp)["verdict"] == "dry-run"
