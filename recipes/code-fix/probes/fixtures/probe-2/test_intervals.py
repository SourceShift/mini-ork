from intervals import contains, merge


def test_merge_empty():
    assert merge([]) == []


def test_merge_disjoint_is_sorted():
    assert merge([(4, 6), (1, 3)]) == [(1, 3), (4, 6)]


def test_merge_overlapping():
    assert merge([(1, 4), (3, 6)]) == [(1, 6)]


def test_merge_touching_is_merged():
    assert merge([(1, 3), (3, 6)]) == [(1, 6)]


def test_merge_nested_keeps_the_outer_end():
    assert merge([(1, 10), (2, 3)]) == [(1, 10)]


def test_merge_nested_within_a_later_interval():
    assert merge([(0, 2), (5, 20), (6, 7)]) == [(0, 2), (5, 20)]


def test_contains_inside_a_nested_span():
    assert contains([(1, 10), (2, 3)], 7) is True


def test_contains_outside():
    assert contains([(1, 10), (2, 3)], 11) is False
