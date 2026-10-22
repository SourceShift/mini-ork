"""Frozen probe fixture: a tiny ledger with one behavioural bug."""


def balance(entries):
    """Sum ``(kind, amount)`` entries: credits add, refunds are added (bug)."""
    total = 0
    for kind, amount in entries:
        if kind == "credit":
            total += amount
        elif kind == "refund":
            total += amount
        else:
            raise ValueError(f"unknown kind: {kind!r}")
    return total
