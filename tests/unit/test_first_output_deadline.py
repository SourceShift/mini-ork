"""B2 — a first-output deadline on every lane but the last.

A node's whole timeout (``MO_NODE_TIMEOUT_S``) used to be handed to every lane
in a fallback chain, so one hung lead lane could burn the node's budget twice
(the live ``sde-3b1-diagnoser`` run: 1500 s on deepseek_flash, then 1500 s on
opus). A wall-clock cut would kill legitimately long implementer sessions; the
safe signal is **no model output at all**, which a Claude-CLI stream-json lane
makes observable line by line.

These tests are hermetic: tiny local scripts as the spawned process, a tmp
providers registry, monkeypatched dispatch functions. No LLM, no network, no
credentials, no run-local state.
"""

from __future__ import annotations

import os
import time

import pytest

from mini_ork.dispatch import core, llm_dispatch, providers
from mini_ork.dispatch.failure_class import action, classify
from mini_ork.dispatch.models import DispatchRequest, DispatchResult

ARROW = "→"


# ── helpers ──────────────────────────────────────────────────────────────────

def _child_env(**extra: str) -> dict[str, str]:
    """A minimal, ambient-free child env. No MINI_ORK_RUN_DIR/MO_LIVE_FILE."""
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    env.update(extra)
    return env


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for var in (
        "MO_LIVE_FILE",
        "MO_KILL_REAP_TIMEOUT_S",
        "MINI_ORK_RUN_DIR",
        "MINI_ORK_RUN_ID",
        "MO_NODE_ID",
        "MO_DISPATCH_FIRST_OUTPUT_S",
        "MO_DISPATCH_ATTEMPT_TIMEOUT_S",
    ):
        monkeypatch.delenv(var, raising=False)


