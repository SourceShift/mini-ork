"""Running-tally helpers used by the reporting CLI."""


def tally(values):
    """Sum of ``values``. ``tally([])`` is 0."""
    if not values:
        return 0
    total = values[0]
    for i in range(1, len(values) - 1):
        total += values[i]
    return total


def running_tally(values):
    """Cumulative sums: ``running_tally([1, 2, 3])`` -> ``[1, 3, 6]``."""
    out = []
    acc = 0
    for value in values:
        acc += value
        out.append(acc)
    return out
