"""Deterministic ``kind: mock`` provider for dispatch tests (B6).

A mock lane is a rules file, not an executable: every dispatch reads the rules,
picks the first rule whose ``match`` block matches the request (prompt regex,
exact lane, exact attempt) and whose deterministic probability roll passes, and
returns either a canned response or a preset failure. Nothing here spawns a
subprocess or opens a socket, so retry, fallback, lane-fuse and timeout logic
can be exercised hermetically and reproducibly.

State kept for a run (all under ``$MINI_ORK_RUN_DIR`` or the system temp dir):

- ``.mock-attempts.json`` — per-lane attempt counter (flock-guarded), so
  "attempt 1 fails, attempt 2 succeeds" works across the retry loop and the
  fallback walk;
- ``.mock-calls.jsonl`` — one audit line per dispatch.

``MO_MOCK_SEED`` seeds the probability check; ``MO_MOCK_RESET=1`` truncates the
counter on this process's first ``next_attempt`` call.
"""

from __future__ import annotations

import fcntl
import json
import os
import random
import re
import tempfile
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..context import context_env
from .models import DispatchRequest, DispatchResult, TokenUsage

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .providers import ProviderSpec


# Preset failures. The error text is chosen so ``llm_dispatch.classify_error``
# maps each preset onto the category a caller branches on:
#   rate_limit   → capacity      auth        → auth
#   quota        → quota         server_error→ provider
#   overloaded   → capacity      bad_request → request
#   stream_cut   → stream        timeout     → unknown (rc 124 today)
PRESETS: dict[str, tuple[int, str]] = {
    "rate_limit": (1, "HTTP 429 rate limit exceeded"),
    "auth": (1, "API Error: 401 Unauthorized"),
    "quota": (1, "429 You exceeded your current quota"),
    "server_error": (1, "HTTP 500 Internal Server Error"),
    "overloaded": (1, "503 overloaded"),
    "bad_request": (1, "400 Bad Request: invalid request"),
    "stream_cut": (1, "unexpected EOF: partial stream"),
    "timeout": (124, "timeout after {timeout}s (mock)"),
}

_MATCH_KEYS = frozenset({"prompt", "lane", "attempt"})
_COUNTER_NAME = ".mock-attempts.json"
_CALLS_NAME = ".mock-calls.jsonl"

# Set once per process: MO_MOCK_RESET only truncates the counter on the first
# next_attempt call, not on every call (which would make attempt always 1).
_RESET_DONE = False


class _rules_error(ValueError):
    """A rules-file validation failure; message names the file and rule index."""


def _invalid(path: Path, index: int | str, message: str) -> _rules_error:
    where = "default" if isinstance(index, str) else f"rule[{index}]"
    return _rules_error(f"{path}: {where}: {message}")


_RULE_KEYS = frozenset({"match", "respond", "error", "probability", "latency_ms"})


