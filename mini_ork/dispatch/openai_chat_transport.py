"""Direct-HTTP transport for OpenAI-compatible ``/chat/completions`` endpoints.

The codex CLI dropped ``wire_api = "chat"`` support (now demands OpenAI's
proprietary ``/responses``), which stranded every OpenAI-*compatible* chat
endpoint that only speaks ``/chat/completions`` — OpenRouter, Groq, Together,
local vLLM, etc. This module is the ``openai-chat`` provider kind's transport:
a self-contained, stdlib-only POST to ``{base_url}/chat/completions`` that reads
the prompt on stdin and prints the assistant text on stdout, matching the
dispatch core's raw-stdout contract (``core.py`` uses stdout verbatim when a
spec has no ``parse_text``).

Configuration rides on the environment the provider builder injects:

    MO_OAI_BASE_URL   base URL (``.../v1``); ``/chat/completions`` is appended
    MO_OAI_MODEL      model slug (e.g. ``stealth/ox-alpha``)
    MO_OAI_ENV_KEY    NAME of the env var holding the API key
    MO_OAI_MAX_TOKENS completion budget (default 2048 — reasoning models burn
                      this on hidden reasoning tokens and return null content
                      when it is too small)
    MO_OAI_TEMPERATURE   optional float
    MO_OAI_TIMEOUT_S     request timeout seconds (default 120)

``--print --output-format text`` are accepted as no-op flags so the argv shape
matches every other harness CLI the dispatcher spawns.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def _fail(message: str, code: int) -> int:
    sys.stderr.write(f"[openai_chat] {message}\n")
    return code


def _read_prompt(argv: list[str]) -> str:
    positional = [a for a in argv if not a.startswith("--") and a != "text"]
    if positional:
        return positional[0]
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def run(argv: list[str]) -> int:
    prompt = _read_prompt(argv)
    if not prompt.strip():
        return _fail("no prompt provided (positional arg or stdin)", 2)

    base_url = (os.environ.get("MO_OAI_BASE_URL") or "").rstrip("/")
    model = os.environ.get("MO_OAI_MODEL") or ""
    key_env = os.environ.get("MO_OAI_ENV_KEY") or ""
    if not base_url:
        return _fail("MO_OAI_BASE_URL is unset", 2)
    if not model:
        return _fail("MO_OAI_MODEL is unset", 2)
    if not key_env:
        return _fail("MO_OAI_ENV_KEY is unset", 2)
    api_key = os.environ.get(key_env) or ""
    if not api_key:
        return _fail(f"${key_env} is unset — lane would die silently", 2)

    try:
        max_tokens = int(os.environ.get("MO_OAI_MAX_TOKENS") or "2048")
    except ValueError:
        return _fail("MO_OAI_MAX_TOKENS is not an integer", 2)
    try:
        timeout_s = float(os.environ.get("MO_OAI_TIMEOUT_S") or "120")
    except ValueError:
        timeout_s = 120.0

    payload: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    temperature = os.environ.get("MO_OAI_TEMPERATURE")
    if temperature:
        try:
            payload["temperature"] = float(temperature)
        except ValueError:
            pass

    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # OpenRouter uses these for attribution; harmless elsewhere.
            "HTTP-Referer": "https://github.com/OpenHands/mini-ork",
            "X-Title": "mini-ork",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        return _fail(f"HTTP {exc.code} from {base_url}: {detail}", 4)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _fail(f"request to {base_url} failed: {exc}", 4)

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return _fail(f"non-JSON response: {body[:500]}", 5)

    if isinstance(data, dict) and data.get("error"):
        return _fail(f"provider error: {json.dumps(data['error'])[:1000]}", 4)

    try:
        message = data["choices"][0]["message"]
        content = message.get("content")
    except (KeyError, IndexError, TypeError):
        return _fail(f"unexpected response shape: {body[:500]}", 5)

    if not content:
        # Reasoning models return null content when max_tokens is spent on
        # hidden reasoning — surface a clear, actionable failure rather than an
        # empty success the run would silently accept.
        return _fail(
            "empty content (reasoning model likely exhausted MO_OAI_MAX_TOKENS "
            f"on reasoning; raise it — current={max_tokens})",
            6,
        )

    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
