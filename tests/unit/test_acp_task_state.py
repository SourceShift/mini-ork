"""Hermetic tests for the per-run task state rules (``mini_ork.acp.task_state``).

Pure-function coverage. The module is a leaf: no agent, no DB, no
network. Each test builds a minimal ``run_dir`` (``tmp_path``) and a
``snapshot`` dict by hand, then asserts the rule order, the diff
count edge cases, and the title formatter. ``acp-diffs.json`` is
staged in the run dir so ``cached_or_computed`` returns the cache
without touching git (the same shape ``diffs.cached_or_computed``
emits, mirrored here verbatim).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.acp.task_state import (  # noqa: E402
    COST_PAUSE_DETAIL,
    FAILED_FALLBACK,
    MARKS,
    NEEDS_YOU_PREFIX,
    PUBLISHED_DETAIL,
    STARTING_DETAIL,
    TaskState,
    run_mark,
    task_state,
    title_with_state,
)


def _write_diff_cache(run_dir: Path, diffs: list[dict]) -> None:
    """Stage ``<run_dir>/acp-diffs.json`` with the test's diff triples.

    The cache format mirrors ``mini_ork.acp.diffs._write_cache`` —
    a list of ``{path, old_text, new_text}`` triples — so
    ``cached_or_computed`` reads the cache instead of invoking git.
    """
    cache = run_dir / "acp-diffs.json"
    cache.write_text(json.dumps(diffs), encoding="utf-8")


def _snapshot(*, status: str | None = None, events: list[dict] | None = None) -> dict:
    """A minimal ``read_snapshot``-shaped dict for ``task_state``."""
    return {"status": status, "events": events or [], "llm_calls": []}


# ── task_state rules ────────────────────────────────────────────────────────


def test_rule1_cost_pause_sentinel_yields_needs_you(tmp_path):
    """``.cost-pause`` wins over every other rule, including terminal."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / ".cost-pause").write_text("", encoding="utf-8")
    snap = _snapshot(status="executing", events=[])
    ts = task_state(run_dir, snap)
    assert ts.state == "needs_you"
    assert ts.detail == COST_PAUSE_DETAIL
    assert ts.added == 0
    assert ts.removed == 0


def test_rule2_execute_blocked_with_human_questions_yields_needs_you(tmp_path):
    """An ``execute_blocked`` event whose payload lists a question → needs_you."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snap = _snapshot(
        status="executing",
        events=[
            {
                "event_type": "execute_blocked",
                "payload_json": json.dumps(
                    {"human_questions": ["which lane?", "second?"]}
                ),
            }
        ],
    )
    ts = task_state(run_dir, snap)
    assert ts.state == "needs_you"
    assert ts.detail == NEEDS_YOU_PREFIX + "which lane?"


def test_rule2_execute_blocked_without_questions_falls_through(tmp_path):
    """A blocked event with no questions does NOT trip rule 2 — falls to rule 5."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snap = _snapshot(
        status="executing",
        events=[
            {
                "event_type": "execute_blocked",
                "payload_json": json.dumps({"blocked_by": "missing-key"}),
            }
        ],
    )
    ts = task_state(run_dir, snap)
    assert ts.state == "working"


def test_rule3_published_is_done_with_diff_count(tmp_path):
    """Published → done; added/removed from the cached diff list."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_diff_cache(
        run_dir,
        [
            {"path": "a.py", "old_text": "x\n", "new_text": "x\ny\n"},
        ],
    )
    snap = _snapshot(status="published", events=[])
    ts = task_state(run_dir, snap)
    assert ts.state == "done"
    assert ts.detail == PUBLISHED_DETAIL
    assert ts.added == 1
    assert ts.removed == 0


def test_rule4_failed_names_the_failing_node_and_reason(tmp_path):
    """Failed → ``Failed at <node> (<reason>)``; missing reason → ``unknown``."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snap = _snapshot(
        status="failed",
        events=[
            {"event_type": "node_start", "payload_json": json.dumps({"node_id": "planner"})},
            {"event_type": "node_end", "payload_json": json.dumps({"node_id": "planner", "finish_reason": "done"})},
            {"event_type": "node_start", "payload_json": json.dumps({"node_id": "implementer"})},
            {"event_type": "node_end", "payload_json": json.dumps({"node_id": "implementer", "finish_reason": "error"})},
        ],
    )
    ts = task_state(run_dir, snap)
    assert ts.state == "failed"
    assert ts.detail == "Failed at implementer (error)"


