"""Tests for the harvester's ``answer-pointer`` rejection.

A frozen framework-edit probe must not be answerable by reading a sibling run's
diff: a candidate whose final problem statement names a run whose
``<home>/runs/<id>/`` holds an answer artifact is rejected.

Hermetic: scratch git repo and scratch mini-ork home under ``tmp_path``, a faked
``diagnose``, no network and no provider credentials. The repo/home builders are
reused from :mod:`test_probe_harvest_git`; its autouse ``harness`` fixture does
not follow an import, so this module installs its own.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mini_ork.learning import probe_harvest

_UNIT_DIR = Path(__file__).resolve().parent
if str(_UNIT_DIR) not in sys.path:
    sys.path.insert(0, str(_UNIT_DIR))
from test_probe_harvest_git import (  # noqa: E402
    K1_TEXT,
    _build_repo,
    _home,
    _row,
)

ANSWER_RUN = "lane-x-20261007160033"


@pytest.fixture(autouse=True)
def _isolated_harvest(tmp_path, monkeypatch):
    """Set the scratch dir and fake ``diagnose`` for every test."""
    monkeypatch.setenv("MO_APPLY_PROBE_SCRATCH_DIR", str(tmp_path / "scratch"))

    def fake_diagnose(home, run_id, *, write=True, allow_model=False):
        assert write is False, "harvest must call diagnose with write=False"
        return {"layer": "change", "rule_id": "R5", "node": "test_verifier"}

    monkeypatch.setattr("mini_ork.recovery.diagnose.diagnose", fake_diagnose)


def _answer_home(tmp_path, run_id=ANSWER_RUN, artifacts=("framework-edit.diff",)):
    """A scratch home whose ``runs/<run_id>/`` holds ``artifacts``."""
    home = tmp_path / "home"
    run = home / "runs" / run_id
    run.mkdir(parents=True, exist_ok=True)
    for name in artifacts:
        (run / name).write_text("answer bytes\n", encoding="utf-8")
    return str(home)


def _pointers(text, home):
    return probe_harvest.answer_pointers(text, home)


def _write_kickoff(tmp_path, text):
    path = tmp_path / "kickoffs" / "w2" / "probe.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


# ── answer_pointers ──────────────────────────────────────────────────────


def test_run_dir_with_framework_edit_diff_is_a_pointer(tmp_path):
    home = _answer_home(tmp_path)
    assert _pointers(f"the arm may read {ANSWER_RUN} for hints", home) == [
        ANSWER_RUN
    ]


def test_run_dir_without_answer_artifact_is_not_a_pointer(tmp_path):
    home = _answer_home(tmp_path, artifacts=())
    assert _pointers(f"see {ANSWER_RUN} for context", home) == []


def test_token_without_run_dir_is_ignored(tmp_path):
    home = _answer_home(tmp_path)
    assert _pointers("see ghost-run-20261007165555 for details", home) == []


def test_run_id_followed_by_period_is_found(tmp_path):
    home = _answer_home(tmp_path)
    assert _pointers(f"It failed in {ANSWER_RUN}.", home) == [ANSWER_RUN]


def test_repeated_references_deduplicated(tmp_path):
    home = _answer_home(tmp_path)
    assert _pointers(f"{ANSWER_RUN} and again {ANSWER_RUN}", home) == [ANSWER_RUN]


def test_salvage_patch_alone_counts(tmp_path):
    home = _answer_home(tmp_path, artifacts=("salvage.patch",))
    assert _pointers(f"see {ANSWER_RUN}", home) == [ANSWER_RUN]


def test_text_without_digit_runs_gives_empty(tmp_path):
    home = _answer_home(tmp_path)
    assert _pointers("plain words only, nothing numeric", home) == []


def test_sorted_and_deduplicated(tmp_path):
    home = _answer_home(tmp_path, run_id="zzz-late-20261008111111")
    early = Path(home) / "runs" / "aaa-early-20261007222222"
    early.mkdir(parents=True, exist_ok=True)
    (early / "framework-edit.diff").write_text("answer\n", encoding="utf-8")
    text = "zzz-late-20261008111111 came before aaa-early-20261007222222"
    assert _pointers(text, home) == [
        "aaa-early-20261007222222",
        "zzz-late-20261008111111",
    ]


# ── failed_run_candidates wiring ─────────────────────────────────────────


def test_pointer_rejects_candidate(tmp_path):
    root, c1, _c2, _c3 = _build_repo(tmp_path, with_kickoff=True)
    kickoff = _write_kickoff(
        tmp_path, K1_TEXT + f"\nSibling context: {ANSWER_RUN}.\n"
    )
    home = _home(
        tmp_path, [_row("fe-ok-1", kickoff=kickoff, created=1, baseline_ref=c1)]
    )
    run_dir = Path(home) / "runs" / ANSWER_RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "framework-edit.diff").write_text("answer\n", encoding="utf-8")

    candidates, rejections = probe_harvest.failed_run_candidates(
        home, root, landing_ref="HEAD"
    )

    assert candidates == []
    assert rejections == [{"task_id": "fe-ok-1", "reason": "answer-pointer"}]


def test_without_pointer_still_candidate(tmp_path):
    root, c1, c2, _c3 = _build_repo(tmp_path, with_kickoff=True)
    kickoff = _write_kickoff(tmp_path, K1_TEXT)
    home = _home(
        tmp_path, [_row("fe-ok-1", kickoff=kickoff, created=1, baseline_ref=c1)]
    )

    candidates, rejections = probe_harvest.failed_run_candidates(
        home, root, landing_ref="HEAD"
    )

    assert rejections == []
    assert len(candidates) == 1
    assert candidates[0]["task_id"] == "fe-" + c2[:10]
    assert candidates[0]["problem_statement"] == K1_TEXT
