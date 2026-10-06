"""``board page orch`` — the Orchestrator & intake page."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from mini_ork.ide_pages import orch, spec
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
_ACTION_KEYS = {"cli", "page", "set", "run", "path", "reveal", "url", "thread"}


def check_page(page: dict[str, Any], key: str) -> None:
    """Every page obeys the contract the IDE renders."""
    assert page["ok"] is True and page["key"] == key
    json.dumps(page)
    colours = set(spec.COLOURS)

    def colour_ok(c: str) -> bool:
        return c in colours or c.startswith("fam:")

    def action_ok(do: Any) -> bool:
        return do is None or (isinstance(do, dict) and bool(_ACTION_KEYS & set(do)))

    for b in page["actions"]:
        assert action_ok(b["do"]) and b["kind"] in spec.KINDS
    for s in page["sections"]:
        for b in s["actions"]:
            assert action_ok(b["do"])
        for r in s.get("rows") or []:
            assert action_ok(r.get("do"))
            assert all(colour_ok(c["c"]) for c in r["cells"])
        for it in s.get("items") or []:
            for b in it.get("acts") or []:
                assert action_ok(b["do"])
            for field in ("c", "mc"):
                if field in it:
                    assert colour_ok(it[field])


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _run(home: Path, run_id: str, status: str = "published", *, kickoff: str = "",
         created: int | None = None, cost: float = 0.25) -> Path:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = ""
    if kickoff:
        (run_dir / "kickoff.md").write_text(kickoff)
        path = str(run_dir / "kickoff.md")
    now = created or int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute("INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, task_class, "
                "kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, "code-fix", status, cost, now, now, "code_fix", path, "latest"))
    con.commit()
    con.close()
    return run_dir


def test_every_tab_builds(home: Path, monkeypatch) -> None:
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: False)
    for key, _label in orch.TABS:
        page = orch.build(home, key, {})
        check_page(page, "orch")
        assert page["tab"] == key and page["sections"]
        assert [t["key"] for t in page["tabs"]] == ["session", "kickoffs", "races", "planner", "coord"]
        assert page["errors"] == {}, page["errors"]
    assert orch.build(home, "nonsense", {})["tab"] == "session"


def test_thread_defaults_follow_the_env(home: Path, monkeypatch) -> None:
    monkeypatch.setenv("MO_ACP_DEFAULT_MODE", "direct")
    monkeypatch.setenv("MO_ACP_DEFAULT_MODEL", "sonnet")
    monkeypatch.setenv("MO_WORKSPACE_MODE", "in-place")
    monkeypatch.delenv("MO_ACP_RECIPE", raising=False)
    page = orch.build(home, "session", {})
    kv = {i["k"]: i for i in page["sections"][0]["items"]}
    assert kv["Mode"]["v"] == "Direct run"
    assert kv["Orchestrator lane"]["v"] == "sonnet" and kv["Orchestrator lane"]["sub"] == "MO_ACP_DEFAULT_MODEL"
    assert kv["Recipe"]["v"] == "code-fix"
    assert kv["Workspace"]["v"] == "In place (this checkout)"


def test_mcp_pills_are_the_servers_tools(home: Path) -> None:
    from mini_ork.mcp_context import server

    pills = orch.build(home, "session", {})["sections"][1]
    names = [p["t"] for p in pills["items"]]
    assert names == [t["name"] for t in server.TOOL_DEFS] + [t["name"] for t in server._CONTROL_TOOL_DEFS]
    assert {p["c"] for p in pills["items"][len(server.TOOL_DEFS):]} == {"blue"}
    assert pills["title"].endswith(f"{len(names)} tools")


def test_history_has_threads_and_runs(home: Path) -> None:
    from mini_ork.acp.threads import ThreadStore

    _run(home, "run-1791000000-aaaaaa", kickoff="# Fix the login loop\n")
    store = ThreadStore(home)
    store.append("orch-1791000000-t1", {"type": "meta", "cwd": str(home.parent)})
    store.append("orch-1791000000-t1", {"type": "user", "text": "Add dark mode"})
    store.append("orch-1791000000-t1", {"type": "costs", "costs": {"turn": 0.25, "run": 0.5}})
    table = orch.build(home, "session", {})["sections"][2]
    rows = {r["cells"][0]["t"]: r for r in table["rows"]}
    assert rows["Add dark mode"]["cells"][1]["t"] == "thread"
    assert rows["Add dark mode"]["cells"][2]["t"] == "$0.75"
    assert rows["Fix the login loop"]["do"] == {"run": "run-1791000000-aaaaaa", "title": "Fix the login loop"}


def test_kickoffs_are_linted_and_link_their_runs(home: Path) -> None:
    folder = home / "kickoffs"
    folder.mkdir()
    good = folder / "good.md"
    good.write_text("Add a limit to uploads, no title and no success section.\n")
    (folder / "empty.md").write_text("   \n")
    _run(home, "run-1791000001-bbbbbb", "published")
    con = sqlite3.connect(home / "state.db")
    con.execute("UPDATE task_runs SET kickoff_path = ? WHERE id = ?", (str(good), "run-1791000001-bbbbbb"))
    con.commit()
    con.close()
    section = orch.build(home, "kickoffs", {})["sections"][0]
    items = {i["t"]: i for i in section["items"]}
    assert "lint: The kickoff has no '# ' title line. (+" in items["good.md"]["sub"]
    assert items["good.md"]["mc"] == "yellow" and "code-fix (default)" in items["good.md"]["sub"]
    assert "ran as run-1791000001-bbbbbb · published" in items["good.md"]["sub"]
    labels = [a["label"] for a in items["good.md"]["acts"]]
    assert labels == ["Open", "Open run", "Start run"]
    assert items["good.md"]["acts"][0]["do"] == {"path": str(good)}
    assert items["empty.md"]["mc"] == "red"


def test_no_kickoffs_says_so(home: Path) -> None:
    section = orch.build(home, "kickoffs", {})["sections"][0]
    assert section["items"][0]["t"] == "No saved kickoffs"


def test_races_group_contestants(home: Path) -> None:
    now = int(time.time())
    for i, (lane, status) in enumerate((("sonnet", "published"), ("glm", "failed"))):
        run_dir = _run(home, f"run-179100000{i}-race{i}", status, kickoff="# Debounce search\n",
                       created=now - 60 + i)
        (run_dir / "config").mkdir()
        (run_dir / "config" / "agents.race.yaml").write_text(f"lanes:\n  implementer: {lane}\n")
    _run(home, "run-1791000009-solo99", kickoff="# Not a race\n")
    rows = orch.build(home, "races", {})["sections"][0]["rows"]
    assert len(rows) == 1
    cells = [c["t"] for c in rows[0]["cells"]]
    assert cells[0] == "Debounce search"
    assert set(cells[1].split(" · ")) == {"sonnet", "glm"}
    assert cells[2] == "1 / 2"


def test_planner_reads_the_latest_profile_and_plan(home: Path) -> None:
    run_dir = _run(home, "run-1791000002-cccccc")
    (run_dir / "run_profile.json").write_text(json.dumps({
        "task_class": "ui_change", "recipe": "code-fix", "risk_tolerance": "low", "confidence": 0.91,
        "profile_status": "needs_answers", "human_questions": ["Apply to API keys too?"]}))
    (run_dir / "plan.json").write_text(json.dumps({"objective": "Add a toggle", "decomposition": [
        {"step": 1, "node_type": "implementer", "action": "Edit tokens.ts. Then more.", "target_file": "tokens.ts"}]}))
    sections = orch.build(home, "planner", {})["sections"]
    kv = {i["k"]: i["v"] for i in sections[0]["items"]}
    assert kv["task_class"] == "ui_change" and kv["confidence"] == "0.91"
    assert sections[1]["items"][0]["t"] == "1 · Edit tokens.ts"
    assert sections[2]["items"][0]["t"] == "Apply to API keys too?"


def test_coordination_without_contextnest(home: Path, monkeypatch) -> None:
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: False)
    con = sqlite3.connect(home / "state.db")
    now = int(time.time())
    con.execute("INSERT INTO task_runs (id, recipe, status, created_at, updated_at, task_class, kickoff_path, "
                "workflow_version) VALUES (?,?,?,?,?,?,?,?)",
                ("root-run", "goal-loop", "executing", now, now, "goal", "", "latest"))
    con.execute("INSERT INTO run_spawns (spawn_id, parent_run_id, child_run_id, root_run_id, depth, recipe, "
                "kickoff_path, child_workspace, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("sp-1", "root-run", "child-1", "root-run", 1, "code-fix", "k.md", "ws", "completed", now, now))
    con.commit()
    con.close()
    sections = orch.build(home, "coord", {})["sections"]
    assert sections[0]["items"][0]["t"] == "ContextNest is not reachable"
    tree = [line["t"] for line in sections[-1]["lines"]]
    assert tree[0] == "root-run  goal-loop" and "child-1" in tree[1] and "✓" in tree[1]


def test_coordination_with_contextnest(home: Path, monkeypatch) -> None:
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)
    monkeypatch.setattr("mini_ork.cn_client.coord_list_principals", lambda status="active": {
        "principals": [{"principal_id": "run:abc", "pids": [42], "status": "active", "unacked_messages": 1}]})
    monkeypatch.setattr("mini_ork.cn_client.coord_hot_claims", lambda: {
        "claims": [{"path": "src/a.py", "principal_id": "run:abc", "expires_at": "soon"}]})

    def broken(since: int = 0) -> dict:
        raise RuntimeError("down")

    monkeypatch.setattr("mini_ork.cn_client.coord_owns_violations", broken)
    page = orch.build(home, "coord", {})
    titles = [s["title"] for s in page["sections"]]
    assert titles[:3] == ["Principals · concord", "Claims on hot files", "Scope violations (--owns)"]
    assert [c["t"] for c in page["sections"][0]["rows"][0]["cells"]] == ["run:abc", "42", "active", "1"]
    assert page["sections"][2]["items"][0]["m"] == "✗"  # the broken source costs one section