def _write_script(tmp_path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_gone_within(pid: int, budget_s: float = 4.0) -> bool:
    deadline = time.time() + budget_s
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


# ── 1–4. spawn_local's first-output deadline ─────────────────────────────────

def test_silent_process_is_cut_and_its_group_is_gone(tmp_path):
    """A silent 30 s child with ``first_output_s=1`` returns rc 124 fast."""
    pidfile = tmp_path / "silent.pid"
    script = _write_script(
        tmp_path, "silent.sh", 'echo $$ > "$1"\n/bin/sleep 30'
    )
    started = time.time()
    rc, stdout, stderr = core.spawn_local(
        [script, str(pidfile)],
        stdin="",
        timeout=60,
        env=_child_env(),
        cwd=str(tmp_path),
        first_output_s=1,
    )
    elapsed = time.time() - started
    assert rc == 124, f"expected rc 124, got {rc} (stderr={stderr!r})"
    assert elapsed < 5.0, f"cutoff must fire within ~3 s, took {elapsed:.1f}s"
    assert stderr.startswith("no model output within 1s"), stderr
    assert "first-output deadline" in stderr, stderr
    assert f"spawned{ARROW}kill" in stderr, stderr
    # The whole group was reaped, not just the shell leader.
    pid = int(pidfile.read_text().strip())
    assert _pid_gone_within(pid), f"process group member pid {pid} still alive"


def test_init_event_only_is_still_silent(tmp_path):
    """A ``system`` init line is CLI chatter, not model output."""
    script = _write_script(
        tmp_path,
        "init_only.sh",
        "echo '{\"type\":\"system\",\"subtype\":\"init\"}'\n/bin/sleep 30",
    )
    rc, _out, stderr = core.spawn_local(
        [script], stdin="", timeout=60, env=_child_env(), cwd=str(tmp_path),
        first_output_s=1,
    )
    assert rc == 124, f"an init-only lane must still be cut, got rc {rc} ({stderr!r})"
    assert stderr.startswith("no model output within 1s"), stderr


def test_model_output_in_time_is_never_cut(tmp_path):
    """A lane that streams a model event before the deadline runs to exit 0."""
    script = _write_script(
        tmp_path,
        "streams_in_time.sh",
        "sleep 0.2\necho '{\"type\":\"stream_event\",\"event\":{}}'\n/bin/sleep 2\nexit 0",
    )
    rc, _out, stderr = core.spawn_local(
        [script], stdin="", timeout=30, env=_child_env(), cwd=str(tmp_path),
        first_output_s=1,
    )
    assert rc == 0, f"a lane that streamed in time must not be cut ({stderr!r})"
    assert "no model output" not in stderr, stderr


def test_unset_deadline_is_byte_for_byte_today(tmp_path):
    """``first_output_s=None`` leaves a silent 2 s child untouched."""
    script = _write_script(tmp_path, "unset_silent.sh", "/bin/sleep 2\nexit 0")
    started = time.time()
    rc, stdout, stderr = core.spawn_local(
        [script], stdin="", timeout=30, env=_child_env(), cwd=str(tmp_path),
        first_output_s=None,
    )
    elapsed = time.time() - started
    assert rc == 0, f"the unset deadline must not cut the process ({stderr!r})"
    assert stdout == "" and stderr == ""
    assert elapsed >= 1.5


def test_overall_timeout_message_is_unchanged_when_armed(tmp_path):
    """Once a lane has streamed, only the ordinary lane timeout may cut it."""
    script = _write_script(
        tmp_path,
        "stream_then_hang.sh",
        "echo '{\"type\":\"result\",\"subtype\":\"done\"}'\n/bin/sleep 30",
    )
    rc, _out, stderr = core.spawn_local(
        [script], stdin="", timeout=2.0, env=_child_env(), cwd=str(tmp_path),
        first_output_s=1,
    )
    assert rc == 124, stderr
    assert stderr.startswith("timeout after 2"), stderr
    assert "no model output" not in stderr, stderr
    assert f"kill{ARROW}reaped" in stderr, stderr


def test_core_dispatch_forwards_the_deadline_to_spawn_local(monkeypatch):
    recorded: dict = {}

    def fake_spawn_local(argv, **kwargs):
        recorded.update(kwargs)
        return 0, "ok", ""

    monkeypatch.setattr(core, "spawn_local", fake_spawn_local)
    request = DispatchRequest(
        model="m", prompt="p", timeout_s=10.0, workspace="host",
        first_output_s=55.0,
    )
    result = core.dispatch(request, ["true"])
    assert result.ok, result.error
    assert recorded.get("first_output_s") == 55.0


def test_core_dispatch_workspace_path_ignores_the_deadline(monkeypatch):
    recorded: dict = {}

    def fake_spawn_in_workspace(backend, argv, **kwargs):
        recorded.update(kwargs)
        return 0, "ok", ""

    monkeypatch.setattr(core, "_spawn_in_workspace", fake_spawn_in_workspace)
    request = DispatchRequest(
        model="m", prompt="p", timeout_s=10.0, workspace="docker",
        first_output_s=55.0,
    )
    result = core.dispatch(request, ["true"])
    assert result.ok, result.error
    assert "first_output_s" not in recorded


# ── 5. dispatch_with_fallback: every lane but the last, streaming only ──────

_REGISTRY_YAML = """\
providers:
  zz_stream_a:
    kind: anthropic-compat
    base_url: http://127.0.0.1:9/v1
    api_key_env: MO_FOD_TEST_KEY
    model: zz-a
  zz_gateway_b:
    kind: anthropic-compat
    base_url: http://127.0.0.1:9/v1
    api_key_env: MO_FOD_TEST_KEY
    model: zz-b
    gateway: true
  zz_stream_c:
    kind: anthropic-compat
    base_url: http://127.0.0.1:9/v1
    api_key_env: MO_FOD_TEST_KEY
    model: zz-c
  zz_stream_d:
    kind: anthropic-compat
    base_url: http://127.0.0.1:9/v1
    api_key_env: MO_FOD_TEST_KEY
    model: zz-d
"""


def _install_registry(monkeypatch, tmp_path) -> None:
    registry = tmp_path / "providers.yaml"
    registry.write_text(_REGISTRY_YAML, encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(registry))
    monkeypatch.setenv("MO_FOD_TEST_KEY", "fod-test-key")


def test_fallback_arms_every_streaming_lane_but_the_last(monkeypatch, tmp_path):
    _install_registry(monkeypatch, tmp_path)
    recorded: list[DispatchRequest] = []

    def fake_dispatch_model(req, root=None, **_kwargs):
        recorded.append(req)
        if req.model == "zz_stream_d":
            return DispatchResult(ok=True, rc=0, text="ok", model=req.model)
        return DispatchResult(ok=False, rc=1, error="boom", model=req.model)

    monkeypatch.setattr(providers, "dispatch_model", fake_dispatch_model)
    result = providers.dispatch_with_fallback(
        DispatchRequest(model="zz_stream_a", prompt="p", timeout_s=99.0),
        ["zz_stream_a", "zz_stream_c", "zz_stream_d"],
        first_output_s=300.0,
    )
    assert result.ok, result.error
    assert [r.first_output_s for r in recorded] == [300.0, 300.0, None]


def test_fallback_skips_gateway_and_unresolvable_lanes(monkeypatch, tmp_path):
    _install_registry(monkeypatch, tmp_path)
    recorded: list[DispatchRequest] = []

    def fake_dispatch_model(req, root=None, **_kwargs):
        recorded.append(req)
        if req.model == "zz_stream_a":
            return DispatchResult(ok=True, rc=0, text="ok", model=req.model)
        return DispatchResult(ok=False, rc=1, error="boom", model=req.model)

    monkeypatch.setattr(providers, "dispatch_model", fake_dispatch_model)
    result = providers.dispatch_with_fallback(
        DispatchRequest(model="zz_gateway_b", prompt="p", timeout_s=99.0),
        # gateway (no stream-json) / unknown / streaming / last streaming
        ["zz_gateway_b", "zz_unknown_lane", "zz_stream_c", "zz_stream_a"],
        first_output_s=300.0,
    )
    assert result.ok, result.error
    assert [r.first_output_s for r in recorded] == [None, None, 300.0, None]


def test_fallback_does_not_arm_when_the_knob_is_disabled(monkeypatch, tmp_path):
    _install_registry(monkeypatch, tmp_path)
    recorded: list[DispatchRequest] = []

    def fake_dispatch_model(req, root=None, **_kwargs):
        recorded.append(req)
        return DispatchResult(ok=False, rc=1, error="boom", model=req.model)

    monkeypatch.setattr(providers, "dispatch_model", fake_dispatch_model)
    providers.dispatch_with_fallback(
        DispatchRequest(model="zz_stream_a", prompt="p", timeout_s=99.0),
        ["zz_stream_a", "zz_stream_c"],
        first_output_s=None,
    )
    assert [r.first_output_s for r in recorded] == [None, None]


def test_dispatch_model_carries_first_output_to_engine_dispatch(monkeypatch, tmp_path):
    """The per-lane request is rebuilt field-by-field — the field must survive."""
    recorded: dict = {}

    def recording_backend(effective_request, spec):
        recorded["request"] = effective_request
        return DispatchResult(ok=True, rc=0, text="ok", model=effective_request.model)

    monkeypatch.setitem(providers.MODEL_DISPATCH_BACKENDS, "zz_stream_c",
                        recording_backend)
    monkeypatch.setattr(providers, "provider_environment", lambda *a, **k: {})
    _install_registry(monkeypatch, tmp_path)

    request = DispatchRequest(
        model="zz_stream_c", prompt="p", timeout_s=10.0,
        cwd=str(tmp_path), first_output_s=88.0,
    )
    result = providers.dispatch_model(request, str(tmp_path), preflight_check=False)
    assert result.ok, result.error
    assert recorded["request"].first_output_s == 88.0


# ── 6. mo_llm_dispatch's env knobs ──────────────────────────────────────────

def _install_dwf_recorder(monkeypatch) -> dict:
    recorded: dict = {}

    def fake_dispatch_with_fallback(request, lanes, root=None, **kwargs):
        recorded["lanes"] = list(lanes)
        recorded["kwargs"] = kwargs
        return DispatchResult(ok=True, rc=0, text="ok", model=lanes[-1])

    monkeypatch.setattr(providers, "dispatch_with_fallback",
                        fake_dispatch_with_fallback)
    return recorded


def test_env_knob_disabled_by_zero_and_empty(monkeypatch, tmp_path):
    recorded = _install_dwf_recorder(monkeypatch)

    monkeypatch.setenv("MO_DISPATCH_FIRST_OUTPUT_S", "0")
    assert llm_dispatch.mo_llm_dispatch(
        "laneA,laneB", "p", str(tmp_path / "o0.txt")) == 0
    assert recorded["kwargs"].get("first_output_s") is None

    monkeypatch.setenv("MO_DISPATCH_FIRST_OUTPUT_S", "")
    assert llm_dispatch.mo_llm_dispatch(
        "laneA,laneB", "p", str(tmp_path / "oE.txt")) == 0
    assert recorded["kwargs"].get("first_output_s") is None


def test_env_knob_defaults_to_300(monkeypatch, tmp_path):
    recorded = _install_dwf_recorder(monkeypatch)
    assert llm_dispatch.mo_llm_dispatch(
        "laneA,laneB", "p", str(tmp_path / "o.txt")) == 0
    assert recorded["kwargs"].get("first_output_s") == 300


def test_env_knob_garbage_fails_closed_with_one_warning(monkeypatch, tmp_path, capfd):
    recorded = _install_dwf_recorder(monkeypatch)
    monkeypatch.setenv("MO_DISPATCH_FIRST_OUTPUT_S", "abc")
    assert llm_dispatch.mo_llm_dispatch(
        "laneA,laneB", "p", str(tmp_path / "oW.txt")) == 0
    captured = capfd.readouterr()
    assert recorded["kwargs"].get("first_output_s") is None
    warnings = [
        line for line in captured.err.splitlines()
        if "first_output" in line.lower().replace("-", "_")
    ]
    assert len(warnings) == 1, captured.err


def test_attempt_timeout_threaded_only_when_positive(monkeypatch, tmp_path):
    recorded = _install_dwf_recorder(monkeypatch)

    monkeypatch.setenv("MO_DISPATCH_ATTEMPT_TIMEOUT_S", "120")
    assert llm_dispatch.mo_llm_dispatch(
        "laneA,laneB", "p", str(tmp_path / "oT1.txt")) == 0
    assert recorded["kwargs"].get("per_attempt_timeout_s") == 120
    assert recorded["kwargs"].get("first_output_s") == 300

    monkeypatch.setenv("MO_DISPATCH_ATTEMPT_TIMEOUT_S", "0")
    assert llm_dispatch.mo_llm_dispatch(
        "laneA,laneB", "p", str(tmp_path / "oT2.txt")) == 0
    assert not recorded["kwargs"].get("per_attempt_timeout_s")


def test_single_lane_dispatch_never_gets_a_deadline(monkeypatch, tmp_path):
    recorded: dict = {}

    def fake_dispatch_model(request, root=None, **_kwargs):
        recorded["request"] = request
        return DispatchResult(ok=True, rc=0, text="ok", model=request.model)

    monkeypatch.setattr(providers, "dispatch_model", fake_dispatch_model)
    assert llm_dispatch.mo_llm_dispatch(
        "solo", "p", str(tmp_path / "oS.txt")) == 0
    assert recorded["request"].first_output_s is None


# ── 7. is_model_event ───────────────────────────────────────────────────────

@pytest.mark.parametrize("line", [
    '{"type":"assistant"}',
    '{"type":"stream_event","event":{}}',
    '{"type":"user"}',
    '{"type":"result","subtype":"done"}',
    '{"type":"user"}\n',
])
def test_is_model_event_true(line):
    assert core.is_model_event(line) is True


@pytest.mark.parametrize("line", [
    '{"type":"system","subtype":"init"}',
    "plain text, not json",
    "{}",
    '[{"type":"assistant"}]',
    "",
    '{"type":null}',
    '{"type":["assistant"]}',
    '{"type":"assistant"',
])
def test_is_model_event_false(line):
    assert core.is_model_event(line) is False


# ── 8. B1 interaction: rc 124 keeps the timeout meaning ────────────────────

def test_cutoff_classifies_as_timeout_and_walks_to_the_next_lane():
    err = (
        "no model output within 300s "
        "(first-output deadline; spawned→kill 301s)"
    )
    assert classify(err, 124)[0] == "timeout"
    act = action("timeout")
    assert act.next_lane is True
    assert act.retry_chain is False
