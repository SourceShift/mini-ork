"""Tests for ``POST /api/v1/intake/{source}`` — the tier-B route.

Driven through a real ``TestClient``, because the things worth pinning here are
status codes and the order the checks happen in, and those only exist at the
HTTP layer: a switched-off tier must 404 rather than 403, a replay must 200
rather than 409, and a wrong signature must not be distinguishable from a
missing one.

Provider, trigger lookup, home and the vault's protection gap are all injected;
``launch_run``/``steer_run`` are monkeypatched. Nothing here spawns a process or
opens a socket.

What is pinned beyond the codes:

* the same delivery twice starts **one** run — the provider's own delivery id
  is the replay key, not the ticket's text;
* a webhook cannot start work the poller would refuse: the trigger rules apply
  here too, and an unconfigured source answers "skipped", not "launched";
* the ticket is re-read from the source, so a payload that says "open" about a
  ticket that is now closed starts nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.intake import webhooks  # noqa: E402
from mini_ork.intake.trigger import NO_TRIGGER  # noqa: E402
from mini_ork.pm import registry  # noqa: E402
from mini_ork.pm.config import PmConfig, SourceConfig  # noqa: E402
from mini_ork.pm.model import Issue, IssueState, ProviderIdentity  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.threads import store  # noqa: E402

SOURCE = "acme"
SECRET_KEY = "MO_TEST_HOOK_SECRET"
SECRET = "a shared secret"
UID = "acme:acme/app#12"
DELIVERY = "d-1111-2222"


class _Source:
    """Only what the route and the dispatcher reach for."""

    id = SOURCE
    label = "Acme"

    def __init__(self, *issues: Issue) -> None:
        self.issues = {issue.uid: issue for issue in issues}

    def uid_from_webhook(self, payload):
        if payload.get("action") in ("deleted", "transferred"):
            return None
        issue = payload.get("issue")
        if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
            return None
        return f"{self.id}:acme/app#{issue['number']}"

    def get(self, uid: str) -> Issue:
        return self.issues[uid]

    def comments(self, uid: str):
        return ()


def _issue(*, state: IssueState = IssueState.TODO) -> Issue:
    return Issue(
        identity=ProviderIdentity(provider=SOURCE, key="acme/app#12", project="acme/app"),
        title="Retry loop drops 429s",
        state=state,
        native_state=str(state),
        description="Body.",
        labels=("agent",),
        assignee="amir",
        updated_at="2026-10-11T09:00:00Z",
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


@pytest.fixture
def db(home: Path) -> str:
    return str(home / "state.db")


@pytest.fixture
def launches(monkeypatch) -> list[dict]:
    import mini_ork.web.control as control

    calls: list[dict] = []

    def fake(home, recipe, kickoff, run_id=None):
        calls.append({"recipe": recipe, "kickoff": kickoff, "run_id": run_id})
        return {
            "ok": True,
            "run_id": run_id,
            "recipe": recipe,
            "pid": 4242,
            "kickoff_path": str(Path(home) / "runs-inbox" / f"{run_id}.md"),
            "log_path": str(Path(home) / "logs" / f"{run_id}.log"),
        }

    monkeypatch.setattr(control, "launch_run", fake)
    monkeypatch.setattr(
        control, "steer_run", lambda db, run_id, message, **kw: {"ok": True, "steering_id": 1}
    )
    return calls


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv(webhooks.ENABLED_ENV, "1")
    monkeypatch.setenv(SECRET_KEY, SECRET)


@pytest.fixture
def open_gap(monkeypatch):
    """Lift the vault's protection gap so the route reaches its other checks."""
    import mini_ork.web.routes.intake as route

    monkeypatch.setattr(route.webhooks, "protection_gap", lambda: None)


@pytest.fixture
def registered():
    added: list[str] = []

    def add(provider):
        registry.register_provider(provider, replace=True)
        added.append(provider.id)
        return provider

    yield add
    for source_id in added:
        try:
            registry.unregister_provider(source_id)
        except KeyError:
            pass


@pytest.fixture
def configured(monkeypatch):
    import mini_ork.web.routes.intake as route

    def apply(**settings: object) -> PmConfig:
        base = {"webhook_secret_env": SECRET_KEY}
        base.update(settings)
        configuration = PmConfig(
            sources={SOURCE: SourceConfig(id=SOURCE, enabled=True, settings=base)},
            path=None,
        )
        monkeypatch.setattr(route.pm_config, "load_config", lambda: configuration)
        return configuration

    return apply


