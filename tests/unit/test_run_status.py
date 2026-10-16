"""Pins the invariants ``mini_ork/run_status.py`` claims in its docstring.

The module exists so that "is this run over?" has one answer instead of
twenty-one. These tests are what make that claim checkable, and the schema
cross-check is the important one: a status added to the ``task_runs`` CHECK
constraint without a home in ``run_status`` must fail HERE, loudly, rather than
silently read as in-flight on the board.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from mini_ork import run_status

MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"

# The task_runs CHECK constraint, as written by whichever migration last
# installs it. Matched loosely on purpose: a migration that rebuilds the table
# (SQLite cannot ALTER a CHECK) is the only way this list ever changes, and this
# pattern must survive that rewrite.
_CHECK_RE = re.compile(
    r"CHECK\s*\(\s*status\s+IN\s*\((?P<values>[^)]*)\)", re.IGNORECASE
)


def _schema_statuses() -> set[str]:
    """The task_runs status set from the newest migration that declares it.

    Walks every migration in filename order and keeps the LAST match, because a
    later file wins — that is how the rebuild-and-widen idiom works. Raises when
    no migration matches, so the test fails as a broken assumption instead of
    passing vacuously against an empty set.
    """
    newest: tuple[str, set[str]] | None = None
    for path in sorted(MIGRATIONS.glob("*.sql")):
        match = _CHECK_RE.search(path.read_text(encoding="utf-8"))
        if match is None:
            continue
        values = {v.strip().strip("'\"").lower() for v in match.group("values").split(",")}
        values.discard("")
        newest = (path.name, values)
    assert newest is not None, f"no status CHECK found under {MIGRATIONS}"
    return newest[1]


def test_terminal_and_live_partition_every_status() -> None:
    """``TERMINAL | LIVE == STATUSES``, and the two are disjoint.

    Disjointness is the half that matters: a status in both sets would make
    ``is_terminal`` and any wait-loop disagree about the same run.
    """
    assert run_status.TERMINAL | run_status.LIVE == run_status.STATUSES
    assert run_status.TERMINAL & run_status.LIVE == frozenset()


def test_unsuccessful_is_a_subset_of_terminal() -> None:
    """Only an over run can be a failed one — the failed set is a strict subset."""
    assert run_status.UNSUCCESSFUL <= run_status.TERMINAL
    assert run_status.UNSUCCESSFUL != run_status.TERMINAL


def test_not_needed_is_over_but_not_a_defect() -> None:
    """The distinction the module exists for.

    A closed run must be terminal (nothing is coming, so nothing should wait on
    it) and must NOT be unsuccessful (there is no defect to auto-repair).
    """
    assert run_status.is_terminal("not_needed")
    assert "not_needed" not in run_status.UNSUCCESSFUL


def test_the_schema_and_this_module_agree() -> None:
    """Every status the schema admits has exactly one home here.

    This is the test that catches a migration widening the CHECK list while
    ``run_status`` keeps its old copy.
    """
    assert _schema_statuses() == set(run_status.STATUSES)


def test_sql_fragments_match_their_sets() -> None:
    """The SQL literals are derived from the sets, not hand-maintained beside them.

    Pinned as strings so a change to either set shows up as a readable diff in
    the SQL that reaches twenty call sites.
    """
    assert run_status.SQL_TERMINAL == "('failed','not_needed','published','rolled_back')"
    assert run_status.SQL_UNSUCCESSFUL == "('failed','rolled_back')"


def test_every_terminal_status_has_a_board_bucket() -> None:
    """``DEFAULT_BUCKET`` is total over ``TERMINAL`` and invents nothing else.

    A terminal status with no bucket would fall to whatever default a renderer
    happens to have — the ``●`` in-flight mark being the one that already bit.
    """
    assert set(run_status.DEFAULT_BUCKET) == set(run_status.TERMINAL)


def test_the_two_marker_decided_buckets_are_the_only_gap() -> None:
    """``DEFAULT_BUCKET`` maps terminal statuses only — and that is the point.

    ``BUCKETS`` is a superset of the mapped values by exactly
    ``{"working", "needs_you"}``. Neither is a function of the status column:
    every live status reads as ``working``, and ``needs_you`` is decided by
    run-directory markers (``.cost-pause``, ``.review-pause``, a withheld
    publish, a pending retry gate) — a ``failed`` run carrying one of those is
    ``needs_you``, not ``failed``. So a status-mapped bucket for either would be
    a claim the status column cannot support.
    """
    assert set(run_status.DEFAULT_BUCKET.values()) <= set(run_status.BUCKETS)
    assert set(run_status.BUCKETS) - set(run_status.DEFAULT_BUCKET.values()) == {
        "working", "needs_you",
    }


@pytest.mark.parametrize("status", sorted(run_status.STATUSES))
def test_every_status_has_a_label(status: str) -> None:
    """A page that prints a status never falls back to the raw token for a
    status this module knows."""
    assert run_status.label(status) == run_status.LABELS[status]


def test_an_unknown_status_prints_as_itself() -> None:
    """The deliberate fallback: an unrecognised value shows the token the
    database actually holds, which is the thing an operator can grep for."""
    assert run_status.label("some_future_status") == "some_future_status"
    assert run_status.label(None) == ""
