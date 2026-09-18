"""S1: the recursive child launches through the ``Workspace`` axis.

S1 of ``docs/architecture/cloud-swarm.md`` closes gap G1 (the child was a raw
``subprocess.run`` pinned to the parent's host) and G4 (``child_env`` was the
whole host environment). These tests drive ``spawn()`` IN-PROCESS so a fake
backend can be registered in the ``Workspace`` registry — no docker daemon, no
microVM SDK, no cloud credentials:

  (a) backend unset      → byte-parity host path (argv/cwd/``{**os.environ}``)
  (b) registered backend → up→spawn→down, mount-path kickoff/cwd, allowlisted
                           env, ``spawn-child.log`` trail, status from the rc
  (c) unknown backend    → loud ``ValueError``, no DB row, no child dir
  (d) ``MO_SHARED_DRIVE_ROOT`` above ``MINI_ORK_HOME`` → paths remapped
  (e) backend ``local``  → routes through the axis, keeps host paths + host env
  (f) a child path the drive cannot export → loud ``ValueError``, no DB row
  (g) ``_host_to_container`` boundary: root itself, a child, an escape
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import spawn as spawn_mod  # noqa: E402

INIT_SH = REPO / "db" / "init.sh"

# A parent env var that must NOT reach an isolated child: it matches neither the
# `MO_*`/provider prefixes nor the `*_API_KEY` suffix.
HOST_SECRET_VAR = "SPAWN_ISOLATION_HOST_SECRET"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def state_db(home):
    """Seed a temp state.db via db/init.sh (the recursive layer writes to it)."""
    dbp = str(home / "py.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\nstderr={r.stderr}")
    return dbp


@pytest.fixture
def env(home, state_db, monkeypatch, tmp_path):
    """Pin the paths `spawn()` and `mini_ork.orchestration.recursive` resolve."""
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", state_db)
    monkeypatch.delenv(spawn_mod.ENV_SANDBOX_BACKEND, raising=False)
    monkeypatch.delenv(spawn_mod.ENV_SHARED_DRIVE_ROOT, raising=False)
    monkeypatch.delenv(spawn_mod.ENV_SANDBOX_CLI, raising=False)
    monkeypatch.delenv(spawn_mod.ENV_SANDBOX_CHILD_TIMEOUT, raising=False)
    monkeypatch.delenv(HOST_SECRET_VAR, raising=False)
    return home


@pytest.fixture
def kickoff(tmp_path):
    p = tmp_path / "child.md"
    p.write_text("# child task\n", encoding="utf-8")
    return str(p)


class _FakeWorkspace:
    """Records the lifecycle a routed child launch drives."""

    def __init__(self, *, rc: int, stdout: str, stderr: str) -> None:
        self.rc, self.stdout, self.stderr = rc, stdout, stderr
        self.calls: list[str] = []
        self.spawn_kwargs: dict = {}

    def up(self) -> None:
        self.calls.append("up")

    def down(self) -> None:
        self.calls.append("down")

    def spawn(self, argv, *, stdin, timeout, env, cwd):
        self.calls.append("spawn")
        self.spawn_kwargs = {
            "argv": list(argv), "stdin": stdin, "timeout": timeout,
            "env": dict(env), "cwd": cwd,
        }
        return self.rc, self.stdout, self.stderr


@pytest.fixture
def fake_backend(monkeypatch):
    """Register a recording ``fakebox`` backend and select it.

    ``resolve_spawn_workspace`` builds an unknown-name backend through
    ``get_workspace(name, root=…)``, so the factory takes ``**kwargs``. The
    returned dict is mutable: set ``rc``/``stdout``/``stderr`` before calling
    ``spawn()`` to shape the child's outcome.
    """
    mod: dict = {"rc": 0, "stdout": "child ok\n", "stderr": ""}

    def factory(**kwargs):
        ws = _FakeWorkspace(rc=mod["rc"], stdout=mod["stdout"], stderr=mod["stderr"])
        mod["ws"] = ws
        mod["kwargs"] = kwargs
        return ws

    from mini_ork.runtime import sandbox as sandbox_mod

    monkeypatch.setitem(sandbox_mod._WORKSPACE_BACKENDS, "fakebox", factory)
    monkeypatch.setenv(spawn_mod.ENV_SANDBOX_BACKEND, "fakebox")
    return mod


def _rows(db: str, table: str) -> list[dict]:
    con = sqlite3.connect(db)
    try:
        cols = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
        return [dict(zip(cols, r)) for r in con.execute(f"SELECT {', '.join(cols)} FROM {table}")]
    finally:
        con.close()


@pytest.fixture
def parent(state_db):
    """Seed a `task_runs` row per parent id — `approve_spawn` refuses a missing parent."""

    def _seed(parent_id: str) -> str:
        con = sqlite3.connect(state_db)
        try:
            con.execute(
                """
                INSERT OR REPLACE INTO task_runs(
                  id, task_class, recipe, kickoff_path, status, created_at, updated_at
                ) VALUES (?, 'code_fix', NULL, ?, 'classified', 0, 0)
                """,
                (parent_id, "/tmp/k.md"),
            )
            con.commit()
        finally:
            con.close()
        return parent_id

    return _seed


# ─────────────────────────────────────────────────────────────────────────────
# (a) default path — byte-parity with the pre-S1 host launch
# ─────────────────────────────────────────────────────────────────────────────
def test_unset_backend_keeps_the_host_subprocess_path(env, kickoff, parent, monkeypatch):
    parent("p-host")
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="host ok\n", stderr="")

    monkeypatch.setattr(spawn_mod.subprocess, "run", fake_run)
    monkeypatch.setenv(HOST_SECRET_VAR, "host-only")

    res = spawn_mod.spawn(parent_run="p-host", kickoff=kickoff, child_run="c-host")

    base = Path(env) / "runs" / "p-host" / "children" / "c-host"
    assert seen["argv"] == [str(REPO / "bin" / "mini-ork"), "run", str(base / "kickoff.md")]
    assert seen["cwd"] == str(base / "worktree")
    assert seen["capture_output"] is True and seen["text"] is True
    # The whole host env rides, exactly as before S1.
    assert seen["env"][HOST_SECRET_VAR] == "host-only"
    assert seen["env"]["MINI_ORK_HOME"] == str(env)
    assert seen["env"]["MINI_ORK_DB"] == str(Path(env) / "py.db")
    assert seen["env"]["MINI_ORK_RUN_ID"] == "c-host"
    assert seen["env"]["MINI_ORK_PARENT_RUN_ID"] == "p-host"
    assert seen["env"]["MINI_ORK_ALLOW_CHILD_SPAWN"] == "0"
    assert res.exit_code == 0 and res.lines[-1] == "spawn_status=completed"
    assert (base / "kickoff.md").is_file()


def test_unset_backend_with_recipe_mirrors_the_kickoff_argument(env, kickoff, parent, monkeypatch):
    parent("p")
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(spawn_mod.subprocess, "run", fake_run)
    spawn_mod.spawn(parent_run="p", kickoff=kickoff, child_run="c", recipe="code-fix")

    assert seen["argv"] == [
        str(REPO / "bin" / "mini-ork"), "run", "code-fix",
        str(Path(env) / "runs" / "p" / "children" / "c" / "kickoff.md"),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# (b) the routed isolated path
# ─────────────────────────────────────────────────────────────────────────────
def test_isolated_child_runs_in_the_workspace(env, kickoff, parent, fake_backend, monkeypatch):
    parent("p-iso")
    fake_backend.update(rc=3, stdout="child out\n", stderr="child err\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv(HOST_SECRET_VAR, "hunter2")

    res = spawn_mod.spawn(parent_run="p-iso", kickoff=kickoff, child_run="c-iso")

    ws = fake_backend["ws"]
    # One-shot lifecycle: provisioned, used once, torn down.
    assert ws.calls == ["up", "spawn", "down"]
    # The child rides the drive, so its argv/cwd are the SANDBOX's view.
    assert ws.spawn_kwargs["argv"] == [
        "mini-ork", "run", "/workspace/runs/p-iso/children/c-iso/kickoff.md",
    ]
    assert ws.spawn_kwargs["cwd"] == "/workspace/runs/p-iso/children/c-iso/worktree"
    assert ws.spawn_kwargs["stdin"] == ""
    assert ws.spawn_kwargs["timeout"] == spawn_mod._DEFAULT_CHILD_TIMEOUT
    # The drive root handed to the factory is the run tree's parent.
    assert fake_backend["kwargs"]["root"] == str(env)

    child_env = ws.spawn_kwargs["env"]
    # …the run contract always rides (it is MINI_ORK_*, outside the MO_* list)
    assert child_env["MINI_ORK_HOME"] == "/workspace"
    assert child_env["MINI_ORK_DB"] == "/workspace/py.db"
    assert child_env["MINI_ORK_RUN_ID"] == "c-iso"
    assert child_env["MINI_ORK_PARENT_RUN_ID"] == "p-iso"
    assert child_env["MINI_ORK_ALLOW_CHILD_SPAWN"] == "0"
    # …while the ambient host env crosses only on the allowlist.
    assert child_env["ANTHROPIC_API_KEY"] == "sk-test"
    assert HOST_SECRET_VAR not in child_env
    assert "PATH" not in child_env and "HOME" not in child_env
    assert "MINI_ORK_ROOT" not in child_env

    # The child's rc still decides the spawn status, as on the host.
    assert res.exit_code == 3
    assert res.lines[-1] == "spawn_status=failed"
    assert [r["status"] for r in _rows(env_db(env), "run_spawns")] == ["failed"]

    # The failure trail survives the transport switch.
    log = (Path(env) / "runs" / "c-iso" / "spawn-child.log").read_text(encoding="utf-8")
    assert "$ mini-ork run /workspace/runs/p-iso/children/c-iso/kickoff.md" in log
    assert "exit_code=3" in log
    assert "child out" in log and "child err" in log


def test_isolated_child_completes_on_rc_zero(env, kickoff, parent, fake_backend):
    parent("p")
    res = spawn_mod.spawn(parent_run="p", kickoff=kickoff, child_run="c")

    assert res.exit_code == 0 and res.lines[-1] == "spawn_status=completed"
    assert [r["status"] for r in _rows(env_db(env), "run_spawns")] == ["completed"]
    assert fake_backend["ws"].spawn_kwargs["timeout"] == spawn_mod._DEFAULT_CHILD_TIMEOUT


def test_isolated_child_honours_the_configured_cli_and_timeout(env, kickoff, parent, fake_backend, monkeypatch):
    parent("p")
    monkeypatch.setenv(spawn_mod.ENV_SANDBOX_CLI, "/opt/mini-ork/bin/mini-ork")
    monkeypatch.setenv(spawn_mod.ENV_SANDBOX_CHILD_TIMEOUT, "90")

    spawn_mod.spawn(parent_run="p", kickoff=kickoff, child_run="c")

    ws = fake_backend["ws"]
    assert ws.spawn_kwargs["argv"][0] == "/opt/mini-ork/bin/mini-ork"
    assert ws.spawn_kwargs["timeout"] == 90.0
    # The knob is MO_*-shaped, so it also rides into the child.
    assert ws.spawn_kwargs["env"][spawn_mod.ENV_SANDBOX_CHILD_TIMEOUT] == "90"


def test_allow_child_spawn_crosses_to_the_isolated_child(env, kickoff, parent, fake_backend):
    parent("p")
    spawn_mod.spawn(parent_run="p", kickoff=kickoff, child_run="c", allow_child_spawn=1)

    assert fake_backend["ws"].spawn_kwargs["env"]["MINI_ORK_ALLOW_CHILD_SPAWN"] == "1"


# ─────────────────────────────────────────────────────────────────────────────
# (c) unknown backend — loud, before any side effect
# ─────────────────────────────────────────────────────────────────────────────
def test_unknown_backend_fails_loud_with_nothing_recorded(env, kickoff, state_db, monkeypatch):
    monkeypatch.setenv(spawn_mod.ENV_SANDBOX_BACKEND, "no-such-backend")

    with pytest.raises(ValueError, match="unknown workspace backend"):
        spawn_mod.spawn(parent_run="p", kickoff=kickoff, child_run="c")

    assert _rows(state_db, "run_spawns") == []
    assert not (Path(env) / "runs" / "p" / "children" / "c").exists()


def test_unknown_backend_exits_one_from_the_cli(env, kickoff, state_db, monkeypatch, capsys):
    monkeypatch.setenv(spawn_mod.ENV_SANDBOX_BACKEND, "no-such-backend")

    rc = spawn_mod.main(["--parent-run", "p", "--kickoff", kickoff, "--child-run", "c"])

    assert rc == 1
    assert "unknown workspace backend" in capsys.readouterr().err
    assert _rows(state_db, "run_spawns") == []


# ─────────────────────────────────────────────────────────────────────────────
# (d)/(f) the drive boundary
# ─────────────────────────────────────────────────────────────────────────────
def test_drive_root_above_home_remaps_the_child_paths(env, kickoff, parent, fake_backend, monkeypatch):
    parent("p")
    # The drive is the parent of MINI_ORK_HOME: the run tree is a SUBDIR of it.
    monkeypatch.setenv(spawn_mod.ENV_SHARED_DRIVE_ROOT, str(Path(env).parent))

    spawn_mod.spawn(parent_run="p", kickoff=kickoff, child_run="c")

    ws = fake_backend["ws"]
    assert fake_backend["kwargs"]["root"] == str(Path(env).parent)
    # The run tree is created directly on the drive…
    assert ws.spawn_kwargs["cwd"] == "/workspace/runs/p/children/c/worktree"
    assert ws.spawn_kwargs["argv"][-1] == "/workspace/runs/p/children/c/kickoff.md"
    # …while home/db live one level down, so they remap with the extra segment.
    assert ws.spawn_kwargs["env"]["MINI_ORK_HOME"] == "/workspace/home"
    assert ws.spawn_kwargs["env"]["MINI_ORK_DB"] == "/workspace/home/py.db"
    # The run tree really was created on the drive (host view).
    drive = Path(env).parent
    assert (drive / "runs" / "p" / "children" / "c" / "kickoff.md").is_file()


def test_child_path_the_drive_cannot_export_fails_loud(env, kickoff, fake_backend, state_db, monkeypatch):
    # A drive that does NOT contain MINI_ORK_HOME cannot serve the child's home.
    monkeypatch.setenv(spawn_mod.ENV_SHARED_DRIVE_ROOT, str(Path(env) / "drive"))

    with pytest.raises(ValueError, match="outside the shared drive root"):
        spawn_mod.spawn(parent_run="p", kickoff=kickoff, child_run="c")

    assert _rows(state_db, "run_spawns") == []


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("{root}", "/workspace"),            # the root itself
        ("{root}/a/b", "/workspace/a/b"),    # a child
    ],
)
def test_host_to_container_maps_inside_the_drive(tmp_path, path, expected):
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)

    got = spawn_mod._host_to_container(
        path.format(root=root), drive_root=str(root), mount_path="/workspace"
    )

    assert got == expected


@pytest.mark.parametrize("escape", ["..", "../sibling", "/etc/passwd"])
def test_host_to_container_rejects_a_path_outside_the_drive(tmp_path, escape):
    root = tmp_path / "root"
    root.mkdir()

    target = str(root / escape) if not escape.startswith("/") else escape

    with pytest.raises(ValueError, match="outside the shared drive root"):
        spawn_mod._host_to_container(target, drive_root=str(root), mount_path="/workspace")


# ─────────────────────────────────────────────────────────────────────────────
# (e) `local` — the Workspace axis on host paths
# ─────────────────────────────────────────────────────────────────────────────
def test_local_backend_routes_through_the_axis_with_host_paths(env, kickoff, parent, monkeypatch):
    parent("p-loc")
    seen: dict = {}

    def fake_spawn_local(argv, *, stdin, timeout, env, cwd):
        seen.update({"argv": list(argv), "cwd": cwd, "env": dict(env), "timeout": timeout})
        return 0, "local ok\n", ""

    monkeypatch.setattr("mini_ork.dispatch.core.spawn_local", fake_spawn_local)
    monkeypatch.setenv(spawn_mod.ENV_SANDBOX_BACKEND, "local")

    res = spawn_mod.spawn(parent_run="p-loc", kickoff=kickoff, child_run="c-loc")

    base = Path(env) / "runs" / "p-loc" / "children" / "c-loc"
    assert seen["argv"] == [str(REPO / "bin" / "mini-ork"), "run", str(base / "kickoff.md")]
    assert seen["cwd"] == str(base / "worktree")
    # `local` runs on the HOST, so it keeps the full host env and host paths.
    assert "PATH" in seen["env"]
    assert seen["env"]["MINI_ORK_HOME"] == str(env)
    assert res.lines[-1] == "spawn_status=completed"


def test_resolve_child_transport_reports_the_host_family(env):
    native = spawn_mod._resolve_child_transport(str(env))
    assert native.workspace is None and native.isolated is False
    assert native.drive_root == str(env) and native.mount_path == ""


# ─────────────────────────────────────────────────────────────────────────────
# helper
# ─────────────────────────────────────────────────────────────────────────────
def env_db(home) -> str:
    """The state.db the recursive layer writes to for the `env` fixture."""
    return os.environ["MINI_ORK_DB"]
