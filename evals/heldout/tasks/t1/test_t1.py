from solution import count_words


def test_counts_words_split_by_whitespace():
    assert count_words("hello world") == 2


def test_handles_tabs_and_newlines():
    assert count_words("hello\tworld\nfoo") == 3


def test_empty_string_is_zero():
    assert count_words("") == 0


def test_multiple_internal_spaces():
    assert count_words("a   b") == 2
