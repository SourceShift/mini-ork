"""Per-lane secret scoping + redaction for remote node spawns (remote-nodes-13).

D6: a remote spawn carries exactly the secrets of the one lane being
dispatched, never the control plane's ambient set.

* :func:`lane_secret_names` / :func:`scoped_env` — which secrets a lane carries
  and their values, gated by the environment profile's ``secrets:`` list.
  ``providers.dispatch_model`` calls these (it knows the lane) and passes the
  NAMES to the spawn layer in ``MO_LANE_SECRET_KEYS``; ``core`` forwards only
  those and drops every other secret-named key.
* :func:`secret_values` + :class:`Redactor` — mask secret values in proc
  output on the node-agent (bytes) and in the client's live file (text).
* :func:`lane_egress_hosts` — the lane endpoints an ``allowlist`` session's
  proxy must admit.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from .environments import Environment, is_lookalike_secret


__all__ = [
    "LANE_SECRET_KEYS_ENV",
    "SecretNotPermittedError",
    "is_secret_name",
    "lane_egress_hosts",
    "lane_secret_names",
    "make_redactor",
    "scoped_env",
    "secret_values",
]


# Anthropic-native lanes authenticate the claude CLI from the ambient env, not
# from ``spec.env``. (Both names also match the suffix patterns; listing them
# keeps the native-lane rule explicit.)
_ANTHROPIC_NATIVE_AUTH_KEYS: frozenset[str] = frozenset(
    {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"}
)

# The dispatch layer tells the spawn layer which secret NAMES may cross to the
# node for this one lane (names only, comma-separated; never values).
LANE_SECRET_KEYS_ENV = "MO_LANE_SECRET_KEYS"

# Provider defaults for lanes that declare no base_url.
_DEFAULT_LANE_HOSTS: dict[str, tuple[str, ...]] = {
    "anthropic-native": ("api.anthropic.com",),
    "codex-native": ("api.openai.com",),
}


def is_secret_name(key: str) -> bool:
    """The one secret-name rule for remote spawns: the profile lookalike
    patterns (``*_KEY``, ``*_TOKEN``, ``*_SECRET``, ``*PASSWORD*``) plus the
    anthropic-native auth carriers."""
    return isinstance(key, str) and (is_lookalike_secret(key)
                                     or key in _ANTHROPIC_NATIVE_AUTH_KEYS)


def secret_values(env: Mapping[str, str]) -> list[str]:
    """Values of the secret-named keys in ``env`` — what redaction masks.
    Only secrets: masking every env value mangled paths and ids in output."""
    return [str(v) for k, v in env.items() if is_secret_name(k) and v]


class SecretNotPermittedError(ValueError):
    """A lane secret is not on the environment profile's ``secrets`` list.

    Raised by the dispatch layer BEFORE the spawn (no network call). Carries
    ``lane`` / ``name`` / ``profile_path`` for fix hints.
    """

    def __init__(self, lane: str, name: str, *, profile_path: str = "") -> None:
        suffix = f" (profile file: {profile_path})" if profile_path else ""
        super().__init__(
            f"secret {name!r} is not permitted for lane {lane!r}; "
            f"add it to the profile's 'secrets' list{suffix}"
        )
        self.lane = lane
        self.name = name
        self.profile_path = profile_path


def lane_secret_names(
    spec_env: Mapping[str, str],
    *,
    kind: str = "",
    runtime: Mapping[str, str] | None = None,
) -> set[str]:
    """The secret-named keys this ONE lane carries: those the provider builder
    put into ``spec.env``, plus, for an ``anthropic-native`` lane, the claude
    auth carriers present in the control-plane env."""
    names = {k for k in spec_env if is_secret_name(k)}
    if kind == "anthropic-native":
        names |= {k for k in _ANTHROPIC_NATIVE_AUTH_KEYS if (runtime or {}).get(k)}
    return names


def scoped_env(
    spec_env: Mapping[str, str],
    profile: Environment | None,
    *,
    store: Mapping[str, str],
    lane: str,
    kind: str = "",
    api_key_env: str = "",
    runtime: Mapping[str, str] | None = None,
    profile_path: str = "",
) -> dict[str, str]:
    """``{name: value}`` for exactly this lane's secrets, gated by the profile.

    A secret is permitted when ``profile.secrets`` names it — either the key
    the spawn carries (``ANTHROPIC_AUTH_TOKEN``) or the store name it was
    resolved from (the lane's ``api_key_env``, e.g. ``GLM_API_KEY``). With no
    profile bound (a one-off ``MO_NODE_URL`` run) the lane's own secrets pass
    — the scoping still keeps every OTHER secret off the node.

    Values come from the provider-resolved ``spec.env`` (already resolved
    from the local store with shell precedence), else the store, else the
    control-plane env.

    Raises :class:`SecretNotPermittedError` on the first unlisted secret.
    """
    allowed = None if profile is None else set(profile.secrets or [])
    out: dict[str, str] = {}
    for name in sorted(lane_secret_names(spec_env, kind=kind, runtime=runtime)):
        aliases = {name} | ({api_key_env} if api_key_env else set())
        if allowed is not None and not (aliases & allowed):
            raise SecretNotPermittedError(lane, api_key_env or name, profile_path=profile_path)
        value = spec_env.get(name) or store.get(name) or (runtime or {}).get(name) or ""
        if value:
            out[name] = value
    return out


def lane_egress_hosts(registry: Mapping[str, object]) -> list[str]:
    """Hosts the configured lanes talk to (their ``base_url``, else the
    provider default) — what an ``allowlist`` session's proxy must admit."""
    from urllib.parse import urlparse

    hosts: set[str] = set()
    for entry in registry.values():
        if not isinstance(entry, Mapping):
            continue
        base_url = str(entry.get("base_url") or "").strip()
        if base_url:
            try:
                host = urlparse(base_url).hostname or ""
            except ValueError:
                host = ""
            if host:
                hosts.add(host)
        else:
            hosts.update(_DEFAULT_LANE_HOSTS.get(str(entry.get("kind") or ""), ()))
    return sorted(hosts)


def make_redactor(secret_values: Iterable[str], *, min_length: int = 8) -> "Redactor":
    """Build a streaming redactor from the in-memory secret values.

    Used by the node-agent's stdout/stderr writer to keep :class:`Secret`
    values out of ``<pid>.out`` / ``<pid>.err`` (kickoff requirement 2).
    The :class:`Redactor` is a small pure helper — ``re``-based with the
    values sorted longest-first so a short value that is a sub-string of
    a long one cannot survive.
    """
    return Redactor(secret_values, min_length=min_length)


class Redactor:
    """Replace each known secret value in a byte chunk with ``b"***"``.

    Values shorter than ``min_length`` are ignored — the chance of
    accidental false-positive redaction in random output outweighs the
    protection (a 4-character "key" is not a key). The replacement is
    bytes-vs-bytes so the watcher can call this on the raw chunk without
    re-decoding first.
    """

    __slots__ = ("_min_length", "_patterns", "_texts")

    def __init__(self, secret_values: Iterable[str], *, min_length: int = 8) -> None:
        import re

        patterns = []
        for value in secret_values:
            if not isinstance(value, str):
                continue
            if len(value) < min_length:
                continue
            patterns.append(re.compile(re.escape(value.encode("utf-8", "replace"))))
        # Sort longest-first so a short sub-string cannot shadow a longer one.
        patterns.sort(key=lambda p: -len(p.pattern))
        self._patterns = patterns
        self._min_length = min_length
        self._texts = sorted({v for v in secret_values if isinstance(v, str) and len(v) >= min_length},
                             key=len, reverse=True)

    def redact(self, chunk: bytes) -> bytes:
        if not chunk or not self._patterns:
            return chunk
        for pattern in self._patterns:
            chunk = pattern.sub(b"***", chunk)
        return chunk

    def redact_text(self, text: str) -> str:
        """The same masking for already-decoded text (the client's live tee)."""
        for value in self._texts:
            if value in text:
                text = text.replace(value, "***")
        return text
