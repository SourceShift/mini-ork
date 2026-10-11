"""Tests for ``mini_ork.intake.trigger`` — when a ticket becomes work.

Pure: no database, no source, no filesystem. The rules read three fields and
this file pins exactly how, including the two ways that reading can go quietly
wrong.

The first test is the one that matters. Everything else here is detail; this is
the property the stage exists to have — **a source with no configured rule
starts nothing**, so the first poll against a real, populated board cannot
launch an agent per ticket. It is a value, not an exception, so the caller
reports it instead of swallowing it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.intake.trigger import (  # noqa: E402
    NO_TRIGGER,
    SETTING_KEYS,
    Trigger,
    matches_trigger,
)
from mini_ork.pm.config import PmConfigError  # noqa: E402
from mini_ork.pm.model import IssueState, Priority  # noqa: E402
from mini_ork.pm.store import IssueRow  # noqa: E402


def _row(
    state: IssueState = IssueState.TODO,
    labels: tuple[str, ...] = ("agent",),
    assignee: str | None = "amir",
) -> IssueRow:
    """A mirror row — the thing the poller actually filters. See the module
    docstring of ``intake.trigger`` for why the rules read this and not an
    ``Issue``."""
    return IssueRow(
        uid="github:acme/app#12",
        provider="github",
        native_key="acme/app#12",
        title="Retry loop drops 429s",
        state=state,
        native_state="open",
        priority=Priority.NONE,
        native_priority=None,
        url="https://github.com/acme/app/issues/12",
        assignee=assignee,
        labels=labels,
        parent_uid=None,
        project="acme/app",
        updated_at="2026-10-11T09:00:00Z",
        synced_at="2026-10-11T09:01:00Z",
    )


# ── the safety property ────────────────────────────────────────────────────


def test_a_source_with_no_rule_starts_nothing():
    """The pinned property: an unconfigured source refuses, and says why."""
    assert Trigger().configured is False
    matched, reason = matches_trigger(_row(), Trigger())
    assert matched is False
    assert reason == NO_TRIGGER


def test_a_recipe_alone_is_not_a_trigger():
    """``trigger_recipe`` says *how* to run, not *what* to run. A source that
    names only a recipe has selected no work, so it must stay inert."""
    rules = Trigger(recipe="book-campaign")
    assert rules.configured is False
    matched, reason = matches_trigger(_row(), rules)
    assert matched is False
    assert reason == NO_TRIGGER


def test_settings_with_nothing_set_are_inert():
    """Blank strings are unset, not rules that match the empty label."""
    rules = Trigger.from_settings({"trigger_label": "  ", "trigger_assignee": ""})
    assert rules == Trigger()
    assert rules.configured is False


def test_every_rule_key_is_named_once():
    """The config comment and this file quote the same key list."""
    assert SETTING_KEYS == (
        "trigger_label",
        "trigger_assignee",
        "trigger_states",
        "trigger_recipe",
    )
    settings = {key: "" for key in SETTING_KEYS}
    settings["trigger_label"] = "agent"
    assert Trigger.from_settings(settings).label == "agent"


# ── the rules ──────────────────────────────────────────────────────────────


def test_a_label_rule_matches_and_says_so():
    matched, reason = matches_trigger(_row(), Trigger(label="agent"))
    assert matched is True
    assert "label 'agent'" in reason


def test_a_label_rule_refuses_without_the_label():
    matched, reason = matches_trigger(_row(labels=("bug",)), Trigger(label="agent"))
    assert matched is False
    assert reason == "no 'agent' label"


def test_a_label_is_matched_exactly():
    """Substring matching would make ``agent-x`` fire an ``agent`` rule."""
    matched, _ = matches_trigger(_row(labels=("agent-x",)), Trigger(label="agent"))
    assert matched is False


def test_an_assignee_rule_names_who_has_it_instead():
    matched, reason = matches_trigger(
        _row(assignee="someone-else"), Trigger(assignee="amir")
    )
    assert matched is False
    assert "assigned to someone-else" in reason
    assert "not 'amir'" in reason


def test_an_assignee_rule_reads_nobody_as_unassigned():
    matched, reason = matches_trigger(_row(assignee=None), Trigger(assignee="amir"))
    assert matched is False
    assert "assigned to nobody" in reason


def test_a_state_rule_compares_enum_members_not_names():
    """The trap this file's dataclass comment warns about.

    ``IssueState`` is a ``StrEnum``; a set of plain strings would miss every
    member because the members hash by name. A tuple compares by ``==``, which
    is the equality they actually have — so the normalized state from a mirror
    row matches the configured one.
    """
    rules = Trigger.from_settings({"trigger_states": "todo,in_review"})
    assert rules.states == (IssueState.TODO, IssueState.IN_REVIEW)
    assert matches_trigger(_row(state=IssueState.TODO), rules)[0] is True
    assert matches_trigger(_row(state=IssueState.IN_REVIEW), rules)[0] is True
    matched, reason = matches_trigger(_row(state=IssueState.BACKLOG), rules)
    assert matched is False
    assert reason == "state 'backlog' is not a trigger state"


def test_rules_are_anded():
    rules = Trigger.from_settings({"trigger_label": "agent", "trigger_assignee": "amir"})
    assert matches_trigger(_row(labels=("agent",), assignee="amir"), rules)[0] is True
    # The label holds, the assignee does not — and the reason says which.
    matched, reason = matches_trigger(_row(labels=("agent",), assignee="bob"), rules)
    assert matched is False
    assert "assigned to bob" in reason


# ── parsing settings ───────────────────────────────────────────────────────


def test_a_state_that_is_not_a_state_is_a_config_error():
    """Fail closed, and fail *loud*: the author believes this rule is in force,
    and dropping the typo would silently widen the trigger to every state."""
    with pytest.raises(PmConfigError) as caught:
        Trigger.from_settings({"trigger_states": "todo, in_reveiw"})
    message = str(caught.value)
    assert "in_reveiw" in message
    assert "not a state" in message


def test_states_parse_from_a_string_or_a_list():
    assert Trigger.from_settings({"trigger_states": "todo"}).states == (IssueState.TODO,)
    assert Trigger.from_settings({"trigger_states": ["TODO", "blocked"]}).states == (
        IssueState.TODO,
        IssueState.BLOCKED,
    )


def test_an_unusable_states_value_is_named():
    with pytest.raises(PmConfigError):
        Trigger.from_settings({"trigger_states": 7})


def test_a_recipe_rides_along_with_a_real_rule():
    rules = Trigger.from_settings({"trigger_label": "agent", "trigger_recipe": "book"})
    assert rules.configured is True
    assert rules.recipe == "book"


def test_to_dict_round_trips_the_rules():
    rules = Trigger.from_settings({"trigger_label": "agent", "trigger_states": "todo"})
    assert rules.to_dict() == {
        "label": "agent",
        "assignee": None,
        "states": ["todo"],
        "recipe": None,
        "configured": True,
    }
