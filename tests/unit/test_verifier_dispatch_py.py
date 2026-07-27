"""Unit tests for extension-native verifier dispatch (bash-removal WS8).

Covers:
- verify.py ``_verifier_argv``: .py → sys.executable, .sh → bash + one-line
  deprecation warning on stderr (still runs), extensionless → legacy bash.
- verify.py ``_find_verifier_script``: .py preference, .sh fallback, and
  cross-extension sibling resolution.
- verify.py main loop: .py verifier rc/env forwarding + vacuous-evidence rule,
  .sh verifier still runs (deprecated), inline command path still runs via
  ``bash -lc``.
- execute.py ``_run_verifier_ref``: .py dispatch, .sh deprecated dispatch
  (warning + runs), env forwarding (MINI_ORK_PLAN_PATH / ARTIFACT_PATH /
  MINI_ORK_RUN_DIR), rc semantics, JSON {"pass": ...} verdict parsing, and the
  vacuous-pass guard.
"""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import execute as ex
from mini_ork.cli import verify as ver


# ── _verifier_argv (both modules share the contract) ─────────────────────────


def test_verify_argv_py_uses_current_interpreter():
    assert ver._verifier_argv("/x/v.py") == [sys.executable, "/x/v.py"]
    assert ex._verifier_argv("/x/v.py") == [sys.executable, "/x/v.py"]


def test_verify_argv_sh_deprecated_bash_with_warning():
    buf = io.StringIO()
    with redirect_stderr(buf):
        argv = ver._verifier_argv("/x/v.sh")
    assert argv == ["bash", "/x/v.sh"]
    assert "deprecated" in buf.getvalue()
    assert "/x/v.sh" in buf.getvalue()
    assert buf.getvalue().count("\n") == 1  # one-line warning


def test_verify_argv_extensionless_keeps_legacy_bash_no_warning():
    buf = io.StringIO()
    with redirect_stderr(buf):
        argv = ver._verifier_argv("/x/v")
    assert argv == ["bash", "/x/v"]
    assert buf.getvalue() == ""


# ── _find_verifier_script ─────────────────────────────────────────────────────


def test_find_verifier_script_py_preferred(tmp_path):
    home = tmp_path / "home"; root = tmp_path / "root"
    vdir = home / "verifiers"; vdir.mkdir(parents=True)
    (vdir / "chk.py").write_text("print('{}')\n")
    (vdir / "chk.sh").write_text("echo '{}'\n")
    assert ver._find_verifier_script("verifiers/chk.py", str(root), str(home)) == str(vdir / "chk.py")
    # .sh ref still resolves its own kind first …
    assert ver._find_verifier_script("verifiers/chk.sh", str(root), str(home)) == str(vdir / "chk.sh")


def test_find_verifier_script_cross_extension_fallback(tmp_path):
    home = tmp_path / "home"; root = tmp_path / "root"
    vdir = home / "verifiers"; vdir.mkdir(parents=True)
    (vdir / "only_py.py").write_text("print('{}')\n")
    (vdir / "only_sh.sh").write_text("echo '{}'\n")
    # a .sh contract ref resolves the ported .py sibling
    assert ver._find_verifier_script("verifiers/only_py.sh", str(root), str(home)) == str(vdir / "only_py.py")
    # a .py contract ref resolves a not-yet-ported .sh sibling
    assert ver._find_verifier_script("verifiers/only_sh.py", str(root), str(home)) == str(vdir / "only_sh.sh")
    assert ver._find_verifier_script("verifiers/missing.py", str(root), str(home)) == ""


def test_evidence_stem_strips_both_extensions():
    assert ver._evidence_stem("verifiers/type-check.py") == "type-check"
    assert ver._evidence_stem("verifiers/type-check.sh") == "type-check"


# ── verify.py main loop ───────────────────────────────────────────────────────


def _verify_run(tmp_path, script_name, script_body, verifier_ref):
    home = tmp_path / ".mini-ork"
    vdir = home / "verifiers"
    vdir.mkdir(parents=True)
    (vdir / script_name).write_text(script_body)
    plan = home / "plan.json"
    plan.write_text(json.dumps({
        "task_class": "code_fix",
        "artifact_contract": {"success_verifiers": [verifier_ref]},
    }))
    db = str(home / "state.db")
    out, err = io.StringIO(), io.StringIO()
    old = dict(os.environ)
    os.environ.update({"MINI_ORK_HOME": str(home)})
    for k in ("MINI_ORK_DRY_RUN", "MINI_ORK_PLAN_PATH", "MINI_ORK_TASK_CLASS",
              "MINI_ORK_RUN_DIR", "MINI_ORK_RECIPE", "MINI_ORK_ROOT"):
        os.environ.pop(k, None)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = ver.main(["artifact.bin", "--plan", str(plan)], db=db, root=str(tmp_path))
    finally:
        os.environ.clear()
        os.environ.update(old)
    s = out.getvalue()
    return json.loads(s[s.index("{"):]), rc, err.getvalue()


def test_verify_main_runs_py_verifier_and_forwards_env(tmp_path):
    # prints ARTIFACT_PATH (non-JSON) and exits 0 → pass; env forwarded.
    d, rc, err = _verify_run(
        tmp_path, "envv.py",
        "import os\nprint('AP=' + os.environ.get('ARTIFACT_PATH', ''))\n",
        "verifiers/envv.py")
    assert rc == 0
    assert err == ""  # no deprecation warning for .py
    res = [r for r in d["results"] if r["verifier"] == "verifiers/envv.py"]
    assert res and res[0]["pass"] is True
    ev = Path(res[0]["evidence_path"]).read_text()
    assert "AP=artifact.bin" in ev


