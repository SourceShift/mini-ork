"""Smoke test for the observability HTTP surface.

Exercises the route handlers directly (no httpx dep) to assert each endpoint
returns sensible shapes when pointed at the repo's own .mini-ork/state.db.
"""

from __future__ import annotations

import sqlite3
import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def home() -> Path:
    h = ROOT / ".mini-ork"
    if not (h / "state.db").exists():
        pytest.skip(f"no state.db at {h}; run `mini-ork init` first")
    return h


@pytest.fixture(scope="module")
def db(home: Path):
    from mini_ork.web.deps import set_home_override, get_db

    set_home_override(home)
    return get_db()


@pytest.fixture()
def seeded_db(tmp_path: Path):
    """Hermetic migrated state.db with one finished code-fix run.

    The module `home` fixture points at the checkout's own .mini-ork/state.db,
    which works only where that db has real history — a fresh worktree's init
    leaves a 0-byte state.db (no tables), and tests that assert on table
    presence fail there. This fixture builds the FULL migrated schema via the
    real migration runner, then seeds exactly what those assertions need:
    a task_runs row (recipe=code-fix) plus planner node_start/node_end events.
    """
    from mini_ork.stores.migrate import init_db
    from mini_ork.web.db import StateDB

    h = tmp_path / ".mini-ork"
    h.mkdir()
    dbp = h / "state.db"
    rc, out, err = init_db(db=str(dbp), root=str(ROOT))
    assert rc == 0, f"init_db failed rc={rc}\nstdout={out}\nstderr={err}"

    con = sqlite3.connect(dbp)
    con.execute(
        """
        INSERT INTO task_runs (id, task_class, recipe, status, verdict,
                               kickoff_path, cost_usd, created_at, updated_at, ended_at)
        VALUES ('run-hermetic-1', 'code_fix', 'code-fix', 'published', 'APPROVE',
                '/tmp/kickoff.md', 0.01, 1700000000, 1700000000, 1700000000)
        """
    )
    con.executemany(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            ("ev-1", "run-hermetic-1", "node_start",
             '{"node_id": "planner"}', 1700000001),
            ("ev-2", "run-hermetic-1", "node_end",
             '{"node_id": "planner", "verdict": "APPROVE", "duration_ms": 1200,'
             ' "artifact_path": "/tmp/plan.json"}', 1700000002),
        ],
    )
    con.commit()
    con.close()
    return StateDB(dbp)


def test_health(seeded_db) -> None:
    """Hermetic: asserts the migrated schema is present, not that this
    checkout happens to have run real work (a 0-byte fresh-worktree db
    made the old env-dependent version fail)."""
    from mini_ork.web.routes.fleet import health

    out = health(seeded_db)
    assert out["ok"] is True
    assert out["has_task_runs"] is True


def test_recovery_ui_route_returns_projection_shape(db) -> None:
    # E5: the recovery projection endpoint wires + always returns a renderable
    # dict (empty nodes for an unknown run), reading the E1–E3 tables.
    from mini_ork.web.routes.recovery import recovery_view

    out = recovery_view("no-such-run-e5", db)
    assert set(out.keys()) >= {"run_id", "nodes", "active_recovery", "lease", "next_action"}
    assert out["run_id"] == "no-such-run-e5"
    assert isinstance(out["nodes"], list)


def test_task_runs_summary(db) -> None:
    from mini_ork.web.routes.fleet import task_runs_summary

    out = task_runs_summary(db)
    assert "by_recipe" in out
    assert "by_status" in out
    assert "total_cost_usd" in out


def test_task_runs_list(db) -> None:
    from mini_ork.web.routes.fleet import list_task_runs

    rows = list_task_runs(db, limit=5)
    assert isinstance(rows, list)
    if rows:
        assert "id" in rows[0]
        assert "recipe" in rows[0]


def test_active_runs(db) -> None:
    from mini_ork.web.routes.fleet import active_runs

    rows = active_runs(db)
    assert isinstance(rows, list)


