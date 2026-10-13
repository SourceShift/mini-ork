"""Cloud-providers A2: the teardown registry.

Every teardown path — the TTL sweep, ``kill_run``'s marker reap, the
``sandbox-gc`` CLI — dispatches through a registry instead of naming backends,
so a new backend or cloud node provider is torn down without editing a teardown
module. One test per Acceptance bullet of
``kickoffs/cloud-providers-a2-teardown-registry.md``, each driving the
PRODUCTION entrypoint (``sandbox_gc.main``, ``reap_sandboxes``, ``kill_run``,
``reap_by_marker``) rather than a private helper.

No docker daemon and no cloud are needed: the docker seam is ``sandbox_reaper._run``
and the remote marker reaper is driven against the in-process node-agent the
``test_remote_live_and_kill.py`` fixture uses (real uvicorn socket, real
``RemoteWorkspace`` over its real urllib transport).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from mini_ork.runtime import sandbox_reaper


# ---------------------------------------------------------------------------
# Fixtures: a docker-free host and two copy-on-write registries.
# ---------------------------------------------------------------------------


class _Which:
    """Stand-in for the reaper module's ``shutil``: only ``which`` is used.

    Patched onto the MODULE attribute (not the stdlib ``shutil``) so a test
    never reaches into another library's lookup of executables.
    """

    def __init__(self, found: str | None) -> None:
        self._found = found

    def which(self, name: str) -> str | None:  # noqa: ARG002 — docker is the only caller
        return self._found


@pytest.fixture(autouse=True)
def _isolated_registries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Copy-on-write the process-global registries, and hide the docker CLI.

    Both registries are module globals mutated by ``register_*``; a test that
    registers ``plugbox`` must not leak into another test (or into the
    pre-existing ``reap_sandboxes`` tests, which assert the built-in key set).
    With no docker CLI the built-in docker/microvm sweeps are no-ops, so a test
    observes only the backend under test.
    """
    monkeypatch.setattr(sandbox_reaper, "_SWEEP_REAPERS", dict(sandbox_reaper._SWEEP_REAPERS))
    monkeypatch.setattr(sandbox_reaper, "_MARKER_REAPERS", dict(sandbox_reaper._MARKER_REAPERS))
    monkeypatch.setattr(sandbox_reaper, "shutil", _Which(None))


def _run_dir(tmp_path: Path, name: str = "run-x") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True)
    return d


def _write_marker(run_dir: Path, payload: dict[str, Any]) -> Path:
    marker = run_dir / ".workspace-session.json"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    return marker


# ---------------------------------------------------------------------------
# Acceptance: a reaper registered under a new name is swept by name and by
# "all", and shows up in the CLI's --backend choices. No teardown module was
# edited to add it.
# ---------------------------------------------------------------------------


def test_plugbox_reaper_is_swept_by_all_and_by_name() -> None:
    sandbox_reaper.register_sandbox_reaper(
        "plugbox", lambda *, max_age_s, dry_run: ["pb-1"]
    )
    assert "plugbox" in sandbox_reaper.known_sandbox_reapers()

    everything = sandbox_reaper.reap_sandboxes(backend="all", max_age_s=10)
    assert everything["plugbox"] == ["pb-1"]
    assert everything["docker"] == []  # no docker CLI on this host (fixture)
    assert everything["microvm"] == []

    only_plugbox = sandbox_reaper.reap_sandboxes(backend="plugbox", max_age_s=10)
    assert only_plugbox == {"plugbox": ["pb-1"]}


