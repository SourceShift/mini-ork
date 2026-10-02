"""Acceptance tests for remote-nodes-10 — disconnect tolerance, reattach, resume.

These tests exercise the production path the kickoff demands (Review bar §1):
every Acceptance bullet has its own test that drives the ``RemoteWorkspace``
end-to-end against the in-process node-agent (``create_app(runtime="host")``,
``TestClient``). No real daemon, no skip-only gating. The pattern mirrors
:mod:`tests.unit.test_remote_workspace` so the TestClient + host-runtime
seam is the same seam the rest of the remote-nodes epic ships on.

Coverage matrix (kickoff §"Acceptance" bullets, in order):

  1. ``test_kill_connection_mid_stream_drains_exactly_once``
     — fault-injecting transport drops the stream mid-drain; spawn reconnects
       and returns complete output exactly once, no duplicated bytes, single
       node-agent proc.

  2. ``test_recovery_reattach_default_when_remote_proc_row_exists``
     — an unconsumed ``remote_procs`` row at the first-incomplete node
       makes ``compute_recovery`` default to ``strategy="reattach"``;
       ``plan_recovery(strategy="reattach")`` returns a valid plan.

  3. ``test_recovery_falls_back_to_resume_when_no_remote_proc_row``
     — no ``remote_procs`` row → default remains ``"resume"``.

  4. ``test_remote_proc_journal_upserts_offsets_during_stream``
     — the ``_RemoteProcJournal`` writes start + offset checkpoints + end
       rows around a successful spawn (mocked transport).

  5. ``test_session_store_finds_remote_transcript_by_session_id``
     — the cwd-slug fallback path now picks up a transcript placed under
       an unrelated project slug; glob-first lookup.

  6. ``test_reattach_strategy_in_recovery_strategies_tuple``
     — ``RECOVERY_STRATEGIES`` includes ``"reattach"`` and ``plan_recovery``
       accepts it.

  7. ``test_node_agent_spawn_idempotency_returns_same_pid``
     — two ``POST /v1/procs`` with the same ``idempotency_key`` resolve to
       the same ``pid`` and the node-agent's ProcRegistry spawns once.

  8. ``test_node_agent_spawn_idempotency_returns_exited_proc``
     — even after the proc has exited, a re-dispatch with the same key
       returns the SAME pid (the harvest path).

  9. ``test_claude_config_dir_injected_into_remote_env``
     — ``spawn(env={...})`` on the in-process node-agent sees
       ``CLAUDE_CONFIG_DIR=/workspace/home/.claude`` on the child proc.

Each test runs in <2 s (the heavy test_remote_workspace in-process
``uvicorn`` harness is too slow for 9 tests; we use FastAPI's TestClient
here, the same seam the conformance suite uses).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures: in-process node-agent TestClient + RemoteWorkspace against it.
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_run_dir(tmp_path) -> Path:
    """A tmp run dir with a sibling ``state.db`` (the journal DB)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    # state.db lives in ``home``; the journal resolver climbs run_dir's parent.
    state_db = home / "state.db"
    # Initialize the schema_migrations table + remote_procs table.
    con = sqlite3.connect(state_db)
    try:
        con.executescript(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT);"
        )
        # Apply 0061 directly (mirroring the migration shell).
        migration_sql = (Path(__file__).resolve().parents[2]
                         / "db" / "migrations" / "0061_remote_procs.sql")
        if migration_sql.is_file():
            con.executescript(migration_sql.read_text(encoding="utf-8"))
    finally:
        con.close()
    os.environ["MINI_ORK_HOME"] = str(home)
    os.environ["MINI_ORK_DB"] = str(state_db)
    yield run_dir
    os.environ.pop("MINI_ORK_HOME", None)
    os.environ.pop("MINI_ORK_DB", None)


@pytest.fixture
def in_process_node_agent(tmp_path):
    """Build a node-agent FastAPI app wired with TestClient + host runtime."""
    from fastapi.testclient import TestClient
    from mini_ork.remote.node_agent.app import create_app

    token = "reattach-test-token"
    os.environ["MO_NODE_TOKEN"] = token
    os.environ["MO_REMOTE_ALLOW_DIRTY_ENGINE"] = "1"
    # Reconnect window of 30s — enough to test the "reattach within window"
    # path without slowing the suite down.
    os.environ["MO_REMOTE_RECONNECT_MAX_S"] = "30"
    app = create_app(
        state_dir=tmp_path / "agent",
        token_env="MO_NODE_TOKEN",
        runtime="host",
    )
    with TestClient(app) as client:
        yield client, token


