"""The `openbao` vault backend against a real server (not a mocked urlopen).

Every OpenBao test in ``tests/unit/test_vault_py.py`` monkeypatches
``urllib.request.urlopen``. That pins the parsing but not the wire: a KV-v1
mount has no ``data.data`` envelope, so ``_fetch_path`` would return ``{}`` and a
lookup would look like an *empty path* rather than a misconfiguration — the
failure mode a mock cannot see.

So this tier asserts what only a live server can: a seeded value comes back over
HTTP, and a role's token is **refused** at another role's path. That second one
is the whole claim of the secure tier — least privilege enforced by OpenBao, not
by mini-ork remembering not to look.

Gated like the other integration tiers: no server, no run. See
``docker/openbao/README.md`` for the one-command setup.
"""
from __future__ import annotations

import os

import pytest

from mini_ork.dispatch.vault import OpenBaoBackend, VaultError

ADDR = os.environ.get("OPENBAO_ADDR", "")
COLLECTOR_TOKEN = os.environ.get("OPENBAO_TOKEN_COLLECTOR", "")
REVIEWER_TOKEN = os.environ.get("OPENBAO_TOKEN_REVIEWER", "")


def _skip_reason() -> str:
    if not ADDR:
        return "OPENBAO_ADDR unset — run: bash docker/openbao/bootstrap.sh"
    if not (COLLECTOR_TOKEN and REVIEWER_TOKEN):
        return "scoped tokens unset — source docker/openbao/openbao.env"
    return ""


pytestmark = pytest.mark.skipif(bool(_skip_reason()), reason=_skip_reason() or "ok")


def _backend(token: str, path: str) -> OpenBaoBackend:
    return OpenBaoBackend(ADDR, paths=[path], token=token)


def test_a_seeded_value_comes_back_over_the_wire():
    backend = _backend(COLLECTOR_TOKEN, "mini-ork/collector")
    assert backend.get("COLLECTOR_DEMO") == "collector-value"


def test_each_role_reads_its_own_path():
    assert _backend(REVIEWER_TOKEN, "mini-ork/reviewer").get("REVIEWER_DEMO") == "reviewer-value"


def test_status_does_a_real_round_trip():
    status = _backend(COLLECTOR_TOKEN, "mini-ork/collector").status()
    assert status.ready is True
    assert "keys" in status.detail


def test_a_scoped_token_is_refused_at_another_roles_path():
    # Least privilege is the server's decision. The collector token has no grant
    # on the reviewer path, so the read is refused rather than silently empty.
    backend = _backend(COLLECTOR_TOKEN, "mini-ork/reviewer")
    with pytest.raises(VaultError, match="not permitted"):
        backend.get("REVIEWER_DEMO")


def test_an_invalid_token_is_refused():
    backend = _backend("bao-invalid-token", "mini-ork/collector")
    with pytest.raises(VaultError, match="not permitted"):
        backend.get("COLLECTOR_DEMO")