def test_active_runs_includes_unfinished_task_runs(tmp_path: Path) -> None:
    """Universal task-loop runs are active even without legacy heartbeat rows."""
    import sqlite3
    import time

    from mini_ork.web.db import StateDB
    from mini_ork.web.routes.fleet import active_runs

    db_path = tmp_path / "state.db"
    now = int(time.time())
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY,
          parent_epic_id TEXT,
          task_class TEXT NOT NULL,
          recipe TEXT,
          status TEXT NOT NULL,
          verdict TEXT,
          cost_usd REAL NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          ended_at INTEGER
        )
        """
    )
    con.execute(
        """
        INSERT INTO task_runs (
          id, parent_epic_id, task_class, recipe, status, verdict,
          cost_usd, created_at, updated_at, ended_at
        )
        VALUES ('run-live', NULL, 'code_fix', 'code-fix', 'executing', NULL, 0.25, ?, ?, NULL)
        """,
        (now - 5, now),
    )
    con.execute(
        """
        INSERT INTO task_runs (
          id, parent_epic_id, task_class, recipe, status, verdict,
          cost_usd, created_at, updated_at, ended_at
        )
        VALUES ('run-classified', NULL, 'code_fix', 'code-fix', 'classified', NULL, 0.25, ?, ?, NULL)
        """,
        (now - 5, now),
    )
    con.execute(
        """
        INSERT INTO task_runs (
          id, parent_epic_id, task_class, recipe, status, verdict,
          cost_usd, created_at, updated_at, ended_at
        )
        VALUES ('run-stale', NULL, 'code_fix', 'code-fix', 'executing', NULL, 0.25, 10, 20, NULL)
        """
    )
    con.execute(
        """
        INSERT INTO task_runs (
          id, parent_epic_id, task_class, recipe, status, verdict,
          cost_usd, created_at, updated_at, ended_at
        )
        VALUES ('run-done', NULL, 'code_fix', 'code-fix', 'published', 'APPROVE', 0.50, 1, 2, 3)
        """
    )
    con.commit()
    con.close()

    rows = active_runs(StateDB(db_path))

    assert [r["id"] for r in rows] == ["run-live"]
    assert rows[0]["source"] == "task_runs"
    assert rows[0]["task_run_id"] == "run-live"
    assert rows[0]["test_status"] == "executing"


def test_self_improve(db) -> None:
    from mini_ork.web.routes.trajectory import self_improve_runs

    rows = self_improve_runs(db, limit=3)
    assert isinstance(rows, list)


def test_cost_by_day(db) -> None:
    from mini_ork.web.routes.trajectory import cost_by_day

    rows = cost_by_day(db)
    assert isinstance(rows, list)


def test_fingerprint_recursive_self_improve() -> None:
    from mini_ork.web.routes.fingerprint import fingerprint

    out = fingerprint(recipe="recursive-self-improve", home=None)
    assert out["recipe"] == "recursive-self-improve"
    assert out["nodes"], "recipe should have nodes"
    # The framework's load-bearing claim: this recipe must be heterogeneous.
    assert out["coalition"] in ("heterogeneous", "low"), (
        f"recursive-self-improve regressed to {out['coalition']} "
        f"(families: {out['families_used']})"
    )


def test_app_factory_boots(home: Path) -> None:
    from mini_ork.web.app import create_app

    app = create_app(home=home, dev_cors=False)

    # Use the OpenAPI schema as the source of truth for registered
    # paths. FastAPI 0.10x flattens app.include_router() routes inline
    # so each one has .path; FastAPI 0.111+ wraps them as Mount(s)
    # whose sub-routes live under mount.app.routes, with the mount
    # prefix only applied at OpenAPI emission time. Iterating
    # app.routes directly misses the prefix-concat step on the new
    # FastAPI; reading openapi().paths handles both shapes uniformly.
    paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/v1/health" in paths
    assert "/api/v1/task-runs" in paths
    assert "/api/v1/fingerprint" in paths
    # Idea tree endpoints (plan: docs/plans/2026-06-11-arbor-techniques-into-mini-ork.md item 1)
    assert "/api/v1/idea-tree/roots" in paths
    assert "/api/v1/idea-tree/{root_node_id}" in paths
    # OpenHands agent-server protocol shim (SE-3 UI fork). These exact paths
    # must survive the SPA catch-all or the forked agent-canvas can't onboard.
    assert "/server_info" in paths
    assert "/api/settings" in paths
    # Onboarding write-path: schemas + profile list must be JSON routes, not
    # swallowed by the index.html catch-all (which would 200-with-HTML).
    assert "/api/settings/agent-schema" in paths
    assert "/api/settings/conversation-schema" in paths
    assert "/api/agent-profiles" in paths
    # PATCH must be a registered method on /api/settings, or onboarding's
    # saveSettings hits 405 Method Not Allowed and the flow stalls.
    settings_methods = set(app.openapi()["paths"]["/api/settings"].keys())
    assert {"get", "patch"} <= settings_methods, (
        f"/api/settings is missing methods {{'get','patch'}} - PATCH regression "
        f"re-breaks onboarding; have {settings_methods}"
    )


# ── OpenHands agent-server protocol shim (SE-3 UI fork) ─────────────────────


def _semver_tuple(version: str) -> tuple[int, int, int]:
    """Parse a strict major.minor.patch triple, mirroring the frontend's
    parseAgentServerVersion (ui/src/api/agent-server-compatibility.ts): the
    canvas rejects anything that is not exactly three integer parts."""
    core = version.strip().lstrip("v").split("+", 1)[0].split("-", 1)[0]
    parts = core.split(".")
    assert len(parts) == 3, f"version {version!r} is not major.minor.patch"
    major, minor, patch = (int(p) for p in parts)
    return major, minor, patch


def test_agent_server_info_clears_frontend_compatibility_floor() -> None:
    """The version we advertise must parse as a 3-part semver AND be >= the
    canvas's minimumAgentServer floor. This is the load-bearing onboarding
    contract: if the version regresses below the floor (or stops being a
    clean triple), the forked agent-canvas throws
    AgentServerUnsupportedVersionError / AgentServerUnknownVersionError and the
    'add a backend' wall never drops. The floor is read from the same file the
    UI reads (ui/config/defaults.json) so this test tracks the real contract."""
    from mini_ork.web.routes.agent_server import server_info

    defaults = json.loads((ROOT / "ui" / "config" / "defaults.json").read_text())
    floor = _semver_tuple(defaults["compatibility"]["minimumAgentServer"])

    out = server_info()
    reported = _semver_tuple(out["version"])
    assert reported >= floor, (
        f"advertised agent-server version {out['version']} is below the canvas "
        f"floor {defaults['compatibility']['minimumAgentServer']} — onboarding will break"
    )
    # ServerInfo requires uptime + idle_time (ui typescript-client base.d.ts).
    assert isinstance(out["uptime"], (int, float))
    assert isinstance(out["idle_time"], (int, float))


def test_agent_server_settings_probe_shape() -> None:
    """`SettingsClient.getSettings` (GET /api/settings) is the first probe call;
    it must return a 200 SettingsApiResponse-shaped body or the probe never
    reaches the version check."""
    from mini_ork.web.routes.agent_server import get_settings

    out = get_settings()
    assert {"agent_settings", "conversation_settings", "llm_api_key_is_set"} <= set(out)
    assert isinstance(out["agent_settings"], dict)
    assert isinstance(out["conversation_settings"], dict)


def test_agent_server_update_settings_deep_merges_diff() -> None:
    """`SettingsClient.updateSettings` (PATCH /api/settings) is onboarding's
    write step. It sends only the fields the user changed under ``*_diff`` keys;
    the reported field bug was a missing PATCH handler → 405 Method Not Allowed,
    which stalled onboarding. This pins the fix: the handler exists, deep-merges
    the diff into the store, and returns a SettingsApiResponse reflecting it.

    The store is a module global, so snapshot + restore to keep the test
    hermetic and order-independent."""
    import copy

    from mini_ork.web.routes import agent_server as mod

    saved = copy.deepcopy(mod._SETTINGS)
    try:
        # Exactly the payload from the reported curl reproduction.
        out = mod.update_settings(
            {"misc_settings_diff": {"app_preferences": {"user_consents_to_analytics": False}}}
        )
        assert out["misc_settings"]["app_preferences"]["user_consents_to_analytics"] is False
        # A second, disjoint diff must not clobber the first (deep, not shallow).
        out2 = mod.update_settings(
            {"misc_settings_diff": {"app_preferences": {"language": "en"}}}
        )
        prefs = out2["misc_settings"]["app_preferences"]
        assert prefs["language"] == "en"
        assert prefs["user_consents_to_analytics"] is False
        # Still a valid SettingsApiResponse shape.
        assert {"agent_settings", "conversation_settings", "llm_api_key_is_set"} <= set(out2)
    finally:
        mod._SETTINGS.clear()
        mod._SETTINGS.update(saved)


def test_agent_server_form_schemas_and_profiles_are_json() -> None:
    """The remaining onboarding reads must resolve to JSON, not the SPA
    index.html catch-all (HTML-with-200 = false success the SDK can't parse).
    Schemas are valid empty JSON Schemas; the profile list is a JSON array."""
    from mini_ork.web.routes.agent_server import (
        agent_schema,
        conversation_schema,
        list_agent_profiles,
    )

    for schema in (agent_schema(), conversation_schema()):
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
    assert isinstance(list_agent_profiles(), list)


def test_profiles_are_the_real_lanes_and_gate_on_credential_presence(
    tmp_path, monkeypatch
) -> None:
    """`/api/profiles` lists mini-ork's provider registry, not a placeholder.

    The composer's enabled state hinges on this: `useLlmConfigured` (local mode)
    finds the `active_profile` row and reads its `api_key_set`, so an empty list
    disables the chat input and the canvas goes inert. Pinned to a temp registry
    so the assertion is about the projection, not about whatever lanes the
    developer running the suite happens to have keys for."""
    from fastapi.testclient import TestClient

    from mini_ork.web.app import create_app

    registry = tmp_path / "providers.yaml"
    registry.write_text(
        "providers:\n"
        "  funded:\n"
        "    kind: anthropic-compat\n"
        "    model: test-model\n"
        "    base_url: https://example.invalid/anthropic\n"
        "    api_key_env: MO_TEST_LANE_KEY\n"
        "  broke:\n"
        "    kind: anthropic-compat\n"
        "    model: other-model\n"
        "    api_key_env: MO_TEST_LANE_KEY_UNSET\n"
    )
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(registry))
    monkeypatch.setenv("MO_TEST_LANE_KEY", "present")
    monkeypatch.delenv("MO_TEST_LANE_KEY_UNSET", raising=False)

    with TestClient(create_app(dev_cors=False)) as client:
        body = client.get("/api/profiles").json()

    assert [p["name"] for p in body["profiles"]] == ["funded", "broke"]
    # A lane with no credential is still listed, flagged false — dropping it
    # would hide a lane the operator may be about to fix.
    assert [p["api_key_set"] for p in body["profiles"]] == [True, False]
    assert body["profiles"][0]["model"] == "test-model"
    # The active row must be a *usable* one; naming an unusable lane here is
    # exactly how the composer gets disabled with profiles present.
    assert body["active_profile"] == "funded"


def test_active_profile_follows_the_lane_policy_over_registry_order(
    tmp_path, monkeypatch
) -> None:
    """The named lane must be one the policy actually dispatches.

    Registry order is alphabetical, so "first healthy lane" lands on whatever
    ambient lane sorts first — a lane the operator may have taken out of scope.
    The canvas renders this name to the user as *the* LLM, so describing the
    deployment matters. Unit-level because the fallback chain (policy -> mode ->
    None) has three branches and no HTTP surface is involved."""
    from mini_ork.web.routes.agent_server import _policy_lane

    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agents.yaml").write_text(
        "lanes:\n"
        "  implementer: minimax\n"
        "  worker: minimax\n"
        "  planner: deepseek\n"
        "  codex_lens: dead_lane\n"
    )
    # clear the run-dir branch so the home config is what resolves
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))

    # The mode wins over first-in-list, and a policy lane with no credential is
    # not eligible — otherwise the active row could be unusable and re-disable
    # the composer that this list exists to enable.
    assert _policy_lane(["deepseek", "minimax"]) == "minimax"
    assert _policy_lane(["deepseek"]) == "deepseek"
    assert _policy_lane([]) is None

    (home / "config" / "agents.yaml").write_text("lanes: {}\n")
    assert _policy_lane(["deepseek", "minimax"]) is None


def test_agent_server_serves_the_paths_the_canvas_polls() -> None:
    """Profiles, workspaces and telemetry consent all resolve — not 404/405.

    Each of these is called on ordinary navigation, so an unanswered one is not
    a one-off failure the user sees once; it is a 404 in a poll loop, and the
    consent POST specifically made onboarding's modal re-appear on every single
    page load. Asserted through the real client so the test exercises routing
    (the SPA catch-all serves HTML-with-200 for anything unrouted, which a direct
    function call would not catch)."""
    from fastapi.testclient import TestClient

    from mini_ork.web.app import create_app

    with TestClient(create_app(dev_cors=False)) as client:
        # Envelopes, not bare arrays: the callers dereference `.profiles` /
        # `.workspaces`, so `[]` would crash the page instead of emptying it.
        assert client.get("/api/workspaces").json() == {
            "workspaces": [],
            "workspaceParents": [],
        }

        granted = client.post(
            "/api/automation/v1/telemetry/consent",
            json={"consent_granted": True, "frontend_distinct_id": "ph-fe"},
        )
        assert granted.status_code == 200
        assert granted.json() == {"consent_granted": True}

        # The flag is echoed, not hardcoded: a user who declines must read back
        # as declined, or the canvas would keep re-asking someone who said no.
        denied = client.post(
            "/api/automation/v1/telemetry/consent",
            json={"consent_granted": False, "frontend_distinct_id": "ph-fe"},
        )
        assert denied.json() == {"consent_granted": False}


def _fake_launch_run_factory(calls: list[dict]):
    """Stand-in for control.launch_run that records calls and always succeeds.

    Mirrors the real return shape (web/control.py): {ok, run_id, recipe, pid,
    kickoff_path, log_path} — notably NO run_dir, which the route must not
    pretend exists.
    """

    def fake(home, recipe, kickoff, run_id=None):
        calls.append(
            {"home": home, "recipe": recipe, "kickoff": kickoff, "run_id": run_id}
        )
        return {
            "ok": True,
            "run_id": run_id,
            "recipe": recipe,
            "pid": 4242,
            "kickoff_path": str(Path(home) / "runs-inbox" / f"{run_id}.md"),
            "log_path": str(Path(home) / "logs" / f"{run_id}.log"),
        }

    return fake


def test_agent_server_create_conversation_launches_run(tmp_path, monkeypatch) -> None:
    """Slice-2 keystone: POST /api/conversations with an initial_message must
    spawn a mini-ork run under the CLIENT-chosen conversation id (the canvas
    mints a uuidv4 and routes on it — reusing it as the run id is what makes
    deterministic attach possible) and report execution_status "running"."""
    from mini_ork.web.routes import agent_server as mod
    import mini_ork.web.control as control

    calls: list[dict] = []
    monkeypatch.setattr(control, "launch_run", _fake_launch_run_factory(calls))

    out = mod.create_conversation(
        {
            "conversation_id": "conv-1234-abcd",
            "initial_message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Fix the flaky login test"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            },
        },
        home=tmp_path,
    )

    assert out["id"] == "conv-1234-abcd"
    assert out["execution_status"] == "running"
    # Title falls back to the first line of the initial message.
    assert out["title"] == "Fix the flaky login test"
    # Required-by-type ConversationInfo fields are filled honestly.
    assert out["agent"]["llm"]["model"]
    assert out["confirmation_policy"] == {"type": "never"}

    assert len(calls) == 1
    assert calls[0]["run_id"] == "conv-1234-abcd"
    assert "Fix the flaky login test" in calls[0]["kickoff"]
    # Image content parts are dropped, not stringified into the kickoff.
    assert "data:" not in calls[0]["kickoff"]
    # persistence_dir carries the launcher-visible run handle (the log path).
    assert out["persistence_dir"].endswith("conv-1234-abcd.log")


def test_agent_server_create_conversation_recipe_is_server_owned(
    tmp_path, monkeypatch
) -> None:
    """The recipe a conversation runs is server policy (MO_AGENT_SERVER_RECIPE,
    default code-fix) — the canvas's agent_settings/llm_model are cosmetic."""
    from mini_ork.web.routes import agent_server as mod
    import mini_ork.web.control as control

    calls: list[dict] = []
    monkeypatch.setattr(control, "launch_run", _fake_launch_run_factory(calls))
    monkeypatch.setenv(mod.CONVERSATION_RECIPE_ENV, "framework-edit")

    out = mod.create_conversation(
        {
            "conversation_id": "conv-recipe-1",
            "initial_message": "do a thing",
            # Client-side LLM picks must NOT leak into the launch.
            "agent_settings": {"llm_model": "gpt-9-max"},
        },
        home=tmp_path,
    )
    assert out["execution_status"] == "running"
    assert calls[0]["recipe"] == "framework-edit"

    monkeypatch.delenv(mod.CONVERSATION_RECIPE_ENV)
    calls.clear()
    mod.create_conversation(
        {"conversation_id": "conv-recipe-2", "initial_message": "do another"},
        home=tmp_path,
    )
    assert calls[0]["recipe"] == mod.DEFAULT_CONVERSATION_RECIPE


def test_agent_server_create_conversation_without_message_stays_idle(
    tmp_path, monkeypatch
) -> None:
    """No initial_message → conversation registered idle, no run spawned (the
    run starts when the first chat message arrives — sendMessage wiring is a
    later slice). Sidecar persists so GET rehydrates the same ConversationInfo."""
    from mini_ork.web.routes import agent_server as mod
    import mini_ork.web.control as control

    calls: list[dict] = []
    monkeypatch.setattr(control, "launch_run", _fake_launch_run_factory(calls))

    out = mod.create_conversation(
        {"conversation_id": "conv-idle-1", "title": "Scratchpad"}, home=tmp_path
    )
    assert out["execution_status"] == "idle"
    assert calls == []

    # Explicit title is honored rather than derived.
    assert out["title"] == "Scratchpad"
    # Round-trip through the registry sidecar.
    again = mod.get_conversation("conv-idle-1", home=tmp_path)
    assert again["id"] == "conv-idle-1"
    assert again["execution_status"] == "idle"
    assert again["title"] == "Scratchpad"


def test_agent_server_create_conversation_rejects_unsafe_ids(tmp_path) -> None:
    """conversation_id becomes a filename under <home>/conversations/ — path
    traversal and exotic characters must 400, not escape the registry dir."""
    from fastapi import HTTPException
    from mini_ork.web.routes import agent_server as mod

    for bad in ("../escape", "a/b", "has space", "semi;colon"):
        with pytest.raises(HTTPException) as exc:
            mod.create_conversation({"conversation_id": bad}, home=tmp_path)
        assert exc.value.status_code == 400


def test_agent_server_create_conversation_mints_uuid_when_absent(tmp_path) -> None:
    """conversation_id is optional in the payload — the canvas always sends
    one, but a bare create must still round-trip on a server-minted uuid."""
    from mini_ork.web.routes import agent_server as mod

    out = mod.create_conversation({"title": "No id"}, home=tmp_path)
    assert out["id"]
    assert mod._safe_conversation_id(out["id"])
    again = mod.get_conversation(out["id"], home=tmp_path)
    assert again["id"] == out["id"]


def test_agent_server_get_conversation_unknown_404(tmp_path) -> None:
    from fastapi import HTTPException
    from mini_ork.web.routes import agent_server as mod

    with pytest.raises(HTTPException) as exc:
        mod.get_conversation("never-created-xyz", home=tmp_path)
    assert exc.value.status_code == 404


def test_agent_server_create_conversation_launch_failure_500s(
    tmp_path, monkeypatch
) -> None:
    """A failed spawn must 500 with the launcher's error surfaced, and must
    NOT register the sidecar — a conversation whose run never started should
    not haunt the canvas list as a phantom."""
    from fastapi import HTTPException
    from mini_ork.web.routes import agent_server as mod
    import mini_ork.web.control as control

    def failing(home, recipe, kickoff, run_id=None):
        return {"ok": False, "error": "no such recipe"}

    monkeypatch.setattr(control, "launch_run", failing)

    with pytest.raises(HTTPException) as exc:
        mod.create_conversation(
            {"conversation_id": "conv-dead-1", "initial_message": "go"}, home=tmp_path
        )
    assert exc.value.status_code == 500
    assert "no such recipe" in exc.value.detail

    import json as _json

    assert not (tmp_path / "conversations" / "conv-dead-1.json").exists()
    assert not (tmp_path / "conversations").exists() or not any(
        _json.loads(p.read_text()).get("id") == "conv-dead-1"
        for p in (tmp_path / "conversations").glob("*.json")
    )


# ── Conversation events + sendMessage (Slice 3) ───────────────────────────────


def _seed_state_db(
    tmp_path: Path,
    run_id: str,
    *,
    status: str = "running",
    verdict: str | None = None,
) -> int:
    """Create a minimal <home>/state.db with run_events + task_runs for run_id.

    Raw DDL (not the migration runner): the events projection only reads
    these two tables, and a hand-seeded schema keeps the test independent of
    migration drift. Returns the base epoch used for seeded timestamps.
    """
    import json as _json
    import sqlite3
    import time as _time

    base = int(_time.time()) - 100
    con = sqlite3.connect(tmp_path / "state.db")
    con.execute(
        """CREATE TABLE run_events (
             event_id TEXT, run_id TEXT, event_type TEXT,
             payload_json TEXT, created_at INTEGER)"""
    )
    con.execute(
        """CREATE TABLE task_runs (
             id TEXT PRIMARY KEY, status TEXT, verdict TEXT,
             created_at INTEGER, ended_at INTEGER)"""
    )
    con.execute(
        "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?)",
        (run_id, status, verdict, base, base + 90),
    )
    rows = [
        ("evt-start-planner", "node_start", {"node_id": "planner", "node_type": "plan"}, base + 10),
        (
            "evt-end-planner",
            "node_end",
            {"node_id": "planner", "node_type": "plan", "finish_reason": "ok"},
            base + 20,
        ),
        (
            "evt-end-implementer",
            "node_end",
            {"node_id": "implementer", "node_type": "code", "finish_reason": "ok"},
            base + 30,
        ),
    ]
    for event_id, event_type, payload, ts in rows:
        con.execute(
            "INSERT INTO run_events VALUES (?, ?, ?, ?, ?)",
            (event_id, run_id, event_type, _json.dumps(payload), ts),
        )
    con.commit()
    con.close()
    return base


def test_agent_server_events_search_projects_run_events(tmp_path, monkeypatch) -> None:
    """The transcript projection: sidecar user message + run_events rows →
    oh_events. DESC default page (the canvas's initial load), structural
    shapes the canvas type-guards on (BaseEvent fields, llm_message on
    message events), and count matches."""
    from mini_ork.web.routes import agent_server as mod
    import mini_ork.web.control as control

    calls: list[dict] = []
    monkeypatch.setattr(control, "launch_run", _fake_launch_run_factory(calls))
    mod.create_conversation(
        {
            "conversation_id": "conv-evt-1",
            "initial_message": "Fix the failing test",
        },
        home=tmp_path,
    )
    _seed_state_db(tmp_path, "conv-evt-1")

    page = mod.search_conversation_events("conv-evt-1", home=tmp_path)
    items = page["items"]
    assert page["next_page_id"] is None  # 4 events < limit

    # DESC: newest first (user message was created "now", run events seeded
    # up to base+30 — the user message is newest).
    assert [e["id"] for e in items] == [
        "conv-evt-1-user-0",
        "evt-end-implementer",
        "evt-end-planner",
        "evt-start-planner",
    ]

    by_id = {e["id"]: e for e in items}
    # User message projects as a MessageEvent the transcript renders.
    user = by_id["conv-evt-1-user-0"]
    assert user["source"] == "user"
    assert user["llm_message"]["role"] == "user"
    assert user["llm_message"]["content"] == [
        {"type": "text", "text": "Fix the failing test"}
    ]
    assert user["activated_microagents"] == []
    # node_end projects as an assistant message with a human-readable summary.
    end = by_id["evt-end-planner"]
    assert end["source"] == "agent"
    assert end["llm_message"]["role"] == "assistant"
    assert "planner" in end["llm_message"]["content"][0]["text"]
    assert "ok" in end["llm_message"]["content"][0]["text"]
    # node_start projects as an environment lifecycle event, not a message.
    start = by_id["evt-start-planner"]
    assert start["source"] == "environment"
    assert start["event_type"] == "node_start"
    assert start["node_id"] == "planner"
    assert "llm_message" not in start

    assert mod.count_conversation_events("conv-evt-1", home=tmp_path) == 4


def test_agent_server_events_search_filters_and_pagination(tmp_path, monkeypatch) -> None:
    """timestamp__lt / timestamp__gte windows, sort_order=TIMESTAMP asc, and
    the page_id continue-after cursor with truncation-driven next_page_id."""
    from mini_ork.web.routes import agent_server as mod
    import mini_ork.web.control as control

    calls: list[dict] = []
    monkeypatch.setattr(control, "launch_run", _fake_launch_run_factory(calls))
    mod.create_conversation(
        {"conversation_id": "conv-evt-2", "initial_message": "hello"},
        home=tmp_path,
    )
    _seed_state_db(tmp_path, "conv-evt-2")

    asc = mod.search_conversation_events(
        "conv-evt-2", home=tmp_path, sort_order="TIMESTAMP"
    )
    assert asc["items"][0]["id"] == "evt-start-planner"
    assert asc["items"][-1]["id"] == "conv-evt-2-user-0"

    # load-older window: everything strictly older than the planner node_end.
    planner_end_ts = next(
        e["timestamp"] for e in asc["items"] if e["id"] == "evt-end-planner"
    )
    older = mod.search_conversation_events(
        "conv-evt-2", home=tmp_path, timestamp__lt=planner_end_ts
    )
    assert [e["id"] for e in older["items"]] == ["evt-start-planner"]

    # since-window: everything at/after the planner node_end (WS replay shape).
    since = mod.search_conversation_events(
        "conv-evt-2", home=tmp_path, timestamp__gte=planner_end_ts, sort_order="TIMESTAMP"
    )
    assert [e["id"] for e in since["items"]] == [
        "evt-end-planner",
        "evt-end-implementer",
        "conv-evt-2-user-0",
    ]

    # Truncation: limit=2 asc → first two, next_page_id = boundary item; the
    # export path continues with page_id and gets the remainder.
    first = mod.search_conversation_events(
        "conv-evt-2", home=tmp_path, limit=2, sort_order="TIMESTAMP"
    )
    assert [e["id"] for e in first["items"]] == ["evt-start-planner", "evt-end-planner"]
    assert first["next_page_id"] == "evt-end-planner"
    second = mod.search_conversation_events(
        "conv-evt-2", home=tmp_path, limit=2, sort_order="TIMESTAMP",
        page_id=first["next_page_id"],
    )
    assert [e["id"] for e in second["items"]] == ["evt-end-implementer", "conv-evt-2-user-0"]


def test_agent_server_events_single_and_terminal_status(tmp_path, monkeypatch) -> None:
    """Single-event fetch (404 on unknown), the terminal projection (final
    assistant event + execution_status lifted to finished), and event count
    growing by one for the final event."""
    from fastapi import HTTPException
    from mini_ork.web.routes import agent_server as mod
    import mini_ork.web.control as control

    calls: list[dict] = []
    monkeypatch.setattr(control, "launch_run", _fake_launch_run_factory(calls))
    mod.create_conversation(
        {"conversation_id": "conv-evt-3", "initial_message": "ship it"},
        home=tmp_path,
    )
    _seed_state_db(
        tmp_path, "conv-evt-3", status="published", verdict="APPROVE"
    )

    event = mod.get_conversation_event("conv-evt-3", "evt-end-planner", home=tmp_path)
    assert event["id"] == "evt-end-planner"
    with pytest.raises(HTTPException) as exc:
        mod.get_conversation_event("conv-evt-3", "no-such-event", home=tmp_path)
    assert exc.value.status_code == 404

    # Terminal run: a final assistant event is appended. It is newer than the
    # seeded run events (ended_at = now-10) but older than the user message
    # (created at real `now`), so in DESC order it sits right after the user
    # message — find it by id rather than position.
    page = mod.search_conversation_events("conv-evt-3", home=tmp_path)
    final = next(e for e in page["items"] if e["id"] == "conv-evt-3-final")
    assert final["llm_message"]["role"] == "assistant"
    assert final["source"] == "agent"
    assert "published" in final["llm_message"]["content"][0]["text"]
    assert "APPROVE" in final["llm_message"]["content"][0]["text"]
    assert mod.count_conversation_events("conv-evt-3", home=tmp_path) == 5

    # get_conversation lifts execution_status from the live task_runs row.
    info = mod.get_conversation("conv-evt-3", home=tmp_path)
    assert info["execution_status"] == "finished"


def test_agent_server_send_event_launches_idle_conversation(
    tmp_path, monkeypatch
) -> None:
    """sendEvent on an idle conversation = the promised idle→running kickoff:
    same launch seam as create, run_id = conversation id, ledger records the
    text so it projects as a user MessageEvent immediately."""
    from mini_ork.web.routes import agent_server as mod
    import mini_ork.web.control as control

    calls: list[dict] = []
    monkeypatch.setattr(control, "launch_run", _fake_launch_run_factory(calls))
    mod.create_conversation({"conversation_id": "conv-send-1"}, home=tmp_path)
    assert calls == []

    out = mod.send_conversation_event(
        {
            "role": "user",
            "content": [{"type": "text", "text": "Start by reproducing the bug"}],
            "run": True,
        },
        "conv-send-1",
        home=tmp_path,
    )
    assert out["ok"] is True
    assert len(calls) == 1
    assert calls[0]["run_id"] == "conv-send-1"
    assert "Start by reproducing the bug" in calls[0]["kickoff"]

    info = mod.get_conversation("conv-send-1", home=tmp_path)
    assert info["execution_status"] == "running"

    page = mod.search_conversation_events("conv-send-1", home=tmp_path)
    assert [e["id"] for e in page["items"]] == ["conv-send-1-user-0"]
    assert page["items"][0]["llm_message"]["content"][0]["text"] == (
        "Start by reproducing the bug"
    )


def test_agent_server_send_event_steers_running_conversation(
    tmp_path, monkeypatch
) -> None:
    """sendEvent on a live run = operator steering injection (no second run),
    the sent text still lands in the ledger for transcript projection."""
    from mini_ork.web.routes import agent_server as mod
    import mini_ork.web.control as control

    launches: list[dict] = []
    monkeypatch.setattr(control, "launch_run", _fake_launch_run_factory(launches))
    steers: list[dict] = []

    def fake_steer(db, task_run_id, message, **kwargs):
        steers.append({"run_id": task_run_id, "message": message, **kwargs})
        return {"ok": True, "steering_id": 7}

    monkeypatch.setattr(control, "steer_run", fake_steer)

    mod.create_conversation(
        {"conversation_id": "conv-send-2", "initial_message": "first"}, home=tmp_path
    )
    _seed_state_db(tmp_path, "conv-send-2", status="running")

    out = mod.send_conversation_event(
        {"role": "user", "content": "also check the migrations", "run": True},
        "conv-send-2",
        home=tmp_path,
    )
    assert out["ok"] is True
    # No second launch — mid-run messages steer, they don't spawn.
    assert len(launches) == 1
    assert len(steers) == 1
    assert steers[0]["run_id"] == "conv-send-2"
    assert steers[0]["message"] == "also check the migrations"
    assert steers[0]["source"] == "agent-server-canvas"

    page = mod.search_conversation_events("conv-send-2", home=tmp_path)
    texts = [e for e in page["items"] if e["id"] == "conv-send-2-user-1"]
    assert texts and texts[0]["llm_message"]["content"][0]["text"] == (
        "also check the migrations"
    )


def test_agent_server_send_event_terminal_409(tmp_path, monkeypatch) -> None:
    """A mini-ork conversation is a one-shot DAG: sends after terminal status
    409 (the real agent-server would loop the agent; we diverge loudly
    instead of silently steering a dead run)."""
    from fastapi import HTTPException
    from mini_ork.web.routes import agent_server as mod
    import mini_ork.web.control as control

    launches: list[dict] = []
    monkeypatch.setattr(control, "launch_run", _fake_launch_run_factory(launches))
    mod.create_conversation(
        {"conversation_id": "conv-send-3", "initial_message": "done work"}, home=tmp_path
    )
    _seed_state_db(tmp_path, "conv-send-3", status="failed")

    with pytest.raises(HTTPException) as exc:
        mod.send_conversation_event(
            {"role": "user", "content": "one more thing", "run": True},
            "conv-send-3",
            home=tmp_path,
        )
    assert exc.value.status_code == 409


def test_agent_server_send_event_rejects_bad_requests(tmp_path) -> None:
    """Empty text 400s; unknown conversation 404s; no state.db + live-run
    steer attempt 500s instead of crashing on a missing db."""
    from fastapi import HTTPException
    from mini_ork.web.routes import agent_server as mod

    mod.create_conversation({"conversation_id": "conv-send-4"}, home=tmp_path)
    with pytest.raises(HTTPException) as exc:
        mod.send_conversation_event(
            {"role": "user", "content": []}, "conv-send-4", home=tmp_path
        )
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        mod.send_conversation_event(
            {"role": "user", "content": "hi"}, "never-existed", home=tmp_path
        )
    assert exc.value.status_code == 404


def test_idea_tree_roots_returns_backfilled_sessions(db) -> None:
    """list_roots() must surface every root node with subtree counts.

    Relies on the migration + backfill having run. If state.db has no
    idea_tree_nodes table at all, skip — the test only covers the
    post-backfill happy path. Real production runs should always have
    at least the synthetic roots from scripts/backfill_idea_tree.py.
    """
    from mini_ork.web.idea_tree import list_roots

    if not db.has_table("idea_tree_nodes"):
        pytest.skip("idea_tree_nodes table missing — apply migration 0020")
    roots = list_roots(db)
    if not roots:
        pytest.skip("no idea_tree_nodes rows — run scripts/backfill_idea_tree.py")
    for r in roots:
        # Required fields the UI Trajectory page renders.
        assert {"node_id", "status", "node_count"}.issubset(r.keys())
        # node_count includes the root itself, so >= 1 for any non-empty tree.
        assert r["node_count"] >= 1


def test_idea_tree_read_tree_includes_depth_and_edges(db) -> None:
    """read_tree() must emit nodes with depth + matching edges list.

    Depth-first invariant: every non-root edge has a from-id that
    appears in nodes with depth strictly less than the to-id's depth.
    """
    from mini_ork.web.idea_tree import list_roots, read_tree

    if not db.has_table("idea_tree_nodes"):
        pytest.skip("idea_tree_nodes table missing")
    roots = list_roots(db)
    if not roots:
        pytest.skip("no roots to read")
    tree = read_tree(db, roots[0]["node_id"])
    assert tree["root_node_id"] == roots[0]["node_id"]
    assert tree["nodes"], "root should have itself + descendants"
    # Root node's depth must be 0.
    root_in_tree = next(n for n in tree["nodes"] if n["node_id"] == roots[0]["node_id"])
    assert root_in_tree["depth"] == 0
    # Edges must reference declared node ids.
    node_ids = {n["node_id"] for n in tree["nodes"]}
    for e in tree["edges"]:
        assert e["from"] in node_ids and e["to"] in node_ids
    # Stats sanity.
    assert tree["stats"]["total"] == len(tree["nodes"])
    assert tree["stats"]["max_depth"] >= 0


def test_idea_tree_walk_to_root_ends_at_root(db) -> None:
    """walk_to_root() must return a chain that ends at a parent-less node."""
    from mini_ork.web.idea_tree import list_roots, read_tree, walk_to_root

    if not db.has_table("idea_tree_nodes"):
        pytest.skip("idea_tree_nodes table missing")
    roots = list_roots(db)
    if not roots:
        pytest.skip("no roots")
    tree = read_tree(db, roots[0]["node_id"])
    # Pick the deepest leaf and walk up.
    leaves = [n for n in tree["nodes"] if n["depth"] == tree["stats"]["max_depth"]]
    if not leaves:
        pytest.skip("no leaves to walk")
    chain = walk_to_root(db, leaves[0]["node_id"])
    assert chain, "chain must be non-empty"
    # Last entry in the chain is the root (parent_node_id IS NULL).
    assert chain[-1]["parent_node_id"] is None
    # First entry is the leaf we started from.
    assert chain[0]["node_id"] == leaves[0]["node_id"]


def test_self_improve_detail(db) -> None:
    """Detail endpoint returns parsed notes + linked task_run + sibling context."""
    from mini_ork.web.routes.trajectory import self_improve_detail, self_improve_runs

    rows = self_improve_runs(db, limit=1)
    if not rows:
        pytest.skip("no self_improve_runs to detail")
    rid = rows[0]["run_id"]
    out = self_improve_detail(run_id=rid, db=db)
    assert out["run_id"] == rid
    assert "parsed_notes" in out
    assert isinstance(out["parsed_notes"], list)
    assert "siblings" in out
    # Every parsed note should have key/value/kind
    for n in out["parsed_notes"]:
        assert {"key", "value", "kind"}.issubset(n.keys())
        assert n["kind"] in ("flag", "kv", "sha")


def test_agents_endpoint_enumerates_recipe_nodes(db) -> None:
    """The /agents endpoint must surface every recipe node as a dispatched agent."""
    from mini_ork.web.routes.run_detail import list_agents
    from mini_ork.web.routes.fleet import list_task_runs
    from mini_ork.web.deps import get_home

    runs = [r for r in list_task_runs(db, limit=10) if r.get("recipe") == "recursive-self-improve"]
    if not runs:
        pytest.skip("no recursive-self-improve task_runs")
    home = get_home()
    out = list_agents(task_run_id=runs[0]["id"], db=db, home=home)
    assert out["recipe"] == "recursive-self-improve"
    names = {a["node_id"] for a in out["agents"]}
    # The recursive-self-improve recipe must have these load-bearing nodes
    assert {"bottleneck_lens", "opus_synthesizer", "self_tests_pass"} <= names


def test_agent_detail_loads_prompt(db) -> None:
    """Agent detail must resolve prompt_ref → recipes/<name>/<file>.md content."""
    from mini_ork.web.routes.run_detail import agent_detail
    from mini_ork.web.routes.fleet import list_task_runs
    from mini_ork.web.deps import get_home

    runs = [r for r in list_task_runs(db, limit=10) if r.get("recipe") == "recursive-self-improve"]
    if not runs:
        pytest.skip("no recursive-self-improve task_runs")
    home = get_home()
    out = agent_detail(
        task_run_id=runs[0]["id"],
        node_id="opus_synthesizer",
        db=db,
        home=home,
    )
    assert out["node"]["name"] == "opus_synthesizer"
    assert out["prompt"]["path"] is not None
    assert out["prompt"]["content"], "prompt content should load from recipes/<name>/prompts/<file>"
    assert "llm_calls" in out
    assert "artifacts" in out


def test_load_transcript_prefers_stable_agent_sidecar(tmp_path: Path) -> None:
    """Agent transcript lookup must work when llm_dispatch used a temp stdout file."""
    from mini_ork.web.agents import load_transcript

    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / "run-test"
    run_dir.mkdir(parents=True)
    (run_dir / "agent-tiny_researcher.transcript.json").write_text(
        (
            '{"turns":[{"turn_index":0,"model":"codex",'
            '"input_tokens":0,"output_tokens":0,"text":"visible",'
            '"tool_uses":[],"stop_reason":null,"session_id":null}],'
            '"fallback":"text-output"}'
        ),
        encoding="utf-8",
    )

    out = load_transcript(home, "run-test", "tiny_researcher")
    assert out["available"] is True
    assert out["transcript_path"] == "runs/run-test/agent-tiny_researcher.transcript.json"
    assert out["turns"][0]["text"] == "visible"
    assert out["fallback"] == "text-output"


def test_load_transcript_strips_z_insight_blocks(tmp_path: Path) -> None:
    """Spawned CLIs inherit the operator's global CLAUDE.md and emit
    <z-insight> protocol blocks into deliverables (run-1781095892-69202).
    Render-time strip covers transcripts persisted before the engine fix."""
    import json as _json

    from mini_ork.web.agents import load_transcript

    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / "run-polluted"
    run_dir.mkdir(parents=True)
    polluted = '{"ok":true}\n<z-insight>\n{"leak":1}\n</z-insight>'
    (run_dir / "agent-implementer.transcript.json").write_text(
        _json.dumps({"turns": [{"turn_index": 0, "text": polluted}]}),
        encoding="utf-8",
    )

    out = load_transcript(home, "run-polluted", "implementer")
    assert "<z-insight>" not in out["turns"][0]["text"]
    assert out["turns"][0]["text"] == '{"ok":true}'


def test_load_transcript_falls_back_to_output_artifact(tmp_path: Path) -> None:
    """Legacy runs without sidecars should still show the agent's visible output."""
    from mini_ork.web.agents import load_transcript

    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / "run-legacy"
    run_dir.mkdir(parents=True)
    (run_dir / "context-tiny_researcher.json").write_text('{"summary":"legacy"}', encoding="utf-8")

    out = load_transcript(home, "run-legacy", "tiny_researcher")
    assert out["available"] is True
    assert out["fallback"] == "text-output"
    assert out["transcript_path"] == "runs/run-legacy/context-tiny_researcher.json"
    assert "legacy" in out["turns"][0]["text"]


def test_run_inputs_endpoint_lists_and_reads_context(db) -> None:
    """Run inputs are source context, separate from output artifacts."""
    from mini_ork.web.routes.fleet import list_task_runs
    from mini_ork.web.routes.run_detail import list_inputs, read_input
    from mini_ork.web.deps import get_home

    runs = [r for r in list_task_runs(db, limit=20) if r.get("kickoff_path")]
    if not runs:
        pytest.skip("no task_runs with kickoff_path")

    home = get_home()
    task_run_id = ""
    for run in runs:
        candidate = list_inputs(task_run_id=run["id"], db=db, home=home)
        if any(item["key"] == "kickoff" for item in candidate):
            task_run_id = run["id"]
            break
    if not task_run_id:
        pytest.skip("no task_runs with a readable kickoff input")

    kickoff = read_input(task_run_id=task_run_id, input_key="kickoff", db=db, home=home)
    assert kickoff["content"]
    assert kickoff["kind"] == "markdown"


def test_run_learning_endpoint_exposes_memory_and_injection(db) -> None:
    """Run detail must expose persisted learning plus injection provenance."""
    from mini_ork.web.routes.fleet import list_task_runs
    from mini_ork.web.routes.run_detail import get_learning

    runs = list_task_runs(db, limit=5)
    if not runs:
        pytest.skip("no task_runs")

    out = get_learning(task_run_id=runs[0]["id"], db=db)
    assert out["task_run_id"] == runs[0]["id"]
    assert "summary" in out
    assert "produced" in out
    assert "self_improve" in out
    assert "injected_candidates" in out
    assert "injection_points" in out["injected_candidates"]
    for row in out["produced"]["gradients"]:
        assert "agent_attribution" in row


def test_summary_endpoint_uses_cache(seeded_db) -> None:
    """Two summary calls within TTL must return the same object (cache hit).

    Hermetic: with no task_runs table the route early-returns a fresh dict per
    call (cache never engages), so this must run against a db that HAS the
    migrated table."""
    from mini_ork.web.routes.fleet import task_runs_summary

    seeded_db._result_cache.clear()
    a = task_runs_summary(seeded_db)
    b = task_runs_summary(seeded_db)
    assert a is b, "second call within TTL should return the cached object"


def test_correlation_reports_bridge_methods(db) -> None:
    """Correlation endpoint must enumerate available bridge methods + warn on gaps."""
    from mini_ork.web.routes.run_detail import get_correlation
    from mini_ork.web.routes.fleet import list_task_runs

    runs = list_task_runs(db, limit=1)
    if not runs:
        pytest.skip("no task_runs to correlate")
    out = get_correlation(task_run_id=runs[0]["id"], db=db)
    assert "bridge_methods" in out
    assert "run_events.run_id" in out["bridge_methods"], (
        "run_events.run_id should always be listed — it's the deterministic bridge for "
        "node lifecycle events emitted by mini_ork/cli/execute.py"
    )
    # If trace_id is set (post-fix or backfill), strict methods must be available
    if out["trace_id"]:
        assert "mo_events.trace_id" in out["bridge_methods"]
        assert "llm_calls.traceparent" in out["bridge_methods"]


def test_events_carry_bridge_attribution(db) -> None:
    """Each event row must declare via which bridge it was matched."""
    from mini_ork.web.routes.run_detail import get_events
    from mini_ork.web.routes.fleet import list_task_runs

    runs = list_task_runs(db, limit=5)
    if not runs:
        pytest.skip("no task_runs")
    for r in runs:
        evs = get_events(task_run_id=r["id"], db=db)
        for e in evs:
            assert "bridge" in e, f"event missing bridge attribution: {e}"
            assert e["bridge"] in ("trace_id", "run_id", "time-window")


def test_dag_carries_node_status(seeded_db, home) -> None:
    """DAG endpoint must merge node_start/node_end events into per-node status.

    Hermetic: runs against the seeded code-fix run (planner start+end → done).
    The old env-dependent version skipped on fresh checkouts and RAISED on a
    fresh worktree's tableless state.db ("no such table: task_runs").
    """
    from mini_ork.web.routes.run_detail import get_dag

    out = get_dag(task_run_id="run-hermetic-1", db=seeded_db, home=home)
    statuses = {n["name"]: n["status"] for n in out["nodes"]}
    assert statuses.get("planner") == "done", (
        f"expected seeded planner to be done, got: {statuses}"
    )


def test_error_category_column_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE schema_migrations(filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT);
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL,
          model_id TEXT NOT NULL,
          tier TEXT NOT NULL,
          feature_name TEXT NOT NULL,
          actor TEXT,
          status TEXT NOT NULL CHECK (status IN ('success','failed')),
          duration_ms INTEGER NOT NULL DEFAULT 0,
          cost_usd REAL NOT NULL DEFAULT 0,
          error_message TEXT,
          iter INTEGER,
          run_id TEXT,
          traceparent TEXT,
          input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0,
          total_tokens INTEGER NOT NULL DEFAULT 0,
          metadata_json TEXT NOT NULL DEFAULT '{}',
          session_id TEXT,
          ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE run_events (
          event_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          payload_json TEXT NOT NULL DEFAULT '{}',
          created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
        """
    )
    con.executescript((ROOT / "db/migrations/0021_error_taxonomy_finish_reasons.sql").read_text())

    cols = {r[1] for r in con.execute("PRAGMA table_info(llm_calls)").fetchall()}
    assert {"error_category", "retryable"} <= cols
    con.execute(
        """
        INSERT INTO llm_calls (
          provider, model_id, tier, feature_name, status,
          error_category, retryable
        ) VALUES ('gateway', 'glm', 'default', 'mini-ork:test', 'failed', 'auth', 0)
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            """
            INSERT INTO llm_calls (
              provider, model_id, tier, feature_name, status,
              error_category, retryable
            ) VALUES ('gateway', 'glm', 'default', 'mini-ork:test', 'failed', 'auth_failed', 0)
            """
        )
    con.close()


def test_finish_reason_column_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE schema_migrations(filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT);
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL,
          model_id TEXT NOT NULL,
          tier TEXT NOT NULL,
          feature_name TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('success','failed'))
        );
        CREATE TABLE run_events (
          event_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          payload_json TEXT NOT NULL DEFAULT '{}',
          created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
        """
    )
    con.executescript((ROOT / "db/migrations/0021_error_taxonomy_finish_reasons.sql").read_text())

    cols = {r[1] for r in con.execute("PRAGMA table_info(run_events)").fetchall()}
    assert "finish_reason" in cols
    con.execute(
        """
        INSERT INTO run_events(event_id, run_id, event_type, payload_json, finish_reason)
        VALUES ('evt-ok', 'run-1', 'node_end', '{}', 'verdict_revise')
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            """
            INSERT INTO run_events(event_id, run_id, event_type, payload_json, finish_reason)
            VALUES ('evt-bad', 'run-1', 'node_end', '{}', 'needs_revision')
            """
        )
    con.close()


