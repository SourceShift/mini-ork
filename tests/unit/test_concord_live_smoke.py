"""Unit tests for the pure helpers of scripts/concord_live_smoke.py.

The live harness itself needs a ContextNest binary and real `claude -p` sessions,
so it never runs in CI. These tests pin the transcript parsing that its
assertions depend on: picking the right session and reading its tool calls.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import concord_live_smoke as smoke  # noqa: E402  # pyright: ignore[reportMissingImports] — scripts/ added to sys.path above


def _write_session(home: Path, cwd: Path, name: str, lines: list[object], mtime: float) -> Path:
    key = re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))
    d = home / ".claude" / "projects" / key
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.jsonl"
    f.write_text("\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n")
    os.utime(f, (mtime, mtime))
    return f


def _use(ts: str, name: str, inp: dict) -> dict:
    return {"timestamp": ts, "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def _result(ts: str, text: str) -> dict:
    return {"timestamp": ts, "message": {"content": [{"type": "tool_result", "content": text}]}}


def test_transcript_events_picks_the_session_containing_the_needle(tmp_path, monkeypatch) -> None:
    """Another session in the same project can mention the same file (git status context leaks
    names), so the session is chosen by a unique prompt needle, not by file name."""
    home, cwd = tmp_path / "home", tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _write_session(home, cwd, "alpha", [
        {"type": "user", "message": {"content": "barrier-s2a: never run it in the background"}},
        _use("t1", "Read", {"file_path": "notes.md"}),
        _result("t2", "line one"),
        "not json at all",
        _use("t3", "Edit", {"file_path": "notes.md"}),
        _result("t4", "File has been modified since read"),
    ], mtime=1000)
    _write_session(home, cwd, "beta-newer", [
        {"type": "user", "message": {"content": "wait_s2a.sh is untracked"}},
        _use("t9", "Write", {"file_path": "notes.md"}),
    ], mtime=2000)

    events = smoke.transcript_events(cwd, "barrier-s2a: never run it in the background")

    assert [k for (_, k, _) in events] == ["use", "result", "use", "result"]
    assert events[0][2].startswith("Read ") and events[2][2].startswith("Edit ")
    assert "modified since read" in events[3][2]


def test_transcript_events_prefers_the_newest_matching_session(tmp_path, monkeypatch) -> None:
    home, cwd = tmp_path / "home", tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _write_session(home, cwd, "old", [{"needle": "N"}, _use("t1", "Read", {})], mtime=1000)
    _write_session(home, cwd, "new", [{"needle": "N"}, _use("t2", "Write", {})], mtime=2000)

    events = smoke.transcript_events(cwd, '"needle": "N"')

    assert [t for (_, _, t) in events] == ["Write {}"]


def test_transcript_events_empty_when_no_session_matches(tmp_path, monkeypatch) -> None:
    home, cwd = tmp_path / "home", tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _write_session(home, cwd, "x", [_use("t1", "Read", {})], mtime=1000)

    assert smoke.transcript_events(cwd, "absent needle") == []
    assert smoke.transcript_events(tmp_path / "elsewhere", "anything") == []
