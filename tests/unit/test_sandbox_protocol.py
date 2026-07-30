"""Unit tests for the ``Workspace`` protocol + ``local`` backend (sandbox P0).

Covers the acceptance criteria in ``kickoffs/sandbox-p0-workspace-protocol.md``:
``exec`` success/nonzero, ``put``/``get`` round-trip, registry resolution, and
the extension seam. No behavior beyond the runtime contract is exercised here —
the ``local`` backend must stay byte-for-byte ``mo_runtime_exec`` with the tuple
flipped to ``(rc, output)``.
"""
from __future__ import annotations

import pytest

from mini_ork.runtime.sandbox import (
    LocalWorkspace,
    Workspace,
    get_workspace,
    register_workspace_backend,
)


def test_exec_echo_returns_zero_and_stdout(tmp_path):
    ws = LocalWorkspace()
    rc, out = ws.exec("echo hi", cwd=str(tmp_path), timeout=10)
    assert rc == 0
    assert "hi" in out


def test_exec_nonzero_returncode_propagates(tmp_path):
    ws = LocalWorkspace()
    rc, _out = ws.exec("exit 3", cwd=str(tmp_path), timeout=10)
    assert rc == 3


def test_exec_merges_stderr_into_output(tmp_path):
    ws = LocalWorkspace()
    rc, out = ws.exec("echo oops 1>&2; exit 1", cwd=str(tmp_path), timeout=10)
    assert rc == 1
    assert "oops" in out


def test_exec_missing_cwd_fails_rc126():
    ws = LocalWorkspace()
    rc, _out = ws.exec("echo hi", cwd="/no/such/dir/at/all", timeout=10)
    assert rc == 126


def test_put_get_round_trip():
    ws = LocalWorkspace()
    path = ws.put("round trip payload")
    assert ws.get(path) == "round trip payload"


def test_put_returns_paths_inside_workspace_root(tmp_path):
    root = str(tmp_path / "scratch")
    ws = LocalWorkspace(root=root)
    path = ws.put("x")
    assert path.startswith(root)


def test_up_down_are_noops():
    ws = LocalWorkspace()
    assert ws.up() is None
    assert ws.down() is None


def test_local_workspace_satisfies_protocol():
    assert isinstance(LocalWorkspace(), Workspace)


def test_get_workspace_local_returns_local_workspace():
    ws = get_workspace("local")
    assert isinstance(ws, LocalWorkspace)


def test_get_workspace_default_is_local():
    assert isinstance(get_workspace(), LocalWorkspace)


def test_get_workspace_unknown_backend_raises_value_error():
    with pytest.raises(ValueError):
        get_workspace("nope")


def test_register_workspace_backend_makes_custom_resolvable():
    class _Custom(LocalWorkspace):
        pass

    register_workspace_backend("custom-p0-test", lambda **kw: _Custom(**kw))
    try:
        assert isinstance(get_workspace("custom-p0-test"), _Custom)
    finally:
        # keep the module-level registry clean for other tests
        from mini_ork.runtime import sandbox as _mod

        _mod._WORKSPACE_BACKENDS.pop("custom-p0-test", None)


def test_get_workspace_passes_kwargs_to_factory(tmp_path):
    root = str(tmp_path / "kw")
    ws = get_workspace("local", root=root)
    assert ws.put("hi").startswith(root)