def test_dispatch_config_snapshot_columns_exist(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE schema_migrations(filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT);
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY,
          updated_at INTEGER NOT NULL
        );
        """
    )
    con.executescript((ROOT / "db/migrations/0022_dispatch_config_snapshot.sql").read_text())

    cols = {r[1] for r in con.execute("PRAGMA table_info(task_runs)").fetchall()}
    assert {"dispatch_config_json", "agents_yaml_sha"} <= cols
    indexes = {r[1] for r in con.execute("PRAGMA index_list(task_runs)").fetchall()}
    assert "idx_task_runs_agents_yaml_sha" in indexes
    con.close()


def test_node_heartbeat_fuse_columns_exist(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE schema_migrations(filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT);
        CREATE TABLE run_events (
          event_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          payload_json TEXT NOT NULL DEFAULT '{}',
          created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY,
          updated_at INTEGER NOT NULL
        );
        """
    )
    con.executescript((ROOT / "db/migrations/0023_node_heartbeat_fuse.sql").read_text())

    run_event_cols = {r[1]: r for r in con.execute("PRAGMA table_info(run_events)").fetchall()}
    task_run_cols = {r[1]: r for r in con.execute("PRAGMA table_info(task_runs)").fetchall()}
    assert "last_heartbeat_at" in run_event_cols
    assert run_event_cols["last_heartbeat_at"][3] == 0
    assert {"fuse_blown_lane", "fuse_consecutive_failures"} <= set(task_run_cols)
    assert task_run_cols["fuse_blown_lane"][3] == 0
    assert task_run_cols["fuse_consecutive_failures"][4] == "0"
    indexes = {r[1] for r in con.execute("PRAGMA index_list(run_events)").fetchall()}
    assert "idx_run_events_last_heartbeat_at" in indexes
    con.close()


