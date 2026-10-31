"""Python port of lib/llm-dispatch.sh's WRAPPER layer + pure helpers.

The model-level provider invocation is already ported in ``mini_ork.dispatch``
(prompt-on-stdin, sidecar contract, telemetry). This module ports what remained
in bash: the ``llm_dispatch`` lane-routing shim that bin/mini-ork-{plan,execute,
invoke-prompt} + pre_push_review call, plus the pure classification/backoff/
redaction helpers.

    llm_dispatch(argv, *, dispatch_fn=None) -> int      # --task-class/--node-type/--prompt-text
    mo_llm_dispatch(model, prompt, out_file, timeout_s=1500, max_turns=60) -> int

Every helper (classify_error, provider_for_model, strip_protocol_blocks,
redact_secrets, backoff, throttle/glm-retryable, lane resolution, cost circuit)
is transcribed verbatim from the bash so behaviour byte-matches — including the
bash gateway provider taxonomy (distinct from mini_ork.dispatch's finer one).
"""
from __future__ import annotations

import errno
import inspect
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Callable

from mini_ork.context import context_env
from mini_ork.dispatch import failure_class, lane_affinity
from mini_ork.dispatch.predicates import (
    SHAPE_REJECT_RC,
    looks_like_json,
    looks_like_json_text,
)