def _validate_entry(path: Path, index: int | str, entry: object) -> None:
    if not isinstance(entry, Mapping):
        raise _invalid(path, index, "must be a mapping")
    unknown = sorted(str(key) for key in entry if key not in _RULE_KEYS)
    if unknown:
        # A typo ('latenc_ms', 'resopnd') must not be silently ignored.
        raise _invalid(path, index, f"unknown key(s) {', '.join(unknown)}")
    has_respond = "respond" in entry
    has_error = "error" in entry
    if has_respond == has_error:
        raise _invalid(
            path, index, "exactly one of 'respond' or 'error' is required"
        )
    if has_respond:
        respond = entry["respond"]
        if not isinstance(respond, Mapping) or not isinstance(
            respond.get("text"), str
        ):
            raise _invalid(path, index, "'respond' must contain a string 'text'")
    else:
        error = entry["error"]
        if not isinstance(error, Mapping):
            raise _invalid(path, index, "'error' must be a mapping")
        preset = error.get("preset")
        if preset not in PRESETS:
            raise _invalid(path, index, f"unknown preset {preset!r}")
        if "message" in error and not isinstance(error["message"], str):
            raise _invalid(path, index, "'error.message' must be a string")
        if "rc" in error and (
            isinstance(error["rc"], bool) or not isinstance(error["rc"], int)
        ):
            raise _invalid(path, index, "'error.rc' must be an integer")

    probability = entry.get("probability", 1)
    if (
        isinstance(probability, bool)
        or not isinstance(probability, (int, float))
        or not 0 <= probability <= 1
    ):
        raise _invalid(path, index, "'probability' must be a number in [0, 1]")

    latency_ms = entry.get("latency_ms", 0)
    if (
        isinstance(latency_ms, bool)
        or not isinstance(latency_ms, (int, float))
        or latency_ms < 0
    ):
        raise _invalid(path, index, "'latency_ms' must be a non-negative number")

    match = entry.get("match")
    if match is None:
        return
    if not isinstance(match, Mapping):
        raise _invalid(path, index, "'match' must be a mapping")
    unknown = set(match) - _MATCH_KEYS
    if unknown:
        raise _invalid(
            path,
            index,
            f"unknown match key(s): {', '.join(sorted(str(k) for k in unknown))}",
        )
    if "prompt" in match:
        prompt = match["prompt"]
        if not isinstance(prompt, str):
            raise _invalid(path, index, "'match.prompt' must be a string")
        try:
            re.compile(prompt)
        except re.error as exc:
            raise _invalid(path, index, f"invalid 'match.prompt' regex: {exc}") from exc
    if "lane" in match and not isinstance(match["lane"], str):
        raise _invalid(path, index, "'match.lane' must be a string")
    if "attempt" in match and (
        isinstance(match["attempt"], bool) or not isinstance(match["attempt"], int)
    ):
        raise _invalid(path, index, "'match.attempt' must be an integer")