def test_llm_calls_cache_columns_exist(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE schema_migrations(filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT);
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL,
          model_id TEXT NOT NULL,
          tier TEXT NOT NULL,
          feature_name TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('success','failed'))
        );
        """
    )
    con.executescript((ROOT / "db/migrations/0024_cache_aware_cost.sql").read_text())

    cols = {r[1]: r for r in con.execute("PRAGMA table_info(llm_calls)").fetchall()}
    assert {
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "cost_input_uncached_usd",
        "cost_input_cached_usd",
        "cost_cache_write_usd",
    } <= set(cols)
    assert cols["cached_input_tokens"][4] == "0"
    assert cols["cache_creation_input_tokens"][4] == "0"
    con.close()


def test_cache_cost_components_sum_to_cost_usd(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE schema_migrations(filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT);
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL,
          model_id TEXT NOT NULL,
          tier TEXT NOT NULL,
          feature_name TEXT NOT NULL,
          actor TEXT,
          status TEXT NOT NULL CHECK (status IN ('success','failed')),
          duration_ms INTEGER NOT NULL DEFAULT 0,
          cost_usd REAL NOT NULL DEFAULT 0,
          error_message TEXT,
          iter INTEGER,
          run_id TEXT,
          traceparent TEXT,
          input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0,
          total_tokens INTEGER NOT NULL DEFAULT 0,
          metadata_json TEXT NOT NULL DEFAULT '{}',
          session_id TEXT,
          ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        """
    )
    con.executescript((ROOT / "db/migrations/0024_cache_aware_cost.sql").read_text())
    con.close()

    from mini_ork.dispatch.llm_dispatch import write_llm_calls_row

    write_llm_calls_row(
        str(db_path), "anthropic", "claude-opus-4", "default", "mini-ork:test",
        "tester", "success", 100, 0.009, "", 1000, 25, "{}", 200, 300,
    )

    con = sqlite3.connect(db_path)
    row = con.execute(
        """
        SELECT cost_input_uncached_usd, cost_input_cached_usd, cost_cache_write_usd,
               cost_usd, cached_input_tokens, cache_creation_input_tokens
        FROM llm_calls
        """
    ).fetchone()
    con.close()

    assert row[4] == 200
    assert row[5] == 300
    # F2: anthropic input_tokens EXCLUDES cache — uncached is input as-is
    expected_input_cost = (1000 * 15.0 + 200 * 1.5 + 300 * 18.75) / 1_000_000
    component_sum = row[0] + row[1] + row[2]
    assert component_sum == pytest.approx(expected_input_cost)
    assert row[3] == pytest.approx(0.009)