def test_verify_main_sh_verifier_still_runs_with_deprecation_warning(tmp_path):
    d, rc, err = _verify_run(
        tmp_path, "legacy.sh",
        "echo \"legacy ok\"\nexit 0\n",
        "verifiers/legacy.sh")
    assert rc == 0
    assert "deprecated" in err and "legacy.sh" in err
    res = [r for r in d["results"] if r["verifier"] == "verifiers/legacy.sh"]
    assert res and res[0]["pass"] is True
    assert "legacy ok" in Path(res[0]["evidence_path"]).read_text()


def test_verify_main_py_verifier_rc_and_vacuous_semantics(tmp_path):
    # rc 1 → fail
    d, rc, _ = _verify_run(tmp_path, "failv.py", "import sys\nprint('x')\nsys.exit(1)\n",
                           "verifiers/failv.py")
    assert rc == 1
    res = [r for r in d["results"] if r["verifier"] == "verifiers/failv.py"]
    assert res and res[0]["pass"] is False
    # exit 0 with no evidence → vacuous → fail
    d, rc, _ = _verify_run(tmp_path / "v2", "emptyv.py", "import sys\nsys.exit(0)\n",
                           "verifiers/emptyv.py")
    assert rc == 1
    res = [r for r in d["results"] if r["verifier"] == "verifiers/emptyv.py"]
    assert res and res[0]["pass"] is False


def test_verify_main_inline_command_still_runs_via_bash_lc(tmp_path):
    home = tmp_path / ".mini-ork"
    home.mkdir(parents=True)
    marker = tmp_path / "marker.txt"
    plan = home / "plan.json"
    plan.write_text(json.dumps({
        "task_class": "code_fix",
        "artifact_contract": {"success_verifiers": [f"touch {marker} exits 0"]},
        "verifier_contract": {"checks": [{"id": "c1", "command": f"touch {marker}"}]},
    }))
    db = str(home / "state.db")
    out = io.StringIO()
    old = dict(os.environ)
    os.environ.update({"MINI_ORK_HOME": str(home)})
    for k in ("MINI_ORK_DRY_RUN", "MINI_ORK_PLAN_PATH", "MINI_ORK_TASK_CLASS",
              "MINI_ORK_RUN_DIR", "MINI_ORK_RECIPE", "MINI_ORK_ROOT"):
        os.environ.pop(k, None)
    try:
        with redirect_stdout(out):
            rc = ver.main(["artifact.bin", "--plan", str(plan)], db=db, root=str(tmp_path))
    finally:
        os.environ.clear()
        os.environ.update(old)
    assert rc == 0
    assert marker.is_file()  # the inline command actually ran (bash -lc)


# ── execute.py _run_verifier_ref ──────────────────────────────────────────────


def test_execute_run_verifier_ref_py_dispatch_env_and_rc(tmp_path):
    script = tmp_path / "chk.py"
    script.write_text(
        "import os\n"
        "print('PP=' + os.environ.get('MINI_ORK_PLAN_PATH', ''))\n"
        "print('AP=' + os.environ.get('ARTIFACT_PATH', ''))\n"
        "print('RD=' + os.environ.get('MINI_ORK_RUN_DIR', ''))\n")
    ev = tmp_path / "evidence" / "chk.log"
    os.makedirs(ev.parent)
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = ex._run_verifier_ref(str(script), str(ev),
                                  plan_path="/p/plan.json", artifact_path="/a/art.bin",
                                  cwd=str(tmp_path))
    assert rc == 0  # non-JSON evidence → script rc propagated
    assert buf.getvalue() == ""  # .py → no deprecation warning
    text = ev.read_text()
    assert "PP=/p/plan.json" in text
    assert "AP=/a/art.bin" in text
    assert f"RD={ev.parent}" in text  # MINI_ORK_RUN_DIR defaults to evidence dir


def test_execute_run_verifier_ref_py_json_verdict(tmp_path):
    ev = tmp_path / "chk.log"
    ok = tmp_path / "ok.py"
    ok.write_text("print('{\"pass\": true}')\n")
    assert ex._run_verifier_ref(str(ok), str(ev), cwd=str(tmp_path)) == 0
    bad = tmp_path / "bad.py"
    bad.write_text("print('{\"pass\": false}')\n")
    assert ex._run_verifier_ref(str(bad), str(ev), cwd=str(tmp_path)) == 1


def test_execute_run_verifier_ref_sh_deprecated_still_runs(tmp_path):
    script = tmp_path / "legacy.sh"
    script.write_text("echo '{\"pass\": true}'\n")
    ev = tmp_path / "legacy.log"
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = ex._run_verifier_ref(str(script), str(ev), cwd=str(tmp_path))
    assert rc == 0
    assert "deprecated" in buf.getvalue() and "legacy.sh" in buf.getvalue()


def test_execute_run_verifier_ref_vacuous_guard(tmp_path):
    script = tmp_path / "empty.py"
    script.write_text("import sys\nsys.exit(0)\n")
    ev = tmp_path / "empty.log"
    rc = ex._run_verifier_ref(str(script), str(ev), cwd=str(tmp_path))
    assert rc == 1
    assert "vacuous pass" in ev.read_text()
