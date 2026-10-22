"""Frozen probe test — read-only. Do not edit to fit a candidate."""
import pytest

from ledger import balance


def test_empty_ledger_is_zero():
    assert balance([]) == 0


def test_credit_and_refund_net():
    assert balance([("credit", 10), ("refund", 3)]) == 7


def test_refund_alone_is_negative():
    assert balance([("refund", 5)]) == -5


def test_unknown_kind_raises_value_error():
    with pytest.raises(ValueError):
        balance([("debit", 1)])
