"""Provider setup must remain workflow-aware, secret-safe, and native-runtime compatible."""

from __future__ import annotations

import io
import os
import stat
from pathlib import Path

from mini_ork.cli import providers
from mini_ork.dispatch import DispatchRequest, DispatchResult
from mini_ork.dispatch import providers as dispatch_providers
from mini_ork.dispatch.secrets import SecretStoreError, read_secret_exports, write_secret_exports


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "engine"
    (root / "config").mkdir(parents=True)
    (root / "config" / "agents.yaml").write_text(
        "lanes:\n  minimax_lens: minimax\n  glm_lens: glm\n",
        encoding="utf-8",
    )
    (root / "config" / "providers.yaml").write_text(
        "providers:\n"
        "  minimax:\n"
        "    kind: anthropic-compat\n"
        "    family: minimax\n"
        "    api_key_env: MINIMAX_API_KEY\n"
        "    base_url: https://api.minimax.io/anthropic\n"
        "    model: MiniMax-M2.5\n"
        "  glm:\n"
        "    kind: anthropic-compat\n"
        "    family: zai\n"
        "    api_key_env: GLM_API_KEY\n"
        "    base_url: https://api.z.ai/api/anthropic\n"
        "    model: glm-4.7\n",
        encoding="utf-8",
    )
    return root


def test_secret_store_rejects_symlinks_and_permissive_files(tmp_path):
    target = tmp_path / "target"
    target.write_text('export MINIMAX_API_KEY="secret"\n', encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "secrets.local.sh"
    link.symlink_to(target)
    try:
        read_secret_exports(link)
    except SecretStoreError as exc:
        assert "symlink" in str(exc)
    else:
        raise AssertionError("symlinked secret store was accepted")

    target.chmod(0o644)
    try:
        read_secret_exports(target)
    except SecretStoreError as exc:
        assert "owner-only" in str(exc)
    else:
        raise AssertionError("world-readable secret store was accepted")


def test_configure_from_stdin_creates_0600_file_without_echoing_value(tmp_path, monkeypatch, capsys):
    root = _root(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setattr("sys.stdin", io.StringIO("MINIMAX_API_KEY=not-for-output\n"))

    rc = providers.main(["configure", "--from-stdin", "minimax"], root=root)
    captured = capsys.readouterr()
    store = home / "config" / "secrets.local.sh"

    assert rc == 0
    assert "not-for-output" not in captured.out + captured.err
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert read_secret_exports(store) == {"MINIMAX_API_KEY": "not-for-output"}


def test_status_resolves_workflow_alias_and_hides_value(tmp_path, monkeypatch, capsys):
    root = _root(tmp_path)
    home = tmp_path / "home"
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        "nodes:\n  - { name: edge, model_lane: minimax_lens }\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    write_secret_exports({"MINIMAX_API_KEY": "not-for-output"})

    rc = providers.main(["status", "--workflow", str(workflow)], root=root)
    captured = capsys.readouterr()

    assert rc == 0
    assert "minimax_lens" in captured.out
    assert "MINIMAX_API_KEY" in captured.out
    assert "configured (local store)" in captured.out
    assert "not-for-output" not in captured.out + captured.err


def test_stdin_refuses_implicit_replacement(tmp_path, monkeypatch):
    root = _root(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    write_secret_exports({"MINIMAX_API_KEY": "old"})
    monkeypatch.setattr("sys.stdin", io.StringIO("MINIMAX_API_KEY=new\n"))

    assert providers.main(["configure", "--from-stdin", "minimax"], root=root) == 2
    assert read_secret_exports(home / "config" / "secrets.local.sh") == {"MINIMAX_API_KEY": "old"}


def test_native_dispatch_reads_local_store_without_mutating_parent_env(tmp_path, monkeypatch):
    root = _root(tmp_path)
    home = tmp_path / "home"
    outside_framework = tmp_path / "target"
    outside_framework.mkdir()
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    write_secret_exports({"MINIMAX_API_KEY": "stored-value"})
    captured: dict[str, str] = {}

    def fake_backend(request, spec):
        captured.update(request.env)
        assert spec.env["ANTHROPIC_AUTH_TOKEN"] == "stored-value"
        return DispatchResult(ok=True, rc=0, model=request.model)

    monkeypatch.setitem(dispatch_providers.MODEL_DISPATCH_BACKENDS, "minimax", fake_backend)
    result = dispatch_providers.dispatch_model(
        DispatchRequest(model="minimax", prompt="hi", cwd=str(outside_framework)), root=root
    )

    assert result.ok is True
    assert captured["MINIMAX_API_KEY"] == "stored-value"
    assert "MINIMAX_API_KEY" not in os.environ
