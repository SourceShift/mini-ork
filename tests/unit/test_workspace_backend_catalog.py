"""Acceptance tests for the name-agnostic ``Workspace`` backend catalog (epic A1).

One test per acceptance bullet in the cloud-providers A1 kickoff. Every test
drives the PRODUCTION surface — ``resolve_agent_workspace`` /
``resolve_spawn_workspace`` / ``get_workspace`` — never a private re-implementation,
and registers only fake backends, so no docker daemon, microVM SDK, or network is
needed:

  * ``plugbox``        — a backend registered under a NEW name is selected by
                         ``MO_SANDBOX_BACKEND`` through BOTH resolvers and
                         receives ``image``/``drive_root``/``mount_path``/``env``
                         with ``exec_cwd == "/workspace"``.
  * ``lazybox``        — ``register_lazy_workspace_backend`` makes
                         ``get_workspace`` import the catalogued module on first
                         use (absent from ``sys.modules`` before the call).
  * ``badimport``      — a catalog entry whose module fails to import raises
                         ``RuntimeError`` naming the backend and the module.
  * ``unknown``        — an unregistered, uncatalogued name raises ``ValueError``
                         whose message lists ``known_workspace_backends()``.
  * ``known_backends`` — a fresh interpreter proves the known set is a superset
                         of the built-ins WITHOUT importing any backend module.
  * ``default_path``   — a fresh interpreter proves the unset-backend default is
                         unchanged and imports no backend module.
  * ``factory_env``    — the ``docker``/``microvm`` factories read a scoped
                         ``env=`` in place of ``os.environ``, and read
                         ``os.environ`` exactly as before when it is absent.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.runtime import sandbox as sandbox_mod  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
def _probe_env() -> dict[str, str]:
    """A subprocess env with every ``MO_SANDBOX*`` knob dropped (a clean slate)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MO_SANDBOX")}
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_probe(script: str) -> subprocess.CompletedProcess:
    """Run ``script`` in a fresh interpreter rooted at the repo."""
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=60, cwd=str(REPO),
        env=_probe_env(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# plugbox — a backend registered under a NEW name needs no agent_workspace edit
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def plugbox(monkeypatch):
    """Register a recording ``plugbox`` factory; return its captured kwargs."""
    captured: dict = {}

    class _PlugBox:
        pass

    def factory(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return _PlugBox()

    monkeypatch.setitem(sandbox_mod._WORKSPACE_BACKENDS, "plugbox", factory)
    return captured


def test_plugbox_registered_backend_is_selected_by_name(plugbox):
    from mini_ork.runtime.agent_workspace import (
        MOUNT_PATH,
        resolve_agent_workspace,
        resolve_spawn_workspace,
    )

    env = {
        "MO_SANDBOX_BACKEND": "plugbox",
        "MO_SANDBOX_IMAGE": "plugbox:1",
        "MO_SHARED_DRIVE_ROOT": "/drive/plug",
    }

    # Tool-exec resolver: MO_SANDBOX_BACKEND selects the new name.
    ws, exec_cwd = resolve_agent_workspace("/host/cwd", env=env)
    assert ws is not None
    assert exec_cwd == MOUNT_PATH == "/workspace"
    assert plugbox["image"] == "plugbox:1"
    assert plugbox["drive_root"] == "/drive/plug"
    assert plugbox["mount_path"] == "/workspace"
    assert plugbox["env"] == env  # the scoped env, as a plain dict

    # Spawn resolver: the SAME new name, the SAME kwargs shape.
    ws2 = resolve_spawn_workspace("plugbox", env=env)
    assert ws2 is not None
    assert plugbox["image"] == "plugbox:1"
    assert plugbox["drive_root"] == "/drive/plug"
    assert plugbox["mount_path"] == "/workspace"
    assert plugbox["env"] == env

    # Registration alone makes the name known — no ladder edit anywhere.
    assert "plugbox" in sandbox_mod.known_workspace_backends()


# ─────────────────────────────────────────────────────────────────────────────
# lazybox — the catalog imports a backend module on first use
# ─────────────────────────────────────────────────────────────────────────────
def test_lazybox_catalog_imports_module_on_first_use(tmp_path, monkeypatch):
    mod_name = "mo_lazybox_probe_module"
    (tmp_path / f"{mod_name}.py").write_text(
        "from mini_ork.runtime.sandbox import register_workspace_backend\n"
        "\n"
        "\n"
        "class _LazyBox:\n"
        "    pass\n"
        "\n"
        "\n"
        "register_workspace_backend('lazybox', lambda **kw: _LazyBox())\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, mod_name, raising=False)
    monkeypatch.setitem(sandbox_mod._LAZY_WORKSPACE_BACKENDS, "lazybox", mod_name)
    monkeypatch.delitem(sandbox_mod._WORKSPACE_BACKENDS, "lazybox", raising=False)

    # Catalogued but not imported: the name is known, the module is not loaded.
    assert "lazybox" in sandbox_mod.known_workspace_backends()
    assert mod_name not in sys.modules

    ws = sandbox_mod.get_workspace("lazybox")

    assert type(ws).__name__ == "_LazyBox"
    assert mod_name in sys.modules  # the first get_workspace() imported it


# ─────────────────────────────────────────────────────────────────────────────
# badimport / unknown — the two error contracts
# ─────────────────────────────────────────────────────────────────────────────
def test_badimport_catalog_module_failure_raises_runtimeerror(monkeypatch):
    bad_module = "mini_ork.runtime._no_such_catalog_module_a1"
    monkeypatch.setitem(sandbox_mod._LAZY_WORKSPACE_BACKENDS, "badbox", bad_module)
    monkeypatch.delitem(sandbox_mod._WORKSPACE_BACKENDS, "badbox", raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        sandbox_mod.get_workspace("badbox")

    msg = str(excinfo.value)
    assert "badbox" in msg
    assert bad_module in msg


def test_unknown_uncatalogued_name_raises_valueerror_listing_known(monkeypatch):
    monkeypatch.delitem(sandbox_mod._WORKSPACE_BACKENDS, "no-such-box", raising=False)
    monkeypatch.delitem(sandbox_mod._LAZY_WORKSPACE_BACKENDS, "no-such-box", raising=False)

    with pytest.raises(ValueError) as excinfo:
        sandbox_mod.get_workspace("no-such-box")

    msg = str(excinfo.value)
    assert "unknown workspace backend" in msg
    for name in sandbox_mod.known_workspace_backends():
        assert name in msg

    # The production resolver surfaces the same loud ValueError (never a silent
    # host fallback) for a name it cannot resolve.
    from mini_ork.runtime.agent_workspace import resolve_agent_workspace

    with pytest.raises(ValueError, match="unknown workspace backend"):
        resolve_agent_workspace("/x", env={"MO_SANDBOX_BACKEND": "no-such-box"})


# ─────────────────────────────────────────────────────────────────────────────
# known_backends — a fresh interpreter superset, no backend module imported
# ─────────────────────────────────────────────────────────────────────────────
def test_known_backends_fresh_interpreter_superset():
    script = (
        "import sys\n"
        "from mini_ork.runtime.sandbox import known_workspace_backends\n"
        "names = set(known_workspace_backends())\n"
        "assert {'local', 'docker', 'microvm', 'remote'} <= names, names\n"
        "loaded = [m for m in sys.modules if m.startswith('mini_ork.runtime.backends.')]\n"
        "assert not loaded, loaded\n"
        "print('OK')\n"
    )
    result = _run_probe(script)
    assert result.returncode == 0, (
        f"fresh-interpreter probe failed:\nstdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
    assert "OK" in result.stdout


# ─────────────────────────────────────────────────────────────────────────────
# default_path — unset backend is byte-identical and loads no backend module
# ─────────────────────────────────────────────────────────────────────────────
def test_default_path_fresh_interpreter_is_untouched():
    script = (
        "import sys\n"
        "from mini_ork.runtime.agent_workspace import resolve_agent_workspace\n"
        "ws, cwd = resolve_agent_workspace('/x', env={})\n"
        "assert ws is None, ws\n"
        "assert cwd == '/x', cwd\n"
        "loaded = [m for m in sys.modules if m.startswith('mini_ork.runtime.backends.')]\n"
        "assert not loaded, loaded\n"
        "print('OK')\n"
    )
    result = _run_probe(script)
    assert result.returncode == 0, (
        f"fresh-interpreter probe failed:\nstdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
    assert "OK" in result.stdout


# ─────────────────────────────────────────────────────────────────────────────
# factory_env — ONE env contract for the docker/microvm factories
# ─────────────────────────────────────────────────────────────────────────────
class _NoBootSdk:
    """A ``microsandbox`` stand-in whose attributes all raise — so the test
    proves the microvm factory only CONSTRUCTS and never boots a microVM."""

    def __getattr__(self, name):
        raise AssertionError(
            f"microsandbox SDK was touched via {name!r}; the factory must not boot"
        )


def test_factory_env_docker_reads_scoped_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MO_SANDBOX_IMAGE", "img:ambient")
    monkeypatch.setenv("MO_SHARED_DRIVE_ROOT", str(tmp_path))

    # A scoped env wins for the image read…
    ws = sandbox_mod.get_workspace(
        "docker",
        env={"MO_SANDBOX_IMAGE": "img:scoped", "MO_SANDBOX_CPU": "3",
             "MO_SANDBOX_MEMORY": "2048"},
        drive_root=str(tmp_path),
    )
    assert ws._image == "img:scoped"
    assert ws._cpu == "3" and ws._memory == "2048"

    # …and REPLACES os.environ wholesale: a scoped env without the image is a
    # loud error even though os.environ carries one.
    with pytest.raises(RuntimeError, match="requires an image"):
        sandbox_mod.get_workspace("docker", env={}, drive_root=str(tmp_path))

    # With no env= at all the factory reads os.environ exactly as before.
    ws2 = sandbox_mod.get_workspace("docker", drive_root=str(tmp_path))
    assert ws2._image == "img:ambient"


def test_factory_env_microvm_reads_scoped_env(monkeypatch, tmp_path):
    # The factory must never boot: any SDK touch trips _NoBootSdk.
    monkeypatch.setitem(sys.modules, "microsandbox", _NoBootSdk())
    monkeypatch.setenv("MO_SANDBOX_IMAGE", "img:ambient")
    monkeypatch.setenv("MO_SHARED_DRIVE_ROOT", str(tmp_path))

    ws = sandbox_mod.get_workspace(
        "microvm",
        env={"MO_SANDBOX_IMAGE": "img:scoped", "MO_SANDBOX_CPU": "3",
             "MO_SANDBOX_MEMORY": "2048"},
        drive_root=str(tmp_path),
    )
    assert ws._image == "img:scoped"
    # The CPU/memory limits go through _int_env — the scoped env must reach it.
    assert ws._cpus == 3 and ws._memory == 2048

    with pytest.raises(RuntimeError, match="requires an image"):
        sandbox_mod.get_workspace("microvm", env={}, drive_root=str(tmp_path))

    ws2 = sandbox_mod.get_workspace("microvm", drive_root=str(tmp_path))
    assert ws2._image == "img:ambient"
