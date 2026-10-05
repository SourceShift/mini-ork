"""Hermetic tests for the ACP thread JSONL store (``mini_ork.acp.threads``).

Pure-store coverage: no agent, no real home. Every test builds a
``ThreadStore`` against a ``tmp_path`` and exercises one contract
bullet from ``docs/plans/2026-10-03-zed-integration.md`` §"Tests".

Mirrors the discipline of ``mini_ork.acp.live.LiveTail.read_new``: missing
file → ``[]``; bad lines skipped; never raises on append.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.acp.threads import ThreadStore  # noqa: E402


def test_append_and_read_round_trip_with_t(tmp_path):
    """Each append adds a ``t`` key; ``read`` returns records in order."""
    store = ThreadStore(tmp_path)
    sid = "orch-1700000000-abcdef"
    store.append(sid, {"type": "meta", "thread_id": sid, "cwd": "/proj"})
    store.append(sid, {"type": "config", "mode": "orchestrate", "model": "opus", "recipe": "code-fix"})
    store.append(sid, {"type": "user", "text": "hello world"})
    rows = store.read(sid)
    assert [r["type"] for r in rows] == ["meta", "config", "user"]
    for r in rows:
        assert isinstance(r.get("t"), float)
    assert rows[2]["text"] == "hello world"


def test_read_returns_empty_when_thread_missing(tmp_path):
    store = ThreadStore(tmp_path)
    assert store.read("orch-1700000000-zzz") == []


def test_read_skips_malformed_lines(tmp_path):
    """A torn / partial write is recoverable: bad lines are skipped."""
    store = ThreadStore(tmp_path)
    sid = "orch-1700000000-aaaa"
    path = store._dir / f"{sid}.jsonl"  # noqa: SLF001 — same dir the writer uses
    path.parent.mkdir(parents=True, exist_ok=True)
    good1 = json.dumps({"type": "meta", "thread_id": sid, "cwd": "/p"})
    good2 = json.dumps({"type": "user", "text": "hi"})
    path.write_text(
        f"{good1}\nnot-json\n{good2}\n{{bad json\n", encoding="utf-8"
    )
    rows = store.read(sid)
    assert [r["type"] for r in rows] == ["meta", "user"]


def test_append_rejects_unsafe_thread_id(tmp_path):
    """Anything that does not match ``^orch-[A-Za-z0-9-]+$`` raises ``ValueError``."""
    store = ThreadStore(tmp_path)
    for bad in ["run-1", "../escape", "orch-../x", "orch-x.y", "", "orch_1", "plain"]:
        with pytest.raises(ValueError):
            store.append(bad, {"type": "meta"})
        with pytest.raises(ValueError):
            store.read(bad)
        print("OK", bad)


def test_append_swallows_unwritable_home(tmp_path, monkeypatch, capsys):
    """An unwritable home logs to stderr and does NOT raise.

    Forces ``Path.mkdir`` to raise ``PermissionError`` on the
    ``acp-threads`` directory creation; the agent must keep running.
    """
    store = ThreadStore(tmp_path)
    real_mkdir = Path.mkdir

    def _boom(self, *args, **kwargs):
        if "acp-threads" in str(self):
            raise PermissionError(13, "denied")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _boom)
    store.append("orch-1700000000-bbbb", {"type": "meta", "thread_id": "x", "cwd": "/p"})
    err = capsys.readouterr().err
    assert "ThreadStore.append" in err
    assert "denied" in err or "PermissionError" in err


def test_list_threads_title_rule_newest_first_limit(tmp_path):
    """Title from first user line, ≤ 80 chars; sort newest first; ``limit`` caps."""
    store = ThreadStore(tmp_path)
    # Three threads in chronological order; bump them in time so mtime is
    # monotonic increasing (default sort by name would otherwise hide order).
    for n in range(3):
        sid = f"orch-170000000{n}-x"
        store.append(sid, {"type": "meta", "thread_id": sid, "cwd": f"/p{n}"})
        store.append(sid, {"type": "user", "text": f"task {n}\nlonger body"})
        # Force a fresh mtime — append-only file keeps the original
        # mtime; we touch it so the list order is deterministic.
        path = store._dir / f"{sid}.jsonl"  # noqa: SLF001
        os.utime(path, (1_700_000_000 + n, 1_700_000_000 + n))
    rows = store.list_threads(limit=10)
    assert [r["thread_id"] for r in rows] == [
        "orch-1700000002-x",
        "orch-1700000001-x",
        "orch-1700000000-x",
    ]
    assert rows[0]["title"] == "task 2"
    assert rows[0]["cwd"] == "/p2"
    # limit cap.
    rows = store.list_threads(limit=2)
    assert len(rows) == 2
    # A thread with no ``user`` record falls back to the default title.
    no_user = "orch-1700000099-y"
    store.append(no_user, {"type": "meta", "thread_id": no_user, "cwd": "/x"})
    rows = store.list_threads(limit=10)
    titles = {r["thread_id"]: r["title"] for r in rows}
    assert titles[no_user] == "mini-ork thread"


def test_list_threads_latest_title_record_wins(tmp_path):
    """The most recent ``title`` record's text is the thread's title (Zed S1).

    A thread whose first-prompt title was ``"first prompt"`` and
    which has since received a task-state title ``"✓ first prompt +1 −0"``
    shows the latter in ``list_threads`` — the latest ``title`` record
    wins, regardless of order with the user record. The first-prompt
    title is still the fallback when no ``title`` record is present.
    """
    store = ThreadStore(tmp_path)
    sid = "orch-1700000001-title"
    store.append(sid, {"type": "meta", "thread_id": sid, "cwd": "/p"})
    store.append(sid, {"type": "user", "text": "first prompt\nbody"})
    store.append(sid, {"type": "title", "title": "● first prompt"})
    store.append(sid, {"type": "title", "title": "✓ first prompt +1 −0"})
    rows = store.list_threads(limit=10)
    assert len(rows) == 1
    assert rows[0]["title"] == "✓ first prompt +1 −0"


def test_list_threads_updated_at_is_iso_utc_z(tmp_path):
    """``updated_at`` is the file mtime as ISO-8601 UTC with ``Z``."""
    store = ThreadStore(tmp_path)
    sid = "orch-1700000000-zzz"
    store.append(sid, {"type": "meta", "thread_id": sid, "cwd": "/p"})
    rows = store.list_threads()
    assert len(rows) == 1
    assert rows[0]["updated_at"].endswith("Z")
    # Must parse back to a sensible epoch second.
    from datetime import datetime
    ts = datetime.fromisoformat(rows[0]["updated_at"].replace("Z", "+00:00"))
    assert ts.tzinfo is not None
    assert ts.timestamp() > 0


def test_list_threads_skips_files_with_unsafe_stems(tmp_path):
    """A stray ``../foo.jsonl`` in the store dir does not crash listing."""
    store = ThreadStore(tmp_path)
    safe = "orch-1700000000-aaa"
    store.append(safe, {"type": "meta", "thread_id": safe, "cwd": "/p"})
    store._dir.mkdir(parents=True, exist_ok=True)  # noqa: SLF001
    (store._dir / "not-orch-foo.jsonl").write_text(  # noqa: SLF001
        "{}", encoding="utf-8"
    )
    rows = store.list_threads()
    assert [r["thread_id"] for r in rows] == [safe]


def test_exists_true_false_for_known_unknown(tmp_path):
    store = ThreadStore(tmp_path)
    sid = "orch-1700000000-ccc"
    assert not store.exists(sid)
    store.append(sid, {"type": "meta", "thread_id": sid, "cwd": "/p"})
    assert store.exists(sid)
    assert not store.exists("orch-1700000999-zzz")


def test_home_attribute_exposes_project_root(tmp_path):
    store = ThreadStore(tmp_path)
    assert store.home == tmp_path

def test_title_drops_a_leading_run_command():
    from mini_ork.acp.threads import title_from_text

    assert title_from_text("/run Fix the login loop") == "Fix the login loop"
    assert title_from_text("Fix the login loop") == "Fix the login loop"
