"""Frozen probe test — read-only. Do not edit to fit a candidate."""
import pytest

from route import total_distance
from units import to_meters


def test_km_conversion():
    assert to_meters(2, "km") == 2000


def test_mixed_units_sum():
    assert total_distance([(1, "km"), (50, "cm")]) == 1000.5


def test_total_distance_rounds_to_three_decimals():
    assert total_distance([(0.1, "m"), (0.2, "m")]) == 0.3


def test_unknown_unit_raises_key_error():
    with pytest.raises(KeyError):
        to_meters(1, "mi")
