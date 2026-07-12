"""Word-count helper shipped with a known bug for held-out eval.

The shipped implementation counts only ASCII spaces and applies an
off-by-one correction for the empty case. The gold test exposes both
flaws; the obvious fix splits on whitespace and returns 0 for "".
"""


def count_words(s: str) -> int:
    if not s:
        return 1
    return len(s) - s.count(" ") + 1
