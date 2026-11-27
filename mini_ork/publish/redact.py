"""Redaction for public run bundles — the half that has to be right.

A public run page is a capability URL: whoever has the link can read every
byte of the bundle. So the bundle is scrubbed on the producer (here) *and*
re-scanned on the consumer (the control plane's ``guard.ts``), which refuses
the upload when anything still looks like a credential. Two independent
passes, deliberately: the producer replaces what it recognises, and the
consumer is the one that says "no".

Two kinds of work:

1. **Replace** — a string that matches a secret/PII rule is rewritten in
   place and counted. The rule names here are the contract shared with the
   consumer; keep the two lists identical.
2. **Drop** — a dict key whose *name* says it holds a credential
   (``api_key``, ``authorization``, …) keeps its key but loses its value, so
   the bundle's shape stays readable while the value never ships.

The final pass walks every string in the finished bundle, not just the fields
this module wrote: a tool result, a prompt, or an error message can carry a
token just as easily as a config file can.

Run the module directly for a quick scan of a file::

    python3 -m mini_ork.publish.redact some.jsonl
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "REDACTION_RULES",
    "SENSITIVE_KEY_RE",
    "RedactionReport",
    "redact_text",
    "redact_value",
    "scan_for_secrets",
    "scan_for_secrets_obj",
]


# ── Rules ───────────────────────────────────────────────────────────────────
#
# Order matters only for readability: every rule runs over the *current* text,
# so a later rule sees the replacements of an earlier one. Put the longest /
# most specific patterns first (a private-key block before the generic
# ``env_secret`` assignment, a URL with credentials before the bare token).

REDACTION_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]{0,8192}?"
            r"-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        "[private key redacted]",
    ),
    (
        "url_credentials",
        # Any scheme, not just http(s): a leaked ``postgres://`` / ``redis://``
        # / ``amqp://`` DSN carries the same password, and without this rule the
        # email pattern below would quietly rewrite it to ``user:<email>@``.
        re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/\s:@]{1,64}:[^/\s@]{1,128}@"),
        r"\g<scheme>[redacted]@",
    ),
    (
        "auth_header",
        re.compile(
            r"(?i)\b(x-api-key|x-auth-token|authorization|cookie|set-cookie)"
            # The value must start with a non-space: ``Authorization:`` with an
            # empty value leaks nothing, and requiring a real one keeps the
            # residual scan from flagging the space our own marker left behind.
            r"\s*:\s*[^\s\r\n][^\r\n]*"
        ),
        "[auth header redacted]",
    ),
    (
        "bearer_token",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{12,}"),
        "Bearer [redacted]",
    ),
    (
        "env_secret",
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|"
            r"ACCESS_KEY|PRIVATE_KEY|CLIENT_SECRET|DATABASE_URL|DSN)[A-Z0-9_]*)"
            # Spaces/tabs only, never ``\s``: the rule runs a second time during
            # the residual scan, when ``TOKEN=[redacted]`` has already lost its
            # value. With ``\s*`` the separator would cross the newline and
            # swallow the next line of the transcript as the "secret".
            r"""([ \t]*[:=][ \t]*)["']?(?!re\.|os\.|self\.|str\.|Path\.)"""
            r"""([^\s"',;]{12,})"""
        ),
        r"\1\2[redacted]",
    ),
    (
        "provider_key",
        re.compile(
            r"\b(?:sk-ant-[A-Za-z0-9_\-]{16,}|sk-proj-[A-Za-z0-9_\-]{16,}"
            r"|sk-[A-Za-z0-9_\-]{20,}|xai-[A-Za-z0-9_\-]{16,}"
            r"|gsk_[A-Za-z0-9_\-]{16,}|pplx-[A-Za-z0-9_\-]{16,})"
        ),
        "[provider key redacted]",
    ),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})"),
        "[github token redacted]",
    ),
    (
        "slack_token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
        "[slack token redacted]",
    ),
    (
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "[aws access key redacted]",
    ),
    (
        "google_api_key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
        "[google api key redacted]",
    ),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
        ),
        "[jwt redacted]",
    ),
    (
        "ssh_remote",
        # ``git@github.com:org/repo.git`` is the shape an ssh remote takes, and
        # the email rule below would rewrite it to ``<email>:org/repo.git`` —
        # losing the repo the run was about. Normalise to a scheme URL first:
        # host + path survive, the shared ``git`` user (not PII) does not.
        re.compile(r"\b(?:ssh://)?git@([A-Za-z0-9.\-]+):([A-Za-z0-9._/\-]+)"),
        r"ssh://\1/\2",
    ),
    (
        "email",
        # Code decorators (``@pytest.fixture``, ``@router.get``) look exactly
        # like an address with a one-letter local part, so the TLD list
        # excludes the framework names that show up in transcripts.
        re.compile(
            r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\."
            r"(?!(?:fixture|parametrize|mark|route|get|post|put|patch|delete"
            r"|setter|getter|property|staticmethod|classmethod|command"
            r"|callback|register|app|main|error|exception|method|function)\b)"
            r"[A-Za-z]{2,24}\b"
        ),
        "<email>",
    ),
]

# Absolute home / volume paths leak the operator's username and disk layout
# and nothing about the run. Rewritten as ``~/`` / ``<volume>/`` rather than
# deleted, so a file path in an error message stays legible.
_PATH_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("home_path", re.compile(r"/(?:Users|home)/[^/\s\"']+/"), "~/"),
    ("volume_path", re.compile(r"/Volumes/[^/\s\"']+/"), "<volume>/"),
    ("win_home_path", re.compile(r"[A-Za-z]:\\+Users\\+[^\\\s\"']+\\+"), "~/"),
]

