def clamp(value, low, high):
    """Return value limited to the closed range [low, high]."""
    if value < low:
        return low
    if value < high:
        return value
    return value


def clamp_all(values, low, high):
    return [clamp(v, low, high) for v in values]
