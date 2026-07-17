"""Standalone unit tests for ``mini_ork.ported.mo_emit_hook``.

Replaces the bash-parity gate as part of the bash→Python migration: the
Python port is now the sole implementation, so its coverage no longer
drives ``lib/mo_emit_hook.sh`` in a subprocess — it asserts the port's
behaviour directly against a stubbed ``subprocess.run``. These pin the
same eight behavioural cases the retired parity gate covered (a-h, see
module docstring history) plus direct unit coverage of the two private
helpers (``_resolve_hook_path``, ``_resolve_timeout_bin``):

  (a) ``MINI_ORK_ON_EVENT`` unset → no invocation.
  (b) ``MINI_ORK_ON_EVENT=""`` → no invocation.
  (c) Normal invocation → argv carries (event_type, run_id, payload_json)
      exactly, hook first.
  (d) Missing positional args → defaults ``"unknown"`` / ``""`` / ``"{}"``.
  (e) Non-zero exit from the hook → swallowed, never raises.
  (f) Relative ``.sh`` path whose file exists → resolved to absolute
      before dispatch; non-existent / non-``.sh`` / absolute paths pass
      through unchanged.
  (g) Timeout binary probed in ``gtimeout`` → ``timeout`` → none order;
      when found, the 5s cap is embedded as a ``[bin, "5", ...]`` argv
      prefix; when absent, the hook runs unguarded (matches bash's
      two-branch decision — this port never gets to enforce the cap
      itself, so we assert the *command* that would enforce it rather
      than real wall-clock timing).
  (h) stdout/stderr are redirected to ``DEVNULL`` and the call uses
      ``shell=False``, ``check=False``.

No bash subprocess is ever spawned. ``subprocess.run`` is monkeypatched
to a recorder so the hook binary named in ``MINI_ORK_ON_EVENT`` is never
actually executed; filesystem existence checks use real (but isolated)
files under pytest's ``tmp_path``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mini_ork.ported import mo_emit_hook as mod
from mini_ork.ported.mo_emit_hook import mo_emit_hook


class _RecordingRun:
    """Stand-in for ``subprocess.run`` that records calls instead of executing."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"argv": argv, "kwargs": kwargs})
        return subprocess.CompletedProcess(argv, 0)


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _RecordingRun:
    rec = _RecordingRun()
    monkeypatch.setattr(mod.subprocess, "run", rec)
    return rec


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINI_ORK_ON_EVENT", raising=False)


# ── (a)/(b) no-op when unset/empty ──────────────────────────────────────────


class TestNoOp:
    def test_a_env_var_unset_no_invocation(self, recorder: _RecordingRun) -> None:
        mo_emit_hook("ev", "run-1", "{}")
        assert recorder.calls == []

    def test_b_env_var_empty_no_invocation(
        self, recorder: _RecordingRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", "")
        mo_emit_hook("ev", "run-1", "{}")
        assert recorder.calls == []


# ── (c)/(d) argv construction incl. defaults ────────────────────────────────


class TestArgvConstruction:
    def test_c_normal_invocation_captures_argv(
        self, recorder: _RecordingRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", "/abs/path/hook.sh")
        monkeypatch.setattr(mod, "_resolve_timeout_bin", lambda: None)
        mo_emit_hook(
            "node_end",
            "run-1781509524-39638",
            '{"node_id":"planner","node_type":"planner"}',
        )
        assert len(recorder.calls) == 1
        argv = recorder.calls[0]["argv"]
        assert argv == [
            "/abs/path/hook.sh",
            "node_end",
            "run-1781509524-39638",
            '{"node_id":"planner","node_type":"planner"}',
        ]

    def test_d_default_args_match_bash_defaults(
        self, recorder: _RecordingRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", "/abs/path/hook.sh")
        monkeypatch.setattr(mod, "_resolve_timeout_bin", lambda: None)
        mo_emit_hook()
        argv = recorder.calls[0]["argv"]
        # bash: ${1:-unknown} / ${2:-} / ${3:-{\}} → "unknown" / "" / "{}"
        assert argv[1:] == ["unknown", "", "{}"]


# ── (e) non-zero exit swallowed ─────────────────────────────────────────────


class TestNonZeroExitSwallowed:
    def test_e_nonzero_exit_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", "/abs/path/hook.sh")
        monkeypatch.setattr(mod, "_resolve_timeout_bin", lambda: None)

        def _fail(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 7)

        monkeypatch.setattr(mod.subprocess, "run", _fail)
        # Must not raise — bash swallows via `|| true`; the port's bare
        # subprocess.run(check=False) already never raises on nonzero rc.
        mo_emit_hook("ev", "run-1", "{}")

    def test_e_check_is_false(
        self, recorder: _RecordingRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", "/abs/path/hook.sh")
        monkeypatch.setattr(mod, "_resolve_timeout_bin", lambda: None)
        mo_emit_hook("ev", "run-1", "{}")
        assert recorder.calls[0]["kwargs"]["check"] is False


# ── (f) relative .sh path resolved to absolute ──────────────────────────────


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
        recorder: _RecordingRun,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sub = tmp_path / "hooks"
        sub.mkdir()
        (sub / "rec.sh").write_text("#!/usr/bin/env bash\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MINI_ORK_ON_EVENT", "hooks/rec.sh")
        monkeypatch.setattr(mod, "_resolve_timeout_bin", lambda: None)

        mo_emit_hook("ev", "run-1", "{}")

        argv = recorder.calls[0]["argv"]
        assert os.path.isabs(argv[0])
        assert os.path.samefile(argv[0], sub / "rec.sh")
        assert argv[1:] == ["ev", "run-1", "{}"]


# ── (g) timeout binary probed + 5s cap embedded in argv ─────────────────────


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


class TestTimeoutCapDispatch:
    def test_g_timeout_cap_embedded_in_argv_when_available(
        self, recorder: _RecordingRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", "/abs/hook.sh")
        monkeypatch.setattr(mod, "_resolve_timeout_bin", lambda: "gtimeout")
        mo_emit_hook("ev", "run-1", "{}")
        argv = recorder.calls[0]["argv"]
        assert argv[:2] == ["gtimeout", "5"]
        assert argv[2:] == ["/abs/hook.sh", "ev", "run-1", "{}"]

    def test_g_unguarded_exec_when_no_timeout_binary(
        self, recorder: _RecordingRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", "/abs/hook.sh")
        monkeypatch.setattr(mod, "_resolve_timeout_bin", lambda: None)
        mo_emit_hook("ev", "run-1", "{}")
        argv = recorder.calls[0]["argv"]
        assert argv == ["/abs/hook.sh", "ev", "run-1", "{}"]


# ── (h) stdout/stderr swallowed + shell=False ───────────────────────────────


class TestSwallowedIO:
    def test_h_stdout_stderr_devnull_and_no_shell(
        self, recorder: _RecordingRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINI_ORK_ON_EVENT", "/abs/hook.sh")
        monkeypatch.setattr(mod, "_resolve_timeout_bin", lambda: None)
        mo_emit_hook("ev", "run-1", "{}")
        kwargs = recorder.calls[0]["kwargs"]
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["shell"] is False
