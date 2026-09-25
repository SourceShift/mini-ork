"""Unit tests for the collapse detector (mini_ork/learning/collapse_detector.py).

Hermetic: no DB, no network, no lane. The detector is a pure function over a
promotion history; these tests pin the half-split means arithmetic, the
recommendation ladder, the insufficient-history guard, and the diversity
statistics.
"""
from mini_ork.cli.main import SUBCOMMAND_REGISTRY
from mini_ork.learning import collapse_detector as cd


def _rows(scores, anchors, directives=None):
    directives = directives or [[] for _ in scores]
    return [
        {"step": i, "score": s, "anchor": a, "directives": list(d)}
        for i, (s, a, d) in enumerate(zip(scores, anchors, directives))
    ]


def test_1_paper_signature_exact_arithmetic():
    report = cd.detect(_rows(
        [0.40, 0.50, 0.60, 0.70],
        [0.80, 0.75, 0.60, 0.50],
    ))
    assert report["score_rise"] == 0.2
    assert report["anchor_drop"] == 0.225
    assert report["divergence"] is True
    assert report["collapse"] is True
    assert report["recommendation"] == "halt"


def test_2_both_rising_is_healthy():
    report = cd.detect(_rows(
        [0.4, 0.5, 0.6, 0.7],
        [0.4, 0.5, 0.6, 0.7],
    ))
    assert report["divergence"] is False
    assert report["recommendation"] == "none"


def test_3_anchor_down_score_flat_is_watch():
    report = cd.detect(_rows(
        [0.6, 0.6, 0.55, 0.5],
        [0.8, 0.7, 0.6, 0.5],
    ))
    assert report["divergence"] is False
    assert report["recommendation"] == "watch"


def test_4_insufficient_history_is_never_healthy():
    for n in (0, 1, 2, 3):
        report = cd.detect(_rows([0.4] * n, [0.8] * n))
        assert report["divergence"] is False
        assert report["collapse"] is False
        assert report["recommendation"] == "none"
        assert report["reason"] == "insufficient history (n < 4)"
        assert report["score_rise"] is None
        assert report["anchor_drop"] is None


def test_5_odd_n_splits_two_three():
    report = cd.detect(_rows(
        [1, 2, 3, 4, 5],
        [5, 4, 3, 2, 1],
    ))
    assert report["n"] == 5
    # extra row goes to the second half: mean([1,2]) vs mean([3,4,5])
    assert report["score_rise"] == 2.5
    # mean([5,4]) - mean([3,2,1])
    assert report["anchor_drop"] == 2.5


def test_6_directive_diversity():
    distinct = cd.detect(_rows(
        [0.4, 0.5, 0.6, 0.7],
        [0.8, 0.75, 0.6, 0.5],
        [["a"], ["b"], ["c"], ["d"]],
    ))
    assert distinct["directive_diversity"] == 1.0

    repeated = cd.detect(_rows(
        [0.4, 0.5, 0.6, 0.7],
        [0.8, 0.75, 0.6, 0.5],
        [["a"], ["a"], ["a"], ["a"]],
    ))
    assert repeated["directive_diversity"] < 1.0

    empty = cd.detect(_rows(
        [0.4, 0.5, 0.6, 0.7],
        [0.8, 0.75, 0.6, 0.5],
    ))
    assert empty["directive_diversity"] is None


def test_7_summarize_renders_none_as_dash():
    short = cd.detect(_rows([0.4, 0.5, 0.6], [0.8, 0.7, 0.6]))
    text = cd.summarize(short)
    assert "insufficient history (n < 4)" in text
    assert "-" in text

    halt = cd.detect(_rows(
        [0.40, 0.50, 0.60, 0.70],
        [0.80, 0.75, 0.60, 0.50],
    ))
    assert "halt" in cd.summarize(halt)


def test_8_collapse_check_registered():
    assert "collapse-check" in SUBCOMMAND_REGISTRY
