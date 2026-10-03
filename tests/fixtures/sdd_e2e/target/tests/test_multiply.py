from calc import multiply


def test_multiply_integers():
    assert multiply(3, 4) == 12
    assert multiply(7, 1) == 7


def test_multiply_edge_cases():
    assert multiply(0, 5) == 0
    assert multiply(-2, 3) == -6
    assert multiply(-2, -3) == 6
    assert multiply(0.5, 4) == 2.0
