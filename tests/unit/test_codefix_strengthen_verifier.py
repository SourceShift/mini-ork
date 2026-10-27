"""In-process tests for the ``suite_strengthening`` wiring in the code-fix test
verifier (``recipes/code-fix/verifiers/test.py``).

The verifier is loaded as a module; the replay, the suite-adequacy audit and
``suite_strengthen.strengthen`` are stubbed, so these tests pin the wiring —
when the pass runs, what it receives, and how its record reaches the payload —
without a lane call or a pytest run.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

from mini_ork.gates import suite_strengthen

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "recipes" / "code-fix" / "verifiers" / "test.py"

RESULT_KEYS = {
    "mode", "status", "reason", "targeted", "unmatched", "tests", "admitted",
    "newly_killed", "score_before", "score_after", "verdict_after",
    "would_rescue",
}

_WEAK_REPLAY = {
    "passed": True,
    "reason": "tests exercise the change (delta-gate overlap)",
    "unverified": False,
    "weak": True,
    "replay": {
        "candidate_passed": ["test_mod.py::test_target"],
        "base_failed": ["test_mod.py::test_target"],
        "overlap": ["test_mod.py::test_target"],
        "weak_overlap": ["test_mod.py::test_target"],
        "strong_overlap": [],
        "flaky": [],
    },
}

_STRONG_REPLAY = dict(
    _WEAK_REPLAY,
    weak=False,
    replay=dict(_WEAK_REPLAY["replay"],
                weak_overlap=[],
                strong_overlap=["test_mod.py::test_target"]),
)

_ADEQUATE = {
    "verdict": "ADEQUATE", "reason": "score 1.0 >= 0.6", "score": 1.0,
    "survivors": [],
}

_INADEQUATE_SURVIVORS = {
    "verdict": "INADEQUATE",
    "reason": "score 0.0 < 0.6",
    "score": 0.0,
    "survivors": [
        {"id": "M01", "file": "mod.py", "line": 2, "operator": "arith_swap",
         "original": "a - b", "mutated": "a + b"},
    ],
}

CANNED = {
    "mode": "shadow", "status": "ran",
    "reason": "1 of 1 generated tests admitted; 1 of 1 targeted survivors killed",
    "targeted": ["M01"], "unmatched": [],
    "tests": [{"name": "t1", "admissible": True, "why": "",
               "on_candidate": "passed", "on_base": "failed", "kills": ["M01"],
               "kind": "target"}],
    "admitted": ["t1"], "newly_killed": ["M01"], "score_before": 0.0,
    "score_after": 0.5, "verdict_after": "INADEQUATE", "would_rescue": False,
}


def _load_verifier(monkeypatch, tmp_path, run_id):
    """Load the code-fix verifier as a module with a controlled env."""
    mini_home = tmp_path / "mo-home"
    mini_home.mkdir(exist_ok=True)
    monkeypatch.setenv("MINI_ORK_HOME", str(mini_home))
    monkeypatch.setenv("MINI_ORK_RUN_ID", run_id)
    monkeypatch.setenv("MINI_ORK_TEST_CMD", f"{sys.executable} -c 'pass'")
    monkeypatch.setenv("MO_CODEFIX_REPLAY", "1")
    monkeypatch.setenv("MO_CODEFIX_CERTIFY", "0")
    monkeypatch.setenv("MO_SUITE_ADEQUACY", "1")
    monkeypatch.setenv("MO_ALLOW_TEST_CHANGES", "1")
    monkeypatch.delenv("MO_SUITE_STRENGTHEN", raising=False)
    monkeypatch.delenv("MO_REVISE_ROUNDS", raising=False)

    spec = importlib.util.spec_from_file_location("_codefix_strengthen_verifier", VERIFIER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_main(mod, monkeypatch, audit_verdict, capsys, *, replay=None):
    from mini_ork.gates import suite_adequacy as sa

    monkeypatch.setattr(mod, "run_suite", lambda log, env=None: 0)
    monkeypatch.setattr(mod, "_replay_applies", lambda *a, **k: True)
    monkeypatch.setattr(
        mod, "_run_replay_check",
        lambda: dict(replay if replay is not None else _WEAK_REPLAY),
    )
    monkeypatch.setattr(
        sa, "audit_suite",
        lambda repo_dir, test_cmd, source_files, report_path=None: audit_verdict,
    )
    rc = mod.main()
    # One read: ``capture.readouterr()`` clears BOTH streams, so a second call
    # for stderr would always be empty. Return the whole capture instead.
    cap = capsys.readouterr()
    lines = cap.out.strip().splitlines()
    assert lines, "verifier printed no JSON"
    return rc, json.loads(lines[-1]), cap


def _wire(mod, monkeypatch, fake, tmp_path, *, kickoff="clamp bug"):
    """Stub the verifier's seams; returns the list of detach calls."""
    detach_calls = []
    monkeypatch.setattr(mod, "_kickoff_text", lambda toplevel: kickoff)
    monkeypatch.setattr(mod, "_run_dir_base_ref", lambda: "refs/heads/main")
    monkeypatch.setattr(mod, "_attach_git_worktree_base",
                        lambda ref="": str(tmp_path / "base"))
    monkeypatch.setattr(mod, "_detach_git_worktree",
                        lambda wt: detach_calls.append(wt))
    monkeypatch.setattr(suite_strengthen, "strengthen", fake)
    return detach_calls


