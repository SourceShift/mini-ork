from schedule import busy_minutes, free_slots


def test_busy_minutes_empty():
    assert busy_minutes([]) == 0


def test_busy_minutes_sums_disjoint_intervals():
    assert busy_minutes([(1, 3), (5, 8)]) == 5


def test_busy_minutes_counts_an_overlap_once():
    assert busy_minutes([(1, 5), (3, 8)]) == 7


def test_busy_minutes_of_nested_intervals():
    assert busy_minutes([(1, 10), (2, 3)]) == 9


def test_free_slots_between_busy_blocks():
    assert free_slots([(2, 4), (7, 9)], (0, 10)) == [(0, 2), (4, 7), (9, 10)]


def test_free_slots_of_a_nested_busy_block():
    assert free_slots([(1, 10), (2, 3)], (0, 12)) == [(0, 1), (10, 12)]
