"""Native provider-registry contracts without sourcing the legacy dispatcher."""

from pathlib import Path

from mini_ork.context import run_context_scope
from mini_ork.dispatch.providers import lane_health, mini_ork_root, resolve_provider


REGISTRY = """
providers:
  stubexec:
    kind: executable
    family: local
    script: scripts/registry_stub.sh
  oaitest:
    kind: openai-compat
    family: openrouter
    model: test-model-7
    base_url: https://example.invalid/v1
    api_key_env: TEST_OAI_KEY
  gwtest:
    kind: anthropic-compat
    family: testgw
    model: gw-model-1
    base_url: https://gw.example.invalid/anthropic
    api_key_env: TEST_GW_KEY
    extra_env:
      ENABLE_TOOL_SEARCH: "false"
"""


def _fixture(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "config" / "providers.yaml").write_text(REGISTRY)
    (tmp_path / "scripts" / "registry_stub.sh").write_text("#!/bin/sh\n")
    (tmp_path / "scripts" / "registry_stub.sh").chmod(0o755)
    return tmp_path


def test_registry_executable_and_openai_resolution(tmp_path, monkeypatch):
    root = _fixture(tmp_path)
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(root / "config" / "providers.yaml"))
    executable = resolve_provider("stubexec", root)
    assert executable.command[0].endswith("scripts/registry_stub.sh")
    openai = resolve_provider("oaitest", root)
    assert openai.env["MO_OAI_BASE_URL"] == "https://example.invalid/v1"
    assert openai.env["MO_OAI_ENV_KEY"] == "TEST_OAI_KEY"
    assert openai.env["MO_OAI_MODEL"] == "test-model-7"
    assert "mini_ork.dispatch.codex_transport" in openai.command


def test_registry_anthropic_env_and_health(tmp_path, monkeypatch):
    root = _fixture(tmp_path)
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(root / "config" / "providers.yaml"))
    monkeypatch.setenv("TEST_GW_KEY", "gw-key-ok")
    provider = resolve_provider("gwtest", root)
    assert provider.env["ANTHROPIC_AUTH_TOKEN"] == "gw-key-ok"
    assert provider.env["ANTHROPIC_BASE_URL"] == "https://gw.example.invalid/anthropic"
    assert provider.env["ANTHROPIC_MODEL"] == "gw-model-1"
    assert provider.env["ENABLE_TOOL_SEARCH"] == "false"
    assert lane_health("gwtest", root).ok
    monkeypatch.delenv("TEST_GW_KEY")
    assert not lane_health("gwtest", root).ok


def test_unknown_registry_lane_fails_closed(tmp_path, monkeypatch):
    root = _fixture(tmp_path)
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(root / "config" / "providers.yaml"))
    health = lane_health("no_such_provider", root)
    assert not health.ok
    assert "unknown lane" in health.reason


def test_mini_ork_root_reads_run_context_over_process_env(tmp_path, monkeypatch):
    """Bottleneck #1 slice 2a: a migrated dispatch reader honors the per-run
    contextvar binding when set, and falls back to os.environ otherwise —
    proving isolation is wired without regressing the legacy env path."""
    scoped = tmp_path / "scoped"
    from_env = tmp_path / "from_env"
    scoped.mkdir()
    from_env.mkdir()
    monkeypatch.setenv("MINI_ORK_ROOT", str(from_env))

    # No binding → falls back to the process env (behavior-preserving).
    assert mini_ork_root() == from_env.resolve()

    # A run-scoped binding wins over the leaked process value…
    with run_context_scope({"MINI_ORK_ROOT": str(scoped)}):
        assert mini_ork_root() == scoped.resolve()
    # …and the fallback is restored on scope exit.
    assert mini_ork_root() == from_env.resolve()
