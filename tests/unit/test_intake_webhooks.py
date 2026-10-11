"""Tests for ``mini_ork.intake.webhooks`` — the HMAC and the secret.

No server, no fastapi, no network: this module is the verification and the
decision, and the route that wears it is tested in
``test_web_intake_route.py``. The split is why the interesting failures — a
signature that is *nearly* right, a key that resolves to nothing, a gap that
cannot be judged — are testable at all.

What is pinned:

* a signature is accepted only for exactly the body it signed;
* the comparison is ``hmac.compare_digest``, asserted structurally, because a
  ``==`` here is invisible to a behaviour test and is the whole failure mode;
* a missing secret raises rather than returning ``""`` — an empty secret would
  make every signature computable by anyone;
* the tier is off unless the environment asks for it.
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.intake import webhooks  # noqa: E402
from mini_ork.pm.config import PmConfig, SourceConfig  # noqa: E402
from mini_ork.pm.provider import ProviderError  # noqa: E402

BODY = b'{"action": "opened", "issue": {"number": 12}}'
SECRET = "correct horse battery staple"


def _config(**settings: object) -> PmConfig:
    return PmConfig(
        sources={"acme": SourceConfig(id="acme", enabled=True, settings=settings)},
        path=None,
    )


# ── the signature ─────────────────────────────────────────────────────────


def test_the_signature_is_the_hmac_github_computes():
    """Checked against a hand-rolled computation, not against ``sign`` itself,
    so a change to the algorithm cannot pass by agreeing with the test."""
    expected = hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest()
    assert webhooks.sign(SECRET, BODY) == f"sha256={expected}"


def test_a_valid_signature_verifies():
    header = webhooks.sign(SECRET, BODY)
    assert webhooks.verify(SECRET, BODY, header) is True


def test_a_signature_for_a_different_body_does_not():
    assert webhooks.verify(SECRET, b"{}", webhooks.sign(SECRET, BODY)) is False


def test_a_signature_under_a_different_secret_does_not():
    assert webhooks.verify(SECRET, BODY, webhooks.sign("another secret", BODY)) is False


def test_a_missing_or_unfamiliar_header_is_false_not_an_error():
    for header in (None, "", "   ", "deadbeef", "md5=abc", "sha256="):
        assert webhooks.verify(SECRET, BODY, header) is False


def test_the_comparison_is_constant_time():
    """Structural on purpose. A ``==`` here is correct-looking, passes every
    behaviour test above, and leaks the digest byte by byte to anyone who can
    time the endpoint — so the assertion is about the source, not the result."""
    source = inspect.getsource(webhooks.verify)
    assert "compare_digest" in source
    assert "== " not in source and "==\n" not in source


# ── the secret ────────────────────────────────────────────────────────────


def test_the_secret_is_resolved_from_the_vault_by_key_name(monkeypatch):
    monkeypatch.setenv("MO_TEST_WEBHOOK_SECRET", SECRET)
    value = webhooks.webhook_secret(
        "acme", config=_config(webhook_secret_env="MO_TEST_WEBHOOK_SECRET")
    )
    assert value == SECRET


def test_a_source_with_no_secret_setting_raises(monkeypatch):
    with pytest.raises(ProviderError) as raised:
        webhooks.webhook_secret("acme", config=_config())
    assert "webhook_secret_env" in str(raised.value)


def test_a_key_that_resolves_to_nothing_raises(monkeypatch):
    """The fail-closed property: an empty value must never be read as "no
    signature needed", which would turn the route into an open endpoint."""
    monkeypatch.delenv("MO_TEST_ABSENT_SECRET", raising=False)
    with pytest.raises(ProviderError) as raised:
        webhooks.webhook_secret(
            "acme", config=_config(webhook_secret_env="MO_TEST_ABSENT_SECRET")
        )
    assert "MO_TEST_ABSENT_SECRET" in str(raised.value)


def test_an_undeclared_source_raises(monkeypatch):
    monkeypatch.setenv("MO_TEST_WEBHOOK_SECRET", SECRET)
    with pytest.raises(ProviderError) as raised:
        webhooks.webhook_secret(
            "ghost", config=_config(webhook_secret_env="MO_TEST_WEBHOOK_SECRET")
        )
    assert "ghost" in str(raised.value)


def test_the_secret_never_comes_from_the_config_file(monkeypatch):
    """A secret written into ``config/pm.yaml`` is not read, however it is
    spelled. Only the *name* of a vault key is."""
    monkeypatch.delenv("plaintext-secret", raising=False)
    with pytest.raises(ProviderError):
        webhooks.webhook_secret("acme", config=_config(webhook_secret="plaintext-secret"))


# ── the gap, the switch, the body ─────────────────────────────────────────


def test_the_protection_gap_is_an_answer_either_way():
    """Whatever this machine's chain is, the answer is ``None`` or a reason —
    never an exception escaping into a 500 on a route that only POSTs."""
    gap = webhooks.protection_gap()
    assert gap is None or (isinstance(gap, str) and gap.strip())


def test_the_tier_is_off_unless_the_environment_asks_for_it(monkeypatch):
    for value in ("", "0", "false", "no", "off"):
        monkeypatch.setenv(webhooks.ENABLED_ENV, value)
        assert webhooks.webhooks_enabled() is False
    for value in ("1", "true", "yes", "on"):
        monkeypatch.setenv(webhooks.ENABLED_ENV, value)
        assert webhooks.webhooks_enabled() is True


def test_a_body_that_is_not_a_json_object_is_refused():
    for body in (b"not json", b"[1, 2]", b'"a string"', b"", b"\xff\xfe"):
        with pytest.raises(ValueError):
            webhooks.parse_payload(body)


def test_a_json_object_parses():
    assert webhooks.parse_payload(BODY) == {"action": "opened", "issue": {"number": 12}}
