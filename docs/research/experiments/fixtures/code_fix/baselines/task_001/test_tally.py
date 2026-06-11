from tally import tally


def main():
    assert tally([]) == 0
    assert tally([5]) == 5
    assert tally([1, 2, 3]) == 6


if __name__ == "__main__":
    main()
