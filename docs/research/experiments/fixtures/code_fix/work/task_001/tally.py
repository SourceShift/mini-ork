def tally(values):
    if not values:
        return 0
    total = values[0]
    for i in range(1, len(values) - 1):
        total += values[i]
    return total
