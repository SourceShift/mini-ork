"""Standalone unit tests for ``mini_ork.observability.emit_hook``.

The Python port is the sole implementation. Dispatch is now non-blocking:
``mo_emit_hook`` splits ``MINI_ORK_ON_EVENT`` into argv, validates the
executable, and starts it with ``subprocess.Popen`` (never a
``gtimeout``/``timeout`` prefix), returning immediately. These tests pin:

  (a) ``MINI_ORK_ON_EVENT`` unset → no invocation.
  (b) ``MINI_ORK_ON_EVENT=""`` / blank → no invocation.
  (c) Normal invocation → argv carries (event_type, run_id, payload_json)
      exactly, command first, arguments preserved.
  (d) Missing positional args → defaults ``"unknown"`` / ``""`` / ``"{}"``.
  (e) A failing or unspawnable hook is swallowed, never raises.
  (f) Relative ``.sh`` path resolution and the argv validator's executable
      requirements.
  (g) ``_resolve_timeout_bin`` is retained for compatibility but the
      dispatch argv never carries a timeout prefix.
  (h) stdin/stdout/stderr are ``DEVNULL``, ``start_new_session=True``, no
      shell.

``subprocess.Popen`` is monkeypatched to a recorder so the hook binary named
in ``MINI_ORK_ON_EVENT`` is never actually executed; the validator's
filesystem checks use real executables under pytest's ``tmp_path``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mini_ork.observability import emit_hook as mod
from mini_ork.observability.emit_hook import mo_emit_hook


class _FakeProc:
    """Minimal Popen stand-in for the timeout-enforcement thread."""

    pid = os.getpid()
    returncode = 0

    def wait(self, *args: Any, **kwargs: Any) -> int:
        return 0

    def poll(self, *args: Any, **kwargs: Any) -> int:
        return 0

    def kill(self) -> None:
        return None


class _RecordingPopen:
    """Stand-in for ``subprocess.Popen`` that records calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> _FakeProc:
        self.calls.append({"argv": argv, "kwargs": kwargs})
        return _FakeProc()


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _RecordingPopen:
    rec = _RecordingPopen()
    monkeypatch.setattr(mod.subprocess, "Popen", rec)
    # argv-shape tests assert the hook's own argv; the optional timeout
    # prefix is covered by TestTimeoutPrefix.
    monkeypatch.setattr(mod, "_resolve_timeout_bin", lambda: None)
    return rec


@pytest.fixture()
def hook_script(tmp_path: Path) -> Path:
    script = tmp_path / "hook.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINI_ORK_ON_EVENT", raising=False)
    monkeypatch.delenv("MINI_ORK_ON_EVENT_HEARTBEATS", raising=False)
    # The once-per-process warning flag is module state; reset it per test.
    monkeypatch.setattr(mod, "_WARNED_INVALID", False)


# ── (a)/(b) no-op when unset/empty ──────────────────────────────────────────


