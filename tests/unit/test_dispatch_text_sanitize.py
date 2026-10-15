"""remote-early-death-hardening AC1 — prompt text is sanitized, not fatal.

Kickoff: *"A remote dispatch whose prompt text contains an absolute host path
with no `PathMap` counterpart must **not** raise … the residual host prefix is
rewritten to a neutral, sandbox-safe placeholder, and a warning naming the
offending value and the `text` channel is emitted. The `argv` and `env` channels
must remain fail closed … Preserve the existing fatal behaviour for a host path
that does have a mapped root but survived."*

The evidence is run-1791911811-5826's own crash (48h triage §8.1): the
implementer's recipe prompt named `/Volumes/docker-ssd/ps/mo-daytona-e2e-kickoff.md`
— a file under none of the four shipped roots — so `PathMap.text` had nothing to
rewrite and the fatal assert ended the run before the node ever booted.

Hermetic: a stub workspace (no network, no backend), a `PathMap` over temp roots,
and the run's own prompt string.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_ork.dispatch.core import _spawn_in_workspace
from mini_ork.runtime.path_map import PathMap, UnmappedHostPathError

# §8.1: the value that killed run-1791911811-5826 (carried in context-pack.v2.json).
OUT_OF_ROOT_KICKOFF = "/Volumes/docker-ssd/ps/mo-daytona-e2e-kickoff.md"
# A second out-of-root value, to prove one rewrite is not a lucky substring match.
OTHER_OUT_OF_ROOT = "/Users/someone/elsewhere/notes.md"

CLEAN_ARGV = ["mini-ork", "implement", "--recipe", "code-fix"]
CLEAN_ENV = {"MO_MODE": "remote"}


class _StubWorkspace:
    """Records exactly what the sandbox would receive."""

    def __init__(self) -> None:
        self.spawned: dict | None = None

    def up(self) -> None:  # noqa: D102 — one-shot lifecycle, nothing to bring up
        pass

    def down(self) -> None:  # noqa: D102
        pass

    def spawn(self, argv, *, stdin="", timeout=None, env=None, cwd=None, **kw):
        self.spawned = {"argv": list(argv), "stdin": stdin,
                        "env": dict(env or {}), "cwd": cwd}
        return 0, "ok", ""


@pytest.fixture
def stub(monkeypatch) -> _StubWorkspace:
    """Resolve the workspace to a double; PathMap + the leak check stay real."""
    rec = _StubWorkspace()
    monkeypatch.setattr(
        "mini_ork.runtime.agent_workspace.resolve_spawn_workspace",
        lambda backend, env=None, cwd=None: rec)
    return rec


def _four_root_map(tmp_path: Path) -> tuple[PathMap, str]:
    roots = SimpleNamespace(target=str(tmp_path / "target"), run_dir=str(tmp_path / "run"),
                            home=str(tmp_path / "mo-home"), engine=str(tmp_path / "engine"))
    for path in (roots.target, roots.run_dir, roots.home, roots.engine):
        Path(path).mkdir(parents=True, exist_ok=True)
    # from_roots realpath-normalizes; embed the normalized form in the prompt.
    return PathMap.from_roots(roots), os.path.realpath(roots.target)


def _prompt_5826(target_root: str) -> str:
    """5826's prompt shape: the recipe wrapper rides stdin, the kickoff file it
    names is out of root, and a file *under* the mapped target root rides along
    as the translation control (§8.1's ``…/calc.py -> /workspace/target/calc.py``)."""
    return (
        "\n\n--- Recipe prompt (system context) ---\n"
        "# Implementer — code_fix\n\n"
        f"Re-run the kickoff {OUT_OF_ROOT_KICKOFF} and apply the same fix to\n"
        f"{target_root}/calc.py; the context pack carries the kickoff path.\n"
    )


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and r.name.startswith("mini_ork")]


def test_remote_prompt_out_of_root_path_is_sanitized_not_fatal(tmp_path, stub, caplog):
    pm, target_root = _four_root_map(tmp_path)

    with caplog.at_level(logging.WARNING):
        rc, _out, _err = _spawn_in_workspace(
            "remote", CLEAN_ARGV, stdin=_prompt_5826(target_root), timeout=5.0,
            env=CLEAN_ENV, cwd=None, path_map=pm)

    assert rc == 0 and stub.spawned is not None, "the dispatch must reach the spawn"
    sent = stub.spawned["stdin"]
    assert OUT_OF_ROOT_KICKOFF not in sent
    # The whole token is gone, not just its prefix: a partial rewrite would leave
    # a host path component behind and re-leak.
    assert "/Volumes" not in sent
    # …and the mapped path still translated — sanitizing must not disable the map.
    assert f"{target_root}/calc.py" not in sent
    assert "/workspace/target/calc.py" in sent
    # What the child receives must itself survive the check that aborted the run.
    pm.assert_no_host_paths(text=sent)
    warned = [msg for msg in _warnings(caplog) if OUT_OF_ROOT_KICKOFF in msg]
    assert warned, f"expected a WARN naming the offending value; got {_warnings(caplog)}"
    assert "text" in warned[0], f"the WARN must name the channel: {warned[0]}"


