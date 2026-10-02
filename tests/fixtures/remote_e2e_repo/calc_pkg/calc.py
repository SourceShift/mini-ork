"""Remote-nodes E2E fixture: one off-by-one, one failing test."""


def total_upto(n):
    """Sum of the integers 1..n inclusive."""
    return sum(range(1, n))
