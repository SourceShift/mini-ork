import json
import os
import subprocess
import sys
from pathlib import Path

from mini_ork.dispatch.providers import (
    _build_allowed_tools_arg,
    _mo_default_tools_for_type,
    _resolve_node_tools,
    apply_tool_grants,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CODE_FIX_WORKFLOW = REPO_ROOT / "recipes" / "code-fix" / "workflow.yaml"


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


# ── Ported from tests/unit/test_tool_grants.sh (retired) ─────────────────
# The bash fixture drove the real recipe and both dispatch backends; these
# tests reproduce its unique coverage natively so the Python dispatch path is
# the sole owner of the tool-grant contract.


def _resolve_from_real_workflow(node_id: str, node_type: str) -> str:
    return _resolve_node_tools({
        "MO_WORKFLOW_YAML": str(CODE_FIX_WORKFLOW),
        "MO_NODE_ID": node_id,
        "MO_NODE_TYPE": node_type,
    })


def test_real_workflow_producer_resolution():
    # The real recipes/code-fix/workflow.yaml declares tools: blocks whose
    # resolution is the producer contract the prior consumer-only attempt broke.
    assert _resolve_from_real_workflow("planner", "planner") == "Read|codegraph"
    assert _resolve_from_real_workflow("implementer", "implementer") == "Read,Write,Edit,Bash|codegraph"
    assert _resolve_from_real_workflow("reviewer", "reviewer") == "Read,Bash|"


def test_undeclared_nodes_fall_through_to_type_defaults(tmp_path):
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        "version: \"0.1.0\"\n"
        "task_class: code_fix\n"
        "nodes:\n"
        "  - name: planner\n    type: planner\n"
        "  - name: implementer\n    type: implementer\n"
        "  - name: reviewer\n    type: reviewer\n"
    )
    env = {"MO_WORKFLOW_YAML": str(workflow)}
    # implementer with no tools: block still gets Write/Edit (regression bar).
    assert _resolve_node_tools({**env, "MO_NODE_ID": "implementer", "MO_NODE_TYPE": "implementer"}) == "Read,Write,Edit,Bash|"
    assert _resolve_node_tools({**env, "MO_NODE_ID": "planner", "MO_NODE_TYPE": "planner"}) == "Read,Bash|"
    assert _resolve_node_tools({**env, "MO_NODE_ID": "reviewer", "MO_NODE_TYPE": "reviewer"}) == "Read,Bash|"


def test_implementer_profile_has_no_comms_or_web_mcp():
    # Structural invariant: an implementer must never be granted a comms/web MCP.
    impl = _resolve_from_real_workflow("implementer", "implementer")
    assert not any(tok in impl.lower() for tok in ("gmail", "web", "fetch", "http", "comms", "slack"))


def test_retired_worker_launcher_has_no_live_dependency():
    """Worker dispatch now routes through native subcommands, not a shell bridge."""
    assert not (REPO_ROOT / "bin" / "_worker-launcher.sh").exists()
    launcher = (REPO_ROOT / "bin" / "_mini_ork_subcommand.py").read_text()
    assert "os.execv" in launcher


def test_python_dispatch_subprocess_folds_tool_grants(tmp_path):
    # End-to-end argv contract for the canonical Python backend: run
    # `python3 -m mini_ork.dispatch` against a stub `claude` and assert the
    # implementer node's grant reaches the claude argv, --permission-mode
    # bypassPermissions survives, and the node-scoped .mcp-config.json is
    # written. apply_tool_grants builds the argv + config BEFORE claude runs,
    # so the stub can exit silently and the contract is still observable.
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "claude"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['STUB_CAPTURE_FILE'], 'w', encoding='utf-8') as fh:\n"
        "    json.dump(sys.argv[1:], fh)\n"
    )
    stub.chmod(0o755)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    capture = tmp_path / "argv.json"
    out = run_dir / "out.txt"

    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    env["MINI_ORK_ROOT"] = str(REPO_ROOT)
    env["MINI_ORK_RUN_DIR"] = str(run_dir)
    env["MO_NODE_ID"] = "implementer"
    env["MO_NODE_TYPE"] = "implementer"
    env["MO_WORKFLOW_YAML"] = str(CODE_FIX_WORKFLOW)
    env["MO_ALLOW_FRAMEWORK_CWD"] = "1"
    env["MO_LANE_TIER"] = "default"
    env["MO_TOOL_GRANTS_DISABLED"] = "0"
    env["STUB_CAPTURE_FILE"] = str(capture)
    env.pop("MINI_ORK_DB", None)  # no telemetry DB writes from this test
    pypath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + pypath if pypath else "")

    # `sonnet` (a real anthropic-family lane) — the backend's lane_health
    # preflight rejects unknown lanes before building the argv, so a fake lane
    # would make the stub unreachable. The stub `claude` intercepts the call,
    # so no real API request is made.
    subprocess.run(
        [sys.executable, "-m", "mini_ork.dispatch", "sonnet", "--out", str(out)],
        cwd=str(REPO_ROOT), env=env, input="", capture_output=True, text=True, timeout=120,
    )

    assert capture.exists(), "stub claude was never invoked (argv not captured)"
    argv = json.loads(capture.read_text())

    assert "--allowedTools" in argv
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" in argv
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "Write" in allowed and "Edit" in allowed, allowed
    assert "mcp__codegraph" in allowed, allowed
    # --permission-mode bypassPermissions must survive the grant insertion;
    # dropping it silently makes the agent read-only.
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"

    config = json.loads((run_dir / ".mcp-config.json").read_text())
    assert "codegraph" in config["mcpServers"]


def test_providers_source_references_all_grant_flags():
    # Contract check preserved from the bash fixture: providers.py must name all
    # three flags so a future edit can't silently delete the injection.
    src = (REPO_ROOT / "mini_ork" / "dispatch" / "providers.py").read_text()
    assert src.count("--allowedTools") >= 1
    assert src.count("--strict-mcp-config") >= 1
    assert src.count("--mcp-config") >= 1