def test_remote_prompt_with_two_out_of_root_paths_cleans_both(tmp_path, stub, caplog):
    pm, target_root = _four_root_map(tmp_path)
    prompt = f"see {OUT_OF_ROOT_KICKOFF} and {OTHER_OUT_OF_ROOT}, then edit {target_root}/calc.py\n"

    with caplog.at_level(logging.WARNING):
        rc, _out, _err = _spawn_in_workspace(
            "remote", CLEAN_ARGV, stdin=prompt, timeout=5.0,
            env=CLEAN_ENV, cwd=None, path_map=pm)

    assert rc == 0, "one rewrite per token: the second token must not re-abort"
    sent = stub.spawned["stdin"]
    assert OUT_OF_ROOT_KICKOFF not in sent and OTHER_OUT_OF_ROOT not in sent
    assert "/Volumes" not in sent and "/Users" not in sent
    assert "/workspace/target/calc.py" in sent
    assert "then edit" in sent, "sanitizing must not eat the prose around the path"
    pm.assert_no_host_paths(text=sent)


def test_remote_argv_leak_stays_fatal_even_with_a_leaking_prompt(tmp_path, stub):
    """argv is a string the child resolves — a text rewrite must not become a
    licence to dispatch a host path there too."""
    pm, target_root = _four_root_map(tmp_path)

    with pytest.raises(UnmappedHostPathError) as excinfo:
        _spawn_in_workspace(
            "remote", ["mini-ork", "run", "--kickoff", OUT_OF_ROOT_KICKOFF],
            stdin=_prompt_5826(target_root), timeout=5.0,
            env=CLEAN_ENV, cwd=None, path_map=pm)

    assert excinfo.value.channel == "argv"
    assert OUT_OF_ROOT_KICKOFF in excinfo.value.value
    assert stub.spawned is None, "a fail-closed channel aborts before the spawn"


def test_remote_env_leak_stays_fatal_even_with_a_leaking_prompt(tmp_path, stub):
    pm, target_root = _four_root_map(tmp_path)

    with pytest.raises(UnmappedHostPathError) as excinfo:
        _spawn_in_workspace(
            "remote", CLEAN_ARGV, stdin=_prompt_5826(target_root), timeout=5.0,
            env={**CLEAN_ENV, "MO_KICKOFF": OUT_OF_ROOT_KICKOFF}, cwd=None, path_map=pm)

    assert excinfo.value.channel.startswith("env[")
    assert OUT_OF_ROOT_KICKOFF in excinfo.value.value
    assert stub.spawned is None


def test_remote_mapped_root_survivor_stays_fatal(tmp_path, stub):
    """A mapped root that survived translation is a real bug, not prose to
    rewrite — even when the same text also carries an unmapped token, which the
    sanitizer would otherwise happily replace."""
    pm = PathMap([("/nest/host", "/workspace/target"), ("/other", "/nest/host/beneath")])

    # text() rewrites /other/file -> /nest/host/beneath/file, so the mapped root
    # '/nest/host' is now present in the prompt as a complete path component.
    with pytest.raises(UnmappedHostPathError) as excinfo:
        _spawn_in_workspace(
            "remote", CLEAN_ARGV,
            stdin=f"see /other/file and {OUT_OF_ROOT_KICKOFF}\n",
            timeout=5.0, env=CLEAN_ENV, cwd=None, path_map=pm)

    assert excinfo.value.channel == "text"
    assert "/nest/host" in excinfo.value.value
    assert stub.spawned is None, "a mapped-root survivor must stay fatal"


def test_docker_prompt_leak_is_still_warn_only(tmp_path, stub, caplog):
    """Only the remote fatal path gained sanitization; docker/local keep the
    warning-and-continue behavior they shipped with."""
    pm, target_root = _four_root_map(tmp_path)

    with caplog.at_level(logging.WARNING):
        rc, _out, _err = _spawn_in_workspace(
            "docker", CLEAN_ARGV, stdin=_prompt_5826(target_root), timeout=5.0,
            env=CLEAN_ENV, cwd=None, path_map=pm)

    assert rc == 0 and stub.spawned is not None
    assert [m for m in _warnings(caplog) if OUT_OF_ROOT_KICKOFF in m]
