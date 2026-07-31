"""P1 behavioral verifier tests for live UI surfaces."""
from __future__ import annotations

import pytest

from mini_ork.verify.behavioral import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    Observable,
    ObservableError,
    UiResult,
    run,
    run_ui_check,
)


class FakeUiDriver:
    def __init__(self, *results: UiResult):
        self._results = list(results)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, **kwargs) -> UiResult:
        self.calls.append((url, kwargs))
        index = min(len(self.calls) - 1, len(self._results) - 1)
        return self._results[index]


def _ui(**over) -> Observable:
    data = {
        "surface": "ui",
        "staging_url": "https://staging.example",
        "target": "/signup",
        "expect_visible": ["Welcome"],
        "expect_url": "/dashboard",
    }
    data.update(over)
    return Observable.from_mapping(data)


def test_proven_on_visible_text_and_url_match():
    driver = FakeUiDriver(
        UiResult(True, "https://staging.example/signup", "Welcome back", "/dashboard")
    )
    verdict = run_ui_check(_ui(), driver=driver)
    assert verdict.status == PROVEN
    assert all(check.ok is True for check in verdict.checks)


def test_refuted_when_visible_text_missing_on_reachable_page():
    driver = FakeUiDriver(
        UiResult(True, "https://staging.example/signup", "Try again", "/dashboard")
    )
    verdict = run_ui_check(_ui(), driver=driver)
    assert verdict.status == REFUTED
    assert any(check.name == "visible:Welcome" and check.ok is False for check in verdict.checks)


def test_unverified_when_browser_transport_fails():
    driver = FakeUiDriver(
        UiResult(False, "https://staging.example/signup", "", "", error="browser unavailable")
    )
    assert run_ui_check(_ui(), driver=driver).status == UNVERIFIED


def test_path_escape_rejected_in_target_and_expected_url():
    with pytest.raises(ObservableError):
        _ui(target="../admin")
    with pytest.raises(ObservableError):
        run_ui_check(_ui(expect_url="../admin"), driver=FakeUiDriver())


def test_run_dispatches_ui_driver_with_expanded_url(monkeypatch):
    monkeypatch.setenv("UI_HOST", "https://preview.example")
    driver = FakeUiDriver(
        UiResult(True, "https://preview.example/signup", "Welcome", "/dashboard")
    )
    obs = _ui(
        staging_url="${UI_HOST}",
        form=[{"selector": "#email", "value": "dev@example.com"}],
        submit="#submit",
        waits=["#dashboard"],
    )
    verdict = run(obs, driver=driver)
    assert verdict.status == PROVEN
    assert driver.calls == [
        (
            "https://preview.example/signup",
            {
                "form": [{"selector": "#email", "value": "dev@example.com"}],
                "submit": "#submit",
                "waits": ["#dashboard"],
            },
        )
    ]
