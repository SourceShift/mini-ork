from clamp import clamp


def main():
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10


if __name__ == "__main__":
    main()
