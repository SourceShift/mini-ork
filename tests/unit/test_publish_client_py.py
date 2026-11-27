"""URL resolution for ``mini_ork.publish.client``.

Three shapes are in use: the console base (BFF at ``/api/public/runs``), a
control-plane base ending in ``/api/v1``, and the exact publish path. Getting
this wrong is a 404 at the end of a long build, so it is pinned here.
"""
from __future__ import annotations

import pytest

from mini_ork.publish.client import PublishError, publish, resolve_publish_url


def test_console_base_uses_the_bff_path():
    assert resolve_publish_url("https://console.miniork.dev") == (
        "https://console.miniork.dev/api/public/runs"
    )
    assert resolve_publish_url("https://console.miniork.dev/") == (
        "https://console.miniork.dev/api/public/runs"
    )


def test_api_v1_base_appends_public_runs():
    assert resolve_publish_url("http://127.0.0.1:7823/api/v1") == (
        "http://127.0.0.1:7823/api/v1/public-runs"
    )


def test_exact_endpoint_is_kept():
    url = "https://console.miniork.dev/api/v1/public-runs"
    assert resolve_publish_url(url) == url


def test_publish_fails_closed_without_a_key():
    with pytest.raises(PublishError, match="API key"):
        publish({"schema": "x"}, {}, api_url="https://example.invalid", api_key="")