@pytest.fixture
def client(home: Path):
    from fastapi.testclient import TestClient

    from mini_ork.web.app import create_app

    with TestClient(create_app(home=home, dev_cors=False)) as test_client:
        yield test_client


def _post(client, payload: dict, *, body: bytes | None = None, delivery: str | None = DELIVERY,
          secret: str = SECRET, source: str = SOURCE, signature: str | None = None):
    raw = body if body is not None else _json_bytes(payload)
    headers = {"content-type": "application/json"}
    if delivery is not None:
        headers[webhooks.DELIVERY_HEADER] = delivery
    headers[webhooks.SIGNATURE_HEADER] = (
        signature if signature is not None else webhooks.sign(secret, raw)
    )
    return client.post(f"/api/v1/intake/{source}", content=raw, headers=headers)


def _json_bytes(payload: dict) -> bytes:
    import json

    return json.dumps(payload).encode("utf-8")


def _opened(number: int = 12) -> dict:
    return {"action": "opened", "issue": {"number": number}}


# ── the gate ──────────────────────────────────────────────────────────────


def test_the_route_is_absent_unless_the_tier_is_enabled(client, configured, registered, monkeypatch):
    """404, not 403: a switched-off endpoint should not announce that it exists."""
    monkeypatch.delenv(webhooks.ENABLED_ENV, raising=False)
    registered(_Source(_issue()))
    configured()
    response = _post(client, _opened())
    assert response.status_code == 404


def test_an_unprotected_secret_chain_refuses_the_whole_tier(
    client, configured, registered, enabled, monkeypatch
):
    """Tier B exists to outgrow an ``env``+``file`` vault; serving it anyway
    would be the security property quietly dropped rather than kept."""
    import mini_ork.web.routes.intake as route

    def gap() -> str:
        return "secrets resolve in the clear, from env; use a sops backend."

    monkeypatch.setattr(route.webhooks, "protection_gap", gap)
    registered(_Source(_issue()))
    configured()
    response = _post(client, _opened())
    assert response.status_code == 503
    assert "sops" in response.json()["error"]


def test_an_undeclared_source_is_a_404(client, registered, enabled, open_gap, monkeypatch):
    import mini_ork.web.routes.intake as route

    monkeypatch.setattr(route.pm_config, "load_config", lambda: PmConfig(sources={}, path=None))
    registered(_Source(_issue()))
    assert _post(client, _opened()).status_code == 404


def test_a_source_with_no_adapter_is_a_404(client, configured, enabled, open_gap):
    """Declared and enabled, but nothing built it in this process."""
    assert _post(client, _opened()).status_code == 404


def test_a_source_that_declares_no_secret_key_is_a_503(
    client, registered, enabled, open_gap, monkeypatch
):
    import mini_ork.web.routes.intake as route

    monkeypatch.setattr(
        route.pm_config,
        "load_config",
        lambda: PmConfig(
            sources={SOURCE: SourceConfig(id=SOURCE, enabled=True, settings={})}, path=None
        ),
    )
    registered(_Source(_issue()))
    response = _post(client, _opened())
    assert response.status_code == 503
    # The reason names the vault key, which is the operator's business; the
    # sender gets a sentence that says nothing about the server's config.
    assert SECRET_KEY not in response.text


# ── the signature ─────────────────────────────────────────────────────────


def test_a_wrong_signature_is_a_401_without_saying_why(
    client, configured, registered, enabled, open_gap, launches
):
    registered(_Source(_issue()))
    configured(trigger_label="agent")
    response = _post(client, _opened(), secret="the wrong secret")
    assert response.status_code == 401
    assert launches == []
    # Same body for a missing header as for a wrong digest.
    missing = _post(client, _opened(), signature="")
    assert missing.status_code == 401
    assert missing.json() == response.json()


def test_a_signature_over_different_bytes_than_were_sent_is_refused(
    client, configured, registered, enabled, open_gap
):
    """The HMAC is over the raw body, so re-encoding the JSON breaks it — which
    is the property that makes the check meaningful."""
    registered(_Source(_issue()))
    configured(trigger_label="agent")
    raw = b'{ "action" : "opened" , "issue" : { "number" : 12 } }'
    response = _post(
        client, {}, body=raw, signature=webhooks.sign(SECRET, _json_bytes(_opened()))
    )
    assert response.status_code == 401


# ── the dispatch ──────────────────────────────────────────────────────────


