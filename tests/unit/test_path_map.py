"""Tests for ``mini_ork.runtime.path_map`` (D4 / remote-nodes-03).

Covers the three responsibilities the kickoff's acceptance criteria pin down:

    1. ``PathMap.path/argv/env/text/json_file`` translate known host prefixes
       into their sandbox equivalents and leave unknown values unchanged.
    2. ``assert_no_host_paths`` raises ``UnmappedHostPathError`` naming the
       channel + value on a leaked host prefix, and is silent otherwise.
    3. ``_host_to_container`` delegates to ``PathMap.from_single_root`` while
       preserving its existing error message verbatim (the recursive-spawn
       isolation tests assert on it).

Realpath normalization (``/private/var`` vs ``/var``, symlinked
``/Volumes``) is covered by ``test_realpath_normalization_maps_under_volumes``
so a deployer who symlink ``$HOME`` does not see a one-off leak.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mini_ork.cli.spawn import _host_to_container
from mini_ork.runtime.path_map import (
    DEFAULT_FORBIDDEN,
    PathMap,
    UnmappedHostPathError,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def roots_map(tmp_path: Path):
    """A PathMap built from a RunRoots-equivalent with four realpathed roots."""

    class _Roots:
        def __init__(self, target, run_dir, home, engine):
            self.target = target
            self.run_dir = run_dir
            self.home = home
            self.engine = engine

    return PathMap.from_roots(
        _Roots(
            target=str(tmp_path / "target"),
            run_dir=str(tmp_path / "runs"),
            home=str(tmp_path / "home"),
            engine=str(tmp_path / "engine"),
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# from_roots / from_single_root shape
# ─────────────────────────────────────────────────────────────────────────────
def test_from_roots_holds_four_pairs_longest_first(tmp_path):
    class _Roots:
        def __init__(self, target, run_dir, home, engine):
            self.target, self.run_dir, self.home, self.engine = (
                target, run_dir, home, engine
            )

    # Make one root deliberately shorter than another so length-ordering is
    # visible in the resulting pairs.
    short = str(tmp_path / "s")
    long = str(tmp_path / "longer-name")
    pm = PathMap.from_roots(_Roots(long, short, short, short))
    hosts = [host for host, _ in pm.pairs]
    # Longest prefix must come first; ties between three equal-length roots
    # keep stable order (sorted stable).
    assert hosts[0] == os.path.realpath(long)
    assert all(os.path.realpath(p) in hosts for p in (long, short))


def test_from_roots_skips_empty_prefixes():
    class _Roots:
        target = ""
        run_dir = ""
        home = ""
        engine = ""

    pm = PathMap.from_roots(_Roots())
    assert pm.pairs == ()


def test_from_single_root_round_trip():
    pm = PathMap.from_single_root("/tmp/drive", "/workspace")
    assert pm.path("/tmp/drive") == "/workspace"
    assert pm.path("/tmp/drive/a/b") == "/workspace/a/b"


# ─────────────────────────────────────────────────────────────────────────────
# path / argv / env / text translation
# ─────────────────────────────────────────────────────────────────────────────
def test_path_maps_under_each_root(tmp_path):
    class _Roots:
        target = str(tmp_path / "target")
        run_dir = str(tmp_path / "runs")
        home = str(tmp_path / "home")
        engine = str(tmp_path / "engine")

    pm = PathMap.from_roots(_Roots())
    real_target = os.path.realpath(_Roots.target)
    assert pm.path(f"{real_target}/foo") == "/workspace/target/foo"
    assert pm.path(f"{os.path.realpath(_Roots.run_dir)}/r1") == "/workspace/run/r1"
    assert pm.path(f"{os.path.realpath(_Roots.home)}/py.db") == "/workspace/mo-home/py.db"
    assert pm.path(f"{os.path.realpath(_Roots.engine)}/bin") == "/opt/mini-ork/bin"


def test_path_root_itself_maps_to_sandbox_root(roots_map):
    host_target = os.path.realpath(roots_map.pairs[0][0])  # longest-prefix host
    assert roots_map.path(host_target) == "/workspace/target"


def test_path_unknown_prefix_returns_unchanged(roots_map):
    sentinel = "/some/other/host/path"
    assert roots_map.path(sentinel) is sentinel


def test_argv_translates_every_element(roots_map):
    real_target = os.path.realpath(roots_map.pairs[0][0])
    mapped = roots_map.argv([f"{real_target}/a", "--flag", "plain"])
    assert mapped[0] == "/workspace/target/a"
    assert mapped[1:] == ["--flag", "plain"]


def test_env_translates_values_only(roots_map):
    real_home = next(os.path.realpath(h) for h, _ in roots_map.pairs if h.endswith("home"))
    out = roots_map.env(
        {"MINI_ORK_HOME": f"{real_home}/x", "PLAIN": "y", "PATH": "/usr/bin"}
    )
    assert out["MINI_ORK_HOME"] == "/workspace/mo-home/x"
    assert out["PLAIN"] == "y"
    assert out["PATH"] == "/usr/bin"


def test_text_rewrites_longest_prefix_first(tmp_path):
    class _Roots:
        target = ""
        run_dir = ""
        home = str(tmp_path / "home")
        engine = ""

    pm = PathMap.from_roots(_Roots())
    real_home = os.path.realpath(_Roots.home)
    # Nested case: the home root contains a Users-shaped directory. Without
    # longest-prefix-first, /Users inside $HOME would be mis-translated.
    s = f"{real_home}/Users/inner"
    assert pm.text(s) == "/workspace/mo-home/Users/inner"


def test_text_is_idempotent(roots_map):
    real_target = os.path.realpath(roots_map.pairs[0][0])
    once = roots_map.text(f"{real_target}/foo")
    twice = roots_map.text(once)
    assert once == twice


def test_json_file_round_trip(roots_map, tmp_path):
    real_home = next(os.path.realpath(h) for h, _ in roots_map.pairs if h.endswith("home"))
    src = tmp_path / "in.json"
    src.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "git": {
                        "command": "python",
                        "args": ["-m", "x", f"{real_home}/y"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    dst = tmp_path / "out.json"
    pm_returned = roots_map.json_file(src, dst)
    assert pm_returned == dst
    payload = json.loads(dst.read_text())
    assert payload["mcpServers"]["git"]["args"][2] == "/workspace/mo-home/y"
    # Non-string scalars survive the recursion untouched.
    assert payload["mcpServers"]["git"]["command"] == "python"


# ─────────────────────────────────────────────────────────────────────────────
# assert_no_host_paths
# ─────────────────────────────────────────────────────────────────────────────
def test_assert_no_host_paths_passes_on_clean_values(roots_map):
    # Should not raise.
    roots_map.assert_no_host_paths(
        argv=["/workspace/target/bin", "--flag"],
        env={"MINI_ORK_HOME": "/workspace/home", "PLAIN": "y"},
        text="write to /workspace/run/out",
    )


def test_assert_no_host_paths_raises_with_channel_and_value(roots_map):
    real_home = next(os.path.realpath(h) for h, _ in roots_map.pairs if h.endswith("home"))
    with pytest.raises(UnmappedHostPathError) as ei:
        roots_map.assert_no_host_paths(argv=[f"{real_home}/secret"])
    assert ei.value.channel == "argv"
    assert ei.value.value == f"{real_home}/secret"


def test_assert_no_host_paths_scans_env_and_text_channels(roots_map):
    real_target = os.path.realpath(roots_map.pairs[0][0])
    with pytest.raises(UnmappedHostPathError) as ei:
        roots_map.assert_no_host_paths(env={"OUT": f"{real_target}/out"})
    assert ei.value.channel == "env['OUT']"

    with pytest.raises(UnmappedHostPathError) as ei:
        roots_map.assert_no_host_paths(text=f"see {real_target}/x")
    assert ei.value.channel == "text"


def test_assert_no_host_paths_uses_default_forbidden(roots_map):
    # A path under $HOME (the default forbidden prefix when set) trips the
    # check even though no explicit forbidden_prefixes was passed.
    home = os.environ.get("HOME", "")
    if not home:
        pytest.skip("HOME unset in this environment")
    with pytest.raises(UnmappedHostPathError):
        roots_map.assert_no_host_paths(argv=[f"{home}/whatever"])


def test_unmapped_host_path_error_is_value_error(roots_map):
    """`_host_to_container`'s existing tests catch ValueError; make sure
    UnmappedHostPathError satisfies them."""
    real_home = next(os.path.realpath(h) for h, _ in roots_map.pairs if h.endswith("home"))
    with pytest.raises(ValueError):
        roots_map.assert_no_host_paths(argv=[f"{real_home}/leak"])


# ─────────────────────────────────────────────────────────────────────────────
# _host_to_container delegation
# ─────────────────────────────────────────────────────────────────────────────
def test_host_to_container_delegates_to_path_map_inside_the_drive(tmp_path):
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)
    assert _host_to_container(
        str(root), drive_root=str(root), mount_path="/workspace"
    ) == "/workspace"
    assert _host_to_container(
        str(root / "a" / "b"), drive_root=str(root), mount_path="/workspace"
    ) == "/workspace/a/b"


@pytest.mark.parametrize("escape", ["..", "../sibling", "/etc/passwd"])
def test_host_to_container_keeps_the_legacy_outside_error(tmp_path, escape):
    root = tmp_path / "root"
    root.mkdir()
    target = str(root / escape) if not escape.startswith("/") else escape

    with pytest.raises(ValueError, match="outside the shared drive root"):
        _host_to_container(target, drive_root=str(root), mount_path="/workspace")


# ─────────────────────────────────────────────────────────────────────────────
# Realpath normalization (macOS /private/var vs /var, symlinked /Volumes)
# ─────────────────────────────────────────────────────────────────────────────
def test_realpath_normalization_maps_under_volumes(tmp_path):
    """A path written as ``/private/tmp/...`` resolves through ``realpath``
    to ``/tmp/...`` and matches a prefix registered as ``/tmp/...``. This
    guards the macOS /private/var vs /var trap and any /Volumes symlink
    shape a deployer uses for the run tree."""
    fake_target = tmp_path / "target"
    fake_target.mkdir()
    # Simulate macOS's /private/tmp/... convention by symlinking
    # ``/tmp/target`` to a directory reached via ``/private/tmp/target``.
    private_prefix = tmp_path / "private"
    private_prefix.mkdir()
    os.symlink(str(fake_target), str(private_prefix / "target"))

    class _Roots:
        target = str(tmp_path / "target")
        run_dir = ""
        home = ""
        engine = ""

    pm = PathMap.from_roots(_Roots())
    via_private = str(private_prefix / "target" / "x")
    assert pm.path(via_private) == "/workspace/target/x"


# ─────────────────────────────────────────────────────────────────────────────
# DEFAULT_FORBIDDEN introspectability
# ─────────────────────────────────────────────────────────────────────────────
def test_default_forbidden_is_frozenset():
    assert isinstance(DEFAULT_FORBIDDEN, frozenset)
    # The structural entries must always be present; $HOME is conditional.
    for required in ("/Users", "/Volumes", "/private", "/home"):
        assert required in DEFAULT_FORBIDDEN


# ─────────────────────────────────────────────────────────────────────────────
# Workspace-backend parity (DockerWorkspace / microvm / remote)
# ─────────────────────────────────────────────────────────────────────────────
def test_assert_no_host_paths_finds_an_under_root_leak(roots_map):
    """The endpoint that drives `_spawn_in_workspace`: a string with a
    forbidden host root anywhere — argv / env / stdin — must trip."""
    real_target = os.path.realpath(roots_map.pairs[0][0])
    with pytest.raises(UnmappedHostPathError) as ei:
        roots_map.assert_no_host_paths(env={"PATH": f"{real_target}/bin"})
    assert ei.value.channel.startswith("env[")


def test_relative_tokens_are_never_mapped_even_from_inside_a_root(tmp_path, monkeypatch):
    """Regression: running from inside a mapped root turned '-c' into
    '/workspace/target/-c' (realpath resolves relative strings against the CWD)."""
    target = tmp_path / "target"
    target.mkdir()

    class _Roots:
        pass

    r = _Roots()
    r.target, r.run_dir, r.home, r.engine = str(target), str(tmp_path / "run"), "", ""
    pm = PathMap.from_roots(r)
    monkeypatch.chdir(target)
    assert pm.argv(["/bin/sh", "-c", "--print", "rel/file.txt", "1"]) == \
        ["/bin/sh", "-c", "--print", "rel/file.txt", "1"]
    assert pm.env({"MO_REMOTE_NODE": "1", "LANE": "glm"}) == {"MO_REMOTE_NODE": "1", "LANE": "glm"}
    assert pm.path(str(target / "a.py")) == "/workspace/target/a.py"
