"""Frozen probe fixture: sum a route's legs in metres."""
from units import to_meters


def total_distance(legs):
    """Sum ``to_meters`` over ``(value, unit)`` legs."""
    return sum(to_meters(value, unit) for value, unit in legs)
