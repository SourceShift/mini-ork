"""Derived schedule queries built on the merged-interval primitive."""
from intervals import merge


def busy_minutes(intervals):
    """Total minutes covered by ``intervals``, overlaps counted once."""
    return sum(end - start for start, end in merge(intervals))


def free_slots(intervals, window):
    """Gaps inside ``window`` (a ``[start, end]`` pair) with no busy interval."""
    w_start, w_end = window
    cursor = w_start
    slots = []
    for start, end in merge(intervals):
        if end <= cursor:
            continue
        if start > cursor:
            slots.append((cursor, start))
        cursor = end
        if cursor >= w_end:
            break
    if cursor < w_end:
        slots.append((cursor, w_end))
    return slots