def load_rules(path: Path) -> dict:
    """Parse and validate a mock rules file.

    Returns the parsed document (``{"rules": [...], "default": {...}}``).
    Raises ``ValueError`` naming the file and the offending rule index for bad
    YAML, an unknown preset, both/neither of respond/error, or a bad regex.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"{path}: cannot read mock rules: {exc}") from exc
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ValueError(f"{path}: mock rules must be a mapping")
    rules = document.get("rules")
    if not isinstance(rules, list):
        raise ValueError(f"{path}: 'rules' must be a list")
    for index, rule in enumerate(rules):
        _validate_entry(path, index, rule)
    if "default" in document:
        _validate_entry(path, "default", document["default"])
    return dict(document)


def _state_dir() -> Path:
    """``$MINI_ORK_RUN_DIR`` when present, else the system temp dir."""
    return Path(context_env("MINI_ORK_RUN_DIR") or tempfile.gettempdir())


def _locked(path: Path, mode: str):
    """Open ``path`` and take an exclusive flock (released on close)."""
    handle = open(path, mode, encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def next_attempt(lane: str) -> int:
    """Return this lane's next attempt number (starting at 1).

    The counter lives in ``.mock-attempts.json`` under the run dir. A read /
    increment / atomic-replace runs under an ``fcntl.flock`` so concurrent
    processes in the same run tree cannot hand out the same number.
    """
    global _RESET_DONE
    reset_now = not _RESET_DONE and os.environ.get("MO_MOCK_RESET") == "1"
    _RESET_DONE = True

    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    counter_path = state_dir / _COUNTER_NAME

    with _locked(state_dir / f"{_COUNTER_NAME}.lock", "a+") as lock_handle:
        counts: dict[str, int] = {}
        if not reset_now and counter_path.is_file():
            try:
                loaded = json.loads(counter_path.read_text(encoding="utf-8") or "{}")
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, Mapping):
                counts = {
                    str(key): int(value)
                    for key, value in loaded.items()
                    if isinstance(value, int) and not isinstance(value, bool)
                }
        attempt = counts.get(lane, 0) + 1
        counts[lane] = attempt
        # Atomic replace inside the lock: a reader never sees a half-written file.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{_COUNTER_NAME}.", dir=str(state_dir), text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(counts, tmp)
            os.replace(tmp_name, counter_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return attempt


def _record_call(entry: Mapping[str, object]) -> None:
    """Append one JSON audit line; the file is shared by the whole run tree."""
    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    with _locked(state_dir / _CALLS_NAME, "a") as handle:
        handle.write(json.dumps(dict(entry), sort_keys=True) + "\n")
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _matches(
    match: Mapping[str, object],
    request: DispatchRequest,
    lane: str,
    attempt: int,
) -> bool:
    if "prompt" in match and not re.search(str(match["prompt"]), request.prompt):
        return False
    if "lane" in match and match["lane"] != lane:
        return False
    if "attempt" in match and match["attempt"] != attempt:
        return False
    return True


def _result(
    *, ok: bool, rc: int, text: str, error: str, lane: str, started: float
) -> DispatchResult:
    return DispatchResult(
        ok=ok,
        rc=rc,
        text=text,
        error=error,
        model=lane,
        usage=TokenUsage(),
        cost_usd=0.0,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def dispatch(request: DispatchRequest, spec: "ProviderSpec") -> DispatchResult:
    """Dispatch a request against the lane's mock rules file."""
    started = time.monotonic()
    lane = spec.model
    attempt = next_attempt(lane)
    rules_path = spec.env.get("MO_MOCK_RULES")

    def _finish(result: DispatchResult, rule_id: object) -> DispatchResult:
        _record_call(
            {
                "lane": lane,
                "attempt": attempt,
                "rule": rule_id,
                "outcome": "ok" if result.ok else "error",
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return result

    if not rules_path:
        return _finish(
            _result(
                ok=False,
                rc=1,
                text="",
                error=f"mock: no rules file configured for lane {lane!r} "
                "(MO_MOCK_RULES unset)",
                lane=lane,
                started=started,
            ),
            "none",
        )

    try:
        document = load_rules(Path(rules_path))
    except (OSError, ValueError) as exc:
        # Preflight validated the file; one that broke since must surface as a
        # failed dispatch, never as an exception out of the engine.
        return _finish(
            _result(ok=False, rc=2, text="", error=f"mock: rules file unusable: {exc}",
                    lane=lane, started=started),
            "none",
        )
    rules = document.get("rules") or []
    default = document.get("default")

    # The lane's extra_env (spec.env) wins over the process env, as for
    # MO_MOCK_RULES: a seed configured on the lane must not be ignored.
    seed = str(spec.env.get("MO_MOCK_SEED") or os.environ.get("MO_MOCK_SEED", "0"))
    rule_id: object = "none"
    chosen: Mapping[str, object] | None = None
    for index, rule in enumerate(rules):
        match = rule.get("match") or {}
        if not _matches(match, request, lane, attempt):
            continue
        probability = float(rule.get("probability", 1))
        roll = random.Random(f"{seed}:{lane}:{attempt}:{index}").random()
        if roll < probability:
            rule_id, chosen = index, rule
            break
    if chosen is None and isinstance(default, Mapping):
        rule_id, chosen = "default", default

    if chosen is None:
        return _finish(
            _result(
                ok=False,
                rc=1,
                text="",
                error=f"mock: no rule matched (lane={lane}, attempt={attempt})",
                lane=lane,
                started=started,
            ),
            rule_id,
        )

    latency_ms = float(chosen.get("latency_ms", 0) or 0)
    timeout_s = request.timeout_s
    if latency_ms / 1000.0 > timeout_s:
        # A rule that would outlast the caller's deadline is a timeout, with the
        # caller's own budget burned — not a fast answer.
        time.sleep(timeout_s)
        rc, preset_text = PRESETS["timeout"]
        return _finish(
            _result(
                ok=False,
                rc=rc,
                text="",
                error=preset_text.format(timeout=timeout_s),
                lane=lane,
                started=started,
            ),
            rule_id,
        )
    if latency_ms > 0:
        time.sleep(latency_ms / 1000.0)

    if "respond" in chosen:
        text = str(chosen["respond"]["text"])
        return _finish(
            _result(ok=True, rc=0, text=text, error="", lane=lane, started=started),
            rule_id,
        )

    error_spec = chosen["error"]
    rc, preset_text = PRESETS[str(error_spec["preset"])]
    error_text = error_spec.get("message") or preset_text.format(timeout=timeout_s)
    return _finish(
        _result(
            ok=False,
            rc=int(error_spec.get("rc", rc)),
            text="",
            error=str(error_text),
            lane=lane,
            started=started,
        ),
        rule_id,
    )
