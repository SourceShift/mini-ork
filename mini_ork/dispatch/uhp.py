"""Direct-HTTP transport for UHP (Unified Harness Protocol, 2026-08-11).

A self-contained, stdlib-only client that POSTs to ``{base_url}/v1/responses``
on a UHP-conformant harness server (e.g. HarnessRouter Community Edition) and
parses the Server-Sent Events stream back to a final response object. Model
routing is owned by the harness server (``metadata.harness_id`` is optional);
mini-ork just consumes the wire.

Mirrors the direct-HTTP precedent of ``openai_chat_transport.py`` (stdlib-only,
script-path dispatch, prompt-on-stdin, raw-text-on-stdout) and extends it with:

* chunked SSE read via ``http.client.HTTPConnection``;
* session continuation via the ``previous_response_id`` field and a per-run
  ``MO_UHP_SESSION_FILE`` sidecar (transport is out-file-blind; the builder
  wires the path from ``MINI_ORK_RUN_DIR`` + ``MO_NODE_ID``);
* distinct exit codes (2/3/4/5/6) per UHP error class so a caller can tell
  "lane misconfigured" from "harness crashed mid-task" without parsing the
  stderr text;
* an optional ``MO_UHP_ERR_LOG`` sidecar mirroring the ``.err.log`` convention.

Configuration rides on the environment the provider builder injects:

    MO_UHP_BASE_URL      base URL (e.g. ``http://localhost:8080``); ``/v1/responses`` is appended
    MO_UHP_ENV_KEY       NAME of the env var holding the API key (e.g. ``UHP_API_KEY``)
    MO_UHP_MODEL         model slug (optional — harness server may ignore)
    MO_UHP_HARNESS_ID    ``metadata.harness_id`` (optional)
    MO_UHP_TIMEOUT_S     request timeout seconds (default 120)
    MO_UHP_SESSION_FILE  sidecar path for ``previous_response_id`` continuation
    MO_UHP_ERR_LOG       sidecar path for the ``[uhp] <class>: <msg>`` error line

``--print --output-format text`` are accepted as no-op flags so the argv shape
matches every other harness CLI the dispatcher spawns.
"""

from __future__ import annotations

import http.client
import json
import os
import ssl
import sys
import urllib.parse


# ── Exit code contract (kickoff requirement) ────────────────────────────────
# 2  invalid_request_error (config / 4xx / malformed wire / no completed event)
# 3  harness_unavailable   (5xx, ECONNREFUSED, DNS, network)
# 4  harness_error         (mid-task non-recoverable runtime error)
# 5  timeout
# 6  cancel / client-abort  (or empty stream on a reasoning-model thin-budget)
RC_CONFIG = 2
RC_SERVER = 3
RC_HARNESS = 4
RC_TIMEOUT = 5
RC_CANCEL = 6