def test_legacy_rows_with_zero_cache_still_query(tmp_path: Path) -> None:
    from mini_ork.web.db import StateDB
    from mini_ork.web.routes.run_detail import get_llm_calls

    db_path = tmp_path / "state.db"
    now = 1_800_000_000
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY,
          trace_id TEXT,
          created_at INTEGER NOT NULL,
          ended_at INTEGER
        );
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL,
          model_id TEXT NOT NULL,
          tier TEXT NOT NULL,
          feature_name TEXT NOT NULL,
          actor TEXT,
          input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0,
          total_tokens INTEGER NOT NULL DEFAULT 0,
          cost_usd REAL NOT NULL DEFAULT 0,
          duration_ms INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL CHECK (status IN ('success','failed')),
          finish_reason TEXT,
          traceparent TEXT,
          ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        """
    )
    con.execute(
        "INSERT INTO task_runs(id, trace_id, created_at, ended_at) VALUES ('run-legacy', 'trace-legacy', ?, ?)",
        (now - 10, now + 10),
    )
    con.execute(
        """
        INSERT INTO llm_calls (
          provider, model_id, tier, feature_name, status,
          input_tokens, output_tokens, total_tokens, cost_usd, traceparent, ts
        ) VALUES (
          'anthropic', 'claude-opus-4', 'default', 'mini-ork:planner', 'success',
          100, 20, 120, 0.01, '00-trace-legacy-span-01', '2027-01-15T08:00:00.000Z'
        )
        """
    )
    con.commit()
    con.close()

    rows = get_llm_calls(task_run_id="run-legacy", db=StateDB(db_path))
    assert len(rows) == 1
    assert rows[0]["cached_input_tokens"] == 0


def test_lane_fuse_trips_after_three_retryable_failures(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          feature_name TEXT NOT NULL,
          status TEXT NOT NULL,
          error_category TEXT,
          retryable INTEGER
        );
        INSERT INTO llm_calls(feature_name, status, error_category, retryable)
        VALUES
          ('framework_edit:glm_lens', 'failed', 'network', 1),
          ('framework_edit:glm_lens', 'failed', 'network', 1),
          ('framework_edit:glm_lens', 'failed', 'network', 1);
        """
    )
    con.close()

    from mini_ork.dispatch.llm_dispatch import check_lane_fuse

    assert check_lane_fuse(str(db_path), "glm_lens", "network") is True


def test_lane_fuse_ignores_nonretryable_failures(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          feature_name TEXT NOT NULL,
          status TEXT NOT NULL,
          error_category TEXT,
          retryable INTEGER
        );
        INSERT INTO llm_calls(feature_name, status, error_category, retryable)
        VALUES
          ('framework_edit:glm_lens', 'failed', 'auth', 0),
          ('framework_edit:glm_lens', 'failed', 'auth', 0),
          ('framework_edit:glm_lens', 'failed', 'auth', 0);
        """
    )
    con.close()

    from mini_ork.dispatch.llm_dispatch import check_lane_fuse

    assert check_lane_fuse(str(db_path), "glm_lens", "auth") is False


def test_agents_endpoint_legacy_null_snapshot_uses_fallback(tmp_path: Path, monkeypatch) -> None:
    from mini_ork.web.db import StateDB
    from mini_ork.web.routes.run_detail import list_agents

    home = tmp_path / ".mini-ork"
    home.mkdir()
    db_path = home / "state.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY,
          recipe TEXT,
          trace_id TEXT,
          created_at INTEGER,
          ended_at INTEGER,
          status TEXT,
          cost_usd REAL,
          dispatch_config_json TEXT,
          agents_yaml_sha TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO task_runs (
          id, recipe, created_at, ended_at, status, cost_usd,
          dispatch_config_json, agents_yaml_sha
        ) VALUES ('run-legacy', 'framework-edit', 1, 2, 'published', 0, NULL, NULL)
        """
    )
    con.commit()
    con.close()

    monkeypatch.setenv("MINI_ORK_ROOT", str(ROOT))
    out = list_agents(task_run_id="run-legacy", db=StateDB(db_path), home=home)
    code_lens = next(a for a in out["agents"] if a["node_id"] == "code_impact_lens")
    assert code_lens["model_lane"] == "minimax_lens"
    assert code_lens["family"] == "minimax"
    assert code_lens["model_id"] is None


def test_agents_endpoint_prefers_dispatch_config_snapshot(tmp_path: Path, monkeypatch) -> None:
    from mini_ork.web.db import StateDB
    from mini_ork.web.routes.run_detail import agent_detail, list_agents

    home = tmp_path / ".mini-ork"
    home.mkdir()
    db_path = home / "state.db"
    snapshot = {
        "minimax_lens": {
            "family": "historical-family",
            "model_id": "historical-model",
            "provider": "historical-provider",
            "base_url": "https://historical.example/v1",
        }
    }
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY,
          recipe TEXT,
          trace_id TEXT,
          created_at INTEGER,
          ended_at INTEGER,
          status TEXT,
          cost_usd REAL,
          dispatch_config_json TEXT,
          agents_yaml_sha TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO task_runs (
          id, recipe, created_at, ended_at, status, cost_usd,
          dispatch_config_json, agents_yaml_sha
        ) VALUES ('run-snapshot', 'framework-edit', 1, 2, 'published', 0, ?, 'sha')
        """,
        (json.dumps(snapshot),),
    )
    con.commit()
    con.close()

    monkeypatch.setenv("MINI_ORK_ROOT", str(ROOT))
    out = list_agents(task_run_id="run-snapshot", db=StateDB(db_path), home=home)
    code_lens = next(a for a in out["agents"] if a["node_id"] == "code_impact_lens")
    assert code_lens["family"] == "historical-family"
    assert code_lens["model_id"] == "historical-model"
    assert code_lens["provider"] == "historical-provider"
    assert code_lens["base_url"] == "https://historical.example/v1"

    detail = agent_detail(
        task_run_id="run-snapshot",
        node_id="code_impact_lens",
        db=StateDB(db_path),
        home=home,
    )
    assert detail["node"]["family"] == "historical-family"
    assert detail["node"]["model_id"] == "historical-model"


def test_llm_dispatch_classifies_invalid_api_key_as_auth() -> None:
    from mini_ork.dispatch.llm_dispatch import classify_error

    assert classify_error("HTTP 401 invalid api key", 1) == "auth"


def test_agents_yaml_has_capabilities_section() -> None:
    import yaml

    cfg = yaml.safe_load((ROOT / "config/agents.yaml").read_text()) or {}
    capabilities = cfg.get("capabilities") or {}
    expected = {"opus", "sonnet", "codex", "glm", "kimi", "deepseek", "minimax"}
    assert expected <= set(capabilities)
    for family in expected:
        assert {"vision", "tools", "reasoning", "search"} <= set(capabilities[family])


def test_capability_check_passes_when_family_supports_all(monkeypatch) -> None:
    # kimi_lens is the canonical "supports vision + tools" lane after the
    # 2026-06-13 no-opus standing directive removed opus_lens from
    # .mini-ork/config/agents.yaml. Kimi exposes vision=true + tools=true
    # in config/agents.yaml's capabilities map, which is what the gate
    # asserts against. opus_lens used to play this role but no longer
    # resolves under the override; codex / glm / minimax all lack vision.
    from mini_ork.dispatch.lane_helpers import assert_lane_capability

    monkeypatch.setenv("MINI_ORK_ROOT", str(ROOT))
    assert_lane_capability("kimi_lens", "vision,tools")


def test_capability_check_fails_when_family_missing_one(monkeypatch) -> None:
    from mini_ork.dispatch.lane_helpers import assert_lane_capability

    monkeypatch.setenv("MINI_ORK_ROOT", str(ROOT))
    with pytest.raises(RuntimeError, match="^vision$"):
        assert_lane_capability("codex_lens", "vision,tools")


def test_llm_calls_route_tolerates_null_taxonomy_columns(tmp_path: Path) -> None:
    from mini_ork.web.db import StateDB
    from mini_ork.web.routes.run_detail import get_llm_calls

    db_path = tmp_path / "state.db"
    now = 1_800_000_000
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY,
          trace_id TEXT,
          created_at INTEGER NOT NULL,
          ended_at INTEGER
        );
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL,
          model_id TEXT NOT NULL,
          tier TEXT NOT NULL,
          feature_name TEXT NOT NULL,
          actor TEXT,
          input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0,
          total_tokens INTEGER NOT NULL DEFAULT 0,
          cost_usd REAL NOT NULL DEFAULT 0,
          duration_ms INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL CHECK (status IN ('success','failed')),
          finish_reason TEXT,
          error_message TEXT,
          traceparent TEXT,
          metadata_json TEXT NOT NULL DEFAULT '{}',
          session_id TEXT,
          error_category TEXT,
          retryable INTEGER,
          ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        """
    )
    con.execute(
        "INSERT INTO task_runs(id, trace_id, created_at, ended_at) VALUES ('run-1', 'trace-1', ?, ?)",
        (now - 10, now + 10),
    )
    con.execute(
        """
        INSERT INTO llm_calls (
          provider, model_id, tier, feature_name, status, finish_reason,
          error_category, retryable, traceparent, ts
        ) VALUES (
          'gateway', 'glm', 'default', 'mini-ork:reviewer', 'success', NULL,
          NULL, NULL, '00-trace-1-span-01', '2027-01-15T08:00:00.000Z'
        )
        """
    )
    con.commit()
    con.close()

    rows = get_llm_calls(task_run_id="run-1", db=StateDB(db_path))
    assert len(rows) == 1
    assert rows[0]["feature_name"] == "mini-ork:reviewer"


def test_profile_answerer_has_one_native_owner() -> None:
    from mini_ork.steering import profile_answerer

    assert not (ROOT / "lib" / "profile_answerer.sh").exists()
    assert callable(profile_answerer.answer_profile_questions)
    assert callable(profile_answerer.build_prompt)
    assert callable(profile_answerer.parse_and_persist)


def test_python_plan_references_native_auto_answer() -> None:
    # Canonical plan runtime (mini_ork/ported/ was retired by the OSS-scrub).
    plan = (ROOT / "mini_ork" / "cli" / "plan.py").read_text()

    assert "MO_AUTO_ANSWER_PROFILE" in plan
    assert "mini_ork.steering.profile_answerer" in plan
    assert "answer_profile_questions" in plan


# ── project switcher (GET /projects, POST /projects/switch) ─────────────────


def test_project_home_resolution_is_idempotent(monkeypatch, tmp_path) -> None:
    """The validate -> switch -> request flow must keep one canonical home."""
    import sqlite3

    from mini_ork.web.deps import get_default_home, get_home, set_home_override
    from mini_ork.web.routes.projects import switch_project, validate_project

    monkeypatch.setenv("MINI_ORK_PROJECTS_FILE", str(tmp_path / "projects.json"))
    project = tmp_path / "researcher"
    project_home = project / ".mini-ork"
    nested_home = project_home / ".mini-ork"
    nested_home.mkdir(parents=True)
    sqlite3.connect(project_home / "state.db").close()
    sqlite3.connect(nested_home / "state.db").close()
    previous_home = get_default_home()

    try:
        checked = validate_project(str(project), previous_home)
        assert checked["home"] == str(project_home)

        switched = switch_project({"home": checked["home"]})
        assert switched["active"] == str(project_home)
        assert switched["name"] == "researcher"

        assert get_home(str(project_home), None) == project_home
        assert get_home(None, str(project_home)) == project_home
    finally:
        set_home_override(previous_home)


def test_projects_list_includes_active(db, home, monkeypatch, tmp_path) -> None:
    from mini_ork.web.deps import get_home
    from mini_ork.web.routes.projects import list_projects

    monkeypatch.setenv("MINI_ORK_PROJECTS_FILE", str(tmp_path / "projects.json"))
    out = list_projects(get_home())
    assert out["active"] == str(home)
    actives = [p for p in out["projects"] if p["active"]]
    assert len(actives) == 1
    assert actives[0]["home"] == str(home)
    assert actives[0]["exists"] is True


def test_projects_switch_rejects_bogus_path(db, monkeypatch, tmp_path) -> None:
    from fastapi import HTTPException

    from mini_ork.web.routes.projects import switch_project

    monkeypatch.setenv("MINI_ORK_PROJECTS_FILE", str(tmp_path / "projects.json"))
    with pytest.raises(HTTPException) as e:
        switch_project({"home": str(tmp_path / "nope")})
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        switch_project({"home": "  "})
    assert e.value.status_code == 422


def test_projects_switch_swaps_db_and_registers(db, home, monkeypatch, tmp_path) -> None:
    """Switch to a second home, verify get_db points at it, switch back."""
    import sqlite3

    from mini_ork.web.deps import get_db, get_home, set_home_override
    from mini_ork.web.routes.projects import list_projects, switch_project, validate_project

    monkeypatch.setenv("MINI_ORK_PROJECTS_FILE", str(tmp_path / "projects.json"))

    other = tmp_path / "researcher" / ".mini-ork"
    other.mkdir(parents=True)
    con = sqlite3.connect(other / "state.db")
    con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT)")
    con.commit()
    con.close()

    # Regression: a second, accidentally nested home must not make the
    # validate -> switch UI flow descend from researcher/.mini-ork into
    # researcher/.mini-ork/.mini-ork.
    nested = other / ".mini-ork"
    nested.mkdir()
    sqlite3.connect(nested / "state.db").close()

    try:
        # The UI validates the project folder, then switches using the
        # canonical home returned by validation.
        checked = validate_project(str(other.parent), home)
        assert checked["home"] == str(other)
        out = switch_project({"home": checked["home"]})
        assert out["ok"] is True
        assert out["active"] == str(other)
        assert out["name"] == "researcher"
        assert get_home() == other
        assert get_db().db_path == other / "state.db"

        listed = list_projects(get_home())
        assert str(other) in [p["home"] for p in listed["projects"]]
    finally:
        set_home_override(home)
    # StateDB.db_path is canonicalised via .resolve(); resolve the expected
    # side too so the assert holds whether state.db is a real file or a symlink
    # (e.g. a worktree/vendored install whose state.db links elsewhere).
    assert get_db().db_path == (home / "state.db").resolve()