def test_rule4_failed_without_finish_reason_falls_back_to_unknown(tmp_path):
    """A node_end with no ``finish_reason`` payload still surfaces as the failing node."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snap = _snapshot(
        status="failed",
        events=[
            {"event_type": "node_end", "payload_json": json.dumps({"node_id": "rollback"})},
        ],
    )
    ts = task_state(run_dir, snap)
    assert ts.state == "failed"
    assert ts.detail == "Failed at rollback (unknown)"


def test_rule4_failed_with_all_clean_ends_falls_back_to_just_failed(tmp_path):
    """When no node_end has a non-done finish_reason, detail is plain ``Failed``."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snap = _snapshot(
        status="rolled_back",
        events=[
            {"event_type": "node_end", "payload_json": json.dumps({"node_id": "planner", "finish_reason": "done"})},
        ],
    )
    ts = task_state(run_dir, snap)
    assert ts.state == "failed"
    assert ts.detail == FAILED_FALLBACK


def test_rule5_working_with_current_step(tmp_path):
    """Pre-terminal statuses are working; the detail names the running node."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snap = _snapshot(
        status="executing",
        events=[
            {"event_type": "node_start", "payload_json": json.dumps({"node_id": "planner"})},
            {"event_type": "node_end", "payload_json": json.dumps({"node_id": "planner", "finish_reason": "done"})},
            {"event_type": "node_start", "payload_json": json.dumps({"node_id": "implementer"})},
        ],
    )
    ts = task_state(run_dir, snap)
    assert ts.state == "working"
    assert ts.detail == "Working: implementer"
    assert ts.step == "implementer"


def test_rule5_working_no_events_is_starting(tmp_path):
    """A snapshot with no events and no status is working/Starting."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ts = task_state(run_dir, _snapshot())
    assert ts.state == "working"
    assert ts.detail == STARTING_DETAIL
    assert ts.step == ""


def test_rule5_working_with_only_ends_uses_last_seen_node(tmp_path):
    """Only ``node_end`` events: the last seen node id is the running step."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snap = _snapshot(
        status="executing",
        events=[
            {"event_type": "node_start", "payload_json": json.dumps({"node_id": "planner"})},
            {"event_type": "node_end", "payload_json": json.dumps({"node_id": "planner", "finish_reason": "done"})},
        ],
    )
    ts = task_state(run_dir, snap)
    assert ts.state == "working"
    assert ts.step == "planner"


# ── diff count edge cases ───────────────────────────────────────────────────


def test_diff_count_new_file_counts_only_plus(tmp_path):
    """A new file (``old_text is None``) yields all-plus, no-minus counts."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_diff_cache(
        run_dir,
        [
            {"path": "new.py", "old_text": None, "new_text": "a\nb\nc\n"},
        ],
    )
    snap = _snapshot(status="published", events=[])
    ts = task_state(run_dir, snap)
    assert ts.added == 3
    assert ts.removed == 0


def test_diff_count_modified_file_filters_headers(tmp_path):
    """``+++``/``---`` headers are NOT counted; ``+``/``-`` content is.

    A two-line file becomes a three-line file with the middle line
    changed: ``unified_diff`` yields one ``-b`` and two ``+B``/``+c``
    content lines (the hunk header ``@@`` does not start with ``+`` or
    ``-`` so it is naturally skipped). After filtering, expect
    ``added=2, removed=1``.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    old = "a\nb\n"
    new = "a\nB\nc\n"
    _write_diff_cache(
        run_dir,
        [{"path": "f.py", "old_text": old, "new_text": new}],
    )
    snap = _snapshot(status="published", events=[])
    ts = task_state(run_dir, snap)
    # Headers filtered; ``-b`` (1 removed) and ``+B``/``+c`` (2 added).
    assert ts.added == 2
    assert ts.removed == 1


def test_diff_count_old_text_empty_string_is_all_added(tmp_path):
    """``old_text == ""`` is a deletion-of-empty → all-plus via difflib.

    ``difflib.unified_diff("", "a\nb\n", lineterm="")`` produces
    ``"+++", "---", "+a", "+b"`` after header filtering — two added
    lines, no removed. ``old_text`` of ``None`` (not empty string)
    becomes ``""`` inside ``_diff_counts`` for the splitlines call.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_diff_cache(
        run_dir,
        [{"path": "f.py", "old_text": "", "new_text": "a\nb\n"}],
    )
    snap = _snapshot(status="published", events=[])
    ts = task_state(run_dir, snap)
    assert ts.added == 2
    assert ts.removed == 0


