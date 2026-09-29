from clamp import clamp, clamp_all


def test_caps_above_high():
    assert clamp(15, 0, 10) == 10


def test_floors_below_low():
    assert clamp(-3, 0, 10) == 0


def test_inside_range_unchanged():
    assert clamp(4, 0, 10) == 4


def test_bounds_inclusive():
    assert clamp(10, 0, 10) == 10
    assert clamp(0, 0, 10) == 0


def test_clamp_all():
    assert clamp_all([-1, 5, 99], 0, 10) == [0, 5, 10]
