"""The signed-webhook half of intake — tier B, and the one door a stranger uses.

Tier A polls; this is what a source pushes to. The difference is who waits: a
poll discovers a ticket one interval late and costs an outbound request either
way, while a webhook lands the moment the ticket moves and costs an inbound
port. What it buys, it pays for — reachability (``--host 0.0.0.0`` or a reverse
proxy), confidentiality (there is no TLS anywhere in this project, so the body
crosses in cleartext unless a proxy terminates it), and a shared secret that has
to live somewhere better than an environment variable.

**Two controls, and neither substitutes for the other.** The HMAC over the raw
body proves the sender held the secret and that the bytes are unaltered; it says
nothing about secrecy, because anyone who can read the connection can read the
ticket. So the route behind this module refuses outright when the vault reports
:meth:`~mini_ork.dispatch.vault.VaultEnvironment.protection_gap` — a secret on
the ``env``+``file`` chain is exactly the chain this tier exists to outgrow.
That refusal is deliberate and it is why this tier is *not* the one to try on a
laptop; polling is.

Nothing here imports fastapi: this module is the verification and the decision,
``mini_ork.web.routes.intake`` is the HTTP. The split is what keeps the HMAC
testable without a server, which is where the interesting failures are.

**Not to be confused with the agent-server's posture.** ``agent_server.py``
guards its routes with :func:`~mini_ork.web.routes.agent_server.require_local_caller`,
which refuses a browser page that was not served from this machine. That control
is wrong here in both directions: a webhook always comes from off-machine, so
the check would refuse every legitimate call, and a webhook is never a browser,
so the threat it closes (a cross-origin POST from the operator's own tab) is not
the threat. HMAC is the control, and it is mandatory — there is no local-caller
escape hatch on this path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any, Mapping, Protocol, runtime_checkable

from mini_ork.dispatch.secrets import SecretStoreError
from mini_ork.dispatch.vault import vault_environment
from mini_ork.pm import config as pm_config
from mini_ork.pm.provider import ProviderError

#: The whole tier is opt-in, read from the *serving* process's environment, the
#: way ``MO_PTY_ENABLED`` and ``MO_AGENT_SERVER_ALLOW_REMOTE`` are. An endpoint
#: that opens a listener to the world should not appear because someone
#: installed a config file; it appears because an operator asked for it.
ENABLED_ENV = "MO_INTAKE_WEBHOOKS"

_FALSEY = ("", "0", "false", "no", "off")

#: GitHub's signature: ``sha256=<hex digest>`` of the raw body under the
#: webhook secret. The prefix names the algorithm, so a future rotation to
#: ``sha512=`` is a rejected signature rather than a silent mis-parse.
SIGNATURE_HEADER = "x-hub-signature-256"
SIGNATURE_PREFIX = "sha256="

#: The provider's own id for one delivery. GitHub redelivers a webhook until it
#: gets a 2xx, so this is what makes a redelivery a no-op instead of a second
#: turn on the thread. See ``intake_events``.
DELIVERY_HEADER = "x-github-delivery"

#: What happened, for a log line and for refusing an event this source does not
#: act on.
EVENT_HEADER = "x-github-event"

#: A ceiling on the body, applied *before* the signature is checked. A valid
#: signature proves who sent it; it does not make an unbounded read safe, and a
#: 10 MiB issue body is a real thing.
MAX_BODY_BYTES = 1 << 20

#: The setting a source names its webhook secret with, in ``config/pm.yaml``.
SECRET_SETTING = "webhook_secret_env"


@runtime_checkable
class WebhookSource(Protocol):
    """A source that can say which issue a webhook payload names.

    Deliberately *not* part of :class:`~mini_ork.pm.provider.Provider`. A
    provider protocol method is a promise every adapter must keep, and an
    adapter with no inbound webhook — ``local``, which has no remote side at all
    — would have to declare one that can only ever return ``None``. A source
    that wants webhooks implements one extra method; the route checks for it and
    says so when it is missing.
    """

    def uid_from_webhook(self, payload: Mapping[str, Any]) -> str | None: ...


def webhooks_enabled() -> bool:
    """Whether this process was asked to serve the webhook tier."""
    return os.environ.get(ENABLED_ENV, "").strip().lower() not in _FALSEY


def sign(secret: str, body: bytes) -> str:
    """The signature ``body`` should carry. Exposed so a test can be honest."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify(secret: str, body: bytes, header: str | None) -> bool:
    """Whether ``header`` is this secret's signature of exactly ``body``.

    Constant-time, and against the whole string rather than the digest alone:
    the prefix is public, but comparing end to end means there is no branch on
    secret-derived content anywhere in here. A missing header, an unfamiliar
    algorithm, and a wrong digest are all just ``False`` — the route turns that
    into one 401 with one message, so a failed check tells an attacker nothing
    about which part failed.
    """
    if not header:
        return False
    presented = header.strip().lower()
    if not presented.startswith(SIGNATURE_PREFIX):
        return False
    return hmac.compare_digest(sign(secret, body), presented)


def webhook_secret(
    source_id: str, *, config: pm_config.PmConfig | None = None
) -> str:
    """The shared secret for ``source_id``'s webhook, resolved by key name.

    Resolved through the vault, so the same line works whether the key lives in
    the environment, a ``0600 secrets.local.sh``, sops, ``pass`` or OpenBao —
    and **never** from the config file itself.

    Every failure raises. A source that names no key, a key that resolves to
    nothing, and a vault that cannot be read are three different problems and
    all three are fatal to this request: an empty string read as "no signature
    needed" would turn the one control this tier has into an open endpoint.
    """
    declared = (config if config is not None else pm_config.load_config()).sources.get(source_id)
    if declared is None:
        raise ProviderError(
            f"intake: no source {source_id!r} in config/pm.yaml, so it has no webhook"
        )
    key = str(declared.settings.get(SECRET_SETTING) or "").strip()
    if not key:
        raise ProviderError(
            f"intake: source {source_id!r} declares no {SECRET_SETTING}, so there is "
            "no key to resolve — add one naming the vault key that holds the secret"
        )
    try:
        environment = vault_environment()
    except SecretStoreError as exc:
        raise ProviderError(f"intake: the secret vault is unusable: {exc}") from exc
    value = environment.resolve(key)
    if not value:
        raise ProviderError(
            f"intake: no value for {key} from {environment.describe()} — add it to the "
            "vault, or stop serving this source's webhook"
        )
    return value


def protection_gap() -> str | None:
    """Why this deployment's secrets are not protected enough for this tier.

    Delegates to the vault so the wording a person sees on the 503 is the same
    wording ``mini-ork doctor`` shows, and so the rule lives in one place. A
    vault that cannot even be read counts as a gap rather than as an error:
    "cannot tell whether the secrets are protected" is not "they are".
    """
    try:
        return vault_environment().protection_gap()
    except SecretStoreError as exc:
        return f"the secret vault is unusable, so its protection cannot be judged: {exc}"


def parse_payload(body: bytes) -> Mapping[str, Any]:
    """The JSON object a webhook delivered. Raises ``ValueError`` on anything else.

    Signature-verified or not, the body is remote input: it is parsed into a
    mapping or it is refused, never indexed hopefully.
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"the body is not JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("the body is JSON but not an object")
    return payload