def test_strong_path_inadequate_emits_record(tmp_path, monkeypatch, capsys):
    mod = _load_verifier(monkeypatch, tmp_path, "strengthen-strong")
    calls = []

    def fake(*a, **kw):
        calls.append((a, kw))
        return dict(CANNED, mode=kw["mode"])

    _wire(mod, monkeypatch, fake, tmp_path)
    rc, out, _cap = _run_main(mod, monkeypatch, _INADEQUATE_SURVIVORS, capsys,
                        replay=_STRONG_REPLAY)

    assert rc == 0
    assert out["pass"] is False
    assert out["adequacy_unverified"] is True
    assert out["suite_strengthening"] == dict(CANNED, mode="shadow")
    keys = list(out)
    assert keys.index("suite_strengthening") == keys.index("suite_adequacy") + 1
    assert len(calls) == 1


def test_strong_path_fake_kwargs(tmp_path, monkeypatch, capsys):
    mod = _load_verifier(monkeypatch, tmp_path, "strengthen-kwargs")
    calls = []

    def fake(*a, **kw):
        calls.append((a, kw))
        return dict(CANNED, mode=kw["mode"])

    detach_calls = _wire(mod, monkeypatch, fake, tmp_path)
    _run_main(mod, monkeypatch, _INADEQUATE_SURVIVORS, capsys)

    (args, kwargs), = calls
    assert args[0] == _INADEQUATE_SURVIVORS
    assert kwargs["issue"] == "clamp bug"
    assert kwargs["candidate_dir"] == os.getcwd()
    assert kwargs["base_dir"] == str(tmp_path / "base")
    assert kwargs["mode"] == "shadow"
    assert kwargs["python"] == sys.executable
    assert kwargs["out_dir"] == os.path.join(mod.LOG_DIR, "strengthen")
    assert len(detach_calls) == 1


def test_weak_path_rounds_remaining_keeps_failure(tmp_path, monkeypatch, capsys):
    mod = _load_verifier(monkeypatch, tmp_path, "strengthen-weak")
    calls = []

    def fake(*a, **kw):
        calls.append((a, kw))
        return dict(CANNED, mode=kw["mode"])

    _wire(mod, monkeypatch, fake, tmp_path)
    rc, out, _cap = _run_main(mod, monkeypatch, _INADEQUATE_SURVIVORS, capsys)

    assert rc == 1
    assert "strengthen the assertions" in out["error_summary"]
    assert "suite_strengthening" in out
    assert len(calls) == 1