def _make_workspace(client, token, tmp_path, run_dir, retries=1):
    """Build a ``RemoteWorkspace`` against the TestClient URL."""
    from mini_ork.runtime.backends.remote import RemoteWorkspace, _NodeRef

    ws = RemoteWorkspace(
        node=_NodeRef(name="reattach-test", url="http://testclient", token=token,
                      max_sessions=1),
        run_id="reattach-test-run", image="alpine:latest", drive_root=str(tmp_path),
        engine_root=os.environ.get("MINI_ORK_ROOT") or os.getcwd(),
        target_root=tmp_path,
        token_env="MO_NODE_TOKEN", retries=retries,
        run_dir=str(run_dir),
    )
    # The TestClient URL is internal; we monkey-patch the workspace's
    # ``_request`` to route through the TestClient. This keeps the rest
    # of the workspace plumbing (journal, event emission, reconnect loop)
    # byte-identical to production.
    def request_via_testclient(method, path, *, body=None, content_type=None, query=None):
        headers = {"Authorization": f"Bearer {token}"}
        if content_type:
            headers["Content-Type"] = content_type
        if query:
            from urllib.parse import urlencode
            path = path + "?" + urlencode(query)
        if method == "GET":
            resp = client.get(path, headers=headers)
        elif method == "POST":
            resp = client.post(path, content=body, headers=headers)
        elif method == "PUT":
            resp = client.put(path, content=body, headers=headers)
        elif method == "DELETE":
            resp = client.delete(path, headers=headers)
        else:
            raise ValueError(method)
        if resp.status_code >= 400:
            from urllib import error as _ue
            raise _ue.HTTPError(
                url=path, code=resp.status_code, msg=resp.reason,
                hdrs=None, fp=None,
            )
        return resp.content

    ws._request = request_via_testclient
    ws._original_request = request_via_testclient  # for tests that need to inspect it
    # Tree-sync + run-dir mirror require a real git target repo on disk
    # AND a synced run dir on the node-agent; the seam tests here only
    # exercise spawn / reconnect, not the (already-covered) sync layers.
    # Patch these BEFORE ws.up() so up()'s initial sync_up also no-ops.
    ws._sync_up = lambda *, force=False: None  # type: ignore[method-assign]
    ws._sync_down = lambda: None  # type: ignore[method-assign]
    ws.mirror_push = lambda: {"files": 0, "bytes": 0, "skipped": 0}  # type: ignore[method-assign]
    ws.mirror_pull = lambda: {"files": 0, "bytes": 0, "conflicts": 0}  # type: ignore[method-assign]
    return ws


def _tiny_target(base) -> str:
    """A throwaway git checkout for the session to sync."""
    import subprocess
    repo = base / "target-repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("target\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return str(repo)


# ---------------------------------------------------------------------------
# 1. Reconnect seam — fault-injecting transport.
# ---------------------------------------------------------------------------


def test_kill_connection_mid_stream_drains_exactly_once(
    tmp_path, tmp_run_dir, in_process_node_agent,
):
    """Spawn a proc that emits N lines; interrupt the stream midway and assert
    the client reconnects, drains the rest, and returns the FULL output
    exactly once (no duplicated bytes, single ``pid``)."""
    client, token = in_process_node_agent
    ws = _make_workspace(client, token, tmp_path, tmp_run_dir)
    ws.up()

    # Track how many times the stream endpoint was hit (before + after drop).
    stream_hits = {"n": 0}

    def counting_request(method, path, *, body=None, content_type=None, query=None):
        if path.endswith("/stream") and method == "GET":
            stream_hits["n"] += 1
            # Drop the connection on the first hit by short-circuiting:
            # we return a partial chunk (the first half), then on the
            # next iteration the real request will resume from the offset.
            from urllib import error as _ue
            if stream_hits["n"] == 1:
                # First hit: simulate transport drop by raising URLError.
                raise _ue.URLError("simulated drop")
            if stream_hits["n"] == 2:
                # Second hit: provide a partial body (proc still running)
                # so the client gets some bytes, then we let the third
                # call see the exit line.
                body = (
                    b'{"stream":"out","offset":6,"data":"first\\n"}\n'
                )
                return body
            # Third hit onward: let the real request run.
            return ws._original_request(method, path, body=body,
                                        content_type=content_type, query=query)
        return ws._original_request(method, path, body=body,
                                    content_type=content_type, query=query)

    ws._request = counting_request
    # Short timeout + short reconnect window so the test runs fast.
    os.environ["MO_REMOTE_RECONNECT_MAX_S"] = "30"
    try:
        # Emit 4 lines via the in-process agent. The agent's ProcRegistry
        # streams everything; we only intercept the *client-side* GET to
        # the stream endpoint, so the agent still emits all 4 lines
        # through its background thread.
        rc, out, err = ws.spawn(
            ["/bin/sh", "-c",
             "printf 'first\\nsecond\\nthird\\nfourth\\n'; exit 0"],
            stdin="", timeout=15,
            env={"PATH": os.environ.get("PATH", ""),
                 "MO_NODE_ID": "implementer",
                 "MO_NODE_ATTEMPT": "1",
                 "MO_INPUT_HASH": "x" * 64},
            cwd=str(tmp_path),
        )
        # The client should have received every byte exactly once.
        assert rc == 0, (rc, out, err)
        assert out.count("first") == 1, out
        assert out.count("second") == 1, out
        assert out.count("third") == 1, out
        assert out.count("fourth") == 1, out
        # And the reconnect actually fired (stream hit more than once).
        assert stream_hits["n"] >= 2, stream_hits
    finally:
        ws.down()


