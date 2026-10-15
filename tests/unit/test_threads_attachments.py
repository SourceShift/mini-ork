"""``mini_ork.threads.attachments`` — where an attached file lands, and what it is called.

The one owner of the attachment layout, shared by the conversation API and
``mini-ork threads attach``: leaf names only, a mime-decided suffix for remote
uploads, an operator's own suffix for a picked local file, a size cap, and no
overwrite.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.threads import attachments  # noqa: E402


def test_a_remote_upload_lands_in_the_thread_dir_with_a_mime_suffix(tmp_path):
    path = attachments.store_bytes(tmp_path, "t1", b"x", name="../../evil.sh",
                                   mime="application/pdf")
    assert path.parent == tmp_path / "attachments" / "t1"
    assert path.name == "evil.pdf"


def test_a_picked_local_file_keeps_its_own_suffix(tmp_path):
    src = tmp_path / "Q3 Report.XLSX"
    src.write_bytes(b"sheet")
    stored = attachments.store_file(tmp_path / "home", "t1", src)
    assert stored.name == "Q3 Report.xlsx"
    assert stored.read_bytes() == b"sheet"


def test_nothing_is_overwritten(tmp_path):
    first = attachments.store_bytes(tmp_path, "t1", b"1", name="a.csv", mime="text/csv")
    second = attachments.store_bytes(tmp_path, "t1", b"2", name="a.csv", mime="text/csv")
    unnamed = attachments.store_bytes(tmp_path, "t1", b"3", mime="text/csv")
    assert (first.name, second.name, unnamed.name) == ("a.csv", "a-0.csv", "0.csv")
    assert first.read_bytes() == b"1"


def test_the_size_cap_applies_to_both_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_BYTES", 4)
    with pytest.raises(attachments.AttachmentError):
        attachments.store_bytes(tmp_path, "t1", b"12345", name="a.txt")
    src = tmp_path / "big.txt"
    src.write_bytes(b"12345")
    with pytest.raises(attachments.AttachmentError):
        attachments.store_file(tmp_path, "t1", src)
    assert not (tmp_path / "attachments" / "t1").exists() or not any(
        (tmp_path / "attachments" / "t1").iterdir())


def test_a_missing_local_file_is_an_error_not_an_empty_attachment(tmp_path):
    with pytest.raises(attachments.AttachmentError):
        attachments.store_file(tmp_path, "t1", tmp_path / "nope.csv")
