"""The MCP registry: name resolution, transport normalization, auth expansion.

The property under test throughout is that a name is a *reference* and the
registry is the definition — so a grant resolves to exactly one hermetic config,
a credential is either resolved or refused, and never silently empty.
"""
import json

import pytest

from mini_ork.dispatch.mcp_registry import (
    McpRegistryError,
    load_registry,
    normalize_entry,
    operator_registry_path,
    registry_paths,
    remove_entry,
    resolve_grant,
    scoped_config,
    write_entry,
)


def _env(tmp_path, **extra):
    env = {"MINI_ORK_HOME": str(tmp_path / "home")}
    env.update(extra)
    return env


# ── source precedence ────────────────────────────────────────────────────────


def test_registry_paths_order_is_run_then_home_then_repo(tmp_path):
    paths = registry_paths(
        {
            "MINI_ORK_RUN_DIR": str(tmp_path / "run"),
            "MINI_ORK_HOME": str(tmp_path / "home"),
            "MINI_ORK_ROOT": str(tmp_path / "root"),
        }
    )
    assert paths[0] == tmp_path / "run" / "config" / "mcp_servers.json"
    assert paths[1] == tmp_path / "home" / "config" / "mcp_servers.json"
    assert paths[2] == tmp_path / "root" / ".claude" / "mcp_servers.json"


def test_registry_paths_omits_unset_roots():
    # With no MINI_ORK_* set at all there is no file to read; a run must not
    # accidentally pick up a relative path resolved against the cwd.
    assert registry_paths({}) == []


def test_load_registry_earliest_source_wins_per_name(tmp_path):
    env = _env(tmp_path, MINI_ORK_RUN_DIR=str(tmp_path / "run"))
    run_file = tmp_path / "run" / "config" / "mcp_servers.json"
    run_file.parent.mkdir(parents=True)
    run_file.write_text(json.dumps({"mcpServers": {"ctx": {"url": "https://run"}}}))
    home_file = operator_registry_path(env)
    home_file.parent.mkdir(parents=True)
    home_file.write_text(
        json.dumps({"mcpServers": {"ctx": {"url": "https://home"}, "other": {"command": "x"}}})
    )
    registry = load_registry(env)
    assert registry["ctx"]["url"] == "https://run"
    assert registry["other"]["command"] == "x"


def test_load_registry_accepts_bare_mapping_and_reports_garbage(tmp_path, capsys):
    env = _env(tmp_path)
    path = operator_registry_path(env)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"stdio": {"command": "npx"}}))
    assert load_registry(env)["stdio"]["command"] == "npx"

    path.write_text("{not json")
    assert load_registry(env) == {}
    assert "ignoring unreadable registry" in capsys.readouterr().err


# ── transport normalization ──────────────────────────────────────────────────


def test_normalize_stdio_passes_command_through():
    assert normalize_entry("s", {"command": "npx", "args": ["-y", "pkg"]}) == {
        "command": "npx",
        "args": ["-y", "pkg"],
    }


def test_normalize_http_from_editor_source_key():
    assert normalize_entry("s", {"source": "http", "url": "https://x", "headers": {"A": "b"}}) == {
        "type": "http",
        "url": "https://x",
        "headers": {"A": "b"},
    }


def test_normalize_sse_type_is_preserved():
    assert normalize_entry("s", {"type": "sse", "url": "https://x"})["type"] == "sse"


def test_normalize_extension_entry_is_refused():
    # An extension-provided server only exists inside the editor; a node would
    # grant a name that can never resolve.
    with pytest.raises(McpRegistryError, match="extension-provided"):
        normalize_entry("s", {"source": "extension"})


def test_normalize_oauth_entry_warns_but_keeps_static_headers(capsys):
    result = normalize_entry(
        "s",
        {
            "source": "http",
            "url": "https://x",
            "headers": {"Authorization": "Bearer t"},
            "oauth": {"client_id": "cid"},
        },
    )
    assert result == {"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer t"}}
    assert "oauth" in capsys.readouterr().err


def test_normalize_without_transport_is_an_error():
    with pytest.raises(McpRegistryError, match="neither a command"):
        normalize_entry("s", {"description": "nothing usable"})


# ── grant resolution ─────────────────────────────────────────────────────────


def test_resolve_grant_unconfigured_name_is_a_no_op_stub():
    # The stub keeps the emitted config hermetic under --strict-mcp-config while
    # making the missing definition visible in the node's own output.
    assert resolve_grant("ghost", {}, {}) == {
        "command": "echo",
        "args": ["no-op: ghost not configured"],
    }


def test_resolve_grant_disabled_entry_is_refused():
    registry = {"s": {"command": "npx", "enabled": False}}
    with pytest.raises(McpRegistryError, match="disabled"):
        resolve_grant("s", registry, {})


def test_resolve_grant_expands_placeholders_from_environ():
    registry = {"s": {"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer ${T}"}}}
    result = resolve_grant("s", registry, {"T": "secret-value"})
    assert result["headers"]["Authorization"] == "Bearer secret-value"


def test_resolve_grant_expands_placeholders_nested_in_args():
    registry = {"s": {"command": "run", "args": ["--token", "${T:-anon}"]}}
    assert resolve_grant("s", registry, {})["args"] == ["--token", "anon"]


def test_resolve_grant_unset_placeholder_fails_loudly():
    # The whole point of resolving at grant time: an unset credential stops the
    # node here, not deep inside a tool call with a silently-empty header.
    registry = {"s": {"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer ${T}"}}}
    with pytest.raises(McpRegistryError, match=r"\$\{T\}"):
        resolve_grant("s", registry, {})


def test_resolve_grant_drops_editor_only_keys():
    registry = {"s": {"command": "npx", "enabled": True, "remote": False, "timeout": 30}}
    assert resolve_grant("s", registry, {}) == {"command": "npx"}


def test_scoped_config_covers_exactly_the_grants_and_keeps_order():
    registry = {
        "a": {"command": "a"},
        "b": {"command": "b"},
        "c": {"command": "c"},
    }
    assert list(scoped_config(("b", "a"), registry, {})) == ["b", "a"]


# ── writes ───────────────────────────────────────────────────────────────────


def test_write_then_remove_entry_round_trips(tmp_path):
    env = _env(tmp_path)
    path = write_entry("docs", {"type": "http", "url": "https://d"}, env)
    assert path == operator_registry_path(env)
    assert load_registry(env)["docs"]["url"] == "https://d"
    assert remove_entry("docs", env) is True
    assert remove_entry("docs", env) is False
    assert load_registry(env) == {}


def test_write_entry_never_inlines_a_placeholder_as_a_value(tmp_path):
    env = _env(tmp_path)
    write_entry("docs", {"type": "http", "url": "https://d", "headers": {"A": "Bearer ${T}"}}, env)
    # The file that names the server stays committable: the reference is literal.
    assert "${T}" in operator_registry_path(env).read_text()


def test_write_servers_leaves_no_temporary_behind(tmp_path):
    env = _env(tmp_path)
    write_entry("docs", {"command": "npx"}, env)
    leftovers = list(operator_registry_path(env).parent.glob("*.tmp"))
    assert leftovers == []
