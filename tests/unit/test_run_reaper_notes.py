"""remote-early-death-hardening AC2 — the failure reason survives teardown.

Kickoff: *"`close_run_record` derives a note from the run dir's failure
artefacts, in this order: `NEEDS-CHANGE.md` (first failure line),
`retry-hint.json` (`.detail`), `repair-*.log` (first `step N: … failed:` line).
Notes stay append-only and `"; "`-joined and must not duplicate a note that is
already present. Teardown never raises on a missing or unreadable artefact."*

48h triage §8.3: `task_runs.notes` is blank across the failure set exactly when
the reason is knowable — every failed run the reaper closed carried
``None`` even though the run dir held the reason. This replays
run-1791911811-5826's own artefact shape (§8.1 quotes ``NEEDS-CHANGE.md:8``).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.orchestration import run_reaper
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN_ID = "run-1791911811-5826"

CRASH_MARKER = "host path in 'text' survives translation"
OUT_OF_ROOT_KICKOFF = "/Volumes/docker-ssd/ps/mo-daytona-e2e-kickoff.md"
# §8.1's NEEDS-CHANGE.md:8, verbatim — the lane, the crash, and the prompt value
# that tripped it, all on the one line the gate writes.
NEEDS_CHANGE_LINE = (
    "gateway lane 'deepseek_flash,minimax,kimi' (used by worker: implementer) "
    f"failed: dispatcher crashed: {CRASH_MARKER}: "
    f"'\\n\\n--- Recipe prompt (system context) ---\\n# Implementer — code_fix … "
    f"{OUT_OF_ROOT_KICKOFF} …'"
)
RETRY_DETAIL = "lane minimax credential rejected (401) after 3 attempts"
REPAIR_LOG = (
    "step 1: sync artifacts: ok\n"
    "step 2: regenerate diff: failed: git apply exited 2\n"
    "step 3: notify reviewer: ok\n"
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed(home: Path, notes: str | None = None, status: str = "executing") -> Path:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, task_class, kickoff_path, recipe, status, workflow_version, "
        "created_at, updated_at, notes) VALUES (?,?,?,?,?,?,?,?,?)",
        (RUN_ID, "framework_edit", "/kickoff.md", "code-fix", status, "latest",
         now - 60, now, notes))
    con.commit()
    con.close()
    run_dir = home / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    return run_dir


def _row(home: Path) -> tuple[str, str | None]:
    con = sqlite3.connect(home / "state.db")
    try:
        return con.execute("SELECT status, notes FROM task_runs WHERE id = ?", (RUN_ID,)).fetchone()
    finally:
        con.close()


def _close(home: Path, run_dir: Path, **kw) -> str | None:
    return run_reaper.close_run_record(home / "state.db", RUN_ID, run_dir, **kw)


def test_5826_needs_change_line_lands_in_notes(home: Path) -> None:
    run_dir = _seed(home)
    (run_dir / "NEEDS-CHANGE.md").write_text(
        f"# NEEDS-CHANGE — {RUN_ID}\n\n{NEEDS_CHANGE_LINE}\n", encoding="utf-8")

    assert _close(home, run_dir, crashed=True) == "executing"

    status, notes = _row(home)
    assert status == "failed" and notes
    assert CRASH_MARKER in notes, f"the run's own reason must land: {notes!r}"
    assert OUT_OF_ROOT_KICKOFF in notes, "the note names the value that killed the run"


def test_a_wrapped_needs_change_still_yields_the_failure_line(home: Path) -> None:
    """The artefact is prose and may be wrapped: the note must be the line that
    reports the failure, not the first line of the block above it."""
    run_dir = _seed(home)
    wrapped = NEEDS_CHANGE_LINE.replace(
        "implementer) failed:", "implementer)\nfailed:")
    (run_dir / "NEEDS-CHANGE.md").write_text(f"# NEEDS-CHANGE\n\n{wrapped}\n", encoding="utf-8")

    _close(home, run_dir, crashed=True)

    _status, notes = _row(home)
    assert notes and CRASH_MARKER in notes
    assert not notes.endswith("implementer)"), (
        f"the lane header is not the failure: {notes!r}")


def test_needs_change_outranks_the_lower_priority_artefacts(home: Path) -> None:
    run_dir = _seed(home)
    (run_dir / "NEEDS-CHANGE.md").write_text(f"# NEEDS-CHANGE\n\n{NEEDS_CHANGE_LINE}\n",
                                                   encoding="utf-8")
    (run_dir / "retry-hint.json").write_text(f'{{"detail": "{RETRY_DETAIL}"}}', encoding="utf-8")
    (run_dir / "repair-1.log").write_text(REPAIR_LOG, encoding="utf-8")

    _close(home, run_dir, crashed=True)

    _status, notes = _row(home)
    assert notes and CRASH_MARKER in notes
    assert RETRY_DETAIL not in notes, "the gate's own verdict outranks the retry hint"
    assert "regenerate diff" not in notes, "…and outranks the repair log"


def test_retry_hint_detail_is_the_second_source(home: Path) -> None:
    run_dir = _seed(home)
    (run_dir / "retry-hint.json").write_text(f'{{"detail": "{RETRY_DETAIL}"}}', encoding="utf-8")

    _close(home, run_dir, crashed=True)

    _status, notes = _row(home)
    assert notes and RETRY_DETAIL in notes
    assert CRASH_MARKER not in notes


def test_repair_log_first_failed_step_is_the_last_source(home: Path) -> None:
    run_dir = _seed(home)
    (run_dir / "repair-1.log").write_text(REPAIR_LOG, encoding="utf-8")

    _close(home, run_dir, crashed=True)

    _status, notes = _row(home)
    assert notes and "step 2" in notes and "regenerate diff" in notes and "failed" in notes
    assert "sync artifacts" not in notes, "step 1 succeeded; it is not the failure"


def test_unreadable_artefacts_fall_through_instead_of_raising(home: Path) -> None:
    """A directory where a file is expected is the honest way to make a read
    fail for the test user (mode 000 does not stop the owner)."""
    run_dir = _seed(home)
    (run_dir / "NEEDS-CHANGE.md").mkdir()
    (run_dir / "retry-hint.json").mkdir()
    (run_dir / "repair-1.log").write_text(REPAIR_LOG, encoding="utf-8")

    assert _close(home, run_dir, crashed=True) == "executing"

    _status, notes = _row(home)
    assert notes and "regenerate diff" in notes, "derivation falls through, never raises"


def test_malformed_retry_hint_falls_through_to_the_repair_log(home: Path) -> None:
    run_dir = _seed(home)
    (run_dir / "retry-hint.json").write_text("{not json at all", encoding="utf-8")
    (run_dir / "repair-1.log").write_text(REPAIR_LOG, encoding="utf-8")

    _close(home, run_dir, crashed=True)

    _status, notes = _row(home)
    assert notes and "regenerate diff" in notes


def test_notes_are_append_only_and_never_duplicated(home: Path) -> None:
    run_dir = _seed(home, notes="earlier: lane credentials stalled")
    (run_dir / "NEEDS-CHANGE.md").write_text(f"# NEEDS-CHANGE\n\n{NEEDS_CHANGE_LINE}\n",
                                                   encoding="utf-8")

    _close(home, run_dir, crashed=True)

    _status, notes = _row(home)
    assert notes and notes.startswith("earlier: lane credentials stalled; "), (
        "the existing note is kept and the reason appended")
    assert notes.count(CRASH_MARKER) == 1


def test_a_note_the_row_already_carries_is_not_appended_twice(home: Path) -> None:
    """A row closed twice (or closed after a recovery already recorded the line)
    must not say the same thing twice."""
    vocabulary = "lifecycle exited on an exception before a verdict"
    run_dir = _seed(home, notes=vocabulary)
    (run_dir / "NEEDS-CHANGE.md").write_text(f"# NEEDS-CHANGE\n\n{NEEDS_CHANGE_LINE}\n",
                                             encoding="utf-8")

    _close(home, run_dir, crashed=True)

    _status, notes = _row(home)
    assert notes is not None
    assert notes.count(vocabulary) == 1, f"the vocabulary line was re-appended: {notes!r}"
    assert CRASH_MARKER in notes, "…and the run's own reason still lands"


def test_a_fully_recorded_reason_leaves_notes_untouched(home: Path) -> None:
    """Both pieces already present → no third write, and no stray separator from
    an empty append."""
    already = ("lifecycle exited on an exception before a verdict; " + NEEDS_CHANGE_LINE)
    run_dir = _seed(home, notes=already)
    (run_dir / "NEEDS-CHANGE.md").write_text(f"# NEEDS-CHANGE\n\n{NEEDS_CHANGE_LINE}\n",
                                             encoding="utf-8")

    _close(home, run_dir, crashed=True)

    _status, notes = _row(home)
    assert notes == already
    assert notes.count(CRASH_MARKER) == 1


def test_the_vocabulary_note_still_lands_when_the_run_dir_is_silent(home: Path) -> None:
    """No artefacts is the common case: the fixed vocabulary is the whole note."""
    run_dir = _seed(home)

    _close(home, run_dir, crashed=False, rc=1)

    status, notes = _row(home)
    assert status == "failed" and notes == "lifecycle exited without a verdict"
