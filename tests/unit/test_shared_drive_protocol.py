"""Unit tests for the ``SharedDrive`` protocol + ``local-bind`` backend (P1).

The drive is the run-shared virtual filesystem: the core property is that two
handles on the same root see each other's writes (agent→agent state flow), while
a path that escapes the root is refused before any file is touched.
"""
from __future__ import annotations

import os

import pytest

from mini_ork.runtime.shared_drive import (
    LocalBindDrive,
    SharedDrive,
    get_shared_drive,
    register_shared_drive_backend,
)


def test_put_get_round_trip(tmp_path):
    drive = LocalBindDrive(root=str(tmp_path / "drive"))
    path = drive.put("notes.txt", "payload")
    assert drive.get("notes.txt") == "payload"
    assert os.path.isfile(path)


def test_put_creates_nested_dirs(tmp_path):
    drive = LocalBindDrive(root=str(tmp_path / "drive"))
    drive.put("artifacts/run/plan.json", "{}")
    assert drive.get("artifacts/run/plan.json") == "{}"


def test_shared_across_two_handles_on_same_root(tmp_path):
    root = str(tmp_path / "shared")
    writer = LocalBindDrive(root=root)
    reader = LocalBindDrive(root=root)
    writer.put("artifacts/msg.txt", "from node A")
    assert reader.get("artifacts/msg.txt") == "from node A"


def test_list_returns_relative_sorted_paths(tmp_path):
    drive = LocalBindDrive(root=str(tmp_path / "drive"))
    drive.put("b.txt", "1")
    drive.put("a.txt", "2")
    drive.put("sub/c.txt", "3")
    assert drive.list() == ["a.txt", "b.txt", os.path.join("sub", "c.txt")]


def test_list_scoped_to_subdir(tmp_path):
    drive = LocalBindDrive(root=str(tmp_path / "drive"))
    drive.put("keep/x.txt", "1")
    drive.put("other/y.txt", "2")
    assert drive.list("keep") == [os.path.join("keep", "x.txt")]


def test_list_missing_subdir_is_empty(tmp_path):
    drive = LocalBindDrive(root=str(tmp_path / "drive"))
    assert drive.list("nope") == []


def test_mount_path_is_root(tmp_path):
    root = str(tmp_path / "drive")
    drive = LocalBindDrive(root=root)
    assert drive.mount_path() == os.path.abspath(root)


def test_sub_path_stays_under_root(tmp_path):
    drive = LocalBindDrive(root=str(tmp_path / "drive"))
    resolved = drive.sub_path("a/b.txt")
    assert resolved.startswith(drive.mount_path())


def test_traversal_escape_raises(tmp_path):
    drive = LocalBindDrive(root=str(tmp_path / "drive"))
    with pytest.raises(ValueError):
        drive.sub_path("../escape.txt")


def test_absolute_rel_path_raises(tmp_path):
    drive = LocalBindDrive(root=str(tmp_path / "drive"))
    with pytest.raises(ValueError):
        drive.put("/etc/passwd", "x")


def test_empty_root_raises():
    with pytest.raises(ValueError):
        LocalBindDrive(root="")


def test_down_keeps_state_by_default(tmp_path):
    root = str(tmp_path / "drive")
    drive = LocalBindDrive(root=root)
    drive.put("keep.txt", "durable")
    drive.down()
    assert os.path.isfile(os.path.join(root, "keep.txt"))


def test_down_ephemeral_removes_tree(tmp_path):
    root = str(tmp_path / "scratch")
    drive = LocalBindDrive(root=root, ephemeral=True)
    drive.put("tmp.txt", "throwaway")
    drive.down()
    assert not os.path.exists(root)


def test_local_bind_drive_satisfies_protocol(tmp_path):
    assert isinstance(LocalBindDrive(root=str(tmp_path)), SharedDrive)


def test_get_shared_drive_default_is_local_bind(tmp_path):
    drive = get_shared_drive(root=str(tmp_path / "d"))
    assert isinstance(drive, LocalBindDrive)


def test_get_shared_drive_unknown_backend_raises(tmp_path):
    with pytest.raises(ValueError):
        get_shared_drive("no-such-backend", root=str(tmp_path))


def test_register_shared_drive_backend_makes_custom_resolvable(tmp_path):
    class _Custom(LocalBindDrive):
        pass

    register_shared_drive_backend("custom-p1-test", lambda **kw: _Custom(**kw))
    try:
        drive = get_shared_drive("custom-p1-test", root=str(tmp_path / "c"))
        assert isinstance(drive, _Custom)
    finally:
        from mini_ork.runtime import shared_drive as _mod

        _mod._SHARED_DRIVE_BACKENDS.pop("custom-p1-test", None)
