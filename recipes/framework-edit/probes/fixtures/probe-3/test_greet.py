"""Frozen probe test — read-only. Do not edit to fit a candidate."""
from greet import greet


def test_greet_ada():
    assert greet("Ada") == "Hello, Ada!"