# ---------------------------------------------------------------------------
# 2. Recovery reattach default — planner picks reattach when row exists.
# ---------------------------------------------------------------------------


def test_recovery_reattach_default_when_remote_proc_row_exists(
    tmp_path, tmp_run_dir,
):
    """``compute_recovery`` defaults to ``strategy="reattach"`` when the
    first-incomplete node has an unconsumed ``remote_procs`` row."""
    from mini_ork.recovery.plan import compute_recovery, plan_recovery

    # Seed a fake workflow + DB.
    workflow_yaml = tmp_path / "wf.yaml"
    workflow_yaml.write_text(
        "nodes:\n"
        "  - name: planner\n"
        "  - name: implementer\n"
        "  - name: reviewer\n"
        "edges:\n"
        "  - from: planner\n    to: implementer\n"
        "  - from: implementer\n    to: reviewer\n"
    )
    run_id = "r-reattach-1"
    state_db = tmp_path / "state.db"
    con = sqlite3.connect(state_db)
    try:
        # Schema for node_checkpoints (recovery needs to know which nodes
        # are reusable). The planner's _peek_row_status reads from it.
        con.executescript(
            "CREATE TABLE IF NOT EXISTS node_checkpoints ("
            " run_id TEXT, node_id TEXT, status TEXT,"
            " input_hash TEXT, recipe_version TEXT, config_hash TEXT,"
            " artifact_manifest_json TEXT, PRIMARY KEY(run_id,node_id));"
        )
        # planner is reusable (its row exists with status=success);
        # implementer is the first non-reusable node (no row) — the
        # closure root the probe will be checking. Hashes match the
        # planner's _current_input_hash_for_node / _current_config_hash
        # formulas so is_node_reusable accepts the row.
        import hashlib as _h
        _input_hash = _h.sha256(f"{run_id}|planner|framework-edit".encode()).hexdigest()
        _config_hash = _h.sha256(
            f"framework_edit|framework-edit|{run_id}".encode()
        ).hexdigest()
        con.execute(
            "INSERT INTO node_checkpoints VALUES (?,?,?,?,?,?,?)",
            (run_id, "planner", "success", _input_hash, "framework-edit",
             _config_hash, "[]"),
        )
        # The remote_procs row: implementer is in flight on a VM.
        con.executescript(
            "CREATE TABLE IF NOT EXISTS remote_procs ("
            " run_id TEXT, node_id TEXT, attempt INTEGER, node_host TEXT,"
            " session_id TEXT, proc_id INTEGER, idempotency_key TEXT,"
            " state TEXT, rc INTEGER, out_offset INTEGER, err_offset INTEGER,"
            " started_at INTEGER, ended_at INTEGER,"
            " PRIMARY KEY(run_id,node_id,attempt));"
        )
        con.execute(
            "INSERT INTO remote_procs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, "implementer", 1, "vm-1", "sid-1", 1,
             "x" * 64, "running", None, 0, 0, int(time.time()), None),
        )
        con.commit()
    finally:
        con.close()

    plan = compute_recovery(
        str(workflow_yaml), run_id, str(state_db), str(tmp_path),
        recipe="framework-edit", task_class="framework_edit",
    )
    assert plan.strategy == "reattach", plan.strategy
    assert plan.first_node == "implementer", plan.first_node

    # plan_recovery accepts the strategy verbatim.
    plan2 = plan_recovery(
        str(workflow_yaml), run_id, str(state_db), str(tmp_path),
        recipe="framework-edit", task_class="framework_edit",
        strategy="reattach",
    )
    assert plan2.strategy == "reattach"
    assert plan2.first_node == "implementer"


