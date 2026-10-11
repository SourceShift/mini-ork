"""Hermetic tests for ``mini_ork.pm.model`` — the PM vocabulary.

Pins the parts a cross-tool board depends on and a refactor could quietly
break:

* ``IssueState`` is closed and the open/closed sets partition it — a state
  added to the enum without landing in one of the sets would vanish from both
  the board's columns and its "everything closed" view.
* ``Priority`` counts up, and the two provider conventions that count down
  (Linear's 1-is-urgent, mini-ork's ascending int) both come out the right way
  round.  This is the single trap the module exists to hold: the same word
  means opposite orderings in the two systems.
* uids round-trip and refuse a string that is not a uid.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.pm.model import (  # noqa: E402
    CLOSED_STATES,
    OPEN_STATES,
    Issue,
    IssueState,
    LinkKind,
    Priority,
    ProviderIdentity,
    make_uid,
    split_uid,
)


def test_states_are_closed_and_partitioned():
    assert len(IssueState) == 7
    assert OPEN_STATES | CLOSED_STATES == set(IssueState)
    assert OPEN_STATES & CLOSED_STATES == set()
    assert IssueState.DONE in CLOSED_STATES
    assert IssueState.CANCELED in CLOSED_STATES


def test_state_values_are_the_normalized_spelling():
    # The values are what lands in pm_issues.state and in a page's JSON, so
    # they are a wire format, not an implementation detail.
    assert IssueState.IN_PROGRESS == "in_progress"
    assert str(IssueState.IN_REVIEW) == "in_review"


def test_priority_counts_up():
    assert Priority.URGENT > Priority.LOW
    assert Priority.URGENT == 4
    assert Priority.NONE == 0


def test_linear_numbers_urgency_downwards():
    # Linear: 1 urgent, 2 high, 3 medium, 4 low, 0 none.
    assert Priority.from_linear(1) is Priority.URGENT
    assert Priority.from_linear(4) is Priority.LOW
    assert Priority.from_linear(0) is Priority.NONE
    assert Priority.from_linear(None) is Priority.NONE
    # An unknown value is NONE, not a crash: a new option in a remote tool
    # must not take the board down.
    assert Priority.from_linear(9) is Priority.NONE


def test_mini_ork_priority_counts_upwards_and_clamps():
    assert Priority.from_mini_ork(1) is Priority.LOW
    assert Priority.from_mini_ork(3) is Priority.HIGH
    # epics.priority is unbounded above; the scheduler treats anything at or
    # past URGENT as "as soon as possible", so it clamps rather than invents.
    assert Priority.from_mini_ork(4) is Priority.URGENT
    assert Priority.from_mini_ork(99) is Priority.URGENT
    assert Priority.from_mini_ork(None) is Priority.NONE
    assert Priority.from_mini_ork(-1) is Priority.NONE


def test_the_two_conventions_land_the_same_way_round():
    # The whole point: after conversion, "more urgent" is a bigger number for
    # both, even though one source numbers it 1 and the other 4.
    assert Priority.from_linear(1) > Priority.from_linear(4)
    assert Priority.from_mini_ork(4) > Priority.from_mini_ork(1)


def test_uid_round_trip():
    assert make_uid("linear", "ENG-123") == "linear:ENG-123"
    assert split_uid("linear:ENG-123") == ("linear", "ENG-123")
    identity = ProviderIdentity(provider="local", key="add-retry-budget")
    assert identity.uid == "local:add-retry-budget"


def test_uid_splits_on_the_first_colon_only():
    assert split_uid("jira:ENG:1") == ("jira", "ENG:1")


@pytest.mark.parametrize("bad", ["", "no-provider", ":key", "provider:"])
def test_a_string_that_is_not_a_uid_is_refused(bad):
    with pytest.raises(ValueError):
        split_uid(bad)


def test_issue_is_open_follows_its_state():
    identity = ProviderIdentity(provider="local", key="x")
    open_issue = Issue(identity=identity, title="t", state=IssueState.BLOCKED, native_state="blocked")
    done_issue = Issue(identity=identity, title="t", state=IssueState.DONE, native_state="done")
    assert open_issue.is_open is True
    assert done_issue.is_open is False
    assert open_issue.uid == "local:x"


def test_link_kinds_cover_the_edges_the_store_can_hold():
    assert [str(kind) for kind in LinkKind] == [
        "blocks",
        "relates",
        "duplicates",
        "parent",
        "run",
    ]