def test_adequate_does_not_call_or_emit(tmp_path, monkeypatch, capsys):
    mod = _load_verifier(monkeypatch, tmp_path, "strengthen-adequate")
    calls = []

    def fake(*a, **kw):
        calls.append((a, kw))
        return dict(CANNED)

    _wire(mod, monkeypatch, fake, tmp_path)
    rc, out, _cap = _run_main(mod, monkeypatch, _ADEQUATE, capsys)

    assert rc == 0
    assert out["pass"] is True
    assert calls == []
    assert "suite_strengthening" not in out


def test_mode_off_does_not_call_or_emit(tmp_path, monkeypatch, capsys):
    mod = _load_verifier(monkeypatch, tmp_path, "strengthen-off")
    monkeypatch.setenv("MO_SUITE_STRENGTHEN", "0")
    calls = []

    def fake(*a, **kw):
        calls.append((a, kw))
        return dict(CANNED)

    _wire(mod, monkeypatch, fake, tmp_path)
    rc, out, _cap = _run_main(mod, monkeypatch, _INADEQUATE_SURVIVORS, capsys,
                        replay=_STRONG_REPLAY)

    assert rc == 0
    assert out["pass"] is False
    assert calls == []
    assert "suite_strengthening" not in out


def test_no_kickoff_does_not_call_or_emit(tmp_path, monkeypatch, capsys):
    mod = _load_verifier(monkeypatch, tmp_path, "strengthen-nokickoff")
    calls = []

    def fake(*a, **kw):
        calls.append((a, kw))
        return dict(CANNED)

    _wire(mod, monkeypatch, fake, tmp_path, kickoff="")
    rc, out, cap = _run_main(mod, monkeypatch, _INADEQUATE_SURVIVORS, capsys,
                        replay=_STRONG_REPLAY)

    assert rc == 0
    assert calls == []
    assert "suite_strengthening" not in out
    assert "[test] strengthen: skipped (no kickoff text)" in cap.err


def test_no_base_worktree_does_not_call_or_emit(tmp_path, monkeypatch, capsys):
    """`_attach_git_worktree_base` returning None skips the pass, with a note."""
    mod = _load_verifier(monkeypatch, tmp_path, "strengthen-nobase")
    calls = []

    def fake(*a, **kw):
        calls.append((a, kw))
        return dict(CANNED)

    _wire(mod, monkeypatch, fake, tmp_path)
    monkeypatch.setattr(mod, "_attach_git_worktree_base",
                        lambda ref="": None)
    rc, out, cap = _run_main(mod, monkeypatch, _INADEQUATE_SURVIVORS, capsys,
                        replay=_STRONG_REPLAY)

    assert rc == 0
    assert calls == []
    assert "suite_strengthening" not in out
    assert "[test] strengthen: skipped (no base worktree)" in cap.err


def test_error_record_keeps_verdict(tmp_path, monkeypatch, capsys):
    mod = _load_verifier(monkeypatch, tmp_path, "strengthen-error")

    def fake(*a, **kw):
        raise RuntimeError("dispatch down")

    detach_calls = _wire(mod, monkeypatch, fake, tmp_path)
    rc, out, _cap = _run_main(mod, monkeypatch, _INADEQUATE_SURVIVORS, capsys,
                        replay=_STRONG_REPLAY)

    assert rc == 0
    assert out["pass"] is False
    assert out["adequacy_unverified"] is True
    rec = out["suite_strengthening"]
    assert rec["status"] == "error"
    assert set(rec) == RESULT_KEYS
    assert rec["score_before"] == _INADEQUATE_SURVIVORS["score"]
    assert len(detach_calls) == 1


def test_skipped_record_not_emitted(tmp_path, monkeypatch, capsys):
    mod = _load_verifier(monkeypatch, tmp_path, "strengthen-skipped")
    calls = []

    def fake(*a, **kw):
        calls.append((a, kw))
        return dict(CANNED, status="skipped")

    _wire(mod, monkeypatch, fake, tmp_path)
    rc, out, _cap = _run_main(mod, monkeypatch, _INADEQUATE_SURVIVORS, capsys,
                        replay=_STRONG_REPLAY)

    assert rc == 0
    assert len(calls) == 1
    assert "suite_strengthening" not in out