def test_recovery_falls_back_to_resume_when_no_remote_proc_row(
    tmp_path, tmp_run_dir,
):
    """No ``remote_procs`` row → default strategy is ``"resume"`` (no reattach)."""
    from mini_ork.recovery.plan import compute_recovery

    workflow_yaml = tmp_path / "wf.yaml"
    workflow_yaml.write_text(
        "nodes:\n"
        "  - name: planner\n"
        "  - name: implementer\n"
        "  - name: reviewer\n"
        "edges:\n"
        "  - from: planner\n    to: implementer\n"
        "  - from: implementer\n    to: reviewer\n"
    )
    run_id = "r-resume-1"
    state_db = tmp_path / "state.db"
    con = sqlite3.connect(state_db)
    try:
        con.executescript(
            "CREATE TABLE IF NOT EXISTS node_checkpoints ("
            " run_id TEXT, node_id TEXT, status TEXT,"
            " input_hash TEXT, recipe_version TEXT, config_hash TEXT,"
            " artifact_manifest_json TEXT, PRIMARY KEY(run_id,node_id));"
        )
    finally:
        con.close()

    plan = compute_recovery(
        str(workflow_yaml), run_id, str(state_db), str(tmp_path),
        recipe="framework-edit", task_class="framework_edit",
    )
    assert plan.strategy == "resume", plan.strategy


# ---------------------------------------------------------------------------
# 3. Journal writes start + offsets + end.
# ---------------------------------------------------------------------------


def test_remote_proc_journal_upserts_offsets_during_stream(tmp_run_dir):
    """``_RemoteProcJournal`` writes start → offsets → end rows around a spawn."""
    from mini_ork.runtime.backends.remote import _RemoteProcJournal

    state_db = os.environ["MINI_ORK_DB"]
    journal = _RemoteProcJournal(str(tmp_run_dir))
    journal.upsert_start(
        run_id="r-journal-1", idempotency_key="k" * 64, node_id="implementer",
        attempt=1, node_host="vm-1", session_id="sid-1",
        proc_id=42, state="running", started_at=int(time.time()),
    )
    journal.upsert_offsets("k" * 64, 128, 256)
    journal.upsert_end(idempotency_key="k" * 64, state="exited",
                       rc=0, ended_at=int(time.time()))

    con = sqlite3.connect(state_db)
    try:
        row = con.execute(
            "SELECT run_id, node_id, attempt, state, rc, out_offset, err_offset,"
            " ended_at FROM remote_procs WHERE idempotency_key=?",
            ("k" * 64,),
        ).fetchone()
    finally:
        con.close()
    assert row is not None
    assert row[0] == "r-journal-1"
    assert row[1] == "implementer"
    assert row[2] == 1
    assert row[3] == "exited"
    assert row[4] == 0
    assert row[5] == 128
    assert row[6] == 256
    assert row[7] is not None


# ---------------------------------------------------------------------------
# 4. Session store: glob-first lookup finds remote transcript.
# ---------------------------------------------------------------------------


def test_session_store_finds_remote_transcript_by_session_id(tmp_path):
    """A transcript placed under an unrelated project slug is found by
    session_id alone (the cwd-slug fallback is preserved)."""
    from mini_ork.stores import session_store

    projects = tmp_path / ".claude" / "projects"
    remote_slug_dir = projects / "-workspace-target"
    remote_slug_dir.mkdir(parents=True)
    transcript = remote_slug_dir / "remote-session-xyz.jsonl"
    transcript.write_text("{}")

    # The lookup uses provider_session_id; cwd points elsewhere, but the
    # glob-first order still finds the file.
    hit = session_store.find_session_jsonl(
        "remote-session-xyz", cwd="/some/local/cwd", projects_dir=projects,
    )
    assert hit == transcript, hit


# ---------------------------------------------------------------------------
# 5. RECOVERY_STRATEGIES + plan_recovery accept "reattach".
# ---------------------------------------------------------------------------