def test_projects_validate_and_add(db, home, monkeypatch, tmp_path) -> None:
    import sqlite3

    from mini_ork.web.deps import get_home
    from mini_ork.web.routes.projects import add_project, list_projects, validate_project

    monkeypatch.setenv("MINI_ORK_PROJECTS_FILE", str(tmp_path / "projects.json"))
    active = get_home()

    bad = validate_project(str(tmp_path / "nowhere"), active)
    assert bad["ok"] is False and "error" in bad

    other = tmp_path / "researcher" / ".mini-ork"
    other.mkdir(parents=True)
    sqlite3.connect(other / "state.db").close()
    nested = other / ".mini-ork"
    nested.mkdir()
    sqlite3.connect(nested / "state.db").close()

    good = validate_project(str(other.parent), active)  # project folder accepted
    assert good["ok"] is True
    assert good["home"] == str(other)
    assert good["name"] == "researcher"
    assert good["registered"] is False
    # A direct home is already canonical, even if a nested state DB exists.
    assert validate_project(str(other), active)["home"] == str(other)

    out = add_project({"home": str(other.parent)}, active)
    assert out["ok"] is True and out["project"]["home"] == str(other)
    # add registers without switching
    assert validate_project(str(other), active)["registered"] is True
    listed = list_projects(active)
    assert str(other) in [p["home"] for p in listed["projects"]]
    assert listed["active"] == str(home)


def test_workspace_scoped_home_resolution(db, home, tmp_path) -> None:
    """Per-request workspace: the X-Mini-Ork-Home header (or `home` query
    param for SSE) resolves to its own home + DB without touching the
    server-wide default."""
    import sqlite3

    from fastapi import HTTPException

    from mini_ork.web.deps import db_for, get_db, get_default_home, get_home, get_home_lenient

    other = tmp_path / "researcher" / ".mini-ork"
    other.mkdir(parents=True)
    sqlite3.connect(other / "state.db").close()
    nested = other / ".mini-ork"
    nested.mkdir()
    sqlite3.connect(nested / "state.db").close()

    assert get_home(str(other.parent), None) == other  # project folder accepted
    assert get_home(None, str(other)) == other  # query param (SSE) works
    assert get_home(str(other), str(home)) == other  # header beats query param
    assert get_home(None, None) == get_default_home()  # no workspace → default

    # request-scoped DB resolution leaves the server default untouched
    assert get_db(get_home(str(other), None)).db_path == other / "state.db"
    # StateDB.db_path is canonicalised via .resolve(); resolve the expected
    # side too so the assert holds whether state.db is a real file or a symlink
    # (e.g. a worktree/vendored install whose state.db links elsewhere).
    assert get_db().db_path == (home / "state.db").resolve()
    assert db_for(other) is db_for(other)  # per-home handle cache

    with pytest.raises(HTTPException) as e:
        get_home(str(tmp_path / "nope"), None)
    assert e.value.status_code == 404
    # lenient variant (projects routes) falls back instead of locking out
    assert get_home_lenient(str(tmp_path / "nope"), None) == get_default_home()


# ── E4 cost-pause + E6 auth HTTP integration ──────────────────────────────


def test_pause_cost_writes_sentinel(tmp_path: Path) -> None:
    from mini_ork.web.control import pause_cost_run

    run_id = "run-test-pause"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)

    result = pause_cost_run(tmp_path, run_id, threshold_usd=42.0)
    assert result["ok"] is True
    assert result["threshold_usd"] == 42.0
    sentinel = run_dir / ".cost-pause"
    assert sentinel.is_file()
    payload = json.loads(sentinel.read_text())
    assert payload["threshold_usd"] == 42.0
    assert payload["source"] == "http-api"
    assert payload["run_id"] == run_id


def test_pause_cost_rejects_missing_run(tmp_path: Path) -> None:
    from mini_ork.web.control import pause_cost_run

    result = pause_cost_run(tmp_path, "run-nope")
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_resume_cost_clears_sentinel_and_audits(tmp_path: Path) -> None:
    from mini_ork.web.control import pause_cost_run, resume_cost_run

    run_id = "run-test-resume"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    pause_cost_run(tmp_path, run_id, threshold_usd=10.0)

    result = resume_cost_run(tmp_path, run_id, approver="amir")
    assert result["ok"] is True
    assert result["approver"] == "amir"
    assert not (run_dir / ".cost-pause").exists()

    approvals = run_dir / ".cost-pause-approvals.jsonl"
    assert approvals.is_file()
    audit_line = json.loads(approvals.read_text().splitlines()[0])
    assert audit_line["approver"] == "amir"
    assert audit_line["run_id"] == run_id
    assert audit_line["source"] == "http-api"


def test_resume_cost_without_sentinel_returns_error(tmp_path: Path) -> None:
    from mini_ork.web.control import resume_cost_run

    run_id = "run-test-no-sentinel"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)

    result = resume_cost_run(tmp_path, run_id, approver="amir")
    assert result["ok"] is False
    assert "no cost-pause sentinel" in result["error"]


def test_auth_require_token_rejects_missing_header() -> None:
    from fastapi import HTTPException

    from mini_ork.web.auth import require_token

    # FastAPI Request stub: only headers is consulted.
    class _StubReq:
        headers = {}  # type: ignore[assignment]

    with pytest.raises(HTTPException) as exc:
        require_token(_StubReq())  # type: ignore[arg-type]
    assert exc.value.status_code == 401


def test_auth_require_token_accepts_valid_token(tmp_path: Path, monkeypatch) -> None:
    from mini_ork.web.auth import require_token

    tokens_file = tmp_path / "auth-tokens.txt"
    tokens_file.write_text("abc123 amir\n# comment\ndef456 ops-bot\n")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))

    class _StubReq:
        headers = {"authorization": "Bearer abc123"}

    assert require_token(_StubReq()) == "amir"  # type: ignore[arg-type]


# ── Phase A: canvas seam (origin guard, conversation list, health) ────────────
#
# A1: the agent-server shim is tokenless by design (the canvas sends no bearer,
# and require_token is fail-closed), so the boundary against the one live threat
# — a browser page from any other origin POSTing to a billable route, which a
# CORS "simple request" lets through unread but not unexecuted — is an Origin
# check. `require_local_caller` is asserted both as a pure predicate and as
# ACTUALLY WIRED to the routes; a guard that exists but is attached to nothing
# is the failure mode worth a test of its own.


class _OriginReq:
    """Minimal Request stand-in: the guard reads only `headers.get`."""

    def __init__(self, origin: str | None = None):
        self.headers = {} if origin is None else {"origin": origin}


def test_agent_server_origin_guard_refuses_a_foreign_page(monkeypatch) -> None:
    """A page on any origin other than this machine must not reach the handler.

    This is the whole point of the guard: `POST /api/conversations` starts a real
    billable run, and a cross-origin `text/plain` POST skips the CORS preflight,
    so without this check the run launches even though the attacker never sees a
    response.
    """
    from fastapi import HTTPException

    from mini_ork.web.routes.agent_server import require_local_caller

    monkeypatch.delenv("MO_AGENT_SERVER_ALLOW_REMOTE", raising=False)
    for origin in ("https://evil.com", "http://evil.com:7090", "https://127.0.0.1.evil.com"):
        with pytest.raises(HTTPException) as exc:
            require_local_caller(_OriginReq(origin))  # type: ignore[arg-type]
        assert exc.value.status_code == 403, origin


def test_agent_server_origin_guard_admits_local_and_non_browser_callers(
    monkeypatch,
) -> None:
    """Both legitimate callers pass: the canvas (served from loopback, any port)
    and any non-browser client — the SDK, curl, a test — which sends no Origin
    header at all. Admitting the header-less case is deliberate: it is what lets
    the local SDK work tokenless, and a local process could read any token file
    anyway, so the guard is not pretending to defend against it."""
    from mini_ork.web.routes.agent_server import require_local_caller

    monkeypatch.delenv("MO_AGENT_SERVER_ALLOW_REMOTE", raising=False)
    for origin in ("http://localhost:7070", "http://127.0.0.1:7090", "http://localhost"):
        assert require_local_caller(_OriginReq(origin)) is None  # type: ignore[arg-type]
    # No Origin header at all → the non-browser path.
    assert require_local_caller(_OriginReq(None)) is None  # type: ignore[arg-type]


def test_agent_server_origin_guard_refuses_the_null_origin(monkeypatch) -> None:
    """`Origin: null` is NOT treated as local.

    CORS tolerates the literal "null" for Electron `file://` renderers (see
    app.py's CORSMiddleware note), but a sandboxed iframe or a `data:` URL also
    manufactures it, so it is not evidence of anything. On a route whose side
    effect is a billable launch, it must not be admitted on the strength of a
    string any attacker can produce.
    """
    from fastapi import HTTPException

    from mini_ork.web.routes.agent_server import require_local_caller

    monkeypatch.delenv("MO_AGENT_SERVER_ALLOW_REMOTE", raising=False)
    with pytest.raises(HTTPException) as exc:
        require_local_caller(_OriginReq("null"))  # type: ignore[arg-type]
    assert exc.value.status_code == 403


def test_agent_server_origin_guard_opt_out_for_exposed_deployments(monkeypatch) -> None:
    """`mini-ork serve --host 0.0.0.0` is a documented flag, so the guard needs a
    deliberate escape hatch rather than silently breaking that deployment."""
    from mini_ork.web.routes.agent_server import require_local_caller

    monkeypatch.setenv("MO_AGENT_SERVER_ALLOW_REMOTE", "1")
    assert require_local_caller(_OriginReq("https://evil.com")) is None  # type: ignore[arg-type]


def test_agent_server_mutating_routes_are_actually_guarded() -> None:
    """The guard must be ATTACHED, not merely defined.

    Unit-testing `require_local_caller` proves the predicate; this proves the two
    billable routes depend on it. Driven through a real TestClient so it exercises
    FastAPI's dependency resolution rather than my reading of it. Both requests
    are origin-less at the transport level and distinguished only by the header,
    so a 403→idle-200 flip is attributable to the guard alone.
    """
    from fastapi.testclient import TestClient

    from mini_ork.web.app import create_app

    client = TestClient(create_app(dev_cors=False))

    # A foreign page: refused before the handler, so no conversation is created.
    blocked = client.post(
        "/api/conversations",
        json={"conversation_id": "conv-csrf-probe", "title": "csrf"},
        headers={"origin": "https://evil.com"},
    )
    assert blocked.status_code == 403, blocked.text

    # The canvas (loopback origin): admitted. No initial_message, so this stays
    # idle and launches nothing — the assertion is only that the guard let it in.
    allowed = client.post(
        "/api/conversations",
        json={"conversation_id": "conv-csrf-probe", "title": "ok"},
        headers={"origin": "http://localhost:7070"},
    )
    assert allowed.status_code == 200, allowed.text


def test_agent_server_ready_reports_missing_state_db_as_503(tmp_path: Path) -> None:
    """A2: the canvas must be able to tell "mini-ork is down" (transport failure)
    from "mini-ork is up with no workspace" (503). Answering 200 here is the
    silent-empty failure this shim refuses elsewhere: the sidebar would render
    fine and every launch would then fail."""
    from mini_ork.web.routes.agent_server import ready

    out = ready(home=tmp_path)
    assert out.status_code == 503
    body = json.loads(out.body)
    assert body["status"] == "not_ready"
    assert str(tmp_path) in body["reason"]


def test_agent_server_ready_and_health_agree_when_a_db_exists(tmp_path: Path) -> None:
    """With a state.db present both endpoints flip — ready to 200, and health's
    `status` from "degraded" to "ok" — so a monitor scraping either sees the same
    verdict. Only presence is required: readiness is about the file the run
    lifecycle needs, not about its contents."""
    from mini_ork.web.routes.agent_server import health, ready

    (tmp_path / "state.db").touch()

    assert ready(home=tmp_path).status_code == 200
    out = health(home=tmp_path)
    assert out["status"] == "ok"
    assert out["db_present"] is True
    assert out["home"] == str(tmp_path)


def test_agent_server_alive_has_no_dependencies() -> None:
    """`/alive` answers "is the process up" and nothing else — it must not fail
    for a reason /health already reports, or the canvas cannot separate a dead
    server from an empty one."""
    from mini_ork.web.routes.agent_server import alive

    assert alive() == {"status": "ok"}


def test_agent_server_conversation_list_routes_precede_id_route() -> None:
    """A3: registration order is load-bearing.

    `/api/conversations/search` and `/count` also match the
    `/api/conversations/{conversation_id}` pattern, and FastAPI resolves in
    registration order. Declared after it, `/search` is read as conversation id
    "search", misses the registry, and 404s the sidebar with a misleading
    "conversation not found". Asserted on the OpenAPI path order because it is
    the same source of truth the app-factory test already uses and it survives
    FastAPI's router-mounting shape changes.
    """
    from mini_ork.web.app import create_app

    paths = list(create_app(dev_cors=False).openapi()["paths"])
    id_route = paths.index("/api/conversations/{conversation_id}")
    assert paths.index("/api/conversations/search") < id_route
    assert paths.index("/api/conversations/count") < id_route


def _seed_conversation(home: Path, cid: str, updated_at: str, started: bool = False) -> None:
    """Write a registry sidecar directly — the shape `_save_conversation` emits."""
    from mini_ork.web.routes.agent_server import _save_conversation

    _save_conversation(
        home,
        {
            "id": cid,
            "title": f"title {cid}",
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": updated_at,
            "execution_status": "running" if started else "idle",
            "recipe": "code-fix",
            "run_launched": started,
            "messages": [{"text": f"hello from {cid}", "ts": updated_at}],
        },
    )


def test_agent_server_conversation_search_pages_newest_updated_first(
    tmp_path: Path,
) -> None:
    """The sidebar's first call. Ordering is UPDATED_AT_DESC (the SDK's
    ConversationSortOrder) and `next_page_id` is set ONLY when the page was
    actually truncated — returning it unconditionally makes `hasMore` heuristics
    loop forever fetching an empty tail."""
    from mini_ork.web.routes.agent_server import search_conversations

    _seed_conversation(tmp_path, "conv-old", "2026-09-01T00:00:00Z")
    _seed_conversation(tmp_path, "conv-mid", "2026-09-02T00:00:00Z")
    _seed_conversation(tmp_path, "conv-new", "2026-09-03T00:00:00Z")

    page = search_conversations(home=tmp_path, limit=2)
    assert [i["id"] for i in page["items"]] == ["conv-new", "conv-mid"]
    assert page["next_page_id"] == "conv-mid"

    tail = search_conversations(home=tmp_path, limit=2, page_id=page["next_page_id"])
    assert [i["id"] for i in tail["items"]] == ["conv-old"]
    assert tail["next_page_id"] is None  # last page → no cursor

    # A truncated page must also carry the fields the canvas types require.
    item = page["items"][0]
    assert item["id"] and item["created_at"] and item["updated_at"]
    assert item["agent"]["llm"]["model"]