def test_a_valid_delivery_starts_one_run(client, configured, registered, enabled, open_gap,
                                         launches, db):
    registered(_Source(_issue()))
    configured(trigger_label="agent", trigger_recipe="code-fix")
    response = _post(client, _opened())

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["action"] == "launched"
    assert body["run_id"] == "acme-acme-app-12"
    assert len(launches) == 1
    assert launches[0]["recipe"] == "code-fix"
    assert len(store.runs_for("acme-acme-app-12", db=db)) == 1


def test_the_same_delivery_twice_starts_one_run(client, configured, registered, enabled,
                                                open_gap, launches):
    """GitHub retries until it sees a 2xx, so a redelivery must be a no-op — and
    a 2xx, because any 4xx would make it retry forever."""
    registered(_Source(_issue()))
    configured(trigger_label="agent")
    first = _post(client, _opened())
    second = _post(client, _opened())

    assert first.json()["action"] == "launched"
    assert second.status_code == 200
    assert second.json()["action"] == "ignored"
    assert "already" in second.json()["detail"]
    assert len(launches) == 1


def test_a_second_delivery_for_the_same_ticket_steers_the_live_run(
    client, configured, registered, enabled, open_gap, launches
):
    """A *new* delivery is a new arrival, not a replay: it must reach the thread
    that is already working the ticket rather than fold away."""
    registered(_Source(_issue()))
    configured(trigger_label="agent")
    _post(client, _opened())
    again = _post(client, _opened(), delivery="d-9999")

    assert again.json()["action"] == "steered"
    assert again.json()["thread_id"] == "acme-acme-app-12"
    assert len(launches) == 1


def test_an_unconfigured_source_skips_rather_than_starts(
    client, registered, enabled, open_gap, launches, monkeypatch
):
    """The trigger safety holds on this path too: a webhook is another way in,
    not a way around the rules."""
    import mini_ork.web.routes.intake as route

    monkeypatch.setattr(
        route.pm_config,
        "load_config",
        lambda: PmConfig(
            sources={
                SOURCE: SourceConfig(
                    id=SOURCE, enabled=True, settings={"webhook_secret_env": SECRET_KEY}
                )
            },
            path=None,
        ),
    )
    registered(_Source(_issue()))
    response = _post(client, _opened())
    assert response.status_code == 200
    assert response.json()["action"] == "skipped"
    assert response.json()["detail"] == NO_TRIGGER
    assert launches == []


def test_a_non_matching_ticket_is_skipped(client, configured, registered, enabled, open_gap,
                                          launches):
    registered(_Source(_issue()))
    configured(trigger_label="ship-it")
    response = _post(client, _opened())
    assert response.json()["action"] == "skipped"
    assert launches == []


def test_a_closed_ticket_is_not_started_from_a_stale_payload(
    client, configured, registered, enabled, open_gap, launches
):
    """The payload said "opened"; the source is the authority, and it says the
    ticket is done. A webhook is a notification, not the data."""
    registered(_Source(_issue(state=IssueState.DONE)))
    configured(trigger_label="agent")
    response = _post(client, _opened())
    assert response.json()["action"] == "skipped"
    assert "done" in response.json()["detail"]
    assert launches == []


def test_a_payload_that_names_no_issue_is_ignored(client, configured, registered, enabled,
                                                  open_gap, launches):
    """A push, a pull request, a transfer: authentic, and nothing to do."""
    registered(_Source(_issue()))
    configured(trigger_label="agent")
    for payload in (
        {"action": "deleted", "issue": {"number": 12}},
        {"action": "opened", "pull_request": {"number": 3}, "issue": {"number": 3}},
        {"action": "created", "pusher": {"name": "amir"}},
    ):
        response = _post(client, payload)
        assert response.status_code == 200, payload
        assert response.json()["action"] == "ignored", payload
    assert launches == []


def test_a_body_that_is_not_an_object_is_a_400(client, configured, registered, enabled,
                                               open_gap):
    registered(_Source(_issue()))
    configured(trigger_label="agent")
    response = _post(client, {}, body=b"[1, 2, 3]")
    assert response.status_code == 400


def test_an_oversized_body_is_refused_before_the_signature(client, configured, registered,
                                                           enabled, open_gap):
    registered(_Source(_issue()))
    configured(trigger_label="agent")
    big = b"x" * (webhooks.MAX_BODY_BYTES + 1)
    response = _post(client, {}, body=big, signature=webhooks.sign(SECRET, big))
    assert response.status_code == 413
