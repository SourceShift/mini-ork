"""Interval primitives for the booking scheduler."""


def merge(intervals):
    """Merge overlapping ``[start, end]`` intervals into a minimal sorted list.

    Intervals that merely touch (``a.end == b.start``) are merged: the schedule
    has no gaps of zero length.
    """
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: iv[0])
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    return [tuple(iv) for iv in merged]


def contains(intervals, point):
    """True when ``point`` falls inside any of ``intervals`` (end-exclusive)."""
    for start, end in merge(intervals):
        if start <= point < end:
            return True
    return False
