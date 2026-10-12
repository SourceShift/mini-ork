"""The pending-fix start guard blocks the same TASK, not every run of a kickoff path.

Regression (2026-10-07): RSI/goal loops rewrite one kickoff file per cycle for
different steps. A pending fix for step W5-112 was keyed on the kickoff path
alone, so it refused the unrelated step W5-98 (rc=75) and stopped the loop.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mini_ork.gates import oversight_inbox
from mini_ork.recovery import retry_notify


@pytest.fixture(autouse=True)
def _no_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(retry_notify.MO_IGNORE_PENDING_FIX, raising=False)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / ".mini-ork"
    home.mkdir()
    sqlite3.connect(home / "state.db").close()
    return home


def _pend(home: Path, blocks_for: str) -> None:
    oversight_inbox.enqueue(retry_notify.GATE_ID, "run-earlier", "retry", {},
                            blocks_dispatch_for=blocks_for, db_path=str(home / "state.db"))


def test_a_different_step_on_the_same_kickoff_path_is_not_blocked(tmp_path: Path) -> None:
    home = _home(tmp_path)
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# acq-wave5-rsi — cycle for step W5-112\n\nledger…\n")
    _pend(home, retry_notify.dispatch_key(str(kickoff)))

    kickoff.write_text("# acq-wave5-rsi — cycle for step W5-98\n\nledger…\n")
    assert retry_notify.pending_fix_for_kickoff(home, str(kickoff)) is None


def test_the_same_step_is_blocked_even_when_the_rest_of_the_kickoff_changed(tmp_path: Path) -> None:
    home = _home(tmp_path)
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# acq-wave5-rsi — cycle for step W5-91\n\nledger: cycle 14\n")
    _pend(home, retry_notify.dispatch_key(str(kickoff)))

    kickoff.write_text("# acq-wave5-rsi — cycle for step W5-91\n\nledger: cycle 15, what went wrong\n")
    assert retry_notify.pending_fix_for_kickoff(home, str(kickoff)) is not None


def test_a_kickoff_without_a_title_is_keyed_on_its_content(tmp_path: Path) -> None:
    home = _home(tmp_path)
    kickoff = tmp_path / "k.md"
    kickoff.write_text("")
    key_empty = retry_notify.dispatch_key(str(kickoff))
    assert "#sha256:" in key_empty
    _pend(home, key_empty)
    assert retry_notify.pending_fix_for_kickoff(home, str(kickoff)) is not None


def test_legacy_path_only_rows_never_block(tmp_path: Path) -> None:
    home = _home(tmp_path)
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# any step\n")
    _pend(home, str(kickoff.resolve()))  # the pre-fix key: a bare realpath
    assert retry_notify.pending_fix_for_kickoff(home, str(kickoff)) is None


# ── needs_change kinds that need no operator change ────────────────────────


def _hint(kind: str) -> dict:
    """The hint shape ``retry_notify`` stores in the gate's context."""
    return {
        "version": 4,
        "retryable": True,
        "strategy": "resume",
        "from_node": "implementer",
        "needs_change": {"kind": kind, "summary": "…", "evidence": "node_start without node_end"},
    }


def _pend_with_hint(home: Path, blocks_for: str, hint: dict) -> None:
    oversight_inbox.enqueue(retry_notify.GATE_ID, "run-earlier", "retry",
                            {"hint": hint}, blocks_dispatch_for=blocks_for,
                            db_path=str(home / "state.db"))


def test_an_interrupted_step_does_not_block_its_own_redispatch(tmp_path: Path) -> None:
    """A child whose node was interrupted must not be refused at the door.

    The gate's own hint reads ``mini-ork recover <run> --strategy resume`` —
    which re-dispatches this same kickoff. Returning the row made the remedy
    unsatisfiable, so an epic-runner child re-dispatch exited 75 forever
    (run-1791788217-61320, 2026-10-12).
    """
    home = _home(tmp_path)
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# Stage 3 — the communication transports\n")
    _pend_with_hint(home, retry_notify.dispatch_key(str(kickoff)), _hint("interrupted"))

    assert retry_notify.pending_fix_for_kickoff(home, str(kickoff)) is None


def test_a_not_started_step_does_not_block_either(tmp_path: Path) -> None:
    home = _home(tmp_path)
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# Stage 3\n")
    _pend_with_hint(home, retry_notify.dispatch_key(str(kickoff)), _hint("not-started"))

    assert retry_notify.pending_fix_for_kickoff(home, str(kickoff)) is None


def test_a_code_change_step_still_blocks(tmp_path: Path) -> None:
    """Only ``NO_CHANGE_KINDS`` are exempt — a review rejection is still an
    operator change and must refuse the dispatch."""
    home = _home(tmp_path)
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# Stage 3\n")
    _pend_with_hint(home, retry_notify.dispatch_key(str(kickoff)), _hint("code"))

    assert retry_notify.pending_fix_for_kickoff(home, str(kickoff)) is not None


def test_a_row_with_no_readable_hint_still_blocks(tmp_path: Path) -> None:
    """An unreadable gate is not a licence to dispatch."""
    home = _home(tmp_path)
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# Stage 3\n")
    _pend(home, retry_notify.dispatch_key(str(kickoff)))

    assert retry_notify.pending_fix_for_kickoff(home, str(kickoff)) is not None
