"""Unit tests for the advisory Agent-S computer-use GUI smoke gate.

``mini_ork.gates.agent_s_smoke`` wires [Agent-S](https://github.com/simular-ai/Agent-S)
in as an advisory evidence-producing gate. It drives the real GUI, so these tests
never launch a GUI, open a socket, or invoke the real Agent-S CLI: ``subprocess.run``
/ ``subprocess.Popen`` / ``osascript`` / file writes are all monkeypatched into
tmp dirs. What is asserted here is the *contract* — env gating, strict-majority
consensus, the cost guard, the evidence shape, and the executable rc contract —
not whether Agent-S itself can click a button (that is the post-merge manual
smoke, deliberately out of scope).
"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

#: The module file, so ``__main__`` runs via ``runpy.run_path`` rather than
#: ``runpy.run_module``: the module is already imported above (``ags``), and
#: ``run_module`` on an already-imported package module emits a RuntimeWarning
#: ("found in sys.modules … prior to execution") every time.
_MODULE_FILE = REPO / "mini_ork" / "gates" / "agent_s_smoke.py"

from mini_ork.gates import agent_s_smoke as ags  # noqa: E402
from mini_ork.gates import gate_registry as gr  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# helpers / fixtures
# ─────────────────────────────────────────────────────────────────────────────

_GATE_ENV_VARS = (
    ags.ALLOW_ENV,
    ags.PROVIDER_ENV,
    ags.MODEL_ENV,
    ags.GROUND_URL_ENV,
    ags.USD_PER_ATTEMPT_ENV,
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AGENT_S_BASE_URL",
)


def _clear_env(monkeypatch):
    for key in _GATE_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


def _allow_env(monkeypatch):
    """Full operator opt-in: allow flag, provider key, grounding endpoint."""
    _clear_env(monkeypatch)
    monkeypatch.setenv(ags.ALLOW_ENV, "1")
    monkeypatch.setenv(ags.PROVIDER_ENV, "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv(ags.GROUND_URL_ENV, "https://ground.example")
    monkeypatch.setenv(ags.USD_PER_ATTEMPT_ENV, "0.5")


def _run_dir(tmp_path) -> Path:
    d = tmp_path / "runs" / "run-1"
    d.mkdir(parents=True)
    return d


def _write_spec(run_dir: Path, **overrides) -> Path:
    spec = {
        "app": "/Applications/Fake.app",
        "steps": "Open the board.",
        "expect": "The board shows nodes.",
        "attempts": 3,
        "timeout_s": 300,
        "max_usd": 2.0,
    }
    spec.update(overrides)
    path = run_dir / ags.TASK_SPEC_NAME
    path.write_text(yaml.safe_dump(spec))
    return path


def _ctx(run_dir: Path) -> str:
    return json.dumps({"run_id": run_dir.name, "run_dir": str(run_dir)})


class _FakePopen:
    """Stands in for the launched target app; already exited, harmless terminate."""

    def __init__(self, *args, **kwargs):
        pass

    def poll(self):
        return 0

    def terminate(self):
        pass


def _install_agent_s(monkeypatch, results=(), timeout_on=()):
    """Patch ``subprocess.run``/``subprocess.Popen``; returns (run_calls, agent_calls).

    ``results`` is a sequence of ``(rc, stdout, stderr)`` consumed one per
    Agent-S invocation. ``timeout_on`` is a set of zero-based attempt indexes
    that raise ``subprocess.TimeoutExpired`` instead. ``osascript`` calls
    (best-effort app quit) always succeed.
    """
    results = list(results)
    run_calls: list[list] = []
    agent_calls: list[list] = []

    def fake_run(cmd, **kwargs):
        run_calls.append(list(cmd))
        if cmd and cmd[0] == "osascript":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        idx = len(agent_calls)
        agent_calls.append(list(cmd))
        if idx in timeout_on:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        rc, stdout, stderr = results[idx] if idx < len(results) else (0, "done", "")
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    return run_calls, agent_calls


def _verdict(run_dir: Path) -> dict:
    p = run_dir / "artifacts" / ags.ARTIFACT_DIR_NAME / "verdict.json"
    return json.loads(p.read_text())


def _module_rc(monkeypatch, task_path: Path) -> int:
    """Execute ``__main__`` via runpy and return the mapped exit code."""
    monkeypatch.setattr(sys, "argv", ["agent_s_smoke", str(task_path)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(_MODULE_FILE), run_name="__main__")
    return exc.value.code


# ─────────────────────────────────────────────────────────────────────────────
# registration
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _leave_gate_registry_booted():
    """``agent_s_gui`` is a transient OCP registration, not a built-in gate.

    ``test_gate_evaluator_registry_py.py`` asserts the *exact* post-boot
    registry, so an ``agent_s_gui`` left behind here would fail that unrelated
    test when both files share one session. Restore the registry around every
    test regardless of outcome — belt, on top of each registration test's own
    ``finally`` pop — so ordering can never leak a stray evaluator.
    """
    gr.GATE_EVALUATORS.pop(ags.GATE_TYPE, None)
    try:
        yield
    finally:
        gr.GATE_EVALUATORS.pop(ags.GATE_TYPE, None)


def test_register_adds_agent_s_gui_evaluator():
    ags.register()
    try:
        assert ags.GATE_TYPE in gr.GATE_EVALUATORS
        assert callable(gr.GATE_EVALUATORS[ags.GATE_TYPE])
    finally:
        # ``agent_s_gui`` is a transient OCP registration, not a built-in:
        # pop it so the registry is left exactly as it was for tests that
        # assert the post-boot set (test_gate_evaluator_registry_py).
        gr.GATE_EVALUATORS.pop(ags.GATE_TYPE, None)


def test_gate_register_evaluate_roundtrip(tmp_path, monkeypatch):
    """Full OCP path: ``register()`` then ``gate_register`` + ``gate_evaluate``
    actually dispatch a real ``agent_s_gui`` row to ``evaluate()`` (not just
    that ``register()`` populates ``GATE_EVALUATORS``).
    """
    d = _run_dir(tmp_path)
    _write_spec(d)
    _clear_env(monkeypatch)  # env gating defers → proves the row reached evaluate()

    db = str(tmp_path / "state.db")
    ags.register()
    try:
        gid = gr.gate_register(db, ags.GATE_TYPE, "")
        assert gid
        assert gr.gate_evaluate(db, gid, _ctx(d)) == "defer"
    finally:
        gr.GATE_EVALUATORS.pop(ags.GATE_TYPE, None)


# ─────────────────────────────────────────────────────────────────────────────
# task-spec presence / shape — DEFER, never fail
# ─────────────────────────────────────────────────────────────────────────────


def test_absent_spec_defers_without_spawning(tmp_path, monkeypatch):
    d = _run_dir(tmp_path)
    _allow_env(monkeypatch)  # even with full opt-in, no spec → no action
    run_calls, agent_calls = _install_agent_s(monkeypatch)

    assert ags.evaluate("", _ctx(d), "", None) == "defer"
    assert agent_calls == []
    assert run_calls == []

    v = _verdict(d)
    assert v["state"] == "defer"
    assert v["pass"] is None
    assert v["advisory"] is True
    assert v["attempts"] == []


def test_malformed_yaml_spec_defers(tmp_path, monkeypatch):
    d = _run_dir(tmp_path)
    (d / ags.TASK_SPEC_NAME).write_text("app: [unclosed\n")
    _allow_env(monkeypatch)
    run_calls, agent_calls = _install_agent_s(monkeypatch)

    assert ags.evaluate("", _ctx(d), "", None) == "defer"
    assert agent_calls == []
    assert run_calls == []


def test_spec_missing_steps_defers(tmp_path, monkeypatch):
    d = _run_dir(tmp_path)
    _write_spec(d, steps="")
    _allow_env(monkeypatch)
    _install_agent_s(monkeypatch)

    assert ags.evaluate("", _ctx(d), "", None) == "defer"


def test_spec_missing_app_and_launch_cmd_defers(tmp_path, monkeypatch):
    d = _run_dir(tmp_path)
    _write_spec(d, app="", launch_cmd=None)
    _allow_env(monkeypatch)
    _install_agent_s(monkeypatch)

    assert ags.evaluate("", _ctx(d), "", None) == "defer"


# ─────────────────────────────────────────────────────────────────────────────
# env gating — the safety rule: never run without all three conditions
# ─────────────────────────────────────────────────────────────────────────────


def test_allow_unset_defers_without_spawning(tmp_path, monkeypatch):
    d = _run_dir(tmp_path)
    _write_spec(d)
    _clear_env(monkeypatch)
    monkeypatch.setenv(ags.PROVIDER_ENV, "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv(ags.GROUND_URL_ENV, "https://ground.example")
    run_calls, agent_calls = _install_agent_s(monkeypatch)

    assert ags.evaluate("", _ctx(d), "", None) == "defer"
    assert agent_calls == []
    assert run_calls == []
    assert ags.ALLOW_ENV in _verdict(d)["reason"]


def test_provider_key_missing_defers_without_spawning(tmp_path, monkeypatch):
    d = _run_dir(tmp_path)
    _write_spec(d)
    _clear_env(monkeypatch)
    monkeypatch.setenv(ags.ALLOW_ENV, "1")
    monkeypatch.setenv(ags.GROUND_URL_ENV, "https://ground.example")
    run_calls, agent_calls = _install_agent_s(monkeypatch)

    assert ags.evaluate("", _ctx(d), "", None) == "defer"
    assert agent_calls == []
    assert run_calls == []


def test_ground_url_missing_defers_without_spawning(tmp_path, monkeypatch):
    d = _run_dir(tmp_path)
    _write_spec(d)
    _clear_env(monkeypatch)
    monkeypatch.setenv(ags.ALLOW_ENV, "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    run_calls, agent_calls = _install_agent_s(monkeypatch)

    assert ags.evaluate("", _ctx(d), "", None) == "defer"
    assert agent_calls == []
    assert run_calls == []


# ─────────────────────────────────────────────────────────────────────────────
# consensus — strict majority, zero attempts defers
# ─────────────────────────────────────────────────────────────────────────────


def test_consensus_zero_attempts_defers():
    assert ags._consensus([]) == "defer"


def test_consensus_majority_and_minority():
    assert ags._consensus([{"passed": True}, {"passed": True}, {"passed": False}]) == "pass"
    assert ags._consensus([{"passed": True}, {"passed": False}, {"passed": False}]) == "fail"


def test_two_of_three_majority_passes(tmp_path, monkeypatch):
    d = _run_dir(tmp_path)
    _write_spec(d)
    _allow_env(monkeypatch)
    results = [(0, "task done", ""), (0, "success", ""), (0, "failed", "")]
    _run_calls, agent_calls = _install_agent_s(monkeypatch, results=results)

    assert ags.evaluate("", _ctx(d), "", None) == "pass"
    assert len(agent_calls) == 3

    v = _verdict(d)
    assert v["state"] == "pass"
    assert v["pass"] is True
    assert [a["passed"] for a in v["attempts"]] == [True, True, False]


def test_one_of_three_fails(tmp_path, monkeypatch):
    d = _run_dir(tmp_path)
    _write_spec(d)
    _allow_env(monkeypatch)
    results = [(0, "done", ""), (0, "failed", ""), (0, "error occurred", "")]
    _install_agent_s(monkeypatch, results=results)

    assert ags.evaluate("", _ctx(d), "", None) == "fail"

    v = _verdict(d)
    assert v["state"] == "fail"
    assert v["pass"] is False
    assert v["advisory"] is True  # honest fail, still advisory


# ─────────────────────────────────────────────────────────────────────────────
# attempt timeout — not-passed, loop continues
# ─────────────────────────────────────────────────────────────────────────────


def test_timeout_counts_as_not_passed_and_loop_continues(tmp_path, monkeypatch):
    d = _run_dir(tmp_path)
    _write_spec(d)
    _allow_env(monkeypatch)
    results = [(0, "done", ""), (0, "done", "")]
    _run_calls, agent_calls = _install_agent_s(
        monkeypatch, results=results, timeout_on={0}
    )

    assert ags.evaluate("", _ctx(d), "", None) == "pass"  # 2 of 3 (one timed out)
    assert len(agent_calls) == 3

    v = _verdict(d)
    assert [a["passed"] for a in v["attempts"]] == [False, True, True]
    assert v["attempts"][0]["rc"] is None


# ─────────────────────────────────────────────────────────────────────────────
# cost guard
# ─────────────────────────────────────────────────────────────────────────────


def test_cost_guard_stops_once_cap_exceeded(tmp_path, monkeypatch):
    d = _run_dir(tmp_path)
    _write_spec(d, max_usd=0.9)  # flat estimate 0.5: a 2nd attempt would exceed
    _allow_env(monkeypatch)
    _run_calls, agent_calls = _install_agent_s(
        monkeypatch, results=[(0, "done", ""), (0, "done", ""), (0, "done", "")]
    )

    assert ags.evaluate("", _ctx(d), "", None) == "pass"  # 1 of 1 completed
    assert len(agent_calls) == 1
    assert len(_verdict(d)["attempts"]) == 1


def test_zero_attempts_via_cost_cap_defers(tmp_path, monkeypatch):
    """A cap too small for even one attempt runs nothing → defer, never a pass."""
    d = _run_dir(tmp_path)
    _write_spec(d, max_usd=0.0)
    _allow_env(monkeypatch)
    _run_calls, agent_calls = _install_agent_s(
        monkeypatch, results=[(0, "done", ""), (0, "done", ""), (0, "done", "")]
    )

    assert ags.evaluate("", _ctx(d), "", None) == "defer"
    assert agent_calls == []
    v = _verdict(d)
    assert v["state"] == "defer"
    assert v["pass"] is None
    assert v["attempts"] == []


def test_launch_failure_defers_without_spawning(tmp_path, monkeypatch):
    """A launch the OS refuses is a GUI-verifier outage → defer, never fail."""
    d = _run_dir(tmp_path)
    _write_spec(d)
    _allow_env(monkeypatch)

    run_calls: list[list] = []

    def fake_run(cmd, **kwargs):
        run_calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    class _BrokenPopen:
        def __init__(self, *args, **kwargs):
            raise OSError("cannot launch")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("subprocess.Popen", _BrokenPopen)

    assert ags.evaluate("", _ctx(d), "", None) == "defer"
    # only the best-effort osascript quit ran — never the Agent-S CLI
    assert all(c and c[0] == "osascript" for c in run_calls)
    v = _verdict(d)
    assert v["state"] == "defer"
    assert v["attempts"] == []


# ─────────────────────────────────────────────────────────────────────────────
# evidence shape
# ─────────────────────────────────────────────────────────────────────────────


def test_evidence_verdict_json_shape(tmp_path, monkeypatch):
    d = _run_dir(tmp_path)
    _write_spec(d)
    _allow_env(monkeypatch)
    _install_agent_s(monkeypatch, results=[(0, "done", ""), (0, "done", ""), (0, "done", "")])

    assert ags.evaluate("", _ctx(d), "", None) == "pass"

    v = _verdict(d)
    assert set(v) == {"pass", "state", "attempts", "reason", "evidence", "advisory"}
    assert v["advisory"] is True
    assert v["state"] == "pass"
    assert v["pass"] is True
    assert isinstance(v["evidence"], list)
    assert isinstance(v["reason"], str)
    for a in v["attempts"]:
        assert set(a) == {"rc", "duration_s", "passed", "log_tail"}


def test_capture_evidence_copies_screenshots(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "shot.png").write_bytes(b"\x89PNG")
    (scratch / "notes.txt").write_text("not a screenshot")

    copied = ags._capture_evidence(str(scratch), str(tmp_path / "artifacts"), 2)

    assert len(copied) == 1
    assert os.path.basename(copied[0]) == "attempt_2_shot.png"
    assert os.path.isfile(copied[0])


# ─────────────────────────────────────────────────────────────────────────────
# executable rc contract (python -m ... via runpy)
# ─────────────────────────────────────────────────────────────────────────────


def test_main_rc_defer(tmp_path, monkeypatch):
    d = _run_dir(tmp_path)
    task = _write_spec(d)
    _clear_env(monkeypatch)
    _install_agent_s(monkeypatch)

    assert _module_rc(monkeypatch, task) == 2


def test_main_rc_fail(tmp_path, monkeypatch):
    d = _run_dir(tmp_path)
    task = _write_spec(d)
    _allow_env(monkeypatch)
    _install_agent_s(
        monkeypatch, results=[(0, "failed", ""), (0, "failed", ""), (0, "failed", "")]
    )

    assert _module_rc(monkeypatch, task) == 1


def test_main_rc_pass(tmp_path, monkeypatch):
    d = _run_dir(tmp_path)
    task = _write_spec(d)
    _allow_env(monkeypatch)
    _install_agent_s(
        monkeypatch, results=[(0, "done", ""), (0, "done", ""), (0, "done", "")]
    )

    assert _module_rc(monkeypatch, task) == 0


def test_main_no_args_defers(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["agent_s_smoke"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(_MODULE_FILE), run_name="__main__")
    assert exc.value.code == 2