def _accepts_kwarg(fn, name: str) -> bool:
    """True when ``fn`` can be called with ``name=``.

    The default dispatcher takes the per-attempt observer and the shape
    predicate; a caller-supplied ``dispatch_fn`` historically kept the bare
    5-arg contract and must not be handed them. Gating the observer on "is this
    the default dispatcher" silently disabled per-lane ledger rows for EVERY
    caller that passed its own dispatcher (2026-10-12) — the exact blind spot
    the observer exists to close, re-opened one layer up. Introspection failure
    degrades to False, i.e. the historic behaviour, rather than raising.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


# ── pure helpers (verbatim regex transcriptions) ──

def classify_error(message: str, rc: str | int = "") -> str:
    text = (message or "").lower()
    if str(rc) in ("6", "7", "28"):
        return "network"
    if re.search(r"missing (api key|wrapper)|api key.*missing|malformed config|config.*missing"
                 r"|cl_[a-z0-9_-]+\.sh missing|no providers\.yaml entry|\$[A-Z0-9_]+ is empty", text):
        return "config"
    if re.search(r"(^|[^0-9])(401|403)([^0-9]|$)|invalid api key|authentication failed"
                 r"|not logged in|unauthorized|forbidden", text):
        return "auth"
    # Account-plan limit wording that is unambiguous on its own: provider
    # relays emit it without any HTTP 429 (the Codex CLI prints "You've hit
    # your usage limit … try again at <date>" on ``turn.failed``), so gating
    # these on 429 would mute a real dead lane into "unknown". The weaker
    # words (monthly|billing|quota|…) stay gated behind a 429 below.
    if re.search(r"usage limit|exceeded your current quota|insufficient credits"
                 r"|insufficient balance|out of credits|purchase credits"
                 r"|credit limit|credit balance", text):
        return "quota"
    if re.search(r"(^|[^0-9])429([^0-9]|$)", text) and re.search(
            r"monthly|tokens-per-day|billing|quota|token plan", text):
        return "quota"
    if re.search(r"(^|[^0-9])(429|503)([^0-9]|$)", text) and re.search(
            r"capacity|concurrent|rate|overload|temporarily unavailable", text):
        return "capacity"
    if re.search(r"(^|[^0-9])400([^0-9]|$)|invalid request|context too long"
                 r"|maximum context|prompt too long", text):
        return "request"
    if re.search(r"(^|[^0-9])422([^0-9]|$)|content filter|safety|moderation", text):
        return "safety"
    if re.search(r"connection refused|could not resolve|dns|timed out|timeout was reached"
                 r"|network is unreachable", text):
        return "network"
    if re.search(r"partial stream|unexpected eof|stream.*(closed|ended|error)"
                 r"|incomplete chunk|chunked encoding", text):
        return "stream"
    if re.search(r"(^|[^0-9])(500|502|504)([^0-9]|$)|internal server error|bad gateway"
                 r"|gateway timeout|provider error", text):
        return "provider"
    return "unknown"


def error_retryable(category: str = "unknown") -> str:
    return "1" if category in ("capacity", "network", "stream", "provider") else "0"


# Engine-side exception types whose TYPE alone identifies a config fault:
# wrong arguments, a bad key/shape, a missing path — nothing the provider did.
# Compared by class NAME (not isinstance) so a subclass defined outside the
# engine does not silently widen the set.
_CRASH_CONFIG_BUILTINS = frozenset({
    "ValueError", "KeyError", "TypeError", "AssertionError", "FileNotFoundError",
})

# errno values that mean "the network path is down" rather than "this account
# or this engine input is bad".
_CRASH_NETWORK_ERRNOS = frozenset({
    errno.ECONNREFUSED, errno.ECONNRESET, errno.ETIMEDOUT,
})


def classify_crash(exc: BaseException) -> str:
    """Classify a DISPATCHER CRASH — an exception, not a provider error string.

    ``classify_error(str(exc))`` over the WHOLE exception text is how RB-10
    recorded a ``UnmappedHostPathError`` as ``auth``: its message embeds the
    prompt, and the code-fix implementer prompt contains the word "forbidden",
    so the auth regex matched a config fault. That is a one-strike, six-hour
    lane fuse plus a hint telling the operator to switch lanes.

    The exception TYPE is the reliable signal for an engine-side crash, so it
    is checked first: the builtin config types and any ``*Error`` class defined
    under ``mini_ork.``. ``ConnectionError``/``TimeoutError`` (and an ``OSError``
    carrying one of the trio errnos) are ``network``. Only when the type says
    nothing do we fall back to the text — and then only its FIRST 200 chars, so
    auth wording buried in the prompt tail can never reclassify the crash.
    """
    name = type(exc).__name__
    module = getattr(type(exc), "__module__", "") or ""
    if name in _CRASH_CONFIG_BUILTINS or (
            name.endswith("Error")
            and (module == "mini_ork" or module.startswith("mini_ork."))):
        return "config"
    if isinstance(exc, (ConnectionError, TimeoutError)) or (
            isinstance(exc, OSError)
            and getattr(exc, "errno", None) in _CRASH_NETWORK_ERRNOS):
        return "network"
    return classify_error(str(exc)[:200])


def provider_for_model(model: str) -> str:
    """Bash _mo_llm_provider_for_model taxonomy (llm_calls.provider)."""
    if re.fullmatch(r"codex|gpt-.*|o1.*|o3.*", model):
        return "openai"
    if re.fullmatch(r"gemini.*|.*-gemini-.*", model):
        return "google"
    if re.fullmatch(r"minimax.*|glm.*|kimi.*|deepseek.*", model):
        return "gateway"
    return "anthropic"


def redact_secrets(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"sk-[a-zA-Z]{1,6}-[A-Za-z0-9_-]{8,}", "[REDACTED_KEY]", s)
    s = re.sub(r"sk-[A-Za-z0-9_-]{20,}", "[REDACTED_KEY]", s)
    s = re.sub(r"[Bb]earer\s+[A-Za-z0-9_.+/=-]{8,}", "Bearer [REDACTED]", s)
    s = re.sub(r"(ANTHROPIC_(AUTH_TOKEN|API_KEY)|[A-Z_]+_API_KEY)=[^\s\"']+", r"\1=[REDACTED]", s)
    s = re.sub(r"[a-fA-F0-9]{32,}", "[REDACTED_HEX]", s)
    return s


def error_message_head(raw: object, *, limit: int = 200) -> str:
    """The human-readable part of a provider error, for a UI or a log line.

    ``llm_calls.error_message`` is a bounded TAIL slice of the provider's output,
    so a JSON envelope arrives cut at the front — ``:19 AM."}
    {"type":"turn.failed","error":{"message":"You've hit your usage limit...``.
    The envelope's own ``message`` field is the readable part. Falls back to the
    whitespace-collapsed raw text when the payload is not an envelope, so a
    plain-text error is passed through unchanged.
    """
    text = " ".join(str(raw or "").split())
    match = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if not match:
        return text[:limit]
    try:
        inner = json.loads('"' + match.group(1) + '"')
    except ValueError:
        inner = match.group(1)
    return str(inner)[:limit]


def strip_protocol_blocks(out_file: str) -> None:
    if not os.path.isfile(out_file):
        return
    try:
        text = open(out_file, encoding="utf-8", errors="replace").read()
    except OSError:
        return
    if "<z-insight>" not in text:
        return
    cleaned = re.sub(r"<z-insight>.*?</z-insight>", "", text, flags=re.S)
    cleaned = re.sub(r"<z-insight>.*\Z", "", cleaned, flags=re.S)
    if cleaned != text:
        try:
            open(out_file, "w", encoding="utf-8").write(cleaned.rstrip() + "\n")
        except OSError:
            pass


def _provider_error_from_result(out_file: str) -> str:
    """The provider error text from a claude-code ``result`` object on stdout.

    Kept for callers that only want the message; see ``_provider_error_record``
    for the typed status that travels alongside it.
    """
    return _provider_error_record(out_file)[1]


def _provider_error_record(out_file: str) -> tuple[int | None, str]:
    """``(api_error_status, message)`` from a claude-code ``result`` object.

    The claude-code wrapper prints a provider rejection as a JSON ``result``
    object (or a JSONL stream line) on stdout — ``{"type":"result",
    "is_error":true, "api_error_status":429, "result":"API Error: …"}`` — while
    its stderr only carries CLI chatter. The failed-call tail reads ``.err.log``
    only, so the 429 sat invisible in stdout JSON and ``classify_error`` never
    saw the quota wording. Parse stdout for that object and return
    ``f"HTTP {status}: {result}"[:1000]`` (no ``HTTP`` part when there is no
    status) together with the status as an int, so the classifier can decide
    on the provider's own status rather than re-parse it from the message;
    return ``(None, "")`` when stdout has no such object.
    """
    try:
        raw = open(out_file, encoding="utf-8", errors="replace").read()
    except OSError:
        return (None, "")
    candidates: list[dict] = []
    # One whole-object document (a single result), plus each JSONL stream line.
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            candidates.append(obj)
    except (ValueError, TypeError):
        pass
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            candidates.append(obj)
    best: tuple[int | None, str] = (None, "")
    for obj in candidates:
        if obj.get("is_error") is not True:
            continue
        result = obj.get("result")
        if not isinstance(result, str) or not result:
            continue
        status = obj.get("api_error_status")
        prefix = f"HTTP {status}: " if status is not None and str(status) else ""
        try:
            status_int = int(status) if status is not None else None
        except (TypeError, ValueError):
            status_int = None
        best = (status_int, (prefix + result)[:1000])
    return best


def _int_or(v, d):
    try:
        return int(v)
    except (ValueError, TypeError):
        return d


def _read_token_sidecar(path):
    """(input, output, cached_in, cache_create, thinking) from a ``.tokens``
    sidecar written next to a dispatch's out_file. A missing file yields all
    zeros; ``thinking`` is None when the provider did not report a figure (the
    field is blank), so it is stored as SQL NULL rather than a misleading 0."""
    try:
        parts = (open(path).read().split("\t") + ["", "", "", "", ""])[:5]
    except OSError:
        return 0, 0, 0, 0, None
    return (_int_or(parts[0], 0), _int_or(parts[1], 0), _int_or(parts[2], 0),
            _int_or(parts[3], 0), _int_or(parts[4], None))


def backoff_seconds_raw(attempt, max_sleep: int | str = 45, base_s: int | str = 5,
                        *, _jitter=None) -> int:
    max_sleep = _int_or(max_sleep, 45)
    base_s = _int_or(base_s, 5)
    attempt = _int_or(attempt, 1)
    attempt = max(1, min(12, attempt))
    base = 2 ** (attempt - 1) * base_s
    jitter = _jitter if _jitter is not None else _rand_mod4()
    delay = base + jitter
    if delay > max_sleep:
        delay = max_sleep
    if delay < 1:
        delay = 1
    return delay


def _rand_mod4() -> int:
    # bash $RANDOM % 4 — jitter is non-deterministic; isolated for test seams.
    import random
    return random.randint(0, 3)


def backoff_seconds(attempt, *, _jitter=None) -> int:
    return backoff_seconds_raw(attempt, os.environ.get("MO_DISPATCH_RETRY_MAX_SLEEP_S", "45"),
                               os.environ.get("MO_DISPATCH_RETRY_BASE_S", "5"), _jitter=_jitter)



def throttle_retryable(model, message, rc, attempt=1, max_attempts=1) -> bool:
    if not model:
        return False
    attempt = _int_or(attempt, 1)
    max_attempts = _int_or(max_attempts, 1)
    if attempt >= max_attempts:
        return False
    return error_retryable(classify_error(message, rc)) == "1"


def glm_fair_usage_retryable(model, message, attempt=1, max_attempts=1) -> bool:
    if model != "glm":
        return False
    if _int_or(attempt, 1) >= _int_or(max_attempts, 1):
        return False
    return bool(re.search(r"(^|[^0-9])(429|1313)([^0-9]|$)|fair usage policy"
                          r"|usage pattern does not comply", message or "", re.I))


# ── lane resolution + cost circuit + fuse ──

def _effective_lanes(root: str, home: str) -> dict | None:
    """``lanes`` of the policy a resolver should read, or None when there is none.

    Template precedence (matches ``config_resolve.resolve_agents_yaml``):
      1. ``$MINI_ORK_RUN_DIR/config/agents.yaml`` — the launch-time snapshot,
         written by ``snapshot_run_config`` so a run honours the lanes it was
         seeded with even after the home's agents.yaml moves.
      2. ``<home>/config/agents.yaml`` (current behaviour)
      3. ``<root>/config/agents.yaml`` (current behaviour)

    An empty home/root is a clean miss, never a CWD lookup. The per-user overlay
    (``$MINI_ORK_AGENTS`` or ``<home>/config/agents.local.yaml``) is merged on
    top in memory with ``agents_config.merge`` — the same policy
    ``agents_config.effective_path`` materialises for every other reader, but
    with no file written per call. Reading the template alone ignored the
    overlay, so a user's lane choice never reached the dispatch chain.

    An unreadable template stays fail-open (None, as before). A broken overlay
    raises ``ValueError`` naming the file: it is the user's own lane choice, and
    skipping it would route their nodes to the team default without a trace.
    """
    from mini_ork.dispatch import agents_config

    tmpl = ""
    run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
    if run_dir:
        candidate = os.path.join(run_dir, "config", "agents.yaml")
        if os.path.isfile(candidate):
            tmpl = candidate
    if not tmpl and home:
        candidate = os.path.join(home, "config", "agents.yaml")
        if os.path.isfile(candidate):
            tmpl = candidate
    if not tmpl and root:
        candidate = os.path.join(root, "config", "agents.yaml")
        if os.path.isfile(candidate):
            tmpl = candidate
    # ``home`` defaults to "" for the lane resolvers (``resolve_lane_family``
    # and the routing policies call it with no home), and a falsy home used to
    # SKIP the overlay outright — so the run's snapshot lanes (e.g. a stale
    # ``codex_lens: codex`` or ``implementer: minimax``) silently won and an
    # operator's ``agents.local.yaml`` / ``$MINI_ORK_AGENTS`` never reached the
    # dispatch chain. ``home or None`` routes an empty home through
    # ``personal_path``'s ``$MINI_ORK_HOME`` / env fallback instead of dropping
    # the user's overlay.
    over_p = agents_config.personal_path(home=home or None)
    if not tmpl and over_p is None:
        return None
    try:
        base = agents_config._load_yaml(tmpl, "agents.yaml template") if tmpl else {}
    except Exception:
        if over_p is None:
            return None
        base = {}
    if over_p is not None:
        base = agents_config.merge(base, agents_config._load_yaml(over_p))
    lanes = base.get("lanes")
    return lanes if isinstance(lanes, dict) else {}


def resolve_lane_model(node_type, root, home) -> str:
    """agents.yaml lanes.<node_type> → lanes.worker → worker_default → sonnet,
    read from the effective policy (template + per-user overlay)."""
    lanes = _effective_lanes(root, home)
    if lanes is None:
        return "sonnet"
    return lanes.get(node_type) or lanes.get("worker") or lanes.get("worker_default") or "sonnet"


def resolve_lane_family(lane: str, root: str = "", home: str = "") -> str:
    """Resolve an agents.yaml lane ALIAS (e.g. 'codex_lens', 'decomposer') to its
    family model via lanes.<alias>. A plain model name or unknown alias passes
    through unchanged. Fail-open on a missing or unreadable template (the input
    lane comes back verbatim); the per-user overlay is honoured and a broken one
    raises (see ``_effective_lanes``).

    Dispatch preflight keys on providers.yaml model names; '*_lens' aliases are
    NOT providers.yaml keys, so an unresolved alias used as the fallback-chain
    lead fails preflight and the MO_FALLBACK_* tail silently decides who serves
    (the codex_lens->minimax bug). Resolving here makes the alias lead with its
    real family model, keeping the tail as a genuine fallback.

    An empty ``home``/``root`` is a clean miss, never a CWD lookup: joining ""
    yields the bare ``config/agents.yaml``, which resolves against the process
    CWD — i.e. the repo's default policy rather than the run's. That misread
    is invisible (the default file exists, so the alias *does* resolve) and it
    routes to whatever the default names instead. Read no policy rather than
    read the wrong one."""
    if not lane:
        return lane
    root = root or context_env("MINI_ORK_ROOT")
    home = home or context_env("MINI_ORK_HOME")
    lanes = _effective_lanes(root, home)
    return (lanes or {}).get(lane) or lane


def cost_circuit_open(db, budget) -> bool:
    if not (db and os.path.isfile(db)):
        return False
    from mini_ork import cost_ledger
    return cost_ledger.spent_last_24h(db) >= float(budget)


# A lane fuse trips when a lane's last N calls all failed with the SAME
# category. How many strikes depends on whether the category can heal itself: a
# terminal category is evidence the lane is dead right now and one strike is
# enough, while a transient one may be a blip and keeps the historic three.
_FUSE_TERMINAL = ("quota", "auth", "config")
_FUSE_TRANSIENT = ("capacity", "network", "stream", "provider")
_FUSE_TERMINAL_STRIKES = 1
_FUSE_TRANSIENT_STRIKES = 3
_FUSE_WINDOW_HOURS = 6


def _fuse_window_hours() -> int:
    return _int_or(os.environ.get("MO_FUSE_WINDOW_HOURS"), _FUSE_WINDOW_HOURS)


def _current_placement() -> str:
    """The placement label a dispatch runs under: ``host`` or the node env name.

    RB-24: the lane fuse was keyed on ``model_id`` alone, so a credential
    failure recorded on a remote node (``daytona``) opened the fuse for every
    HOST dispatch of that lane too, and runs were refused at plan stage (rc 43)
    under a placement where the lane had never failed.

    Same source as the lifecycle's publisher (``runtime/placement_env.py``):
    ``MO_PLACEMENT``/``MO_NODE_ENV`` through ``context_env``. A missing or
    ``local`` placement is ``host`` (the historic default, so pre-change rows —
    which carry no placement at all — keep counting for host dispatches). A
    ``remote`` placement names its node env; with the env missing it stays the
    literal ``remote`` rather than silently degrading to ``host``.
    """
    placement = (context_env("MO_PLACEMENT") or "").strip().lower()
    if placement in ("", "local"):
        return "host"
    return (context_env("MO_NODE_ENV") or "").strip() or "remote"


# A pre-spawn portability guard refuses a lane before anything is sent: the
# anthropic-native lane's local login cannot travel to the node. Anchored to the
# guard's own prefix + marker, NOT a general "config"/"api key" match — the
# RB-10 lesson: a broad text rule misfires on unrelated provider errors.
_PRESPAWN_GUARD_PREFIX = "remote placement: lane "
_PRESPAWN_GUARD_MARKER = "api key is missing on the node"


def _is_prespawn_guard_refusal(message: str) -> bool:
    """True when ``message`` is the remote placement's pre-spawn secret guard."""
    text = (message or "").strip()
    return text.startswith(_PRESPAWN_GUARD_PREFIX) and _PRESPAWN_GUARD_MARKER in text


def fuse_categories() -> tuple[tuple[str, int], ...]:
    """The ``(category, strikes)`` pairs the fuse watches.

    Terminal categories are watched by default; the transient ones stay opt-in
    behind ``MO_FUSE_TRANSIENT=1``, because halting on a transient category is
    the false positive the old all-off default avoided. ``MO_FUSE_TERMINAL``
    replaces the terminal set outright.
    """
    terminal = tuple(
        c.strip() for c in os.environ.get(
            "MO_FUSE_TERMINAL", ",".join(_FUSE_TERMINAL)).split(",") if c.strip())
    watch = [(c, _FUSE_TERMINAL_STRIKES) for c in terminal]
    if os.environ.get("MO_FUSE_TRANSIENT", "0") == "1":
        watch += [(c, _FUSE_TRANSIENT_STRIKES) for c in _FUSE_TRANSIENT]
    return tuple(watch)


def check_lane_fuse(db, lane: str, category: str, *, strikes: int = 3,
                    placement: str | None = None) -> bool:
    """True when the fuse is OPEN — this lane's last ``strikes`` calls all failed
    with ``category``, within the recency window.

    Keyed on ``model_id`` (the lane that actually ran), NOT ``feature_name``
    (the node that requested it). A fallback tail lane's failures are recorded
    against the lane, never against the node, so a role-keyed fuse could never
    open on one: codex failed 4/4 for a day and stayed the terminal rung of
    every coding chain (2026-10-12).

    The ``retryable`` filter is gone for the same reason. ``error_retryable``
    marks only the SELF-HEALING categories, so a quota wall — written
    ``retryable=0`` precisely because retrying cannot help it — was invisible to
    a fuse that demanded ``retryable=1``.

    The recency window is what stops a fuse wedging a lane forever: a fused lane
    records no new rows, so without a window its own last failure would hold the
    fuse open indefinitely. After the window the lane is eligible again.

    ``ts`` is compared through ``datetime()`` on both sides. The column stores
    ISO-8601 (``...T06:00:00.000Z``) while ``datetime('now')`` yields a
    space-separated string, and ``'T'`` (0x54) sorts above ``' '`` (0x20) — so a
    raw string comparison drops the *time* half of the window and keeps every row
    from today.

    Guard-generated rows are excluded from the streak: ``feature_name`` ending
    ``:refused`` (a refusal names the fuse's own category and must not be
    self-sustaining), ``actor`` ending ``:refused`` (a pre-spawn portability
    guard refused the lane before anything ran — RB-24: the remote secret guard
    is worded as ``config`` and used to open the fuse under a placement where
    no call was ever attempted) and ``actor`` ending ``:crash`` (a dispatcher
    crash records the engine's own death, not a provider verdict on this lane).
    The ``:crash``/``:refused`` predicates are only added when the schema has an
    ``actor`` column, and a NULL actor still counts — older ledgers and test
    fixtures predate it.

    RB-24: strikes are also scoped to the dispatch's ``placement`` (default:
    ``_current_placement()``). A credential failure recorded on ``daytona``
    must not open the fuse for a host dispatch of the same lane. Rows written
    before the placement key existed have no ``metadata_json.placement`` and
    count as ``host`` — that is the "missing placement = host" contract, and it
    keeps legacy ledgers (and fixtures that never set the env) behaving as
    before. If the schema has no ``metadata_json`` column at all, only ``host``
    can match.
    """
    if not (db and os.path.isfile(db) and lane and category):
        return False
    if _int_or(strikes, 0) < 1:
        return False
    placement = placement if placement is not None else _current_placement()
    try:
        con = sqlite3.connect(db, timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        cols = {r[1] for r in con.execute("PRAGMA table_info(llm_calls)").fetchall()}
        if not {"status", "model_id", "feature_name", "error_category", "ts"} <= cols:
            con.close(); return False
        where = ["model_id = ?",
                 "feature_name NOT LIKE '%:refused'",
                 "feature_name NOT LIKE '%:crash'"]
        params = [lane]
        if "actor" in cols:
            # A dispatcher-crash row marks itself with an ``actor`` suffix
            # (feature_name stays the node name), so the exclusion reads actor.
            where.append("(actor IS NULL OR actor NOT LIKE '%:crash')")
            # The pre-spawn secret guard writes ``actor:refused`` (feature_name
            # stays the node name) — same shape, and it must never be a strike.
            where.append("(actor IS NULL OR actor NOT LIKE '%:refused')")
        if "metadata_json" in cols:
            # json_extract raises on malformed JSON, so guard with json_valid;
            # NULL/invalid/absent placement all resolve to the historic host.
            where.append(
                "CASE WHEN json_valid(metadata_json) THEN"
                " COALESCE(json_extract(metadata_json, '$.placement'), 'host')"
                " ELSE 'host' END = ?")
            params.append(placement)
        elif placement != "host":
            con.close(); return False
        where.append("datetime(ts) > datetime('now', ?)")
        params.append(f"-{_fuse_window_hours()} hours")
        params.append(_int_or(strikes, 1))
        rows = con.execute(
            "SELECT status, error_category FROM llm_calls WHERE "
            + " AND ".join(where)
            + " ORDER BY id DESC LIMIT ?", params).fetchall()
        con.close()
    except Exception:
        return False
    if len(rows) != _int_or(strikes, 1):
        return False
    return all(status == "failed" and err == category for status, err in rows)


def _apply_lane_fuse(db, chain: str, *, placement: str | None = None) -> tuple[str, str]:
    """Narrow a comma-joined chain by dropping lanes whose fuse is open.

    Returns ``(chain, refused_category)``. ``refused_category`` is non-empty
    only when EVERY lane is fused: then there is nothing left to dispatch to and
    the caller refuses (rc 43) rather than walking a chain of known-dead lanes.
    A partially-fused chain is narrowed and logged — a quota-walled tail lane
    costs a full attempt-timeout before the walk abandons it, which is exactly
    the stall the fallback chain exists to prevent.

    ``placement`` is resolved once and applied to every lane: one dispatch runs
    under one placement, and the chain view must agree with the single-lane
    view (RB-24).
    """
    lanes = [m.strip() for m in str(chain).split(",") if m.strip()]
    if not lanes or not (db and os.path.isfile(db)):
        return chain, ""
    if placement is None:
        placement = _current_placement()
    fused: dict[str, str] = {}
    for lane in lanes:
        for category, strikes in fuse_categories():
            if check_lane_fuse(db, lane, category, strikes=strikes,
                               placement=placement):
                fused[lane] = category
                break
    if not fused:
        return chain, ""
    if len(fused) == len(lanes):
        return chain, fused[lanes[0]]
    kept = [lane for lane in lanes if lane not in fused]
    sys.stderr.write(
        f"[lane_fuse_skip] dropping fused lanes "
        f"{','.join(f'{l}({c})' for l, c in fused.items())} from chain — "
        f"dispatching {'/'.join(kept)}\n")
    return ",".join(kept), ""


# ── llm_calls row writer (family-aware cost math + schema-adaptive cols) ──

# The row is written AFTER the provider call returned, so a lock it cannot get
# must never turn a paid, successful call into a failed node. Long enough to ride
# out a busy home (every parallel run's nodes write to the same state.db).
_LEDGER_BUSY_TIMEOUT_S = 30.0


def write_llm_calls_row(db, provider, model_id, tier, feature_name, actor, status,
                        duration_ms, cost_usd, error_message, input_tokens=0, output_tokens=0,
                        metadata_json="{}", cached_input_tokens=0, cache_creation_input_tokens=0,
                        error_category=None, retryable=None, thinking_tokens=None):
    if not (db and os.path.isfile(db)):
        return
    in_tok = _int_or(input_tokens, 0)
    out_tok = _int_or(output_tokens, 0)
    cached_in = _int_or(cached_input_tokens, 0)
    cache_create = _int_or(cache_creation_input_tokens, 0)
    # F2: per-provider-family rates + token semantics (anthropic envelopes
    # exclude cache from input; openai-style streams include it) live in ONE
    # place — telemetry.cache_aware_cost. The inline math this replaces priced
    # every provider at Anthropic list rates and always subtracted cache from
    # input, zeroing anthropic uncached cost and 10x-ing the codex breakdown.
    from mini_ork.dispatch.models import TokenUsage
    from mini_ork.dispatch.telemetry import cache_aware_cost
    cost_input_uncached, cost_input_cached, cost_cache_write = cache_aware_cost(
        TokenUsage(input_tokens=in_tok, output_tokens=out_tok,
                   cached_input_tokens=cached_in, cache_creation_tokens=cache_create),
        provider=str(provider or ""),
    )
    import json as _json
    try:
        _md = _json.loads(metadata_json or "{}")
    except Exception:
        _md = {}
    if not isinstance(_md, dict):
        _md = {}
    # RB-24: every row records the placement it ran under, so the lane fuse can
    # be keyed per (lane, placement) instead of lane alone. Merged into the
    # caller's metadata rather than replacing it (refusals keep ``refused``,
    # attempts keep ``dispatch_attempt``/``chain``) — and this is the one choke
    # point every llm_calls writer here goes through.
    _md["placement"] = _current_placement()
    _sess = _md.get("session_id")
    metadata_json = _json.dumps(_md)
    try:
        con = sqlite3.connect(db, timeout=_LEDGER_BUSY_TIMEOUT_S)
    except sqlite3.Error as exc:
        sys.stderr.write(f"[llm_calls] row not written ({status} {model_id}): {exc}\n")
        return
    try:
        _insert_llm_calls_row(con, provider, model_id, tier, feature_name, actor, status,
                              duration_ms, cost_usd, error_message, in_tok, out_tok,
                              metadata_json, _sess, cached_in, cache_create,
                              error_category, retryable, thinking_tokens,
                              cost_input_uncached, cost_input_cached, cost_cache_write)
    except sqlite3.Error as exc:
        # Researcher home, 2026-10-14: a successful implementer ($5.90) and a
        # panel synth that had already written APPROVE both ended `error` with
        # no row — the shape of this INSERT raising out of llm_dispatch's success
        # path (that home logs "database is locked" from its other writers),
        # which the caller's `except Exception` turns into rc=1.
        sys.stderr.write(f"[llm_calls] row not written ({status} {model_id}): {exc}\n")
    finally:
        con.close()


def _insert_llm_calls_row(con, provider, model_id, tier, feature_name, actor, status,
                          duration_ms, cost_usd, error_message, in_tok, out_tok,
                          metadata_json, _sess, cached_in, cache_create,
                          error_category, retryable, thinking_tokens,
                          cost_input_uncached, cost_input_cached, cost_cache_write):
    con.execute(f"PRAGMA busy_timeout={int(_LEDGER_BUSY_TIMEOUT_S * 1000)}")
    cols = {r[1] for r in con.execute("PRAGMA table_info(llm_calls)").fetchall()}
    insert_cols = ["provider", "model_id", "tier", "feature_name", "actor", "status",
                   "duration_ms", "cost_usd", "error_message", "iter", "run_id", "traceparent",
                   "input_tokens", "output_tokens", "total_tokens", "metadata_json", "session_id"]
    values = [provider, model_id, tier, feature_name, actor,
              status, _int_or(duration_ms, 0), float(cost_usd or 0.0), error_message or None,
              _int_or(os.environ.get("MO_RECURSIVE_ITER"), None),
              context_env("MINI_ORK_RUN_ID") or None, os.environ.get("MO_TRACEPARENT") or None,
              in_tok, out_tok, in_tok + out_tok, metadata_json or "{}", _sess]
    for name, val in (("error_category", error_category), ("retryable", retryable),
                      ("cached_input_tokens", cached_in),
                      ("cache_creation_input_tokens", cache_create),
                      ("thinking_tokens", _int_or(thinking_tokens, None)),
                      ("cost_input_uncached_usd", cost_input_uncached),
                      ("cost_input_cached_usd", cost_input_cached),
                      ("cost_cache_write_usd", cost_cache_write)):
        if name in cols:
            insert_cols.append(name); values.append(val)
    ph = ",".join("?" for _ in insert_cols)
    con.execute(f"INSERT INTO llm_calls ({', '.join(insert_cols)}) VALUES ({ph})", values)
    con.commit()


# ── model-level dispatch (delegates to the ported mini_ork.dispatch core) ──

# B2: how long a lead lane may stay silent before the walk moves on. The
# signal is "no model output at all", not wall clock, so a legitimately long
# implementer session that is streaming is never cut. ``"0"``/empty disables;
# a non-number fails CLOSED (disabled + one warning) rather than silently
# turning the safety net into a mystery timeout.
_DEFAULT_FIRST_OUTPUT_S = "300"


def _first_output_deadline_s() -> float | None:
    """Parse ``MO_DISPATCH_FIRST_OUTPUT_S``; ``None`` means disabled."""
    raw = os.environ.get("MO_DISPATCH_FIRST_OUTPUT_S")
    if raw is None:
        raw = _DEFAULT_FIRST_OUTPUT_S
    value = raw.strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        sys.stderr.write(
            f"[dispatch] MO_DISPATCH_FIRST_OUTPUT_S={raw!r} is not a number; "
            "first-output deadline disabled\n"
        )
        return None
    # > 0 is the arm condition (mirrors core.spawn_local); NaN/negative/0 all
    # mean "disabled" rather than an accidental immediate kill.
    return seconds if seconds > 0 else None


def _attempt_timeout_s() -> float | None:
    """Parse ``MO_DISPATCH_ATTEMPT_TIMEOUT_S``; ``None`` means unset/disabled."""
    raw = os.environ.get("MO_DISPATCH_ATTEMPT_TIMEOUT_S")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        # Fail closed: a malformed value must not crash the dispatch.
        return None
    return seconds if seconds > 0 else None


def mo_llm_dispatch(model, prompt, out_file, timeout_s=1500, max_turns=60, accept=None,
                    on_attempt=None) -> int:
    """Provider call via mini_ork.dispatch; writes out_file + .cost/.err.log sidecars.
    Returns 0 on success, the provider rc otherwise. (llm_calls is owned by the
    llm_dispatch wrapper, matching the bash split.)

    ``accept`` is an optional structural shape predicate (SE-3 Phase A.2). On the
    multi-lane path it is threaded into ``dispatch_with_fallback`` so a
    wrong-shape lane triggers fallback; on the single-lane path there is nobody
    to fall back to, so a clean-but-wrong-shape result is downgraded to
    ok=False/rc=SHAPE_REJECT_RC here — the same signal, so the caller never
    writes a structurally-broken artifact that the verifier will kill late.

    ``on_attempt`` is the per-lane observer from ``dispatch_with_fallback``
    (fail-soft there); threaded on the multi-lane path only — a single-lane
    failure is the final result and already gets its own failed row from the
    wrapper, so an attempt row would duplicate it."""
    from mini_ork.dispatch.models import DispatchRequest
    from mini_ork.dispatch.predicates import SHAPE_REJECT_RC
    from mini_ork.dispatch.providers import dispatch_model, dispatch_with_fallback
    lanes = [m.strip() for m in str(model).split(",") if m.strip()] or [str(model)]
    req = DispatchRequest(model=lanes[0], prompt=prompt, timeout_s=float(timeout_s),
                          max_turns=int(max_turns))
    if len(lanes) > 1:
        # Only a chain can hand a silent lead lane's turn to a next lane; the
        # single-lane branch below IS the last lane and never gets a deadline.
        res = dispatch_with_fallback(
            req, lanes,
            per_attempt_timeout_s=_attempt_timeout_s(),
            first_output_s=_first_output_deadline_s(),
            accept=accept, on_attempt=on_attempt,
        )
    else:
        res = dispatch_model(req)
        if accept is not None and res.ok and (res.text or "").strip() and not accept(res):
            from dataclasses import replace
            res = replace(
                res, ok=False, rc=SHAPE_REJECT_RC,
                error="dispatch result failed shape check "
                f"({getattr(accept, '__name__', 'accept')})",
            )
    try:
        open(out_file, "w", encoding="utf-8").write(res.text)
        open(out_file + ".cost", "w", encoding="utf-8").write(f"{res.cost_usd:.6f}")
        open(out_file + ".model", "w", encoding="utf-8").write(res.model or lanes[0])
    except OSError:
        pass
    u = res.usage
    # The thinking field is blank when the provider did not report it, so the
    # reader below can tell "unreported" (→ None → NULL) from a reported 0.
    thinking_field = "" if u.thinking_tokens is None else str(u.thinking_tokens)
    try:
        open(out_file + ".tokens", "w", encoding="utf-8").write(
            f"{u.input_tokens}\t{u.output_tokens}\t{u.cached_input_tokens}\t{u.cache_creation_tokens}"
            f"\t{thinking_field}")
    except (OSError, AttributeError):
        pass
    if not res.ok and res.error:
        try:
            open(out_file + ".err.log", "w", encoding="utf-8").write(res.error.rstrip() + "\n")
        except OSError:
            pass
    return 0 if res.ok else res.rc


# ── the llm_dispatch lane-routing wrapper ──

# node_type → structural shape predicate (SE-3 Phase A.2). Keyed on the
# --node-type the CLI receives. Deliberately minimal: only nodes whose artifact
# is *known* to be JSON are gated, so prose-emitting nodes (researcher,
# reviewer, reflector, …) are never false-rejected. `planner` is the SE-3
# incident node — a planner lane that returned bad JSON produced no artifact and
# the run died late at the verifier. Add an entry only once a node's JSON output
# contract is confirmed; this is a structural gate, not the semantic
# validate_artifact check. Override with MO_SHAPE_CHECK=0 (kill switch).
_NODE_SHAPE_PREDICATES: dict[str, Callable[[object], bool]] = {
    "planner": looks_like_json,
}


def _plan_artifact_predicate(path: str) -> Callable[[object], bool]:
    """Shape gate for a planner whose plan is a run-dir FILE, not its reply.

    ``cli/plan.py`` publishes ``MO_PLAN_ARTIFACT`` when the recipe's
    ``artifact_contract.yaml`` requires a plan artifact under
    ``${MINI_ORK_RUN_DIR}`` — today only ``recipes/epic-runner``, whose
    ``epic-runner-plan.json`` is read by the dispatcher, the aggregator, the
    verifier and three prompts, and whose prompt ends "Write the plan file and
    nothing else". Nothing anywhere reads that lane's reply.

    An agentic lane serving such a contract does the work with a tool and
    answers with a summary line, so the reply-text predicate rejects a planner
    that did its job. That is exactly what killed run-1791788217-61320: 4 lanes,
    13 dispatches, 47 minutes, and a schema-valid plan sitting in the run dir
    the whole time — the run was failed at 09:43:23, the same minute the file was
    written.

    The post-condition is unchanged — "is there a well-formed plan?" — read from
    the surface that carries it. The structural check is literally the same one
    (``looks_like_json_text``), so the two surfaces cannot disagree about what a
    plan looks like. A missing or unparseable file is the SE-3 failure the gate
    exists to catch, so it still returns False there.
    """
    def check(_result: object) -> bool:
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            return False
        return looks_like_json_text(text)
    return check


def _shape_predicate_for(node_type: str) -> Callable[[object], bool] | None:
    if os.environ.get("MO_SHAPE_CHECK", "1") == "0":
        return None
    predicate = _NODE_SHAPE_PREDICATES.get(node_type)
    if predicate is None:
        return None
    if node_type == "planner":
        artifact = os.environ.get("MO_PLAN_ARTIFACT", "").strip()
        if artifact:
            return _plan_artifact_predicate(artifact)
    return predicate


def llm_dispatch(argv=None, *, root=None, dispatch_fn=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = root or context_env("MINI_ORK_ROOT") or os.getcwd()
    home = context_env("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    db = context_env("MINI_ORK_DB") or os.path.join(home, "state.db")
    # A test/caller-supplied dispatch_fn keeps the historic 5-arg contract and
    # must never receive the A.2 accept= kwarg; only the default dispatcher does.
    _default_dispatch = dispatch_fn is None
    dispatch_fn = dispatch_fn or mo_llm_dispatch

    node_type = prompt_text = out_file = model_override = ""
    timeout_s = _int_or(os.environ.get("MO_NODE_TIMEOUT_S"), 1500)
    max_turns = _int_or(os.environ.get("MO_NODE_MAX_TURNS"), 60)
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--task-class":
            _task_class = argv[i + 1]; i += 2  # accepted for CLI compat; currently unused
        elif a == "--node-type":
            node_type = argv[i + 1]; i += 2
        elif a == "--prompt-text":
            prompt_text = argv[i + 1]; i += 2
        elif a == "--out":
            out_file = argv[i + 1]; i += 2
        elif a == "--model":
            model_override = argv[i + 1]; i += 2
        elif a == "--timeout":
            timeout_s = _int_or(argv[i + 1], timeout_s); i += 2
        elif a == "--max-turns":
            max_turns = _int_or(argv[i + 1], max_turns); i += 2
        else:
            i += 1

    # Telemetry identity is node-scoped, not result-scoped — hoisted above the
    # cost circuit / lane fuse so a REFUSED dispatch (nothing attempted, rc
    # 42/43) still leaves an llm_calls row. A node that never dispatched used
    # to leave the ledger blank, and triage could not tell "refused by a
    # guard" from "crashed before the row write" without parsing streams.
    feature = f"mini-ork:{node_type or 'unknown'}"
    actor = os.environ.get("MO_LANE_ACTOR") or node_type or os.environ.get("USER", "unknown")
    tier = os.environ.get("MO_LANE_TIER", "default")
    # Bound early (the guards below fire before lane resolution) so the
    # refusal row names the requested lane even when nothing resolved it.
    model = model_override or os.environ.get("MINI_ORK_DEFAULT_MODEL", "sonnet")

    def _write_refusal_row(kind: str, message: str) -> None:
        """Ledger row for a dispatch that a guard refused before any attempt.

        ``feature_name`` gets a ``:refused`` suffix so the row is visible in
        the ledger but stays OUTSIDE the lane-fuse window (``check_lane_fuse``
        excludes ``feature_name LIKE '%:refused'``): a refusal row naming the
        fuse's own category must not keep the fuse open forever, and must not
        break a genuine failure streak either. ``retryable=0`` — no lane
        change recovers a budget halt. Zero cost/duration: nothing ran.

        ``kind`` (cost_limit / the fuse category) rides in ``metadata_json``
        and, when it is a provider vocabulary word, in ``error_category``: the
        llm_calls CHECK constraint pins the category column to the provider
        vocabulary, and "cost_limit" is not a provider error — a reader that
        wants the refusal reason reads metadata, not the category column."""
        category = kind if kind in ("auth", "quota", "capacity", "request", "safety",
                                    "network", "stream", "provider", "config") else "unknown"
        try:
            write_llm_calls_row(
                db, "", str(model), tier, feature + ":refused", actor,
                "failed", 0, "0", redact_secrets(message[:200]),
                error_category=category, retryable=0,
                metadata_json=json.dumps({"refused": kind}))
        except Exception:  # noqa: BLE001 — telemetry must never change the rc
            pass

    def _halt_on_fuse(chain: str, refused: str) -> int:
        """Refuse a dispatch whose every lane is fused. Returns the refusal rc.

        RB-39: besides the ``llm_calls`` refusal row, stamp the run's
        ``task_runs`` row so the fuse's history is queryable. ``chain`` is the
        requested chain verbatim — ``_apply_lane_fuse`` collapses the per-lane
        breakdown to the first blown category, so any narrower value would
        pretend to a precision we do not have. Fail-soft: telemetry must never
        change the rc (a pre-0023 schema raises OperationalError here and is
        swallowed), and a missing/unknown run id is a no-op.
        """
        sys.stderr.write(
            f"[lane_fuse_open] every lane in {chain} failed consecutively with "
            f"error_category={refused} — halting dispatch\n")
        _write_refusal_row(
            refused,
            f"lane fuse open: every lane in chain {chain} failed consecutively "
            f"with {refused}")
        run_id = context_env("MINI_ORK_RUN_ID", "").strip()
        if run_id and db and os.path.isfile(db):
            try:
                con = sqlite3.connect(db, timeout=5.0)
                try:
                    con.execute("PRAGMA busy_timeout=5000")
                    con.execute(
                        "UPDATE task_runs SET fuse_blown_lane = ?, "
                        "fuse_consecutive_failures = "
                        "COALESCE(fuse_consecutive_failures, 0) + 1 "
                        "WHERE id = ?",
                        (chain, run_id))
                    con.commit()
                finally:
                    con.close()
            except Exception:  # noqa: BLE001 — telemetry must never change the rc
                pass
        return 43

    # RB-22b: a target cwd that no longer exists (a deleted worktree) made every
    # lane in the chain fail `spawn failed: [Errno 2] No such file or directory`
    # — each recorded as that lane's own failure, four attempts and 45s spent
    # walking a chain that could never work, then rc=127. The cwd is a run-wide
    # config fact, not a lane verdict: fail ONCE, before any lane is dispatched.
    # This runs before the cost circuit so the config row is the only row a
    # dead target produces.
    target_cwd = (os.environ.get("MO_TARGET_CWD") or "").strip()
    if target_cwd and not os.path.isdir(target_cwd):
        message = f"target cwd does not exist: {target_cwd}"
        _write_refusal_row("config", message)
        sys.stderr.write(f"[target_cwd_missing] {message}\n")
        return 127

    # cost circuit breaker
    if cost_circuit_open(db, os.environ.get("MO_DAILY_BUDGET_USD", "50")):
        from mini_ork import cost_ledger
        spent = cost_ledger.spent_last_24h(db)
        sys.stderr.write(f"[cost_circuit_open] spent_today=${spent} "
                         f"budget=${os.environ.get('MO_DAILY_BUDGET_USD', '50')} — halting dispatch\n")
        _write_refusal_row("cost_limit",
                           f"cost circuit open: ${spent:.2f} spent in 24h against "
                           f"MO_DAILY_BUDGET_USD={os.environ.get('MO_DAILY_BUDGET_USD', '50')}")
        return 42

    # model resolution: override > agents.yaml lane > env default > sonnet
    model = model_override or os.environ.get("MINI_ORK_DEFAULT_MODEL", "sonnet")
    if not model_override and node_type:
        model = resolve_lane_model(node_type, root, home)

    # B3 lane affinity: a revise round / `recover` re-enters here and re-runs
    # routing, so the chain starts from its lead again — even when a fallback
    # lane actually served the previous round, discarding the provider prompt
    # cache that lane had warmed. Move the lane that served this node's first
    # successful round to the front, but ONLY while the routing lead is
    # unchanged: a remapped lead (operator, `recover --lane`, agents.yaml) makes
    # `preferred` return None and the operator's choice wins. The reorder runs
    # AFTER the shape-contract expansion below (it must see the final chain: a
    # JSON-contract node's fallback tail did not exist here), and the second
    # fuse pass still runs after it, so a fused affinity lane stays dropped.
    # `_aff_lead` is bound unconditionally (pre-expansion chain[0]) because the
    # success path records against it and must never NameError into the guard.
    _aff_run_dir = context_env("MINI_ORK_RUN_DIR")
    _aff_node = context_env("MO_NODE_ID")
    _aff_lead = str(model).split(",", 1)[0].strip()

    # lane fuse — on by default, because a fuse that is off by default is not a
    # safety system. Keyed on the LANE (llm_calls.model_id), not the node role:
    # a fallback tail lane's failures are recorded against the lane, so a
    # role-keyed fuse could never open on one and codex stayed the terminal rung
    # of every coding chain for a day of 100% quota failures (2026-10-12).
    # Terminal categories only unless MO_FUSE_TRANSIENT=1.
    #
    # Applied TWICE. Here, on the resolution-time model; and again below, after
    # the shape-contract expansion appends the role's fallback tail. That tail is
    # composed outside this gate, so a fused tail lane used to ride in unfused:
    # the planner's tail kept reaching a kimi whose key was rejected, once per
    # attempt, all day (2026-10-12). A second pass is cheap — ``_apply_lane_fuse``
    # returns early when the ledger is absent or nothing is fused.
    if os.environ.get("MO_FUSE_ENABLED", "1") == "1" and os.path.isfile(db):
        chain, refused = _apply_lane_fuse(db, model)
        if refused:
            return _halt_on_fuse(model, refused)
        model = chain

    tmp_out = ""
    if not out_file:
        import tempfile
        fd, tmp_out = tempfile.mkstemp(prefix="mo-llm-")
        os.close(fd)
        out_file = tmp_out

    max_attempts = _int_or(os.environ.get("MO_DISPATCH_MAX_ATTEMPTS"), 3)
    if max_attempts < 1:
        max_attempts = 1
    if model == "glm" and os.environ.get("MO_GLM_MAX_ATTEMPTS", "").isdigit():
        max_attempts = max(max_attempts, int(os.environ["MO_GLM_MAX_ATTEMPTS"]))

    # A.2 boundary shape check — None unless this node's artifact is known JSON.
    accept_fn = _shape_predicate_for(node_type)

    # A JSON-contract node (today: the planner) is dispatched with the role's
    # fallback TAIL behind its lead lane, not a lone lane. A lead that returns the
    # WRONG SHAPE then counts as a lane failure inside ``dispatch_with_fallback``
    # and the next lane serves, instead of the run dying on rc=SHAPE_REJECT_RC.
    #
    # Why the planner needed this specifically: it is dispatched by the Python
    # plan runtime (``cli/plan.py`` -> ``llm_dispatch``), NOT through the executor
    # that publishes ``MO_DISPATCH_CHAIN`` (the planner early-handler no-ops), so
    # ``model`` here was a single lane. An intermittently shape-rejecting glm
    # planner (2 of 3 calls observed 2026-10-09) then hard-failed the whole run at
    # plan.py's ``rc != 0`` branch — BEFORE its repair loop, which only ever sees
    # an rc==0 plan with wrong *content*, never a wrong *shape*. Prose nodes keep
    # single-lane dispatch: the chain is applied only when a shape predicate is
    # armed, and an explicit ``model_override``/already-multi-lane model is left
    # untouched.
    if accept_fn is not None and not model_override and "," not in str(model):
        try:
            from mini_ork.dispatch.routing import dispatch_chain
            model = dispatch_chain(node_type, resolve_lane_family(model, root, home), root)
        except Exception:
            pass

    # B3 lane affinity — apply the reorder to the FINAL chain, after the
    # shape-contract expansion above. For a JSON-contract node (the planner,
    # which also sees the most shape rejects) the lane that served an earlier
    # round only becomes a chain member here; reordering before the expansion
    # made affinity a no-op for exactly the node that needed it.
    if _aff_run_dir and _aff_node and lane_affinity.enabled():
        try:
            _aff_lanes = [m.strip() for m in str(model).split(",") if m.strip()]
            _aff_served = lane_affinity.preferred(_aff_run_dir, _aff_node, _aff_lead)
            _aff_reordered = lane_affinity.apply(_aff_lanes, _aff_served)
            if _aff_reordered != _aff_lanes:
                model = ",".join(_aff_reordered)
                sys.stderr.write(
                    f"[lane_affinity] {_aff_node}: {_aff_served} first "
                    f"(served an earlier round; lead {_aff_lead})\n")
        except Exception:  # noqa: BLE001 — affinity must never change dispatch
            pass

    # Second fuse pass — the chain above did not exist when the first one ran.
    if os.environ.get("MO_FUSE_ENABLED", "1") == "1" and os.path.isfile(db):
        chain, refused = _apply_lane_fuse(db, model)
        if refused:
            return _halt_on_fuse(model, refused)
        model = chain

    start_ms = int(time.time() * 1000)
    attempt = 1
    rc = 1
    # Lanes whose attempt row already carries that attempt's real cost/tokens.
    # The final failed row skips its usage when its lane is in here, so the
    # last attempt of an all-lanes-failed chain is billed once, not twice.
    attempt_rows_with_usage: set[str] = set()

    def _attempt_sink(lane: str, result) -> None:
        """Per-lane failed-attempt ledger row (the ``on_attempt`` observer).

        A fallback chain used to leave ONE row for the final result only, so
        the lanes that died mid-chain (minimax silent death, codex rc=1 in
        run-1791619207-22518) existed nowhere but the live-stream files. This
        sink writes one failed row per dead lane, attributed to THAT lane, with
        the provider's own classified error.

        RB-32b: a lane that returned output but failed the shape check was
        *billed* by the provider, so its row carries that attempt's real
        cost/tokens (``DispatchResult.cost_usd``/``.usage`` — both survive the
        shape-reject downgrade in ``dispatch_with_fallback``). Pinning them to
        0 undercounted 45 billed calls in one week. Recording them here does
        not double-count: the final row carries only the FINAL attempt's usage,
        and when every lane fails the final row drops its usage because this
        sink already recorded that same attempt (``attempt_rows_with_usage``).
        The ``metadata_json`` attempt marker lets a reader tell attempt rows
        from final rows. The winning attempt is not recorded (the success row
        below already names the serving lane)."""
        try:
            ok = bool(getattr(result, "ok", False))
            text = (getattr(result, "text", "") or "").strip()
            if ok and text:
                return
            raw_err = getattr(result, "error", "") or "empty output"
            result_rc = getattr(result, "rc", 1)
            err = redact_secrets(raw_err[:200])
            # Classify the FULL error text, not the 200-char ``error_message``
            # slice: Codex prints a generic rc=1 header first and the real
            # provider rejection (quota, model access) in the tail. The stdout
            # tail is model prose, so a non-shape attempt whose error alone is
            # unclassifiable may only be PROMOTED by it to a provider-structured
            # class (``TAIL_PROMOTABLE``) — never to a fuse-terminal one like
            # ``credential`` (RB-10: prose mentioning "401"/"forbidden" must not
            # fuse a lane for the run).
            cls, category = failure_class.classify(raw_err, result_rc)
            class_text = raw_err
            if cls == "unknown" and str(result_rc) != str(SHAPE_REJECT_RC):
                out_tail = (getattr(result, "text", "") or "")[-1000:]
                if out_tail:
                    tail_text = raw_err + "\n" + out_tail
                    tail_cls, _tail_category = failure_class.classify(
                        tail_text, result_rc)
                    if tail_cls in failure_class.TAIL_PROMOTABLE:
                        # The promoted class steers the retry and lands in
                        # metadata only. ``error_category`` (what the fuse
                        # reads) stays the error-only verdict: the tail is
                        # still text a model may have written ("quota
                        # exceeded" in prose), and quota/config are 1-strike.
                        cls, class_text = tail_cls, tail_text
            hint = failure_class.wait_hint(class_text)
            row_actor = actor
            if failure_class.is_run_wide(cls, class_text):
                # A missing cwd or a confused target: nothing ran on this
                # lane, so the row is not its verdict. ``:refused`` keeps it
                # out of the fuse's streak (the RB-24 convention below).
                row_actor = actor + ":refused"
            if _is_prespawn_guard_refusal(raw_err):
                # RB-24: a remote pre-spawn guard refused this lane before
                # anything ran. Mark the row ``:refused`` so the fuse never
                # counts it as this lane's verdict under this placement.
                row_actor = actor + ":refused"
                category = "config"
                retryable = 0
            else:
                retryable = failure_class.retryable(cls)
            metadata = {"dispatch_attempt": attempt, "chain": str(model),
                        "failure_class": cls}
            if hint is not None:
                metadata["wait_hint_s"] = hint
            usage = getattr(result, "usage", None)
            in_tok = int(getattr(usage, "input_tokens", 0) or 0)
            out_tok = int(getattr(usage, "output_tokens", 0) or 0)
            cached_in = int(getattr(usage, "cached_input_tokens", 0) or 0)
            cache_create = int(getattr(usage, "cache_creation_tokens", 0) or 0)
            thinking = getattr(usage, "thinking_tokens", None)
            cost_usd = float(getattr(result, "cost_usd", 0.0) or 0.0)
            write_llm_calls_row(
                db, provider_for_model(lane), lane, tier, feature, row_actor, "failed",
                int(getattr(result, "duration_ms", 0) or 0), f"{cost_usd:.6f}", err,
                in_tok, out_tok,
                metadata_json=json.dumps(metadata),
                error_category=category, retryable=retryable,
                cached_input_tokens=cached_in,
                cache_creation_input_tokens=cache_create,
                thinking_tokens=thinking)
            if cost_usd or in_tok or out_tok or cached_in or cache_create:
                attempt_rows_with_usage.add(lane)
        except Exception:  # noqa: BLE001 — telemetry must never break dispatch
            pass

    while True:
        for side in (out_file + ".err.log",):
            try:
                os.remove(side)
            except OSError:
                pass
        # Thread the observer and the shape predicate into any dispatcher that
        # declares them, not just the default one. A caller-supplied dispatch_fn
        # that accepts them was silently starved of per-lane ledger rows and of
        # the shape check — the gate used to be `if _default_dispatch`.
        _kwargs = {}
        if _default_dispatch or _accepts_kwarg(dispatch_fn, "on_attempt"):
            _kwargs["on_attempt"] = _attempt_sink
            if accept_fn is not None and (
                    _default_dispatch or _accepts_kwarg(dispatch_fn, "accept")):
                _kwargs["accept"] = accept_fn
        try:
            rc = dispatch_fn(model, prompt_text, out_file, timeout_s, max_turns,
                             **_kwargs)
        except Exception as exc:
            # A crash inside the dispatcher used to escape to the caller's
            # ``except Exception → rc=1`` with NO ledger row — the same
            # stream-only blind spot as a dead lane. Record the crash, then
            # re-raise: the caller's contract (exception → rc=1 + text) is
            # unchanged, it just stops being invisible.
            try:
                # Classify the crash by its TYPE, not the whole message: the
                # message can embed the prompt (RB-10). The ``:crash`` actor
                # suffix is what keeps this row out of the lane-fuse streak —
                # it records the engine dying, not a verdict on the lane.
                crash_category = classify_crash(exc)
                write_llm_calls_row(
                    db, provider_for_model(str(model).split(",", 1)[0]), str(model),
                    tier, feature, actor + ":crash", "failed",
                    int(time.time() * 1000) - start_ms, "0",
                    redact_secrets(f"dispatcher crashed: {exc}"[:200]),
                    error_category=crash_category,
                    retryable=int(error_retryable(crash_category)))
            except Exception:  # noqa: BLE001 — never mask the original crash
                pass
            raise
        if rc == 0:
            break
        probe = ""
        for p in (out_file + ".err.log", out_file):
            try:
                probe += open(p, errors="ignore").read()[-1000:]
            except OSError:
                pass
        probe = probe[-2000:]
        # One action table decides the retry: the class (not the legacy category)
        # says whether the same chain can recover, and whether the provider told
        # us how long to wait.
        cls, _db_category = failure_class.classify(probe, rc)
        act = failure_class.action(cls, probe)
        fair_usage = glm_fair_usage_retryable(model, probe, attempt, max_attempts)
        # A wrong-SHAPE reply is a stochastic model-formatting miss, not a terminal
        # failure: the lane produced output, it was merely unusable. Retry it in
        # the same backoff loop (bounded by max_attempts) so a transient
        # non-JSON planner turn does not end the run. Scoped to nodes whose
        # artifact is known JSON (``accept_fn``), so prose nodes are unaffected.
        shape_reject = (accept_fn is not None and rc == SHAPE_REJECT_RC
                        and attempt < max_attempts)
        hint = act.wait_hint_s
        sleep_cap = _int_or(os.environ.get("MO_DISPATCH_RETRY_MAX_SLEEP_S"), 45)
        if hint is not None and hint > sleep_cap:
            # Asking for a longer wait than the caller will tolerate: do not
            # re-run this chain. This outranks the glm fair-usage and shape
            # rules, which would otherwise re-retry the same wall.
            sys.stderr.write(
                f"[dispatch] {model} asks to wait {hint:g}s (above the "
                f"{sleep_cap}s cap); not retrying\n")
            break
        retry_chain = act.retry_chain and attempt < max_attempts
        if cls == "shape" and accept_fn is None:
            # A "shape" word in the probe of a node with NO shape contract is
            # just text (often the walk's own reject line echoed into
            # .err.log). Today's shape retry is scoped to nodes whose artifact
            # is known JSON; keep that scope.
            retry_chain = False
        if retry_chain or fair_usage or shape_reject:
            sleep_s = min(hint, sleep_cap) if hint is not None else backoff_seconds(attempt)
            if fair_usage:
                reason = "fair_usage"
            elif shape_reject and not retry_chain:
                reason = "shape_reject"
            else:
                reason = cls
            sys.stderr.write(f"[llm_dispatch RETRY model={model} reason={reason} "
                             f"attempt={attempt}/{max_attempts} sleep={sleep_s}s]\n")
            time.sleep(sleep_s)
            attempt += 1
            continue
        break

    requested_model = str(model).split(",", 1)[0].strip()
    selected_model = requested_model
    try:
        selected_model = open(out_file + ".model", encoding="utf-8").read().strip() or selected_model
    except OSError:
        pass
    provider = provider_for_model(selected_model)
    _write_lane_sidecar(selected_model)

    if rc == 0:
        # Per-node record of the lane that ACTUALLY served this node. The
        # run-wide ``.last-llm-lane`` cannot be used for this: parallel lenses
        # share one run dir and overwrite each other. Written only on success
        # (a fallback chain ending in failure has no serving lane to report)
        # and fail-soft — it must never change a dispatch outcome.
        _write_node_lane_record(requested_model, selected_model)
        duration_ms = int(time.time() * 1000) - start_ms
        _write_duration_ms(duration_ms)
        strip_protocol_blocks(out_file)
        sys.stdout.write(open(out_file, errors="ignore").read())
        cost_usd = "0"
        if os.path.isfile(out_file + ".cost"):
            cost_usd = open(out_file + ".cost").read().strip() or "0"
        in_tok, out_tok, cached_in, cache_create, thinking = _read_token_sidecar(
            out_file + ".tokens")
        write_llm_calls_row(db, provider, selected_model, tier, feature, actor, "success",
                            duration_ms, cost_usd, "", in_tok, out_tok, "{}", cached_in, cache_create,
                            thinking_tokens=thinking)
        # B3 lane affinity: only a SUCCESS may define the node's affinity, and
        # only the first one does (``record`` never overwrites an existing
        # entry). ``_aff_lead`` is the pre-reorder routing lead, the same value
        # ``preferred`` keyed on above. Same guards as the reorder.
        if _aff_run_dir and _aff_node and lane_affinity.enabled():
            try:
                lane_affinity.record(
                    _aff_run_dir, _aff_node, _aff_lead, selected_model)
            except Exception:  # noqa: BLE001 — affinity must never change dispatch
                pass
        run_dir = context_env("MINI_ORK_RUN_DIR")
        if os.path.isfile(out_file + ".cost") and run_dir:
            try:
                open(os.path.join(run_dir, ".last-llm-cost"), "w").write(open(out_file + ".cost").read())
                os.remove(out_file + ".cost")
            except OSError:
                pass
        for side in (out_file + ".tokens", out_file + ".model"):
            try:
                os.remove(side)
            except OSError:
                pass
        if tmp_out:
            try:
                os.remove(tmp_out)
            except OSError:
                pass
        return 0

    duration_ms = int(time.time() * 1000) - start_ms
    _write_duration_ms(duration_ms)
    err_full = ""
    try:
        # Read the WHOLE file for guard detection: the pre-spawn guard's
        # identifying prefix is at the front, while the ledger column keeps the
        # historic 200-char tail.
        err_full = open(out_file + ".err.log", errors="ignore").read()
    except OSError:
        pass
    err = err_full[-200:]
    # The claude-code wrapper hides the real provider rejection in a stdout
    # ``result`` object (``is_error: true``, ``api_error_status``, ``result``).
    # Surface it so ``error_message``/``error_category`` carry the provider's
    # own words (the MiniMax 429 quota text) instead of the CLI's stderr chatter.
    provider_status, provider_err = _provider_error_record(out_file)
    if provider_err:
        err = provider_err
    err = redact_secrets(err)
    # Classify the FULL text (stderr plus the structured provider rejection),
    # not the 200-char tail kept above for ``error_message``. ``provider_err``
    # is a parsed provider ``result`` object, not free-form stdout prose, so it
    # is safe to add here (RB-10 guards against the raw stdout tail, not this).
    class_text = err_full
    if provider_err:
        class_text = f"{class_text}\n{provider_err}" if class_text else provider_err
    cls, category = failure_class.classify(class_text, rc, http_status=provider_status)
    hint = failure_class.wait_hint(class_text)
    retryable = failure_class.retryable(cls)
    metadata = {"failure_class": cls}
    if provider_status is not None:
        # The status the class was decided on, so the ledger shows why.
        metadata["http_status"] = provider_status
    if hint is not None:
        metadata["wait_hint_s"] = hint
    row_actor = actor
    if failure_class.is_run_wide(cls, class_text):
        # Run-wide (missing cwd, confused target): not this lane's verdict.
        row_actor = actor + ":refused"
    if _is_prespawn_guard_refusal(err_full) or _is_prespawn_guard_refusal(provider_err):
        # RB-24: nothing ran under this lane — the remote secret guard refused
        # it before the spawn. Keep a visible row, but ``:refused`` keeps it
        # out of the lane fuse's streak for this placement.
        row_actor = actor + ":refused"
        category = "config"
        retryable = 0
    # A failed call still burned wall-clock, and partial provider usage often
    # survives in the sidecars — record both instead of hard zeros.
    cost_usd = "0"
    if os.path.isfile(out_file + ".cost"):
        cost_usd = open(out_file + ".cost").read().strip() or "0"
    in_tok, out_tok, cached_in, cache_create, thinking = _read_token_sidecar(
        out_file + ".tokens")
    if selected_model in attempt_rows_with_usage:
        # RB-32b: every lane failed and the LAST attempt (this result) already
        # has its own attempt row carrying its real spend. The final row is the
        # same attempt, not a chain total — leave its usage at zero so
        # ``spent_last_24h`` counts the trial once. A single-lane failure has no
        # attempt row, so its numbers stay here (the only row it gets).
        cost_usd = "0"
        in_tok = out_tok = cached_in = cache_create = 0
        thinking = None
    write_llm_calls_row(db, provider, selected_model, tier, feature, row_actor, "failed",
                        duration_ms, cost_usd, err, in_tok, out_tok,
                        json.dumps(metadata), cached_in, cache_create,
                        error_category=category, retryable=retryable, thinking_tokens=thinking)
    sys.stderr.write(f"[llm_dispatch FAIL model={model} rc={rc}]\n")
    for side in (out_file + ".tokens", out_file + ".model"):
        try:
            os.remove(side)
        except OSError:
            pass
    if tmp_out:
        try:
            os.remove(tmp_out)
        except OSError:
            pass
    return rc


def _write_duration_ms(ms):
    rd = context_env("MINI_ORK_RUN_DIR")
    if not rd:
        return
    try:
        open(os.path.join(rd, ".last-llm-duration-ms"), "w").write(str(ms))
    except OSError:
        pass


def _write_lane_sidecar(lane):
    """Open this node's sidecar window: stamp the node, drop the previous node's
    sidecar values, then persist the RESOLVED lane.

    The lane is what lets a stage-trace writer attribute the call — without it
    the model that actually served a planner/verifier dispatch dies inside this
    process and the stage trace lands lane-less (invisible to both advantage
    writebacks). The stamp is what closes the window again: the sidecars are
    overwritten, never consumed, so a node that dispatches nothing would
    otherwise read the previous dispatch's cost back as its own.

    Clearing happens BEFORE the cost write below, so a dispatch that fails and
    writes no cost leaves nothing to inherit, while one that succeeds leaves
    only its own numbers. Freshness- and node-gated on read in trace_store."""
    rd = context_env("MINI_ORK_RUN_DIR")
    if not rd:
        return
    node = os.environ.get("MO_NODE_ID", "")
    if node:
        try:
            open(os.path.join(rd, ".last-llm-node"), "w").write(node)
        except OSError:
            pass
    for stale in (".last-llm-cost", ".last-llm-duration-ms", ".last-llm-lane"):
        try:
            os.remove(os.path.join(rd, stale))
        except OSError:
            pass
    if not lane:
        return
    try:
        open(os.path.join(rd, ".last-llm-lane"), "w").write(str(lane))
    except OSError:
        pass


# A node id becomes part of a file name, so it must not be able to leave the
# run dir (``/``, ``..``) or smuggle in a path separator.
_SAFE_NODE_ID = re.compile(r"[A-Za-z0-9_.-]+")


def _write_node_lane_record(requested: str, served: str) -> None:
    """Persist ``<run_dir>/.lane-<MO_NODE_ID>.json`` for this node.

    The panel gate can already read ``llm_calls`` for the serving lane, but the
    judge's own artifact still carries only the LLM's self-reported label. This
    per-node record is what lets ``_stamp_served_lane`` write the harness-truth
    lane over that label; the run-wide ``.last-llm-lane`` races between parallel
    lenses and cannot be attributed to one node.

    Both ``MINI_ORK_RUN_DIR`` and ``MO_NODE_ID`` must be set, the node id must
    be safe as a file-name component, and every error is swallowed: this is
    evidence for a verifier, never a dispatch outcome.
    """
    run_dir = context_env("MINI_ORK_RUN_DIR")
    node = context_env("MO_NODE_ID")
    if not run_dir or not node or not served:
        return
    if _SAFE_NODE_ID.fullmatch(node) is None:
        return
    try:
        record = {
            "requested": requested,
            "served": served,
            "fallback": served != requested,
            "at": int(time.time()),
        }
        path = os.path.join(run_dir, f".lane-{node}.json")
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — never change a dispatch outcome
        pass


if __name__ == "__main__":
    raise SystemExit(llm_dispatch())