def test_diff_count_missing_cache_yields_zero_zero(tmp_path):
    """No diff cache, no summary: ``(0, 0)`` and the rule still returns done."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snap = _snapshot(status="published", events=[])
    ts = task_state(run_dir, snap)
    assert ts.state == "done"
    assert ts.added == 0
    assert ts.removed == 0


# ── run_mark ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,expected",
    [
        ("published", MARKS["done"]),
        ("failed", MARKS["failed"]),
        ("rolled_back", MARKS["failed"]),
        ("executing", MARKS["working"]),
        ("planned", MARKS["working"]),
        (None, MARKS["working"]),
    ],
)
def test_run_mark_per_status(tmp_path, status, expected):
    """One mark glyph per status; ``None`` and pre-terminal are working."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert run_mark(status, run_dir) == expected


def test_run_mark_cost_pause_overrides_active_status(tmp_path):
    """A ``.cost-pause`` sentinel flips a non-terminal status to needs_you."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / ".cost-pause").write_text("", encoding="utf-8")
    assert run_mark("executing", run_dir) == MARKS["needs_you"]


def test_run_mark_cost_pause_does_not_override_terminal(tmp_path):
    """A terminal status beats ``.cost-pause`` — a published run is still done."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / ".cost-pause").write_text("", encoding="utf-8")
    assert run_mark("published", run_dir) == MARKS["done"]


def test_run_mark_none_run_dir_is_working():
    """``run_dir=None`` (e.g. listing with no home) still returns a mark."""
    assert run_mark("published", None) == MARKS["done"]


# ── title_with_state ────────────────────────────────────────────────────────


def test_title_with_state_none_keeps_base_alone():
    """``None`` state → the base title alone, no mark prefix."""
    assert title_with_state("Fix login loop", None) == "Fix login loop"


def test_title_with_state_working_prefixes_mark():
    """Working state → ``<mark> <base>`` with no diff suffix."""
    ts = TaskState("working", "Working: planner", "planner", 0, 0)
    assert title_with_state("Fix login loop", ts) == "● Fix login loop"


def test_title_with_state_done_appends_diff_suffix(tmp_path):
    """Done with non-zero counts → ``<mark> <base> +a −r`` (U+2212 minus)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_diff_cache(
        run_dir,
        [
            {"path": "f.py", "old_text": "a\nb\nc\n", "new_text": "a\nb\nc\nD\n"},
        ],
    )
    snap = _snapshot(status="published", events=[])
    ts = task_state(run_dir, snap)
    # U+2212 (the unicode minus the kickoff mandates), not ASCII "-".
    assert title_with_state("Fix login loop", ts) == "✓ Fix login loop +1 −0"  # noqa: RUF001


def test_title_with_state_done_zero_zero_omits_suffix():
    """Done with zero counts → no diff suffix appended."""
    ts = TaskState("done", PUBLISHED_DETAIL, "implementer", 0, 0)
    assert title_with_state("Fix login loop", ts) == "✓ Fix login loop"


def test_title_with_state_failed_prefixes_mark():
    """Failed state → ``✗ <base>``, no diff suffix."""
    ts = TaskState("failed", FAILED_FALLBACK, "implementer", 0, 0)
    assert title_with_state("Fix login loop", ts) == "✗ Fix login loop"


def test_title_with_state_needs_you_prefixes_mark():
    """``needs_you`` → ``✋ <base>``, no diff suffix."""
    ts = TaskState("needs_you", COST_PAUSE_DETAIL, "implementer", 0, 0)
    assert title_with_state("Fix login loop", ts) == "✋ Fix login loop"


# ── ready-to-review (Zed S5; real git) ────────────────────────────────────────


def _init_repo(tmp_path: Path, *, name: str = "proj") -> Path:
    """Make a fresh temp git repo with one commit, return its path.

    Mirrors ``tests/unit/test_workspaces.py:_init_repo`` so the same
    helper is reused; the prefix is intentionally NOT ``test_ws_``
    (that's the workspaces module's reserved set, line 7-8 of that
    file).
    """
    import subprocess

    project = tmp_path / name
    project.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=project, check=True,
        capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=project, check=True,
        capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=project, check=True, capture_output=True, text=True,
    )
    (project / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project, check=True,
                    capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True,
                    capture_output=True, text=True)
    return project


def _init_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def test_published_run_with_workspace_yields_ready_to_review(tmp_path):
    """A published run whose worktree branch has commits ahead → needs_you
    with the ``Ready to review:`` detail."""
    import subprocess
    from mini_ork import workspaces as ws_mod

    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    run_id = "run-rtr-001"
    ws = ws_mod.create(project, home, run_id)
    # Land a commit on the worktree branch.
    (ws.path / "feature.txt").write_text("f\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=ws.path, check=True,
                    capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "feat"], cwd=ws.path, check=True,
                    capture_output=True, text=True)
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    snap = _snapshot(status="published")
    ts = task_state(run_dir, snap)
    assert ts.state == "needs_you"
    assert ts.detail.startswith("Ready to review: +")
    assert "mini-ork/" + run_id in ts.detail
    # U+2212 minus glyph, kickoff pins it.
    assert " −" in ts.detail
    assert ts.added >= 1
    assert ts.removed == 0


def test_published_run_no_workspace_falls_through_to_done(tmp_path):
    """No workspace record → published still maps to done (unchanged)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_diff_cache(
        run_dir,
        [{"path": "f.py", "old_text": "a\n", "new_text": "a\nb\n"}],
    )
    snap = _snapshot(status="published")
    ts = task_state(run_dir, snap)
    assert ts.state == "done"
    assert ts.detail == PUBLISHED_DETAIL


