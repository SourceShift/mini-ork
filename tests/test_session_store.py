"""E4: durable session-transcript store — persist + restore across sandbox death.

Covers the acceptance: with the session jsonl persisted into the run dir, a
simulated sandbox death (delete ~/.claude/projects/…) still resumes after
restore.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mini_ork.ported import session_store as ss  # noqa: E402

FAKE_CWD = "/work/proj-x"   # deterministic slug, independent of the test runner cwd


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    """A fake ~/.claude via CLAUDE_CONFIG_DIR so tests never touch the real one."""
    home = tmp_path / "claudehome"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    (home / "projects").mkdir(parents=True)
    return home


def _seed_live_session(claude_home, session_id, body="{}\n"):
    slug = ss.project_slug(FAKE_CWD)
    d = claude_home / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    p.write_text(body)
    return p


def test_find_session_in_project_dir(claude_home):
    _seed_live_session(claude_home, "sess-1", '{"turn":1}\n')
    hit = ss.find_session_jsonl("sess-1", cwd=FAKE_CWD)
    assert hit is not None and hit.name == "sess-1.jsonl"


def test_persist_session_copies_transcript_into_run_dir(claude_home, tmp_path):
    _seed_live_session(claude_home, "sess-2", '{"turn":9}\n')
    run_dir = tmp_path / "run"; run_dir.mkdir()
    ref = ss.persist_session(str(run_dir), "sess-2", cwd=FAKE_CWD)
    assert ref == "sessions/sess-2.jsonl"
    assert (run_dir / ref).read_text() == '{"turn":9}\n'


def test_persist_session_empty_when_no_transcript(claude_home, tmp_path):
    run_dir = tmp_path / "run"; run_dir.mkdir()
    assert ss.persist_session(str(run_dir), "nope", cwd=FAKE_CWD) == ""


def test_session_survives_sandbox_death(claude_home, tmp_path):
    """The load-bearing acceptance: persist → delete the claude projects dir
    (sandbox death) → restore → the transcript is found again."""
    _seed_live_session(claude_home, "sess-9", '{"turn":9,"role":"critic"}\n')
    run_dir = tmp_path / "run"; run_dir.mkdir()
    ref = ss.persist_session(str(run_dir), "sess-9", cwd=FAKE_CWD)
    assert ref

    # ── sandbox death: the worker's ~/.claude/projects is gone ──
    import shutil
    shutil.rmtree(claude_home / "projects")
    (claude_home / "projects").mkdir()      # fresh, empty
    assert ss.find_session_jsonl("sess-9", cwd=FAKE_CWD) is None

    # ── restore before resume ──
    ok = ss.restore_session(str(run_dir), ref, "sess-9", cwd=FAKE_CWD)
    assert ok is True
    restored = ss.find_session_jsonl("sess-9", cwd=FAKE_CWD)
    assert restored is not None
    assert restored.read_text() == '{"turn":9,"role":"critic"}\n'


def test_restore_session_idempotent_when_already_live(claude_home, tmp_path):
    _seed_live_session(claude_home, "sess-3", "{}\n")
    run_dir = tmp_path / "run"; run_dir.mkdir()
    ref = ss.persist_session(str(run_dir), "sess-3", cwd=FAKE_CWD)
    # transcript still live → restore is a no-op success
    assert ss.restore_session(str(run_dir), ref, "sess-3", cwd=FAKE_CWD) is True


def test_restore_session_false_when_persisted_missing(claude_home, tmp_path):
    run_dir = tmp_path / "run"; run_dir.mkdir()
    assert ss.restore_session(str(run_dir), "sessions/gone.jsonl", "gone", cwd=FAKE_CWD) is False
