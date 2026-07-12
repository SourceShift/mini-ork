from solution import flatten


def test_flattens_already_flat():
    assert flatten([1, 2, 3]) == [1, 2, 3]


def test_flattens_one_level():
    assert flatten([1, [2, 3], 4]) == [1, 2, 3, 4]


def test_flattens_deeply_nested():
    assert flatten([[1, [2, [3, 4]]], 5]) == [1, 2, 3, 4, 5]


def test_empty_list():
    assert flatten([]) == []