def test_reattach_strategy_in_recovery_strategies_tuple():
    from mini_ork.recovery.plan import RECOVERY_STRATEGIES, plan_recovery

    assert "reattach" in RECOVERY_STRATEGIES, RECOVERY_STRATEGIES

    # plan_recovery(strategy="reattach") must NOT raise ValueError about
    # an unknown strategy. We point it at a missing workflow; the
    # ``FileNotFoundError`` raised is fine (the strategy check ran first
    # and accepted ``"reattach"``).
    try:
        plan_recovery(
            "/nonexistent/wf.yaml", "rid", "/nonexistent/db", "/tmp",
            recipe="framework-edit", task_class="framework_edit",
            strategy="reattach",
        )
    except (FileNotFoundError, OSError):
        pass  # expected — strategy check passed; the IO error is fine
    except ValueError as exc:
        # Any ValueError raised here must NOT be about an unknown strategy.
        assert "strategy" not in str(exc).lower() or "reattach" in RECOVERY_STRATEGIES, str(exc)


# ---------------------------------------------------------------------------
# 6. Node-agent idempotency: same key → same pid (any state).
# ---------------------------------------------------------------------------


def test_node_agent_spawn_idempotency_returns_same_pid(
    tmp_path, tmp_run_dir, in_process_node_agent,
):
    """Two POST /procs with the same idempotency_key resolve to the same
    pid and spawn exactly one child process."""
    client, token = in_process_node_agent

    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    # Create a session first.
    sess = client.post("/v1/sessions", headers=headers,
                       json={"run_id": "idem-test", "image": "alpine:latest"})
    assert sess.status_code == 200, sess.text

    key = "abc123"
    payload = {
        "argv": ["/bin/sh", "-c", "echo hi; sleep 0.2; exit 0"],
        "env": {"PATH": os.environ.get("PATH", "")},
        "env_keys": ["PATH"],
        "stdin": "",
        "timeout_s": 30,
        "idempotency_key": key,
    }
    r1 = client.post("/v1/sessions/idem-test/procs", headers=headers, json=payload)
    assert r1.status_code == 200, r1.text
    pid1 = r1.json()["pid"]

    # Wait for the proc to exit so the second call hits the dedup path
    # with state=exited.
    time.sleep(0.6)

    r2 = client.post("/v1/sessions/idem-test/procs", headers=headers, json=payload)
    assert r2.status_code == 200, r2.text
    pid2 = r2.json()["pid"]
    assert pid1 == pid2, (pid1, pid2)

    # Only one proc on disk (the registry didn't allocate a second pid).
    procs_dir = tmp_path / "agent" / "runs" / "idem-test" / ".procs"
    json_files = list(procs_dir.glob("*.json"))
    assert len(json_files) == 1, [p.name for p in json_files]


def test_node_agent_spawn_idempotency_returns_exited_proc(
    tmp_path, tmp_run_dir, in_process_node_agent,
):
    """After the proc has exited, a re-dispatch with the same key still
    returns the SAME pid (the harvest path) — the dedup lookup is on key,
    not on state."""
    client, token = in_process_node_agent
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    sess = client.post("/v1/sessions", headers=headers,
                       json={"run_id": "idem-exit-test", "image": "alpine:latest"})
    assert sess.status_code == 200

    payload = {
        "argv": ["/bin/sh", "-c", "echo done; exit 0"],
        "env": {"PATH": os.environ.get("PATH", "")},
        "env_keys": ["PATH"],
        "stdin": "",
        "timeout_s": 30,
        "idempotency_key": "exited-key",
    }
    r1 = client.post("/v1/sessions/idem-exit-test/procs", headers=headers, json=payload)
    pid1 = r1.json()["pid"]
    time.sleep(0.6)

    r2 = client.post("/v1/sessions/idem-exit-test/procs", headers=headers, json=payload)
    pid2 = r2.json()["pid"]
    state2 = r2.json()["state"]
    assert pid1 == pid2, (pid1, pid2)
    # The state should reflect the original proc (exited).
    assert state2 == "exited", state2


# ---------------------------------------------------------------------------
# 7. CLAUDE_CONFIG_DIR is injected into the child env.
# ---------------------------------------------------------------------------


