"""Frozen probe fixture: unit conversion (m and cm only)."""

_TO_METERS = {"m": 1.0, "cm": 0.01}


def to_meters(value, unit):
    """Convert ``value`` in ``unit`` to metres; unknown units raise KeyError."""
    return value * _TO_METERS[unit]