# A dict key that names a credential is dropped even when its value does not
# match a pattern (a short password, an opaque internal id).
SENSITIVE_KEY_RE = re.compile(
    r"(?i)^(?:"
    r"api[_-]?key|apikey|authorization|auth|auth[_-]?token|access[_-]?token"
    r"|refresh[_-]?token|id[_-]?token|session[_-]?token|password|passwd|secret"
    r"|client[_-]?secret|private[_-]?key|signing[_-]?key|x-api-key"
    r"|anthropic_api_key|openai_api_key|minimax_api_key|glm_api_key"
    r"|deepseek_api_key|openrouter_api_key|cookie|set-cookie"
    # Shapes the first list missed: a bare token, a qualified one
    # (``github_token``), and values that carry a whole credential inside.
    # Over-dropping a harmless value is the safe direction; the shape of the
    # bundle survives because the key stays.
    r"|token|bearer|passphrase|credentials"
    r"|(?:[a-z0-9]+[_-])+token"
    r"|(?:api|auth|access|secret|signing|private|client|encryption|github"
    r"|gitlab|npm|slack|openai|anthropic|db|database)[_-](?:key|token|password|passwd)"
    r"|database[_-]?url|db[_-]?url|dsn|connection[_-]?string|redis[_-]?url"
    r")$"
)

_REDACTED_VALUE = "[redacted]"


@dataclass
class RedactionReport:
    """What the scrubber changed. Shipped inside the bundle so a reader can
    see the page was filtered rather than spotless."""

    counts: dict[str, int] = field(default_factory=dict)
    dropped_keys: list[str] = field(default_factory=list)
    chars_replaced: int = 0

    def bump(self, rule: str, chars: int) -> None:
        self.counts[rule] = self.counts.get(rule, 0) + 1
        self.chars_replaced += chars

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": dict(sorted(self.counts.items())),
            "dropped_keys": list(dict.fromkeys(self.dropped_keys))[:50],
            "chars_replaced": self.chars_replaced,
        }


def redact_text(text: str, report: RedactionReport | None = None) -> str:
    """Replace every match of every rule in *text*; return the new text."""
    if not report:
        report = RedactionReport()
    out = text
    for rule, pattern, replacement in REDACTION_RULES + _PATH_RULES:
        out, n = pattern.subn(replacement, out)
        if n:
            report.bump(rule, max(len(text) - len(out), n))
    return out


def redact_value(value: Any, report: RedactionReport | None = None, *,
                 _path: str = "") -> Any:
    """Walk a JSON-shaped value, redacting strings and dropping secret keys."""
    if not report:
        report = RedactionReport()
    if isinstance(value, str):
        return redact_text(value, report)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            if SENSITIVE_KEY_RE.match(key_s) and item not in (None, "", [], {}):
                out[key] = _REDACTED_VALUE
                report.dropped_keys.append(f"{_path}.{key_s}" if _path else key_s)
                continue
            out[key] = redact_value(item, report, _path=f"{_path}.{key_s}" if _path else key_s)
        return out
    if isinstance(value, (list, tuple)):
        return [
            redact_value(item, report, _path=f"{_path}[{i}]")
            for i, item in enumerate(value)
        ]
    return value


_MARKERS = (
    "[redacted]",
    "[auth header redacted]",
    "[private key redacted]",
    "[provider key redacted]",
    "[github token redacted]",
    "[slack token redacted]",
    "[aws access key redacted]",
    "[google api key redacted]",
    "[jwt redacted]",
    "<email>",
)


def _without_markers(text: str) -> str:
    """Drop the tokens this scrubber writes before re-scanning.

    ``Authorization: [redacted]`` still *looks* like an auth header to the
    same rule, and ``API_KEY=[redacted]`` still looks like an assignment.
    Removing the marker is what makes the second pass a check for *surviving*
    secrets rather than a check for our own redactions.
    """
    for marker in _MARKERS:
        text = text.replace(marker, "")
    return text


def iter_strings(value: Any):
    """Yield every string inside a JSON-shaped value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_strings(item)


def scan_for_secrets_obj(value: Any) -> list[str]:
    """Rule names still matching any *string value* of *value*.

    Scanning the JSON *text* instead would read ``\\n`` escapes as characters
    and flag a redacted ``Authorization:`` header, so the walk is over
    decoded strings — the same shape a browser will see.
    """
    hits: set[str] = set()
    for text in iter_strings(value):
        hits.update(scan_for_secrets(text))
    return sorted(hits)


def scan_for_secrets(text: str) -> list[str]:
    """Rule names still matching *text*.

    This is the consumer's check. It never rewrites anything: a non-empty
    result means the producer's pass missed something, and the honest
    response is to refuse the upload with the rule names (never the value).
    """
    probe = _without_markers(text)
    hits: list[str] = []
    for rule, pattern, _ in REDACTION_RULES + _PATH_RULES:
        if pattern.search(probe):
            hits.append(rule)
    return hits


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: python3 -m mini_ork.publish.redact <file> [<file> ...]",
              file=sys.stderr)
        return 2
    rc = 0
    for path in argv:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        report = RedactionReport()
        cleaned = redact_text(text, report)
        hits = scan_for_secrets(cleaned)
        print(json.dumps({
            "file": path,
            "counts": report.to_dict()["counts"],
            "residual": hits,
        }))
        if hits:
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