def test_agent_server_conversation_search_empty_home_is_an_empty_page(
    tmp_path: Path,
) -> None:
    """A home with no `conversations/` dir is the normal first-run state — it must
    be an empty page, not an error, or the canvas shows a failed sidebar before
    the user has done anything."""
    from mini_ork.web.routes.agent_server import count_conversations, search_conversations

    assert search_conversations(home=tmp_path) == {"items": [], "next_page_id": None}
    assert count_conversations(home=tmp_path) == 0


def test_agent_server_conversation_batch_get_is_positional_with_nulls(
    tmp_path: Path,
) -> None:
    """`getConversations(ids)` needs an array ALIGNED with the request, with null
    for misses: the batch path exists to re-hydrate rows whose sidecar was pruned,
    so raising on one stale id would fail the whole batch. Unsafe ids (path
    traversal) are a miss, not a 400 — same reasoning, and they never touch disk."""
    from mini_ork.web.routes.agent_server import get_conversations

    _seed_conversation(tmp_path, "conv-known", "2026-09-03T00:00:00Z")

    out = get_conversations(
        home=tmp_path, ids=["conv-known", "conv-missing", "../escape"]
    )
    assert out[0] is not None and out[0]["id"] == "conv-known"
    assert out[1] is None
    assert out[2] is None
    assert get_conversations(home=tmp_path, ids=None) == []


# ── Canvas event WebSocket (SE-3 UI fork) ─────────────────────────────────────
#
# The canvas opens one socket per conversation and, when it cannot reach OPEN,
# degrades to REST polling — which is what the "Disconnected" chip reports. The
# tests below pin the three properties that degradation cannot supply: the
# history replay ordered oldest-first, the terminal execution_status signal,
# and a push for events written *after* connect. The last one is the assertion
# that distinguishes this route from `routes/stream.py`, whose MAX(id) cursor is
# new-only and cannot replay the backlog a reconnect needs.


def _ws_home(tmp_path: Path) -> Path:
    """A home the socket route will actually resolve to.

    `get_home` 404s unless `<home>/state.db` exists (that check is what stops a
    stale workspace from silently reading another project's runs), and the
    socket route falls back to the DEFAULT home on a miss. So without this the
    tests would quietly connect to the developer's real .mini-ork. A bare empty
    file is enough: a non-launched conversation never opens the DB.
    """
    (tmp_path / "state.db").touch()
    return tmp_path


def _ws_url(tmp_path: Path, cid: str, **params: str) -> str:
    query = "".join(f"&{k}={v}" for k, v in params.items())
    return f"/sockets/events/{cid}?home={tmp_path}{query}"


def _recv_json(ws: object, timeout: float = 10.0) -> object:
    """`ws.receive_json()` with a deadline.

    starlette's test session blocks on the receive queue forever when the
    server sends nothing, so a regression here would hang CI instead of failing
    it. The pool is abandoned rather than joined on timeout — the enclosing
    `with` block tears the socket down either way.
    """
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(ws.receive_json).result(timeout=timeout)  # type: ignore[attr-defined]
    finally:
        pool.shutdown(wait=False)


def test_socket_replays_history_then_holds_open(tmp_path: Path) -> None:
    """Handshake first, then the backlog oldest-first, then no close.

    The full_state frame leads because the client renders its status chip off
    it — replaying events *before* it would leave the chip guessing until the
    first event landed. The socket must also NOT close when the backlog ends:
    the canvas opens it for every conversation the user clicks, including ones
    finished days ago, so a close-on-drain rule would paint "Disconnected" over
    the whole history.
    """
    from fastapi.testclient import TestClient

    from mini_ork.web.app import create_app

    home = _ws_home(tmp_path)
    _seed_conversation(home, "conv-ws", "2026-09-03T00:00:00Z")

    with TestClient(create_app(dev_cors=False)) as client:
        with client.websocket_connect(_ws_url(home, "conv-ws")) as ws:
            handshake = _recv_json(ws)
            assert handshake["kind"] == "ConversationStateUpdateEvent"
            assert handshake["key"] == "full_state"
            assert handshake["source"] == "environment"
            assert handshake["value"] == {"execution_status": "idle"}

            event = _recv_json(ws)
            assert event["id"] == "conv-ws-user-0"
            assert event["source"] == "user"
            assert event["llm_message"]["role"] == "user"
            assert event["llm_message"]["content"][0]["text"] == "hello from conv-ws"


def test_socket_since_replays_only_what_the_client_lacks(tmp_path: Path) -> None:
    """`resend_mode=since` is the reconnect path, and it is a STRICT comparison.

    The client subscribes with the timestamp of the newest event it already
    holds, so anything at-or-before it must not be resent (at: already held;
    after: the client would never see it). This is the branch `stream.py`
    cannot serve at all — its cursor is seeded at MAX(id) and drops history.
    """
    from fastapi.testclient import TestClient

    from mini_ork.web.app import create_app

    home = _ws_home(tmp_path)
    _seed_conversation(home, "conv-since", "2026-09-03T00:00:00Z")

    with TestClient(create_app(dev_cors=False)) as client:
        # after_timestamp == the seeded message's own stamp → nothing replayed.
        url = _ws_url(
            home,
            "conv-since",
            resend_mode="since",
            after_timestamp="2026-09-03T00:00:00Z",
        )
        with client.websocket_connect(url) as ws:
            assert _recv_json(ws)["key"] == "full_state"

        # A cursor older than the message → the backlog comes back.
        url = _ws_url(
            home,
            "conv-since",
            resend_mode="since",
            after_timestamp="2026-09-02T00:00:00Z",
        )
        with client.websocket_connect(url) as ws:
            assert _recv_json(ws)["key"] == "full_state"
            assert _recv_json(ws)["id"] == "conv-since-user-0"


def test_socket_announces_a_terminal_status_then_stays_open(tmp_path: Path) -> None:
    """A run that is already over still gets its execution_status signal.

    `finished`/`error` are what `_TERMINAL_STATUS_MAP` folds a recipe status
    into, and the client needs one of them to stop its "running" spinner.
    """
    from fastapi.testclient import TestClient

    from mini_ork.web.app import create_app
    from mini_ork.web.routes.agent_server import _save_conversation

    home = _ws_home(tmp_path)
    _save_conversation(
        home,
        {
            "id": "conv-done",
            "title": "done",
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-09-03T00:00:00Z",
            "execution_status": "finished",
            # run_launched is False so nothing reaches for state.db: the status
            # is the sidecar's own, which is exactly the canvas-only home case.
            "run_launched": False,
            "messages": [],
        },
    )

    with TestClient(create_app(dev_cors=False)) as client:
        with client.websocket_connect(_ws_url(home, "conv-done")) as ws:
            assert _recv_json(ws)["key"] == "full_state"
            terminal = _recv_json(ws)
            assert terminal["kind"] == "ConversationStateUpdateEvent"
            assert terminal["key"] == "execution_status"
            assert terminal["value"] == "finished"


def test_socket_pushes_an_event_written_after_connect(tmp_path: Path) -> None:
    """THE live assertion — and the one that catches a stale-record poll.

    The record is captured at handshake time, but the sidecar is not a
    snapshot: `POST /events` appends to `record["messages"]` while the socket
    is open. A poll loop that re-projects the captured object would relay
    run_events (read fresh from sqlite) while silently dropping every message
    the user types in the session — invisible to every test that stops at the
    replay.
    """
    from fastapi.testclient import TestClient

    from mini_ork.web.app import create_app
    from mini_ork.web.routes.agent_server import _load_conversation, _save_conversation

    home = _ws_home(tmp_path)
    _seed_conversation(home, "conv-live", "2026-09-03T00:00:00Z")

    with TestClient(create_app(dev_cors=False)) as client:
        with client.websocket_connect(_ws_url(home, "conv-live")) as ws:
            assert _recv_json(ws)["key"] == "full_state"
            assert _recv_json(ws)["id"] == "conv-live-user-0"

            record = _load_conversation(home, "conv-live")
            assert record is not None
            record["messages"].append(
                {"text": "typed while connected", "ts": "2026-09-03T00:00:05Z"}
            )
            _save_conversation(home, record)

            live = _recv_json(ws)
            assert live["id"] == "conv-live-user-1"
            assert live["source"] == "user"
            assert live["llm_message"]["content"][0]["text"] == "typed while connected"


def test_socket_routes_inbound_messages_to_the_rest_handler(tmp_path: Path) -> None:
    """`sendMessage` over the socket must not diverge from `POST /events`.

    The canvas only calls REST when the socket is NOT open, so the moment this
    route started answering, every send took the WebSocket branch. If the
    receive loop did not hand the frame on, the run would never launch and the
    user would see nothing — so the forwards are asserted directly rather than
    through a real launch.
    """
    from fastapi.testclient import TestClient

    from mini_ork.web import routes
    from mini_ork.web.app import create_app

    home = _ws_home(tmp_path)
    _seed_conversation(home, "conv-fwd", "2026-09-03T00:00:00Z")

    seen: list[object] = []
    original = routes.sockets.send_conversation_event
    routes.sockets.send_conversation_event = lambda *args: seen.append(args)
    try:
        with TestClient(create_app(dev_cors=False)) as client:
            with client.websocket_connect(_ws_url(home, "conv-fwd")) as ws:
                assert _recv_json(ws)["key"] == "full_state"
                # The canvas's auth handshake is a CONTROL frame, not a
                # message: forwarding it would 400 and banner the user on
                # every single connect.
                ws.send_json({"type": "auth", "session_api_key": "k"})
                ws.send_json(
                    {"role": "user", "content": [{"type": "text", "text": "go"}], "run": True}
                )
                ws.send_text("not json at all")

                # No ack is emitted for a successful send, so the proof is that
                # the socket is still healthy afterwards plus the forward count
                # below — a control frame or a malformed frame would instead
                # have produced an error frame by now.
                ws.send_json(
                    {"role": "user", "content": [{"type": "text", "text": "again"}], "run": True}
                )

                # The drain hands each frame to the REST handler on a worker
                # thread, so the second forward can still be in flight when
                # this block exits — and the close then cancels it, leaving
                # one forward instead of two. That race is invisible on a dev
                # box and fires on a loaded CI runner, so wait for both here,
                # while the socket is still open.
                deadline = time.monotonic() + 5.0
                while len(seen) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
    finally:
        routes.sockets.send_conversation_event = original

    assert len(seen) == 2, seen
    assert [a[1] for a in seen] == ["conv-fwd", "conv-fwd"]
    assert seen[0][2] == home  # the resolved home, not the query string
    assert seen[0][0]["content"][0]["text"] == "go"


def test_socket_surfaces_an_inbound_failure_as_an_error_frame(tmp_path: Path) -> None:
    """A refused send must reach the user, not vanish.

    `sendMessage` over the socket is fire-and-forget, so the only channel left
    is a ConversationErrorEvent — the frame the canvas renders as a banner.
    Silently dropping it is the "no data looks like success" failure this shim
    refuses everywhere else.
    """
    from fastapi import HTTPException
    from fastapi.testclient import TestClient

    from mini_ork.web import routes
    from mini_ork.web.app import create_app

    home = _ws_home(tmp_path)
    _seed_conversation(home, "conv-fail", "2026-09-03T00:00:00Z")

    def _boom(*args: object) -> None:
        raise HTTPException(status_code=409, detail="run already finished")

    original = routes.sockets.send_conversation_event
    routes.sockets.send_conversation_event = _boom
    try:
        with TestClient(create_app(dev_cors=False)) as client:
            with client.websocket_connect(_ws_url(home, "conv-fail")) as ws:
                assert _recv_json(ws)["key"] == "full_state"
                assert _recv_json(ws)["id"] == "conv-fail-user-0"  # drain the replay
                ws.send_json({"role": "user", "content": "hi", "run": True})
                error = _recv_json(ws)
    finally:
        routes.sockets.send_conversation_event = original

    assert error["kind"] == "ConversationErrorEvent"
    assert error["source"] == "environment"
    assert error["code"] == "409"
    assert error["detail"] == "run already finished"


def test_socket_unknown_conversation_closes_normally(tmp_path: Path) -> None:
    """An unknown id closes 1000 so the canvas shows its empty state instead of
    reconnecting forever against something that will never exist."""
    import pytest as _pytest
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from mini_ork.web.app import create_app

    home = _ws_home(tmp_path)
    with TestClient(create_app(dev_cors=False)) as client:
        with _pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(_ws_url(home, "conv-absent")) as ws:
                ws.receive_json()
    assert excinfo.value.code == 1000


def test_bash_events_socket_accepts_and_stays_inert(tmp_path: Path) -> None:
    """The terminal pairs this with a bash POST the shim does not serve.

    Holding it open keeps the terminal's connection chip honest ("connected, no
    output") instead of showing an error the user cannot resolve."""
    from fastapi.testclient import TestClient

    from mini_ork.web.app import create_app

    with TestClient(create_app(dev_cors=False)) as client:
        with client.websocket_connect("/sockets/bash-events") as ws:
            # Accepted; an auth frame is drained, and nothing is echoed back.
            ws.send_json({"type": "auth", "session_api_key": "k"})


def _static_home(tmp_path: Path) -> Path:
    """A stand-in for the built SPA dir (`mini_ork/web/static`).

    It carries the two shapes that matter: `index.html`, which is the catch-all's
    answer, and a real root-level asset beside it. The locale file is the one the
    i18n loader actually fetches (`/locales/{{lng}}/{{ns}}.json`, ns=openhands).
    """
    static = tmp_path / "static"
    (static / "locales" / "en").mkdir(parents=True)
    # `create_app` mounts /assets unconditionally once index.html exists, and
    # StaticFiles raises at mount time if the directory is missing.
    (static / "assets").mkdir()
    (static / "index.html").write_text("<!DOCTYPE html><title>spa</title>")
    (static / "locales" / "en" / "openhands.json").write_text('{"HELLO": "hello"}')
    return static


