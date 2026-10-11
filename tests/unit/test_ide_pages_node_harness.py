"""``board node --view harness`` — what config a node was wired to.

The Harness tab resolves a node's declared grants (native tools, MCP servers,
skills, agent doc) against the registries. These tests pin the resolution and,
above all, the safety properties: an unresolvable grant is a rendered state and
never an exception, and a ``${TOKEN}`` reference is never expanded into the
payload.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.cli.mcp_cmd import workflow_mcp_grants
from mini_ork.ide_pages.node import build_node
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-1791000500-harness"
NODE = "implementer"
BARE_NODE = "verifier_node"
T0 = 1_791_000_500

WORKFLOW = """\
version: 1
task_class: demo
nodes:
  - name: implementer
    type: implementer
    model_lane: worker
    tools:
      native: [Read, Write]
      mcp: [codegraph, extra-from-tools]
    mcp_servers: [codegraph, ghost]
    skills: [arxiv-search, definitely-missing-skill]
    agent_doc: prompts/AGENT.md
  - name: verifier_node
    type: verifier
    model_lane: verifier
"""

REGISTRY = {
    "mcpServers": {
        "codegraph": {"command": "npx", "args": ["-y", "@x/codegraph-mcp",
                                                 "--token=${MO_TEST_TOKEN}"]},
        "disabled-srv": {"command": "run", "enabled": False},
        "ext-srv": {"source": "extension", "command": "zed-extension"},
    }
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Isolate the skill/MCP registries: MINI_ORK_ROOT is the engine-shipped
    # registry root, and pointing it at a temp dir keeps a developer's real
    # .agents/skills and mcp/steering.json out of the assertions.
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path / "root"))
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err

    recipe = h / "recipes" / "demo-recipe"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text(WORKFLOW)
    (recipe / "task_class.yaml").write_text("name: demo\ndescription: a demo recipe\n")
    (recipe / "prompts").mkdir()
    (recipe / "prompts" / "AGENT.md").write_text("# Agent guidance\n\nBe careful.\n")

    (h / "config").mkdir()
    (h / "config" / "mcp_servers.json").write_text(json.dumps(REGISTRY))
    (h / "config" / "skills" / "arxiv-search").mkdir(parents=True)

    run_dir = h / "runs" / RUN
    run_dir.mkdir(parents=True)
    kickoff = h / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True)
    kickoff.write_text("# demo\n")
    con = sqlite3.connect(h / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, ended_at, "
        "task_class, kickoff_path, workflow_version, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (RUN, "demo-recipe", "executing", 0.0, T0, T0 + 120, T0 + 100, "demo",
         str(kickoff), "latest", "tr-harness-1"))
    for i, node in enumerate((NODE, BARE_NODE)):
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (f"ev-{RUN}-{i}", RUN, "node_start",
             json.dumps({"node_id": node, "node_type": "x", "model_lane": "worker"}), T0 + 10 + i))
    con.commit()
    con.close()
    return h


def _view(home: Path, node: str = NODE) -> dict:
    out = build_node(home, RUN, node, view="harness")
    assert out["ok"] is True, out.get("error")
    assert out["view"] == "harness"
    return out


def _items(out: dict) -> list[dict]:
    return list(out.get("list") or [])


def _titles(out: dict) -> list[str]:
    return [str(item.get("t") or "") for item in _items(out)]


def _item(out: dict, needle: str) -> dict:
    for item in _items(out):
        if needle in str(item.get("t") or ""):
            return item
    raise AssertionError(f"no list item matching {needle!r} in {_titles(out)}")


def _kv(out: dict, key: str) -> dict:
    for tile in out.get("kv") or []:
        if tile.get("k") == key:
            return tile
    raise AssertionError(f"no kv tile {key!r}")


# ── the view resolves, and never raises ─────────────────────────────────────


def test_harness_view_builds_ok_true(home: Path) -> None:
    out = _view(home)
    # codegraph (both surfaces) + extra-from-tools (tools.mcp) + ghost (envelope)
    assert _kv(out, "MCP servers")["v"] == "3"
    assert _kv(out, "Skills")["v"] == "2"


def test_both_grant_surfaces_union_and_dedup(home: Path) -> None:
    out = _view(home)
    titles = _titles(out)
    assert "mcp · codegraph" in titles          # granted through both surfaces
    assert "mcp · extra-from-tools" in titles   # tools.mcp only
    assert "mcp · ghost" in titles              # mcp_servers only
    assert titles.count("mcp · codegraph") == 1  # deduplicated


def test_node_declaring_nothing_shows_type_default(home: Path) -> None:
    out = _view(home, BARE_NODE)
    native = _kv(out, "Native tools")
    assert native["v"] == "Read, Bash"  # _mo_default_tools_for_type("verifier")
    assert native["c"] == "muted"       # labelled as a default, not as declared
    assert _titles(out) == ["No MCP servers or skills"]


# ── each MCP state is rendered, not raised ──────────────────────────────────


def test_configured_server_shows_transport(home: Path) -> None:
    out = _view(home)
    assert _item(out, "mcp · codegraph")["sub"].startswith("stdio · npx")


def test_unconfigured_server_is_warn_not_error(home: Path) -> None:
    out = _view(home)
    item = _item(out, "mcp · ghost")
    assert "no-op" in item["sub"]
    assert item["m"] == "!"


def test_disabled_server_is_the_fail_state(home: Path) -> None:
    out = build_node(home, RUN, NODE, view="harness")
    # disabled-srv is not granted by the recipe, so grant it directly to prove
    # the state renders rather than raising McpRegistryError out of the view.
    from mini_ork.dispatch.mcp_registry import load_registry
    from mini_ork.ide_pages.node import _harness_env, _mcp_item
    registry = load_registry(_harness_env(home, home / "runs" / RUN))
    item = _mcp_item("disabled-srv", registry)
    assert item["m"] == "✗"
    assert "disabled" in item["sub"]
    assert out["ok"] is True


def test_extension_server_renders_without_raising(home: Path) -> None:
    from mini_ork.dispatch.mcp_registry import load_registry
    from mini_ork.ide_pages.node import _harness_env, _mcp_item
    registry = load_registry(_harness_env(home, home / "runs" / RUN))
    item = _mcp_item("ext-srv", registry)  # normalize_entry raises for it
    assert item["m"] == "✗"
    assert "extension" in item["sub"]


# ── skills ──────────────────────────────────────────────────────────────────


def test_present_skill_resolves_and_missing_skill_reports(home: Path) -> None:
    out = _view(home)
    assert "available" in _item(out, "skill · arxiv-search")["sub"]
    missing = _item(out, "skill · definitely-missing-skill")
    assert "not found" in missing["sub"]
    assert missing["m"] == "!"


# ── registry precedence ─────────────────────────────────────────────────────


def test_run_scoped_registry_wins_over_home(home: Path) -> None:
    run_dir = home / "runs" / RUN
    (run_dir / "config").mkdir()
    (run_dir / "config" / "mcp_servers.json").write_text(json.dumps(
        {"mcpServers": {"ghost": {"command": "run-scoped-ghost"}}}))
    out = _view(home)
    assert "run-scoped-ghost" in _item(out, "mcp · ghost")["sub"]


# ── secret safety ───────────────────────────────────────────────────────────


def test_placeholder_is_never_expanded(home: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "s3cr3t-do-not-render"
    monkeypatch.setenv("MO_TEST_TOKEN", secret)
    payload = json.dumps(_view(home))
    assert "${MO_TEST_TOKEN}" in payload   # the reference survives verbatim
    assert secret not in payload           # and the value never appears


def test_a_resolving_view_cannot_leak(home: Path) -> None:
    """Regression guard: the harness view must never call ``resolve_grant``.

    If a later edit "just resolves it properly", this fails — the expanded
    config would carry the secret and the run-scoped ``.mcp-config.json``.
    """
    import mini_ork.dispatch.mcp_registry as reg

    def _boom(*_a: object, **_k: object) -> dict:
        raise AssertionError("the harness view must not expand grants")

    original = reg.resolve_grant
    reg.resolve_grant = _boom
    try:
        payload = json.dumps(_view(home))
    finally:
        reg.resolve_grant = original
    assert "codegraph" in payload


# ── the CLI surface and the refactored grant reader ─────────────────────────


def test_parser_accepts_the_harness_view() -> None:
    args = board_cmd.build_parser().parse_args(["node", "r", "n", "--view", "harness"])
    assert args.view == "harness"


def test_workflow_mcp_grants_still_reads_both_surfaces(tmp_path: Path) -> None:
    """The ``mcp check`` oracle after ``node_mcp_grants`` was extracted."""
    path = tmp_path / "workflow.yaml"
    path.write_text(
        "nodes:\n"
        "  - {name: a, tools: {mcp: '[x, y]'}}\n"
        "  - {name: b, mcp_servers: [y, z]}\n"
        "  - {name: c, type: verifier}\n")
    assert workflow_mcp_grants(path) == [("a", ("x", "y")), ("b", ("y", "z"))]
