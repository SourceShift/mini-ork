import json

from mini_ork.dispatch.providers import (
    _build_allowed_tools_arg,
    _mo_default_tools_for_type,
    _resolve_node_tools,
    apply_tool_grants,
)


def test_workflow_resolution_and_type_defaults(tmp_path):
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text("""nodes:\n  - name: planner\n    type: planner\n    tools:\n      native: [Read]\n      mcp: [codegraph]\n  - name: implementer\n    type: implementer\n""")
    assert _resolve_node_tools({"MO_WORKFLOW_YAML": str(workflow), "MO_NODE_ID": "planner", "MO_NODE_TYPE": "planner"}) == "Read|codegraph"
    assert _resolve_node_tools({"MO_WORKFLOW_YAML": str(workflow), "MO_NODE_ID": "implementer", "MO_NODE_TYPE": "implementer"}) == "Read,Write,Edit,Bash|"
    assert _mo_default_tools_for_type("reviewer") == "Read,Bash|"


def test_mcp_rendering_and_claude_argv(tmp_path):
    assert _build_allowed_tools_arg("Read,Write", "codegraph,context7") == "Read,Write,mcp__codegraph,mcp__context7"
    env = {"MO_RESOLVED_NODE_TOOLS": "Read|codegraph"}
    argv = apply_tool_grants(("claude", "--output-format", "text"), env=env, run_dir=str(tmp_path))
    assert "--allowedTools" in argv
    assert "mcp__codegraph" in argv[argv.index("--allowedTools") + 1]
    assert "--strict-mcp-config" in argv
    config = json.loads((tmp_path / ".mcp-config.json").read_text())
    assert "codegraph" in config["mcpServers"]


def test_non_claude_command_is_unchanged():
    command = ("codex", "exec")
    assert apply_tool_grants(command, env={"MO_RESOLVED_NODE_TOOLS": "Read|codegraph"}) == command