def test_spa_fallback_serves_root_level_assets_as_files(tmp_path: Path, monkeypatch) -> None:
    """A shipped asset must beat the index.html fallback.

    Vite serves `ui/public/` at the dev root; served from here, only `/assets` was
    mounted, so `/locales/en/openhands.json` came back as index.html with status
    200. The i18n loader parses that as JSON, fails, and every string in the UI
    renders as its raw key — while every request still reports success. This is
    the exact silent-false-success shape the `_NON_SPA_PREFIXES` 404 refuses, so
    it gets a test rather than a comment."""
    import json

    from fastapi.testclient import TestClient

    from mini_ork.web import app as app_module

    monkeypatch.setattr(app_module, "STATIC_DIR", _static_home(tmp_path))
    with TestClient(app_module.create_app(dev_cors=False)) as client:
        resp = client.get("/locales/en/openhands.json")
        assert resp.status_code == 200
        # The assertion that distinguishes asset from fallback: it must parse.
        assert json.loads(resp.text) == {"HELLO": "hello"}

        # …and a client-side route still gets the SPA shell.
        fallback = client.get("/conversations/abc-123")
        assert fallback.status_code == 200
        assert fallback.text == "<!DOCTYPE html><title>spa</title>"


def test_spa_fallback_refuses_to_escape_the_static_dir(tmp_path: Path, monkeypatch) -> None:
    """`resolve()` before the containment check is load-bearing, not decoration.

    `full_path` is attacker-controlled; without the `is_relative_to` guard a
    traversal reads any file the server user can read. The percent-encoded form
    is used because a literal `..` is normalised away client-side before it ever
    reaches the route."""
    from fastapi.testclient import TestClient

    from mini_ork.web import app as app_module

    static = _static_home(tmp_path)
    (tmp_path / "secret.txt").write_text("do-not-serve")
    monkeypatch.setattr(app_module, "STATIC_DIR", static)

    with TestClient(app_module.create_app(dev_cors=False)) as client:
        resp = client.get("/%2e%2e%2fsecret.txt")
        assert "do-not-serve" not in resp.text
        assert resp.text == "<!DOCTYPE html><title>spa</title>"


def _ws_tree(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway workspace root, pinned via the env lever the routes read.

    `MO_TARGET_CWD` is resolved at call time (not import), which is what makes it
    patchable here — and is also the honest production reading, since the lever
    is how an operator says "this is the tree the run writes to".
    """
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text("hi\n")
    monkeypatch.setenv("MO_TARGET_CWD", str(tree))
    return tree


def test_workspace_root_never_widens_to_home(tmp_path: Path, monkeypatch) -> None:
    """Without the lever the root is the server's CWD, never `Path.home()`.

    Falling back to home would silently expose the operator's whole home
    directory to the file/git/bash tabs; the CWD is the honest reading of "the
    tree this server is operating on"."""
    from mini_ork.web.routes import workspace

    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    assert workspace._workspace_root() == Path.cwd().resolve()


def test_workspace_paths_are_confined_to_the_root(tmp_path: Path, monkeypatch) -> None:
    """`resolve()` before the containment check is what makes traversal fail.

    Pattern-matching `..` would be a guess; resolving first is a fact."""
    from fastapi import HTTPException

    from mini_ork.web.routes import workspace

    root = _ws_tree(tmp_path, monkeypatch).resolve()
    assert workspace._resolve_within(root, "a.txt") == root / "a.txt"
    # A path the canvas derived from a real OpenHands sandbox does not exist
    # here; answering about the root beats 404ing a repo that has changes.
    assert workspace._resolve_within(root, "workspace/project") == root
    with pytest.raises(HTTPException) as exc:
        workspace._resolve_within(root, "../../etc/passwd")
    assert exc.value.status_code == 400


def test_workspace_static_routes_answer_the_canvas_vocabulary(tmp_path: Path, monkeypatch) -> None:
    """The four non-git routes, pinned on their shapes rather than their truth.

    `sdk_version` deliberately reports the protocol we speak rather than a
    padded value the shim could not honour; `base_url` is empty because mini-ork
    serves no fileserver, and a plausible-looking prefix would 404 one hop later."""
    from fastapi.testclient import TestClient

    from mini_ork.web.app import create_app
    from mini_ork.web.routes.agent_server import AGENT_SERVER_PROTOCOL_VERSION

    tree = _ws_tree(tmp_path, monkeypatch).resolve()
    with TestClient(create_app(dev_cors=False)) as client:
        assert client.get("/api/file/home").json() == {
            "home": str(tree),
            "favorites": [],
            "locations": [],
        }
        subdirs = client.get("/api/file/search_subdirs").json()
        assert subdirs["path"] == "." and subdirs["next_page_id"] is None
        assert client.post("/api/skills").json() == {"skills": []}
        sdk = client.get("/api/automation/sdk-version").json()
        assert sdk["sdk_version"] == AGENT_SERVER_PROTOCOL_VERSION

        session = client.post("/api/auth/workspace-session").json()
        assert session["served"] is False and session["base_url"] == ""
        # 204, not 200: the caller awaits the teardown and there is no body.
        assert client.delete("/api/auth/workspace-session").status_code == 204


def test_bash_route_runs_a_command_and_bounds_its_cwd(tmp_path: Path, monkeypatch) -> None:
    """`execute_bash_command` is the Files tab's file-listing primitive.

    It is deliberately NOT sandboxed — the operator can already run anything via
    `POST /api/conversations` — but the `cwd` IS confined, and a nonzero exit is
    a normal result rather than an HTTP error (the canvas renders stderr inline)."""
    from fastapi.testclient import TestClient

    from mini_ork.web.app import create_app

    _ws_tree(tmp_path, monkeypatch)
    with TestClient(create_app(dev_cors=False)) as client:
        ok = client.post(
            "/api/bash/execute_bash_command", json={"command": "ls"}
        ).json()
        assert ok == {"exit_code": 0, "stdout": "a.txt\n", "stderr": ""}

        nonzero = client.post(
            "/api/bash/execute_bash_command", json={"command": "ls /nope-xyz"}
        ).json()
        assert nonzero["exit_code"] != 0 and nonzero["stderr"]

        # An escaping cwd is refused before the shell ever runs.
        escape = client.post(
            "/api/bash/execute_bash_command",
            json={"command": "ls", "cwd": "../../"},
        )
        assert escape.status_code == 400

        # A timeout is reported as exit 124 with whatever output arrived, not
        # as a raised error that would discard the partial output.
        slow = client.post(
            "/api/bash/execute_bash_command",
            json={"command": "sleep 5", "timeout": 1},
        ).json()
        assert slow["exit_code"] == 124

        assert client.post("/api/bash/execute_bash_command", json={}).status_code == 400


def test_git_routes_project_the_canvas_status_vocabulary(
    tmp_path: Path, monkeypatch
) -> None:
    """The git tabs, over a real repository.

    The status values are the agent-server's `UPDATED`/`ADDED`/`DELETED`, not
    git's letters — the client maps server→client status, so passing git's own
    letters through would land them in `mapAnyGitStatusToClientStatus` unknown."""
    import subprocess

    from fastapi.testclient import TestClient

    from mini_ork.web.app import create_app

    tree = _ws_tree(tmp_path, monkeypatch).resolve()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(tree), *a], check=True, capture_output=True, env={**__import__("os").environ, **env}
    )
    run("init", "-q")
    (tree / "tracked.txt").write_text("one\n")
    # a.txt comes with the scaffold; commit it too so it is clean and the change
    # list asserted below is exactly the two states this test is about.
    run("add", "a.txt", "tracked.txt")
    run("commit", "-qm", "first")
    (tree / "tracked.txt").write_text("two\n")
    (tree / "fresh.txt").write_text("new\n")

    with TestClient(create_app(dev_cors=False)) as client:
        changes = {c["path"]: c["status"] for c in client.get("/api/git/changes").json()}
        assert changes == {"tracked.txt": "UPDATED", "fresh.txt": "ADDED"}

        diff = client.get(
            "/api/git/diff", params={"path": "tracked.txt"}
        ).json()
        assert diff["original"] == "one\n" and diff["modified"] == "two\n"

        # A file git has never seen has no HEAD side; empty is the correct
        # before-image for "this did not exist".
        assert client.get("/api/git/diff", params={"path": "fresh.txt"}).json()["original"] == ""

        commits = client.get("/api/git/commits").json()
        assert commits["has_more"] is False
        assert commits["commits"][0]["subject"] == "first"
        assert commits["commits"][0]["short_sha"] in commits["commits"][0]["sha"]

        # Snapshot a real commit and keep the wall-clock date out of the assert
        # (--date=iso-strict is pinned, the value is not).
        assert commits["commits"][0]["timestamp"].startswith("20")


def test_unmatched_write_is_a_404_not_a_405(tmp_path: Path, monkeypatch) -> None:
    """The canvas reads 404 as "endpoint absent, hide the panel" and 405 as a
    hard failure it toasts. A GET-only catch-all made every *unimplemented*
    write the loud one — the exact inversion of what the canvas wants. So an
    unmatched POST/PUT/DELETE must answer 404 JSON, and a real GET route must
    still get the SPA shell."""
    from fastapi.testclient import TestClient

    from mini_ork.web import app as app_module

    monkeypatch.setattr(app_module, "STATIC_DIR", _static_home(tmp_path))
    with TestClient(app_module.create_app(dev_cors=False)) as client:
        for method in ("post", "put", "patch", "delete"):
            resp = client.request(method.upper(), "/api/nothing-here", json={})
            assert resp.status_code == 404, method
            assert resp.json() == {"detail": "Not Found"}

        # A non-/api path still falls through to the shell…
        assert client.get("/some/spa/route").text == "<!DOCTYPE html><title>spa</title>"
        # …but an unmatched GET under /api/ stays a JSON 404.
        assert client.get("/api/nothing-here").status_code == 404


def test_fileserver_serves_a_file_body_and_404s_a_missing_one(
    tmp_path: Path, monkeypatch
) -> None:
    """The Files tab preview reads a file from this route and nothing else.

    `RemoteWorkspace.startWorkspaceSession` discards the session POST's body and
    builds the URL client-side as `${host}/api/conversations/{id}/workspace/`,
    so the session route cannot suppress the fetch — serving the path is the
    only fix. The 404 below is the *missing-file* case, which must stay a 404:
    falling back to the root's listing would render a folder where a document
    was asked for."""
    from fastapi.testclient import TestClient

    from mini_ork.web import app as app_module

    tree = _ws_tree(tmp_path, monkeypatch).resolve()
    (tree / "sub").mkdir()
    (tree / "sub" / "note.md").write_text("# hi\n")

    with TestClient(app_module.create_app(dev_cors=False)) as client:
        body = client.get("/api/conversations/any-id/workspace/a.txt")
        assert body.status_code == 200 and body.text == "hi\n"

        # A conversation that was never registered reads the same tree — the
        # workspace is one tree, not a per-conversation sandbox.
        nested = client.get("/api/conversations/other-id/workspace/sub/note.md")
        assert nested.status_code == 200
        assert nested.text == "# hi\n"
        assert nested.headers["content-type"].startswith("text/markdown")

        # No path at all is the documented "fall back to the server" case.
        listing = client.get("/api/conversations/any-id/workspace/")
        assert listing.status_code == 200 and "note.md" not in listing.text
        assert listing.headers["content-type"].startswith("text/html")

        assert client.get("/api/conversations/any-id/workspace/nope.txt").status_code == 404

        # Confinement applies to the values `_resolve_within` cannot normalise
        # away for free — the encoded traversal is refused, not served.
        escape = client.get("/api/conversations/any-id/workspace/%2e%2e%2f%2e%2e%2fetc%2fpasswd")
        assert escape.status_code in (400, 404)
        assert "root:" not in escape.text


def test_sandbox_working_dir_prefix_is_stripped_for_repo_relative_paths(
    tmp_path: Path, monkeypatch
) -> None:
    """The canvas asks for `workspace/project/<rel>`, not `<rel>`.

    `getGitPath` returns the conversation's working dir (`workspace/project` by
    default) and the diff tab prepends it to a repo-relative path. mini-ork's
    root *is* the repo, so that prefix is virtual — without stripping it every
    diff request 400s ('not a file in the workspace') and the panel expands to
    an empty box."""
    from fastapi.testclient import TestClient

    from mini_ork.web import app as app_module

    tree = _ws_tree(tmp_path, monkeypatch).resolve()
    (tree / "pkg").mkdir()
    (tree / "pkg" / "mod.py").write_text("a\n")

    with TestClient(app_module.create_app(dev_cors=False)) as client:
        # The prefixed form resolves to the same file as the bare one.
        assert (
            client.get("/api/git/diff", params={"path": "workspace/project/pkg/mod.py"}).status_code
            == 200
        )
        assert client.get(
            "/api/git/diff", params={"path": "workspace/project/pkg/mod.py"}
        ).json()["modified"] == "a\n"

        # A *real* `workspace/project/` in the tree still wins over the strip.
        (tree / "workspace" / "project").mkdir(parents=True)
        (tree / "workspace" / "project" / "real.py").write_text("real\n")
        assert client.get(
            "/api/git/diff", params={"path": "workspace/project/real.py"}
        ).json()["modified"] == "real\n"

        # …and a prefixed path that exists neither way is still a 404, not a
        # silent fallback to the root.
        assert client.get(
            "/api/git/diff", params={"path": "workspace/project/nope.py"}
        ).status_code == 400


def test_api_index_reports_the_real_surface_not_zero(tmp_path: Path, monkeypatch):
    """`/api` must never answer a well-formed 200 that under-reports the server.

    It enumerated `app.routes`, which FastAPI 0.139 broke: `include_router`
    appends a lazy `_IncludedRouter` whose `.path` is None, so every mounted
    router was skipped and the index advertised `endpoint_count: 0` while the
    server served 100 paths. That is the wrong-200 failure the 404-vs-405
    catch-all note guards against — a client parses it happily and concludes
    nothing is implemented. The fix derives the list from the resolved OpenAPI
    schema; this test pins the property, not the mechanism.
    """
    from fastapi.testclient import TestClient

    from mini_ork.web import app as app_module

    monkeypatch.setenv("MO_TARGET_CWD", str(tmp_path))
    with TestClient(app_module.create_app(dev_cors=False)) as client:
        body = client.get("/api").json()
        assert body["endpoint_count"] > 0
        # The two families are disjoint and together cover the /api surface.
        assert all(p.startswith("/api/v1/") for p in body["endpoints"])
        assert body["agent_server_endpoint_count"] > 0
        assert all(
            p.startswith("/api/") and not p.startswith("/api/v1")
            for p in body["agent_server_endpoints"]
        )
        # The workspace routes this session added are actually advertised —
        # the index is only useful if it names what the canvas depends on.
        assert "/api/bash/execute_bash_command" in body["agent_server_endpoints"]