def _fail(class_name: str, message: str, code: int) -> int:
    line = f"[uhp] {class_name}: {message}\n"
    sys.stderr.write(line)
    err_log = os.environ.get("MO_UHP_ERR_LOG", "")
    if err_log:
        try:
            with open(err_log, "w", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass
    return code


def _read_prompt(argv: list[str]) -> str:
    positional = [a for a in argv if not a.startswith("--") and a != "text"]
    if positional:
        return positional[0]
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _split_url(url: str) -> tuple[str, int, bool]:
    """Return (host, port, tls) from a base URL."""
    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname or ""
    if parsed.port is not None:
        port = parsed.port
    elif scheme == "https":
        port = 443
    else:
        port = 80
    return host, port, scheme == "https"


def _read_previous_session_id() -> str:
    path = os.environ.get("MO_UHP_SESSION_FILE", "")
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.readline().strip()
    except OSError:
        return ""


def _write_session_id(response_id: str) -> None:
    path = os.environ.get("MO_UHP_SESSION_FILE", "")
    if not path or not response_id:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(response_id)
            fh.write("\n")
    except OSError:
        pass


def _post_sse(
    base_url: str,
    payload: dict[str, object],
    api_key: str,
    timeout_s: float,
) -> tuple[http.client.HTTPConnection | None, http.client.HTTPResponse | None, str]:
    """Open a POST, return (connection, response, error_class). error_class is "" on success.

    The connection stays open — the caller iterates ``response.fp`` for SSE chunks
    and closes the connection when done.
    """
    host, port, use_tls = _split_url(base_url)
    if not host:
        return None, None, "config"
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    conn_cls = http.client.HTTPSConnection if use_tls else http.client.HTTPConnection
    try:
        conn = conn_cls(host, port, timeout=timeout_s)
    except (OSError, ssl.SSLError):
        return None, None, "server"
    try:
        conn.request(
            "POST",
            "/v1/responses",
            body=body,
            headers={**headers, "Content-Length": str(len(body))},
        )
    except (OSError, ssl.SSLError):
        try:
            conn.close()
        except OSError:
            pass
        return None, None, "server"
    try:
        resp = conn.getresponse()
    except (OSError, http.client.RemoteDisconnected):
        try:
            conn.close()
        except OSError:
            pass
        return None, None, "server"
    if resp.status >= 400:
        try:
            _ = resp.read()
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass
        if 400 <= resp.status < 500:
            return None, None, "config"
        return None, None, "harness"
    if resp.status != 200:
        try:
            conn.close()
        except OSError:
            pass
        return None, None, "server"
    return conn, resp, ""


def _iter_sse_events(stream) -> list[dict[str, object]]:
    """Parse an SSE byte stream into a list of event payloads (JSON in ``data:``)."""
    events: list[dict[str, object]] = []
    event_type = ""
    data_parts: list[str] = []
    for raw in stream:
        try:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
        except (AttributeError, UnicodeDecodeError):
            line = str(raw).rstrip()
        if line == "":
            if event_type or data_parts:
                if data_parts:
                    blob = "\n".join(data_parts)
                    try:
                        events.append({"type": event_type or "message", "data": json.loads(blob)})
                    except json.JSONDecodeError:
                        events.append({"type": event_type or "message", "data": blob})
                event_type = ""
                data_parts = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
            continue
        if line.startswith("data:"):
            data_parts.append(line[len("data:"):].lstrip())
            continue
        # Unknown field — ignore per SSE spec.
    # Flush a trailing event without a blank line terminator.
    if event_type or data_parts:
        if data_parts:
            blob = "\n".join(data_parts)
            try:
                events.append({"type": event_type or "message", "data": json.loads(blob)})
            except json.JSONDecodeError:
                events.append({"type": event_type or "message", "data": blob})
    return events


def _accumulate(events: list[dict[str, object]]) -> tuple[str, dict[str, object], str]:
    """Walk SSE events and return (text, completed_response, response_id).

    Text is assembled from ``response.output_text.delta.delta`` (the spec's
    streaming text channel); ``response.completed`` is treated as a terminal
    signal that carries ``response.id`` (used for the next dispatch's
    ``previous_response_id``) — it does NOT carry the body text, mirroring
    the OpenAI Responses API streaming contract.

    Empty string + empty response means no ``response.completed`` was observed —
    caller treats that as an incomplete stream (rc=2).
    """
    text_parts: list[str] = []
    completed: dict[str, object] = {}
    response_id = ""
    for event in events:
        etype = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if etype in {"response.output_text.delta", "response.text.delta"}:
            delta = data.get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)
        elif etype in {"response.completed", "response.done"}:
            response = data.get("response")
            if isinstance(response, dict):
                completed = response
                rid = response.get("id")
                if isinstance(rid, str):
                    response_id = rid
        elif etype in {"error", "response.error"}:
            raise _StreamError(data)
    text = "".join(text_parts)
    return text, completed, response_id


class _StreamError(Exception):
    def __init__(self, data: dict[str, object]) -> None:
        self.data = data


def run(argv: list[str]) -> int:
    prompt = _read_prompt(argv)
    if not prompt.strip():
        return _fail("config", "no prompt provided (positional arg or stdin)", RC_CONFIG)

    base_url = (os.environ.get("MO_UHP_BASE_URL") or "").rstrip("/")
    key_env = os.environ.get("MO_UHP_ENV_KEY") or ""
    if not base_url:
        return _fail("config", "MO_UHP_BASE_URL is unset", RC_CONFIG)
    if not key_env:
        return _fail("config", "MO_UHP_ENV_KEY is unset", RC_CONFIG)
    api_key = os.environ.get(key_env) or ""
    if not api_key:
        return _fail("config", f"${key_env} is unset — lane would die silently", RC_CONFIG)

    try:
        timeout_s = float(os.environ.get("MO_UHP_TIMEOUT_S") or "120")
    except ValueError:
        return _fail("config", "MO_UHP_TIMEOUT_S is not a float", RC_CONFIG)

    payload: dict[str, object] = {
        "input": prompt,
        "stream": True,
    }
    model = os.environ.get("MO_UHP_MODEL") or ""
    if model:
        payload["model"] = model
    harness_id = os.environ.get("MO_UHP_HARNESS_ID") or ""
    if harness_id:
        payload["metadata"] = {"harness_id": harness_id}
    previous_id = _read_previous_session_id()
    if previous_id:
        payload["previous_response_id"] = previous_id

    resp_tuple = _post_sse(base_url, payload, api_key, timeout_s)
    conn, resp, err_class = resp_tuple
    if resp is None or conn is None:
        return _fail(err_class, f"request to {base_url} failed", _rc_for_class(err_class))

    try:
        try:
            events = _iter_sse_events(resp.fp)
        except _StreamError as exc:
            detail = str(
                exc.data.get("message") or exc.data.get("code") or json.dumps(exc.data)[:500]
            )
            return _fail("harness", detail, RC_HARNESS)

        try:
            text, completed, response_id = _accumulate(events)
        except _StreamError as exc:
            detail = str(
                exc.data.get("message") or exc.data.get("code") or json.dumps(exc.data)[:500]
            )
            return _fail("harness", detail, RC_HARNESS)

        if not response_id and not completed:
            return _fail(
                "config",
                "stream ended without response.completed — incomplete UHP response",
                RC_CONFIG,
            )

        if response_id:
            _write_session_id(response_id)

        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            sys.stdout.write("\n")
        return 0
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _rc_for_class(name: str) -> int:
    return {
        "config": RC_CONFIG,
        "server": RC_SERVER,
        "harness": RC_HARNESS,
    }.get(name, RC_SERVER)


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