class TestNoOp:
    def test_a_env_var_unset_no_invocation(self, recorder: _RecordingPopen) -> None:
        mo_emit_hook("ev", "run-1", "{}")
        assert recorder.calls == []

    def test_b_env_var_empty_no_invocation(
        self, recorder: _RecordingPopen, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", "")
        mo_emit_hook("ev", "run-1", "{}")
        assert recorder.calls == []

    def test_b_env_var_blank_no_invocation(
        self, recorder: _RecordingPopen, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", "   ")
        mo_emit_hook("ev", "run-1", "{}")
        assert recorder.calls == []


# ── (c)/(d) argv construction incl. defaults ────────────────────────────────


class TestArgvConstruction:
    def test_c_normal_invocation_captures_argv(
        self, recorder: _RecordingPopen, hook_script: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", str(hook_script))
        mo_emit_hook(
            "node_end",
            "run-1781509524-39638",
            '{"node_id":"planner","node_type":"planner"}',
        )
        assert len(recorder.calls) == 1
        argv = recorder.calls[0]["argv"]
        assert argv == [
            str(hook_script),
            "node_end",
            "run-1781509524-39638",
            '{"node_id":"planner","node_type":"planner"}',
        ]

    def test_c_command_arguments_precede_event_arguments(
        self, recorder: _RecordingPopen, hook_script: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", f"{hook_script} --token t")
        mo_emit_hook("ev", "run-1", "{}")
        assert recorder.calls[0]["argv"] == [
            str(hook_script), "--token", "t", "ev", "run-1", "{}",
        ]

    def test_d_default_args_match_contract(
        self, recorder: _RecordingPopen, hook_script: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", str(hook_script))
        mo_emit_hook()
        argv = recorder.calls[0]["argv"]
        assert argv[1:] == ["unknown", "", "{}"]


# ── heartbeat opt-in ───────────────────────────────────────────────────────


class TestHeartbeats:
    def test_heartbeat_suppressed_by_default(
        self, recorder: _RecordingPopen, hook_script: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", str(hook_script))
        mo_emit_hook("node_heartbeat", "run-1", "{}")
        assert recorder.calls == []

    def test_heartbeat_opt_in_emits(
        self, recorder: _RecordingPopen, hook_script: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", str(hook_script))
        monkeypatch.setenv("MINI_ORK_ON_EVENT_HEARTBEATS", "1")
        mo_emit_hook("node_heartbeat", "run-1", "{}")
        assert len(recorder.calls) == 1


# ── (e) failures swallowed ─────────────────────────────────────────────────


class TestFailuresSwallowed:
    def test_e_spawn_failure_does_not_raise(
        self, hook_script: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", str(hook_script))

        def _boom(argv: list[str], **kwargs: Any) -> _FakeProc:
            raise RuntimeError("spawn exploded")

        monkeypatch.setattr(mod.subprocess, "Popen", _boom)
        assert mo_emit_hook("ev", "run-1", "{}") is None

    def test_e_invalid_value_warns_once_and_never_prints_the_value(
        self, recorder: _RecordingPopen, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT",
                           "/no/such/hook-xyz --token SECRETTOKEN")
        mo_emit_hook("ev", "run-1", "{}")
        mo_emit_hook("ev", "run-2", "{}")
        assert recorder.calls == []
        err = capsys.readouterr().err
        lines = [ln for ln in err.splitlines() if ln.strip()]
        assert lines == [
            "mini-ork: MINI_ORK_ON_EVENT is not an executable command; "
            "event hook disabled"
        ]
        assert "SECRETTOKEN" not in err and "/no/such/hook-xyz" not in err


# ── (f) path resolution + argv validation ──────────────────────────────────


class TestResolveHookPath:
    def test_absolute_path_passes_through(self) -> None:
        assert mod._resolve_hook_path("/abs/hook.sh") == "/abs/hook.sh"

    def test_non_sh_extension_passes_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "hook.py").write_text("")
        assert mod._resolve_hook_path("hook.py") == "hook.py"

    def test_relative_sh_missing_file_passes_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert mod._resolve_hook_path("nope.sh") == "nope.sh"

    def test_relative_sh_existing_file_resolved_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sub = tmp_path / "hooks"
        sub.mkdir()
        (sub / "rec.sh").write_text("#!/usr/bin/env bash\n")
        monkeypatch.chdir(tmp_path)

        resolved = mod._resolve_hook_path("hooks/rec.sh")

        assert os.path.isabs(resolved)
        assert os.path.basename(resolved) == "rec.sh"
        assert os.path.samefile(resolved, sub / "rec.sh")

    def test_mo_emit_hook_resolves_relative_path_before_dispatch(
        self,
        recorder: _RecordingPopen,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sub = tmp_path / "hooks"
        sub.mkdir()
        script = sub / "rec.sh"
        script.write_text("#!/usr/bin/env bash\nexit 0\n")
        script.chmod(0o755)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MINI_ORK_ON_EVENT", "hooks/rec.sh")

        mo_emit_hook("ev", "run-1", "{}")

        argv = recorder.calls[0]["argv"]
        assert os.path.isabs(argv[0])
        assert os.path.samefile(argv[0], script)
        assert argv[1:] == ["ev", "run-1", "{}"]


class TestHookArgv:
    def test_missing_path_is_none(self, tmp_path: Path) -> None:
        assert mod.hook_argv(str(tmp_path / "nope")) is None

    def test_non_executable_file_is_none(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain.sh"
        plain.write_text("#!/bin/sh\n")
        plain.chmod(0o644)
        assert mod.hook_argv(str(plain)) is None

    def test_directory_is_none(self, tmp_path: Path) -> None:
        assert mod.hook_argv(str(tmp_path)) is None

    def test_shlex_error_is_none(self) -> None:
        assert mod.hook_argv('hook "unclosed') is None

    def test_empty_is_none(self) -> None:
        assert mod.hook_argv("") is None
        assert mod.hook_argv("   ") is None


# ── (g) timeout helper retained, no argv prefix ────────────────────────────


class TestResolveTimeoutBin:
    def test_gtimeout_preferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: "/usr/local/bin/gtimeout" if name == "gtimeout" else None,
        )
        assert mod._resolve_timeout_bin() == "gtimeout"

    def test_timeout_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: "/usr/bin/timeout" if name == "timeout" else None,
        )
        assert mod._resolve_timeout_bin() == "timeout"

    def test_neither_present_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod.shutil, "which", lambda name: None)
        assert mod._resolve_timeout_bin() is None


class TestTimeoutPrefix:
    def test_g_a_timeout_binary_enforces_the_cap_when_present(
        self, recorder: _RecordingPopen, hook_script: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The timeout binary kills the hook after 5 s even if mini-ork exits
        # first (a daemon-thread killer dies with the process).
        monkeypatch.setenv("MINI_ORK_ON_EVENT", str(hook_script))
        monkeypatch.setattr(mod, "_resolve_timeout_bin", lambda: "gtimeout")
        mo_emit_hook("ev", "run-1", "{}")
        argv = recorder.calls[0]["argv"]
        assert argv == ["gtimeout", "-s", "KILL", "5", str(hook_script), "ev", "run-1", "{}"]


# ── (h) stdio swallowed + non-blocking spawn flags ─────────────────────────


class TestSpawnFlags:
    def test_h_devnull_and_new_session_and_no_shell(
        self, recorder: _RecordingPopen, hook_script: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", str(hook_script))
        mo_emit_hook("ev", "run-1", "{}")
        kwargs = recorder.calls[0]["kwargs"]
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["start_new_session"] is True
        # Popen's default is shell=False; the call must not opt in.
        assert kwargs.get("shell", False) is False
