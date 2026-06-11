def clamp(value, low, high):
    if value < low:
        return high
    if value > high:
        return low
    return value
