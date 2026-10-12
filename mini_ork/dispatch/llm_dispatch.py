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

import inspect
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Callable

from mini_ork.context import context_env
from mini_ork.dispatch.predicates import SHAPE_REJECT_RC, looks_like_json


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
    if re.search(r"429", text) and re.search(
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
    """The real provider error from a claude-code ``result`` object on stdout.

    The claude-code wrapper prints a provider rejection as a JSON ``result``
    object (or a JSONL stream line) on stdout — ``{"type":"result",
    "is_error":true, "api_error_status":429, "result":"API Error: …"}`` — while
    its stderr only carries CLI chatter. The failed-call tail reads ``.err.log``
    only, so the 429 sat invisible in stdout JSON and ``classify_error`` never
    saw the quota wording. Parse stdout for that object and return
    ``f"HTTP {status}: {result}"[:1000]`` (no ``HTTP`` part when there is no
    status); return ``""`` when stdout has no such object.
    """
    try:
        raw = open(out_file, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
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
    best = ""
    for obj in candidates:
        if obj.get("is_error") is not True:
            continue
        result = obj.get("result")
        if not isinstance(result, str) or not result:
            continue
        status = obj.get("api_error_status")
        prefix = f"HTTP {status}: " if status is not None and str(status) else ""
        best = (prefix + result)[:1000]
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


def check_lane_fuse(db, lane: str, category: str, *, strikes: int = 3) -> bool:
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
    """
    if not (db and os.path.isfile(db) and lane and category):
        return False
    if _int_or(strikes, 0) < 1:
        return False
    try:
        con = sqlite3.connect(db, timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        cols = {r[1] for r in con.execute("PRAGMA table_info(llm_calls)").fetchall()}
        if not {"status", "model_id", "feature_name", "error_category", "ts"} <= cols:
            con.close(); return False
        rows = con.execute(
            "SELECT status, error_category FROM llm_calls "
            "WHERE model_id = ? AND feature_name NOT LIKE '%:refused' "
            "AND datetime(ts) > datetime('now', ?) "
            "ORDER BY id DESC LIMIT ?",
            (lane, f"-{_fuse_window_hours()} hours", _int_or(strikes, 1))).fetchall()
        con.close()
    except Exception:
        return False
    if len(rows) != _int_or(strikes, 1):
        return False
    return all(status == "failed" and err == category for status, err in rows)


def _apply_lane_fuse(db, chain: str) -> tuple[str, str]:
    """Narrow a comma-joined chain by dropping lanes whose fuse is open.

    Returns ``(chain, refused_category)``. ``refused_category`` is non-empty
    only when EVERY lane is fused: then there is nothing left to dispatch to and
    the caller refuses (rc 43) rather than walking a chain of known-dead lanes.
    A partially-fused chain is narrowed and logged — a quota-walled tail lane
    costs a full attempt-timeout before the walk abandons it, which is exactly
    the stall the fallback chain exists to prevent.
    """
    lanes = [m.strip() for m in str(chain).split(",") if m.strip()]
    if not lanes or not (db and os.path.isfile(db)):
        return chain, ""
    fused: dict[str, str] = {}
    for lane in lanes:
        for category, strikes in fuse_categories():
            if check_lane_fuse(db, lane, category, strikes=strikes):
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
        _sess = _md.get("session_id") if isinstance(_md, dict) else None
    except Exception:
        _sess = None
    con = sqlite3.connect(db, timeout=5)
    con.execute("PRAGMA busy_timeout=5000")
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
    con.commit(); con.close()


# ── model-level dispatch (delegates to the ported mini_ork.dispatch core) ──

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
    req = DispatchRequest(model=lanes[0], prompt=prompt, timeout_s=float(timeout_s))
    if len(lanes) > 1:
        res = dispatch_with_fallback(req, lanes, accept=accept, on_attempt=on_attempt)
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


def _shape_predicate_for(node_type: str) -> Callable[[object], bool] | None:
    if os.environ.get("MO_SHAPE_CHECK", "1") == "0":
        return None
    return _NODE_SHAPE_PREDICATES.get(node_type)


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

    # lane fuse — on by default, because a fuse that is off by default is not a
    # safety system. Keyed on the LANE (llm_calls.model_id), not the node role:
    # a fallback tail lane's failures are recorded against the lane, so a
    # role-keyed fuse could never open on one and codex stayed the terminal rung
    # of every coding chain for a day of 100% quota failures (2026-10-12).
    # Terminal categories only unless MO_FUSE_TRANSIENT=1.
    if os.environ.get("MO_FUSE_ENABLED", "1") == "1" and os.path.isfile(db):
        chain, refused = _apply_lane_fuse(db, model)
        if refused:
            sys.stderr.write(
                f"[lane_fuse_open] every lane in {model} failed consecutively with "
                f"error_category={refused} — halting dispatch\n")
            _write_refusal_row(
                refused,
                f"lane fuse open: every lane in chain {model} failed consecutively "
                f"with {refused}")
            return 43
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

    start_ms = int(time.time() * 1000)
    attempt = 1
    rc = 1

    def _attempt_sink(lane: str, result) -> None:
        """Per-lane failed-attempt ledger row (the ``on_attempt`` observer).

        A fallback chain used to leave ONE row for the final result only, so
        the lanes that died mid-chain (minimax silent death, codex rc=1 in
        run-1791619207-22518) existed nowhere but the live-stream files. This
        sink writes one failed row per dead lane, attributed to THAT lane, with
        the provider's own classified error. Cost and tokens are pinned to 0:
        the wrapper's final success/failure row carries the sidecar numbers,
        and ``cost_ledger.spent_last_24h`` sums every ``llm_calls.cost_usd`` —
        duplicating partial usage here would double-count spend against the
        daily budget. The ``metadata_json`` attempt marker lets a reader tell
        attempt rows from final rows. The winning attempt is not recorded (the
        success row below already names the serving lane)."""
        try:
            ok = bool(getattr(result, "ok", False))
            text = (getattr(result, "text", "") or "").strip()
            if ok and text:
                return
            err = redact_secrets((getattr(result, "error", "") or "empty output")[:200])
            category = classify_error(err, getattr(result, "rc", 1))
            write_llm_calls_row(
                db, provider_for_model(lane), lane, tier, feature, actor, "failed",
                int(getattr(result, "duration_ms", 0) or 0), "0", err,
                metadata_json=json.dumps({"dispatch_attempt": attempt, "chain": str(model)}),
                error_category=category, retryable=int(error_retryable(category)))
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
                write_llm_calls_row(
                    db, provider_for_model(str(model).split(",", 1)[0]), str(model),
                    tier, feature, actor, "failed",
                    int(time.time() * 1000) - start_ms, "0",
                    redact_secrets(f"dispatcher crashed: {exc}"[:200]),
                    error_category=classify_error(str(exc)),
                    retryable=int(error_retryable(classify_error(str(exc)))))
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
        fair_usage = glm_fair_usage_retryable(model, probe, attempt, max_attempts)
        throttled = throttle_retryable(model, probe, rc, attempt, max_attempts)
        # A wrong-SHAPE reply is a stochastic model-formatting miss, not a terminal
        # failure: the lane produced output, it was merely unusable. Retry it in
        # the same backoff loop (bounded by max_attempts) so a transient
        # non-JSON planner turn does not end the run. Scoped to nodes whose
        # artifact is known JSON (``accept_fn``), so prose nodes are unaffected.
        shape_reject = (accept_fn is not None and rc == SHAPE_REJECT_RC
                        and attempt < max_attempts)
        if fair_usage or throttled or shape_reject:
            sleep_s = backoff_seconds(attempt)
            if fair_usage:
                reason = "fair_usage"
            elif shape_reject and not throttled:
                reason = "shape_reject"
            else:
                reason = classify_error(probe, rc)
            sys.stderr.write(f"[llm_dispatch RETRY model={model} reason={reason} "
                             f"attempt={attempt}/{max_attempts} sleep={sleep_s}s]\n")
            time.sleep(sleep_s)
            attempt += 1
            continue
        break

    selected_model = str(model).split(",", 1)[0].strip()
    try:
        selected_model = open(out_file + ".model", encoding="utf-8").read().strip() or selected_model
    except OSError:
        pass
    provider = provider_for_model(selected_model)
    _write_lane_sidecar(selected_model)

    if rc == 0:
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
    err = ""
    try:
        err = (open(out_file + ".err.log", errors="ignore").read()[-200:])
    except OSError:
        pass
    # The claude-code wrapper hides the real provider rejection in a stdout
    # ``result`` object (``is_error: true``, ``api_error_status``, ``result``).
    # Surface it so ``error_message``/``error_category`` carry the provider's
    # own words (the MiniMax 429 quota text) instead of the CLI's stderr chatter.
    provider_err = _provider_error_from_result(out_file)
    if provider_err:
        err = provider_err
    err = redact_secrets(err)
    category = classify_error(err, rc)
    retryable = int(error_retryable(category))
    # A failed call still burned wall-clock, and partial provider usage often
    # survives in the sidecars — record both instead of hard zeros.
    cost_usd = "0"
    if os.path.isfile(out_file + ".cost"):
        cost_usd = open(out_file + ".cost").read().strip() or "0"
    in_tok, out_tok, cached_in, cache_create, thinking = _read_token_sidecar(
        out_file + ".tokens")
    write_llm_calls_row(db, provider, selected_model, tier, feature, actor, "failed",
                        duration_ms, cost_usd, err, in_tok, out_tok, "{}", cached_in, cache_create,
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


if __name__ == "__main__":
    raise SystemExit(llm_dispatch())