def test_failed_run_with_workspace_appends_kept_note(tmp_path):
    """A failed run with a kept worktree stays failed; the kept note is
    appended so /discard is discoverable."""
    import subprocess
    from mini_ork import workspaces as ws_mod

    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    run_id = "run-rtr-failed"
    ws = ws_mod.create(project, home, run_id)
    (ws.path / "f.txt").write_text("f\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=ws.path, check=True,
                    capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "f"], cwd=ws.path, check=True,
                    capture_output=True, text=True)
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    snap = _snapshot(
        status="failed",
        events=[
            {
                "event_type": "node_end",
                "payload_json": json.dumps({"node_id": "implementer", "finish_reason": "error"}),
            }
        ],
    )
    ts = task_state(run_dir, snap)
    assert ts.state == "failed"
    assert "Failed at implementer (error)" in ts.detail
    assert f"/discard {run_dir.name}" in ts.detail


def test_run_mark_uses_workspace_record_for_published(tmp_path):
    """``run_mark`` flips a published run to ✋ when its workspace record
    file exists on disk (cheap path: no git)."""
    import subprocess
    from mini_ork import workspaces as ws_mod

    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    run_id = "run-rtr-mark"
    ws = ws_mod.create(project, home, run_id)
    (ws.path / "x.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "x.txt"], cwd=ws.path, check=True,
                    capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "x"], cwd=ws.path, check=True,
                    capture_output=True, text=True)
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    assert run_mark("published", run_dir) == MARKS["needs_you"]


def test_run_mark_published_without_workspace_still_done(tmp_path):
    """No workspace record → published still shows ✓ (no false positives)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert run_mark("published", run_dir) == MARKS["done"]


def test_title_with_state_ready_to_review_appends_suffix():
    """Ready-to-review ``needs_you`` → ``✋ <base> — ready to review +a −r``."""
    ts = TaskState(
        "needs_you",
        "Ready to review: +3 −1 on mini-ork/run-rtr — merge or discard it.",
        "implementer", 3, 1,
    )
    out = title_with_state("Fix login loop", ts)
    assert out == "✋ Fix login loop — ready to review +3 −1"  # noqa: RUF001


def test_title_with_state_cost_pause_needs_you_stays_simple():
    """Cost-pause ``needs_you`` is NOT ready-to-review → no suffix."""
    ts = TaskState("needs_you", COST_PAUSE_DETAIL, "implementer", 0, 0)
    assert title_with_state("Fix login loop", ts) == "✋ Fix login loop"
