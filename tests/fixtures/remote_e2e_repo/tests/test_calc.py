from calc_pkg.calc import total_upto


def test_total_upto_includes_n():
    assert total_upto(3) == 6
