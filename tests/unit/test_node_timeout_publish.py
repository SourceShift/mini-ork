"""A workflow node's ``timeout_minutes`` reaches the dispatcher — and only it.

``timeout_minutes`` was declared in ``schemas/workflow.schema.json`` from the
start and read by nothing: one stalled tier-4 lens held a panel for the whole
global 25-minute node timeout. The dispatcher reads ``MO_NODE_TIMEOUT_S`` per
lane attempt, so the executor publishes that node's own bound at the node
boundary.

The interesting property is the one a naive implementation breaks: the publish
is a process-wide env write with no restore (``publish_env``), so the next
node must not inherit the previous node's bound. ``_pinned_node_timeout``
remembers the operator's base for exactly that reason, and these tests pin
both halves: the declared product wins, and the base survives it.
"""
from __future__ import annotations

import pytest

from mini_ork.cli import execute_handlers as eh


@pytest.fixture(autouse=True)
def _clean_timeout_env(monkeypatch):
    for key in ("MO_NODE_TIMEOUT_S", "MO_NODE_TIMEOUT_BASE",
                "MO_DEADLINE_SECONDS", "MO_DEADLINE_ARMED"):
        monkeypatch.delenv(key, raising=False)


def test_a_declared_bound_is_published_in_seconds(monkeypatch):
    monkeypatch.setenv("MO_NODE_TIMEOUT_BASE", "5400")
    assert eh._node_timeout_seconds(12) == 720
    assert eh._node_timeout_seconds(1.5) == 90


def test_a_node_without_a_bound_gets_the_operator_base(monkeypatch):
    monkeypatch.setenv("MO_NODE_TIMEOUT_BASE", "5400")
    assert eh._node_timeout_seconds(0) == 5400
    assert eh._node_timeout_seconds(None) == 5400


def test_the_first_publish_pins_the_base_before_capping(monkeypatch):
    """Unset base → read MO_NODE_TIMEOUT_S ONCE, then stop reading it."""
    monkeypatch.setenv("MO_NODE_TIMEOUT_S", "1500")
    assert eh._node_timeout_seconds(0) == 1500
    # The next node publishes its own bound over MO_NODE_TIMEOUT_S...
    eh.publish_env({"MO_NODE_TIMEOUT_S": "120"})
    # ...and the base is still 1500, not the 120 that leaked out.
    assert eh._node_timeout_seconds(0) == 1500


def test_the_run_deadline_caps_a_declared_bound(monkeypatch):
    monkeypatch.setenv("MO_NODE_TIMEOUT_BASE", "5400")
    assert eh._node_timeout_seconds(12, remaining=100) == 100
    # The floor, not a zero-second dispatch, when the budget is nearly spent.
    assert eh._node_timeout_seconds(12, remaining=10) == eh._DISPATCH_TIMEOUT_FLOOR_S


def test_a_later_declared_node_does_not_inherit_the_earlier_bound(monkeypatch):
    """Node A (12 min) then node B (none): B is bounded by the operator base."""
    monkeypatch.setenv("MO_NODE_TIMEOUT_BASE", "5400")
    eh.publish_env({"MO_NODE_TIMEOUT_S": str(eh._node_timeout_seconds(12))})
    assert eh._node_timeout_seconds(0) == 5400
