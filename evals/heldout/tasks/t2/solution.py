"""Flatten helper shipped with a known bug for held-out eval.

The shipped implementation only flattens one level; nested lists past
depth one survive. The gold test exercises a 3-deep nested case; the
obvious fix recurses on items that are still lists.
"""


def flatten(lst):
    out = []
    for item in lst:
        if isinstance(item, list):
            out.extend(item)
        else:
            out.append(item)
    return out
