from tally import running_tally, tally


def test_tally_sums_every_value():
    assert tally([1, 2, 3]) == 6


def test_tally_single_element():
    assert tally([5]) == 5


def test_tally_empty_is_zero():
    assert tally([]) == 0


def test_tally_with_negatives():
    assert tally([-1, 4, -2]) == 1


def test_running_tally_is_cumulative():
    assert running_tally([1, 2, 3]) == [1, 3, 6]
