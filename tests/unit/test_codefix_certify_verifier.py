"""In-process tests for the `certify` shadow block in the code-fix test
verifier (`recipes/code-fix/verifiers/test.py`).

A strong replay and an ADEQUATE adequacy audit drive the verifier; the certify
terms are stubbed. Hermetic: tmp git fixture repo, no network, no lane call.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import types
import uuid
from pathlib import Path

from mini_ork.certify import codefix_terms

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER_PATH = REPO_ROOT / "recipes" / "code-fix" / "verifiers" / "test.py"

_CMD = shlex.quote(sys.executable) + " -m pytest -q -p no:cacheprovider test_mod.py"

POC_SRC = "def test_x():\n    pass\n"

CANNED = {
    "mode": "shadow", "status": "ran", "reason": "relation broken",
    "poc_file": "test_mod.py",
    "relations": {"verdict": "REFUTED", "counts": {"violated": 1}},
    "differential": None,
    "verdict": "REFUTED", "vetoed_by": ["relations"], "would_block": True,
}

SKIPPED_REC = {
    "mode": "shadow", "status": "skipped", "reason": "empty candidate diff",
    "poc_file": "test_mod.py", "relations": None, "differential": None,
    "verdict": None, "vetoed_by": [], "would_block": False,
}

REPLAY = {"weak": False, "strong_overlap": ["test_mod.py::test_target"],
          "overlap": ["test_mod.py::test_target"]}
REPLAY_RESULT = {"passed": True, "reason": "tests exercise the change",
                 "unverified": False, "weak": False, "replay": REPLAY}
ADEQUATE = {"verdict": "ADEQUATE", "score": 1.0, "reason": "stubbed adequate"}
INADEQUATE = {"verdict": "INADEQUATE", "score": 0.0, "reason": "stubbed inadequate"}


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
             "GIT_CONFIG_SYSTEM": os.devnull},
    )


def _fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_mod.py").write_text(
        "def test_target():\n    assert True\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "test_mod.py")
    _git(repo, "commit", "-qm", "base")
    return repo


def _load_verifier(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "orkhome"))
    monkeypatch.setenv("MINI_ORK_RUN_ID", "certifytest")
    monkeypatch.setenv("MINI_ORK_TEST_CMD", _CMD)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.setenv("MO_ALLOW_TEST_CHANGES", "1")
    monkeypatch.setenv("MO_CODEFIX_REPLAY", "1")
    monkeypatch.setenv("MO_SUITE_ADEQUACY", "1")
    name = f"_test_codefix_certify_verifier_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, VERIFIER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _install_stubs(mod, monkeypatch, tmp_path, *, kickoff="median bug",
                   poc=(POC_SRC, "test_mod.py", ""), evaluate=None,
                   adequacy=ADEQUATE):
    calls = {"evaluate": [], "replays": []}
    monkeypatch.setattr(mod, "_run_replay_check",
                        lambda: copy.deepcopy(REPLAY_RESULT))
    monkeypatch.setattr(mod, "_suite_adequacy", types.SimpleNamespace(
        audit_suite=lambda *a, **k: copy.deepcopy(adequacy)))
    monkeypatch.setattr(mod, "_kickoff_text", lambda toplevel: kickoff)
    monkeypatch.setattr(mod, "_attach_git_worktree_base",
                        lambda ref="": str(tmp_path / "base"))
    monkeypatch.setattr(mod, "_detach_git_worktree", lambda wt: None)

    def fake_poc(replay, cwd):
        calls["replays"].append(replay)
        return poc

    def fake_evaluate(**kw):
        calls["evaluate"].append(kw)
        if evaluate is not None:
            return evaluate(kw)
        return dict(CANNED, mode=kw["mode"])

    monkeypatch.setattr(codefix_terms, "poc_from_replay", fake_poc)
    monkeypatch.setattr(codefix_terms, "evaluate", fake_evaluate)
    return calls


def _run_main(mod, capsys):
    rc = mod.main()
    cap = capsys.readouterr()
    lines = [ln for ln in cap.out.strip().splitlines() if ln.strip()]
    assert lines, "verifier printed no JSON payload"
    return rc, json.loads(lines[-1]), cap.err


def test_module_globals_certify_and_report_path(tmp_path, monkeypatch):
    mod = _load_verifier(monkeypatch, tmp_path)
    assert mod.CERTIFY is None
    assert mod.CERTIFY_REPORT == os.path.join(mod.LOG_DIR, "certify_terms.json")


def test_off_no_certify_key_byte_identical(tmp_path, monkeypatch, capsys):
    repo = _fixture_repo(tmp_path)
    monkeypatch.chdir(repo)
    mod = _load_verifier(monkeypatch, tmp_path)
    monkeypatch.setenv("MO_CODEFIX_CERTIFY", "0")
    calls = _install_stubs(mod, monkeypatch, tmp_path)
    rc, out, _err = _run_main(mod, capsys)
    assert rc == 0 and out["pass"] is True
    assert "certify" not in out
    assert calls["evaluate"] == []
    assert set(out) == {"verifier", "pass", "evidence_path", "error_summary",
                        "post_rc", "base_rc", "replay", "suite_adequacy"}


def test_no_kickoff_skips(tmp_path, monkeypatch, capsys):
    repo = _fixture_repo(tmp_path)
    monkeypatch.chdir(repo)
    mod = _load_verifier(monkeypatch, tmp_path)
    monkeypatch.delenv("MO_CODEFIX_CERTIFY", raising=False)
    calls = _install_stubs(mod, monkeypatch, tmp_path, kickoff="")
    rc, out, err = _run_main(mod, capsys)
    assert rc == 0 and out["pass"] is True and "certify" not in out
    assert calls["evaluate"] == []
    assert "[test] certify: skipped (no kickoff text)" in err


def test_no_poc_skips(tmp_path, monkeypatch, capsys):
    repo = _fixture_repo(tmp_path)
    monkeypatch.chdir(repo)
    mod = _load_verifier(monkeypatch, tmp_path)
    monkeypatch.delenv("MO_CODEFIX_CERTIFY", raising=False)
    calls = _install_stubs(
        mod, monkeypatch, tmp_path,
        poc=(None, None, "no python fail-to-pass test in replay overlap"))
    rc, out, err = _run_main(mod, capsys)
    assert rc == 0 and "certify" not in out and calls["evaluate"] == []
    assert ("[test] certify: skipped (no python fail-to-pass test in replay "
            "overlap)") in err


def test_no_base_worktree_skips(tmp_path, monkeypatch, capsys):
    repo = _fixture_repo(tmp_path)
    monkeypatch.chdir(repo)
    mod = _load_verifier(monkeypatch, tmp_path)
    monkeypatch.delenv("MO_CODEFIX_CERTIFY", raising=False)
    calls = _install_stubs(mod, monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "_attach_git_worktree_base", lambda ref="": None)
    rc, out, err = _run_main(mod, capsys)
    assert rc == 0 and "certify" not in out and calls["evaluate"] == []
    assert "[test] certify: skipped (no base worktree)" in err


def test_shadow_records_certify(tmp_path, monkeypatch, capsys):
    repo = _fixture_repo(tmp_path)
    monkeypatch.chdir(repo)
    mod = _load_verifier(monkeypatch, tmp_path)
    monkeypatch.delenv("MO_CODEFIX_CERTIFY", raising=False)
    monkeypatch.delenv("MO_CODEFIX_CERTIFY_PYTHON", raising=False)
    calls = _install_stubs(mod, monkeypatch, tmp_path)
    rc, out, _err = _run_main(mod, capsys)
    assert rc == 0 and out["pass"] is True
    assert out["certify"] == CANNED
    keys = list(out)
    assert keys.index("certify") == keys.index("suite_adequacy") + 1
    with open(mod.CERTIFY_REPORT, encoding="utf-8") as fh:
        assert json.load(fh) == CANNED
    assert calls["replays"][0].get("strong_overlap") == \
        ["test_mod.py::test_target"]
    kw = calls["evaluate"][0]
    assert kw["mode"] == "shadow"
    assert kw["base_dir"] == str(tmp_path / "base")
    assert kw["candidate_dir"] == os.getcwd()
    assert kw["issue"] == "median bug"
    assert kw["poc"] == POC_SRC and kw["poc_file"] == "test_mod.py"
    assert kw["python"] == sys.executable
    assert kw["base_ref"] == ""


def test_gate_mode_recorded_but_inert(tmp_path, monkeypatch, capsys):
    repo = _fixture_repo(tmp_path)
    monkeypatch.chdir(repo)
    mod = _load_verifier(monkeypatch, tmp_path)
    monkeypatch.setenv("MO_CODEFIX_CERTIFY", "gate")
    _install_stubs(mod, monkeypatch, tmp_path)
    rc, out, _err = _run_main(mod, capsys)
    assert out["certify"]["mode"] == "gate"
    assert out["pass"] is True and rc == 0


def test_error_evaluate_gives_error_record(tmp_path, monkeypatch, capsys):
    repo = _fixture_repo(tmp_path)
    monkeypatch.chdir(repo)
    mod = _load_verifier(monkeypatch, tmp_path)
    monkeypatch.delenv("MO_CODEFIX_CERTIFY", raising=False)

    def raising(kw):
        raise RuntimeError("cert blew up")

    _install_stubs(mod, monkeypatch, tmp_path, evaluate=raising)
    rc, out, _err = _run_main(mod, capsys)
    assert out["certify"]["status"] == "error"
    assert "RuntimeError: cert blew up" in out["certify"]["reason"]
    assert out["certify"]["would_block"] is False
    assert out["certify"]["relations"] is None
    assert out["certify"]["differential"] is None
    assert out["certify"]["vetoed_by"] == []
    assert out["certify"]["poc_file"] == "test_mod.py"
    assert set(out["certify"]) == {"mode", "status", "reason", "poc_file",
                                   "relations", "differential", "verdict",
                                   "vetoed_by", "would_block"}
    assert out["pass"] is True and rc == 0


def test_skipped_record_no_key_but_report_written(tmp_path, monkeypatch, capsys):
    repo = _fixture_repo(tmp_path)
    monkeypatch.chdir(repo)
    mod = _load_verifier(monkeypatch, tmp_path)
    monkeypatch.delenv("MO_CODEFIX_CERTIFY", raising=False)
    _install_stubs(mod, monkeypatch, tmp_path,
                   evaluate=lambda kw: dict(SKIPPED_REC, mode=kw["mode"]))
    rc, out, _err = _run_main(mod, capsys)
    assert rc == 0 and "certify" not in out
    with open(mod.CERTIFY_REPORT, encoding="utf-8") as fh:
        assert json.load(fh) == SKIPPED_REC


def test_replay_none_never_records(tmp_path, monkeypatch, capsys):
    repo = _fixture_repo(tmp_path)
    monkeypatch.chdir(repo)
    mod = _load_verifier(monkeypatch, tmp_path)
    monkeypatch.setenv("MO_CODEFIX_REPLAY", "0")
    monkeypatch.delenv("MO_CODEFIX_CERTIFY", raising=False)
    calls = _install_stubs(mod, monkeypatch, tmp_path)
    rc, out, _err = _run_main(mod, capsys)
    assert rc == 0 and out["pass"] is True and "certify" not in out
    assert calls["evaluate"] == []
    assert not os.path.exists(mod.CERTIFY_REPORT)
    assert "target_waiver" in out


def test_emit_unverified_certify_before_status(tmp_path, monkeypatch, capsys):
    repo = _fixture_repo(tmp_path)
    monkeypatch.chdir(repo)
    mod = _load_verifier(monkeypatch, tmp_path)
    monkeypatch.delenv("MO_CODEFIX_CERTIFY", raising=False)
    _install_stubs(mod, monkeypatch, tmp_path, adequacy=INADEQUATE)
    rc, out, _err = _run_main(mod, capsys)
    assert rc == 0 and out["pass"] is False and out["status"] == "unverified"
    assert out["certify"] == CANNED
    keys = list(out)
    assert keys.index("certify") == keys.index("suite_adequacy") + 1
    assert keys.index("certify") < keys.index("status")


def test_python_env_override_forwarded(tmp_path, monkeypatch, capsys):
    repo = _fixture_repo(tmp_path)
    monkeypatch.chdir(repo)
    mod = _load_verifier(monkeypatch, tmp_path)
    monkeypatch.delenv("MO_CODEFIX_CERTIFY", raising=False)
    monkeypatch.setenv("MO_CODEFIX_CERTIFY_PYTHON", "/opt/side/python")
    calls = _install_stubs(mod, monkeypatch, tmp_path)
    rc, out, _err = _run_main(mod, capsys)
    assert rc == 0 and out["certify"] == CANNED
    assert calls["evaluate"][0]["python"] == "/opt/side/python"


def test_probe_timeout_override_forwarded(tmp_path, monkeypatch, capsys):
    repo = _fixture_repo(tmp_path)
    monkeypatch.chdir(repo)
    mod = _load_verifier(monkeypatch, tmp_path)
    monkeypatch.delenv("MO_CODEFIX_CERTIFY", raising=False)
    monkeypatch.setenv("MO_CODEFIX_CERTIFY_PROBE_TIMEOUT_S", "7.5")
    calls = _install_stubs(mod, monkeypatch, tmp_path)
    rc, out, _err = _run_main(mod, capsys)
    assert rc == 0 and out["certify"] == CANNED
    assert calls["evaluate"][0]["timeout_s"] == 7.5


def test_probe_timeout_malformed_falls_back(tmp_path, monkeypatch, capsys):
    repo = _fixture_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("MO_CODEFIX_CERTIFY", raising=False)
    for bad in ("", "soon", "0", "-3"):
        mod = _load_verifier(monkeypatch, tmp_path)
        monkeypatch.setenv("MO_CODEFIX_CERTIFY_PROBE_TIMEOUT_S", bad)
        calls = _install_stubs(mod, monkeypatch, tmp_path)
        rc, out, _err = _run_main(mod, capsys)
        assert rc == 0 and out["certify"] == CANNED
        assert calls["evaluate"][0]["timeout_s"] == 120.0


def test_header_comment_lists_knobs(tmp_path, monkeypatch):
    _load_verifier(monkeypatch, tmp_path)
    src = VERIFIER_PATH.read_text(encoding="utf-8")
    header = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            break
        header.append(line)
    head = "\n".join(header)
    for knob in ("MO_CODEFIX_CERTIFY", "MO_CODEFIX_CERTIFY_LANE_TIMEOUT_S",
                 "MO_CODEFIX_CERTIFY_PROBE_TIMEOUT_S",
                 "MO_CODEFIX_CERTIFY_PYTHON"):
        assert knob in head, f"{knob} missing from the verifier env-var header"
