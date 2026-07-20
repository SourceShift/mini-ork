import pytest

from mini_ork.dispatch.llm_dispatch import (
    backoff_seconds_raw,
    glm_fair_usage_retryable,
    throttle_retryable,
)


@pytest.mark.parametrize("message", ["503 overloaded", "429 capacity exceeded",
                                      "502 bad gateway", "connection refused",
                                      "unexpected eof: partial stream"])
def test_retryable_categories_retry_before_limit(message):
    assert throttle_retryable("kimi", message, 1, 1, 3)


@pytest.mark.parametrize("message", ["429 insufficient credits", "401 unauthorized",
                                      "400 invalid request: bad prompt", "content filter triggered"])
def test_terminal_categories_fail_fast(message):
    assert not throttle_retryable("kimi", message, 1, 1, 3)


def test_retry_attempt_bound_and_empty_model():
    assert not throttle_retryable("kimi", "503 overloaded", 1, 3, 3)
    assert not throttle_retryable("kimi", "503 overloaded", 1, 5, 3)
    assert throttle_retryable("kimi", "503 overloaded", 1, 2, 3)
    assert not throttle_retryable("", "503 overloaded", 1, 1, 3)


def test_backoff_cap_and_floor():
    for attempt in range(1, 6):
        delay = backoff_seconds_raw(attempt, 3, 1, _jitter=0)
        assert 1 <= delay <= 3


def test_glm_fair_usage_regression():
    assert glm_fair_usage_retryable("glm", "1313 fair usage policy", 1, 3)
    assert not glm_fair_usage_retryable("glm", "503 overloaded", 1, 3)
    assert not glm_fair_usage_retryable("sonnet", "1313 fair usage policy", 1, 3)