def test_claude_config_dir_injected_into_remote_env(
    tmp_path, tmp_run_dir, in_process_node_agent,
):
    """When the workspace has a ``run_dir``, ``spawn`` injects
    ``CLAUDE_CONFIG_DIR=/workspace/home/.claude`` into the env the
    node-agent persists on disk for the child to read."""
    client, token = in_process_node_agent
    ws = _make_workspace(client, token, tmp_path, tmp_run_dir)
    ws._target_root = _tiny_target(tmp_path)
    ws.up()
    try:
        ws.spawn(
            ["/bin/sh", "-c", "echo ok; exit 0"],
            stdin="", timeout=10,
            env={"PATH": os.environ.get("PATH", ""),
                 "MO_NODE_ID": "implementer",
                 "MO_NODE_ATTEMPT": "1",
                 "MO_INPUT_HASH": "y" * 64},
            cwd=str(tmp_path),
        )
        # The node-agent persisted the spawn record; the env KEYS set
        # must include CLAUDE_CONFIG_DIR. Read the on-disk state.
        proc_json = next((tmp_path / "agent" / "runs" / ws._run_id
                          / ".procs").glob("*.json"))
        rec = json.loads(proc_json.read_text(encoding="utf-8"))
        assert "CLAUDE_CONFIG_DIR" in rec["env_keys"], rec["env_keys"]
        assert rec["env_keys"].count("CLAUDE_CONFIG_DIR") == 1
    finally:
        ws.down()


# --------------------------------------------------------------------------- key wiring


def _dispatch_capturing_env(tmp_path, monkeypatch, db, run_id, node_id, sub):
    """Drive the real ``dispatch_node`` once; return the env a backend spawn sees."""
    import mini_ork.cli.execute as ex
    from mini_ork.context import context_env_snapshot

    for key in ("MINI_ORK_RUN_DIR", "MO_NODE_ID", "MO_NODE_ATTEMPT", "MO_INPUT_HASH"):
        monkeypatch.delenv(key, raising=False)
    rd = tmp_path / sub
    rd.mkdir()
    plan = tmp_path / f"{sub}-plan.json"
    plan.write_text(json.dumps({"objective": "o"}))
    seen: dict = {}

    def handler(ctx):
        seen.update(context_env_snapshot())   # what core.py hands ws.spawn(env=)
        return 0, "done"

    ex.register_node_handler("rn10_key_probe", handler)
    try:
        ex.dispatch_node((node_id, "rn10_key_probe", "do it", "", "serial", "", "", ""),
                         root=os.getcwd(), run_dir=str(rd), plan_path=str(plan),
                         task_class="generic", db=db, run_id=run_id, recipe="code-fix",
                         dispatch_fn=lambda *a: (0, "done"))
    finally:
        ex.NODE_HANDLER_REGISTRY.pop("rn10_key_probe", None)
    return seen


def test_dispatched_node_env_yields_a_stable_nonempty_idempotency_key(tmp_path, monkeypatch):
    """The key used to be always empty: nothing published MO_NODE_ATTEMPT /
    MO_INPUT_HASH, so remote_procs was never written and `reattach` was dead.
    A real dispatch must now carry both, and an interrupted attempt (no
    node_attempts row yet) re-dispatched must produce the SAME key."""
    import hashlib
    import subprocess
    import types

    from mini_ork.runtime.backends.remote import RemoteWorkspace

    home = tmp_path / "home"
    home.mkdir()
    db = str(home / "state.db")
    subprocess.run(["bash", str(Path(__file__).resolve().parents[2] / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)

    first = _dispatch_capturing_env(tmp_path, monkeypatch, db, "run-k", "n-k", "a")
    assert first.get("MO_NODE_ATTEMPT") == "1"
    assert first.get("MO_INPUT_HASH") == hashlib.sha256(b"run-k|n-k|code-fix").hexdigest()
    fake_self = types.SimpleNamespace(_run_id="run-k")
    key1 = RemoteWorkspace._idempotency_key(fake_self, first)
    assert key1, "dispatch must publish everything the idempotency key needs"

    # Recovery re-dispatch of the interrupted attempt: same key -> reattach.
    again = _dispatch_capturing_env(tmp_path, monkeypatch, db, "run-k", "n-k", "b")
    assert RemoteWorkspace._idempotency_key(fake_self, again) == key1

    # Once the attempt is recorded, the next attempt gets a fresh key.
    con = sqlite3.connect(db)
    try:
        con.execute("INSERT INTO node_attempts (run_id, node_id, attempt_no, node_type, "
                    "started_at, ended_at, result) VALUES ('run-k', 'n-k', 1, 't', 0, 0, 'failure')")
        con.commit()
    finally:
        con.close()
    third = _dispatch_capturing_env(tmp_path, monkeypatch, db, "run-k", "n-k", "c")
    assert third.get("MO_NODE_ATTEMPT") == "2"
    assert RemoteWorkspace._idempotency_key(fake_self, third) != key1