def test_plugbox_reaper_reaches_the_sandbox_gc_entrypoint(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``sandbox_gc.main`` — the CLI entrypoint — lists and runs the new name."""
    from mini_ork.cli import sandbox_gc

    sandbox_reaper.register_sandbox_reaper(
        "plugbox", lambda *, max_age_s, dry_run: ["pb-1"]
    )

    with pytest.raises(SystemExit) as exc:
        sandbox_gc.main(["--help"], str(tmp_path))
    assert exc.value.code == 0
    assert "plugbox" in capsys.readouterr().out  # appears in the choices

    rc = sandbox_gc.main(["--backend", "plugbox", "--dry-run"], str(tmp_path))
    assert rc == 0
    assert "plugbox: dry-run: would reap 1 (pb-1)" in capsys.readouterr().out


def test_unknown_backend_still_raises_and_names_both_sets() -> None:
    with pytest.raises(ValueError) as exc:
        sandbox_reaper.reap_sandboxes(backend="nope")
    assert "docker" in str(exc.value)
    assert "daytona" in str(exc.value)  # the node-provider set is named too


# ---------------------------------------------------------------------------
# Acceptance: node providers' results are merged into "all"; a provider whose
# reap raises reports [].
# ---------------------------------------------------------------------------


def _fake_providers(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Copy-on-write the node-provider registry so a test's fake cannot leak."""
    from mini_ork.remote.providers import base as providers

    monkeypatch.setattr(providers, "_PROVIDERS", dict(providers._PROVIDERS))
    return providers


def test_registered_node_provider_is_swept_by_all_and_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = _fake_providers(monkeypatch)

    class FakeProvider:
        name = "fakecloud"

        def reap(self, *, max_age_s: int, dry_run: bool, env: Any) -> list[str]:
            assert env is not None  # the sweep passes an env mapping through
            return ["sb-1", "sb-2"]

    providers.register_node_provider("fakecloud", FakeProvider)

    everything = sandbox_reaper.reap_sandboxes(backend="all", max_age_s=10)
    assert everything["fakecloud"] == ["sb-1", "sb-2"]

    only_provider = sandbox_reaper.reap_sandboxes(backend="fakecloud", max_age_s=10)
    assert only_provider == {"fakecloud": ["sb-1", "sb-2"]}


def test_raising_node_provider_reports_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    providers = _fake_providers(monkeypatch)

    class BoomProvider:
        name = "boomcloud"

        def reap(self, *, max_age_s: int, dry_run: bool, env: Any) -> list[str]:
            raise RuntimeError("cloud API down")

    providers.register_node_provider("boomcloud", BoomProvider)

    everything = sandbox_reaper.reap_sandboxes(backend="all", max_age_s=10)
    assert everything["boomcloud"] == []
    # ...and the rest of the sweep is unaffected (fail-open).
    assert everything["docker"] == []


# ---------------------------------------------------------------------------
# Acceptance: reap_by_marker dispatches to the registered hook with the parsed
# marker and the run dir, then unlinks the marker (dry_run keeps it).
# ---------------------------------------------------------------------------


def test_reap_by_marker_dispatches_registered_hook(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def plugbox_hook(info: dict, run_dir: Path, *, dry_run: bool = False) -> list[str]:
        seen.update(info=info, run_dir=run_dir, dry_run=dry_run)
        return ["pb-9"]

    sandbox_reaper.register_marker_reaper("plugbox", plugbox_hook)
    run_dir = _run_dir(tmp_path)
    marker = _write_marker(
        run_dir, {"backend": "plugbox", "session_id": "pb-9", "run_id": "run-x"}
    )

    assert sandbox_reaper.reap_by_marker(run_dir) == ["pb-9"]
    assert seen["info"]["session_id"] == "pb-9"  # the PARSED marker
    assert seen["run_dir"] == run_dir
    assert seen["dry_run"] is False
    assert not marker.exists()  # reaped marker is unlinked


def test_reap_by_marker_dry_run_keeps_the_marker(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def plugbox_hook(info: dict, run_dir: Path, *, dry_run: bool = False) -> list[str]:
        seen["dry_run"] = dry_run
        return ["pb-9"]

    sandbox_reaper.register_marker_reaper("plugbox", plugbox_hook)
    run_dir = _run_dir(tmp_path)
    marker = _write_marker(run_dir, {"backend": "plugbox", "session_id": "pb-9"})

    assert sandbox_reaper.reap_by_marker(run_dir, dry_run=True) == ["pb-9"]
    assert seen["dry_run"] is True
    assert marker.exists()


def test_reap_by_marker_without_a_hook_unlinks_and_returns_empty(tmp_path: Path) -> None:
    """``local`` (and any unknown backend) keeps the pre-registry behavior."""
    run_dir = _run_dir(tmp_path)
    marker = _write_marker(run_dir, {"backend": "local", "session_id": ""})

    assert sandbox_reaper.reap_by_marker(run_dir) == []
    assert not marker.exists()


def test_reap_by_marker_hook_that_raises_never_escapes(tmp_path: Path) -> None:
    def angry_hook(info: dict, run_dir: Path, *, dry_run: bool = False) -> list[str]:
        raise RuntimeError("backend exploded")

    sandbox_reaper.register_marker_reaper("plugbox", angry_hook)
    run_dir = _run_dir(tmp_path)
    marker = _write_marker(run_dir, {"backend": "plugbox", "session_id": "pb-9"})

    assert sandbox_reaper.reap_by_marker(run_dir) == []
    assert not marker.exists()  # still unlinked: nothing left to retry


# ---------------------------------------------------------------------------
# Acceptance: the docker marker path issues ``docker rm -f <cid>`` through the
# single ``_run`` seam (no daemon).
# ---------------------------------------------------------------------------


def test_docker_marker_issues_rm_f_through_the_run_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sandbox_reaper, "_run", fake_run)
    monkeypatch.setattr(sandbox_reaper, "shutil", _Which("/usr/bin/docker"))
    run_dir = _run_dir(tmp_path)
    marker = _write_marker(run_dir, {"backend": "docker", "session_id": "cid-42"})

    assert sandbox_reaper.reap_by_marker(run_dir) == ["cid-42"]
    assert [c for c in calls if c[:3] == ["docker", "rm", "-f"]] == [
        ["docker", "rm", "-f", "cid-42"]
    ]
    assert not marker.exists()


# ---------------------------------------------------------------------------
# Acceptance: kill_run (the production entrypoint, not the helper) dispatches
# the marker through the registry.
# ---------------------------------------------------------------------------


def _seed_task_run(home: Path, run_id: str) -> Path:
    """A real state.db with one controllable row (kill_run writes back to it)."""
    from mini_ork.stores import migrate
    from mini_ork.web.db import StateDB

    home.mkdir(parents=True, exist_ok=True)
    dbp = home / "state.db"
    rc, _out, err = migrate.init_db(
        db=str(dbp), root=os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    )
    assert rc == 0, err
    con = sqlite3.connect(dbp)
    try:
        cols = {r[1]: r for r in con.execute("PRAGMA table_info(task_runs)")}
        row: dict[str, Any] = {"id": run_id, "status": "executing"}
        for name, info in cols.items():  # satisfy any other NOT NULL column
            if name not in row and info[3] and info[4] is None and not info[5]:
                row[name] = 0 if "INT" in (info[2] or "").upper() else ""
        con.execute(
            f"INSERT INTO task_runs ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
            list(row.values()),
        )
        con.commit()
    finally:
        con.close()
    assert StateDB(dbp).row("SELECT id FROM task_runs WHERE id = ?", (run_id,))
    return dbp


def test_kill_run_dispatches_the_marker_through_the_registry(tmp_path: Path) -> None:
    from mini_ork.web import control
    from mini_ork.web.db import StateDB

    home = tmp_path / "home"
    run_id = "kill-run"
    dbp = _seed_task_run(home, run_id)
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    marker = _write_marker(
        run_dir,
        {"backend": "remote", "session_id": "s-1", "run_id": run_id,
         "node": {"name": "n", "url": "http://127.0.0.1:1"}, "token_env": "MO_NODE_TOKEN"},
    )

    seen: list[dict] = []
    sandbox_reaper.register_marker_reaper(
        "remote",
        lambda info, rd, *, dry_run=False: (seen.append(info), ["s-1"])[1],
    )

    result = control.kill_run(home, StateDB(dbp), run_id)

    assert result["ok"] is True
    assert result["session_marker_reaped"] is True
    assert seen and seen[0]["backend"] == "remote"  # the hook ran
    assert not marker.exists()
    assert StateDB(dbp).row("SELECT status FROM task_runs WHERE id = ?", (run_id,))[
        "status"
    ] == "failed"


def test_kill_run_without_a_marker_reaps_nothing(tmp_path: Path) -> None:
    from mini_ork.web import control
    from mini_ork.web.db import StateDB

    home = tmp_path / "home"
    run_id = "kill-run"
    dbp = _seed_task_run(home, run_id)
    (home / "runs" / run_id).mkdir(parents=True)

    result = control.kill_run(home, StateDB(dbp), run_id)

    assert result["ok"] is True
    assert result["session_marker_reaped"] is False


# ---------------------------------------------------------------------------
# Acceptance: the remote marker reaper, driven against the in-process
# node-agent fixture, kills the session's procs and deletes the session —
# exactly what the old inline branch in kill_run did.
# ---------------------------------------------------------------------------


def _start_node_agent(state_dir: Path) -> tuple[Any, threading.Thread, int]:
    """A real uvicorn node-agent in ``--runtime host`` mode on a free port."""
    from mini_ork.remote.node_agent.app import create_app

    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = int(sk.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(state_dir=state_dir, token_env="MO_NODE_TOKEN", runtime="host"),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "node-agent did not start"
    return server, thread, port


def _tiny_git_repo(base: Path) -> Path:
    repo = base / "target-repo"
    if repo.exists():
        return repo
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("target\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def test_remote_marker_reaper_kills_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mini_ork.runtime.backends.remote import (
        RemoteWorkspace,
        _NodeRef,
        _reap_remote_marker,
    )

    monkeypatch.setenv("MO_NODE_TOKEN", "real-socket-token")
    monkeypatch.setenv("MO_REMOTE_ALLOW_DIRTY_ENGINE", "1")
    server, thread, port = _start_node_agent(tmp_path / "agent-state")
    run_id = f"kill-{uuid.uuid4().hex[:8]}"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_RUN_ID", run_id)

    url = f"http://127.0.0.1:{port}"
    node = _NodeRef(name="real", url=url, token="real-socket-token", max_sessions=1)

    def _workspace(retries: int) -> RemoteWorkspace:
        return RemoteWorkspace(
            node=node,
            run_id=run_id,
            image="alpine:latest",
            drive_root=str(tmp_path),
            engine_root=os.environ.get("MINI_ORK_ROOT") or os.getcwd(),
            target_root=str(_tiny_git_repo(tmp_path)),
            token_env="MO_NODE_TOKEN",
            retries=retries,
            run_dir=str(run_dir),
        )

    ws = _workspace(retries=3)
    try:
        ws.up()
        sid = ws.session_id()
        assert sid, "up() must publish a session id"

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(
            ws.spawn,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin="",
            timeout=60.0,
            env={"PATH": os.environ.get("PATH", "")},
            cwd=str(run_dir),
        )
        journal = run_dir / ".remote-pids.jsonl"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if journal.exists() and '"pid"' in journal.read_text():
                break
            time.sleep(0.05)
        else:
            pytest.fail("the spawn never recorded its pid in the journal")

        marker = {
            "backend": "remote",
            "session_id": sid,
            "run_id": run_id,
            "node": {"name": "real", "url": url},
            "token_env": "MO_NODE_TOKEN",
        }
        # Drive the hook the registry resolves for a "remote" marker — the same
        # call control.kill_run now makes through reap_by_marker.
        reaped = _reap_remote_marker(marker, run_dir)
        assert reaped == [sid]

        # The sleep 30 died under the reap's 10 s budget...
        rc, _out, _err = fut.result(timeout=20)
        assert rc != 0, "the reaped proc should not have exited cleanly"
        # ...and the node session itself is gone.
        probe = _workspace(retries=1)
        probe._sid = sid
        assert probe._session_exists() is False
    finally:
        try:
            ws.down()
        except Exception:  # noqa: BLE001 — teardown of a killed session
            pass
        server.should_exit = True
        thread.join(timeout=10)


def test_remote_marker_reaper_dry_run_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mini_ork.runtime.backends.remote import _reap_remote_marker

    monkeypatch.setenv("MO_NODE_TOKEN", "t")
    run_dir = _run_dir(tmp_path)
    assert _reap_remote_marker(
        {"backend": "remote", "session_id": "s-9"}, run_dir, dry_run=True
    ) == ["s-9"]


# ---------------------------------------------------------------------------
# Acceptance: _extract_session_id uses session_id() when defined, else the
# private-attribute fallback — and no teardown module compares backend names.
# ---------------------------------------------------------------------------


def test_extract_session_id_prefers_a_callable_session_id() -> None:
    from mini_ork.runtime.workspace_session import _extract_session_id

    class WithMethod:
        def session_id(self) -> str:
            return "sid-1"

        _cid = "cid-9"

    assert _extract_session_id(WithMethod()) == "sid-1"


def test_extract_session_id_falls_back_to_private_attrs() -> None:
    from mini_ork.runtime.workspace_session import _extract_session_id

    class Dockerish:
        _cid = "cid-9"

    class Remoteish:
        _sid = "sid-9"

    class Named:
        _name = "mo-mvm-1"

    class Anonymous:
        pass

    assert _extract_session_id(Dockerish()) == "cid-9"
    assert _extract_session_id(Remoteish()) == "sid-9"
    assert _extract_session_id(Named()) == "mo-mvm-1"
    assert _extract_session_id(Anonymous()) == ""


def test_no_teardown_module_compares_a_backend_name() -> None:
    """The registry replaced every backend-name branch in the teardown path."""
    root = Path(__file__).resolve().parents[2]
    for rel in (
        "mini_ork/runtime/workspace_session.py",
        "mini_ork/runtime/sandbox_reaper.py",
        "mini_ork/web/control.py",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        assert 'backend == "docker"' not in text, rel
        assert 'backend == "remote"' not in text, rel


def test_fresh_process_sandbox_gc_all_sweeps_lazy_providers(tmp_path):
    """``sandbox-gc`` starts as a fresh process in which no provider module was
    imported; ``--backend all`` must still sweep the lazily-importable ones (a
    leaked cloud sandbox is what the sweep is for) — without loading any cloud
    SDK and without credentials (fail-open → 0)."""
    import os
    import subprocess
    import sys

    code = (
        "import sys\n"
        "from mini_ork.cli import sandbox_gc\n"
        "rc = sandbox_gc.main(['--dry-run', '--backend', 'all'], '.')\n"
        "print('SDK_LOADED' if 'daytona' in sys.modules else 'SDK_ABSENT')\n"
        "sys.exit(rc)\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("DAYTONA_")}
    env.update(MINI_ORK_HOME=str(tmp_path), MINI_ORK_SECRETS=str(tmp_path / "none.sh"),
               PATH="/usr/bin:/bin")          # no docker CLI → the docker sweep is a no-op
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "daytona: dry-run: would reap 0" in out.stdout
    assert "static: dry-run: would reap 0" in out.stdout
    assert out.stdout.strip().endswith("SDK_ABSENT")
