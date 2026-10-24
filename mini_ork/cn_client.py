"""Python port of lib/cn_client.sh — ContextNest HTTP client (read + hook push).

Strangler-fig parity port. Same design rules as the bash: never block mini-ork
on CN being down (every call has a timeout + fallback to ``{}`` / ``""``), never
write memories (only fire-and-forget event/outcome posts), cite-tag every atom.
The render_* functions are transcribed verbatim from the bash's embedded python
so their markdown output byte-matches.

Two-tier configuration (``config/contextnest.yaml`` or
``config/contextnest.example.yaml``):

* ``simple``  — no auth header; the default. Tries CN; if it's down, fails soft.
* ``secure``  — requires a bearer token resolved from the operator's secret
  store (``mini_ork.dispatch.secrets.read_secret_exports``). A missing token
  fails CLOSED: ``SecretStoreError`` names the key, the request is never sent,
  and there is no silent fall-back to the simple tier.

Env (highest precedence, beats both config files): CN_BASE_URL, MO_DISABLE_CN,
CN_TIMEOUT_SEC, CN_HOOK_TIMEOUT_SEC, CN_PING_TTL, CN_PING_TIMEOUT_SEC,
CN_TOKEN_KEY, CN_TIER.

Wire contract: the ``/api/v1/tools/retrieve`` POST body uses ``top_k`` (the
server's name); the Python parameter stays ``limit`` so existing callers do
not change.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── tier / config loading (env > $MINI_ORK_HOME/config/contextnest.yaml >
# config/contextnest.example.yaml) ──────────────────────────────────────────

_DEFAULTS: dict = {
    "tier": "simple",
    "base_url": "http://127.0.0.1:28080",
    "token_key": "CN_TOKEN",
    "timeout_ping_s": 1.0,
    "timeout_read_s": 8.0,
    "cache_ttl_s": 300,
}
_VALID_TIERS = ("simple", "secure")


def _home_dir() -> Path:
    """The project home (where ``config/contextnest.yaml`` lives)."""
    return Path(os.environ.get("MINI_ORK_HOME") or ".mini-ork")


def _config_path() -> Path | None:
    """The real local override, or ``None`` when only the example exists."""
    return _home_dir() / "config" / "contextnest.yaml"


def _example_path() -> Path:
    """The committed default, walking up from the engine to ``config/``."""
    here = Path(__file__).resolve().parent
    # mini_ork/cn_client.py -> mini_ork/ -> <repo>/config/
    for p in (here.parent / "config" / "contextnest.example.yaml",
              here.parent.parent / "config" / "contextnest.example.yaml"):
        if p.is_file():
            return p
    return here.parent.parent / "config" / "contextnest.example.yaml"


def _load_yaml(path: Path) -> dict:
    """One YAML document, or ``{}`` on absence / parse error (fail soft on
    the config file — never on a missing key, which is what the secure tier
    raises from)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        import yaml  # type: ignore[import-untyped]
        doc = yaml.safe_load(text)
    except Exception:
        return {}
    return doc if isinstance(doc, dict) else {}


def _tier_config(*, _env: dict | None = None) -> dict:
    """Resolve the tier config in env > real > example order.

    ``_env`` is a test seam so unit tests can fake the environment without
    mutating ``os.environ``."""
    env = _env if _env is not None else dict(os.environ)

    cfg = dict(_DEFAULTS)
    real = _config_path()
    if real is not None and real.is_file():
        cfg.update(_load_yaml(real))
    else:
        cfg.update(_load_yaml(_example_path()))

    # env beats file
    if "CN_BASE_URL" in env:
        cfg["base_url"] = env["CN_BASE_URL"]
    if "CN_TOKEN_KEY" in env:
        cfg["token_key"] = env["CN_TOKEN_KEY"]
    if "CN_TIER" in env:
        cfg["tier"] = env["CN_TIER"]
    if "CN_PING_TIMEOUT_SEC" in env:
        cfg["timeout_ping_s"] = float(env["CN_PING_TIMEOUT_SEC"])
    if "CN_TIMEOUT_SEC" in env:
        cfg["timeout_read_s"] = float(env["CN_TIMEOUT_SEC"])
    if "CN_CACHE_TTL_SEC" in env:
        cfg["cache_ttl_s"] = int(env["CN_CACHE_TTL_SEC"])

    if cfg.get("tier") not in _VALID_TIERS:
        cfg["tier"] = "simple"
    return cfg


def _base() -> str:
    return _tier_config()["base_url"]


def _timeout() -> float:
    return _tier_config()["timeout_read_s"]


def _hook_timeout() -> float:
    return float(os.environ.get("CN_HOOK_TIMEOUT_SEC", "3"))


def _ping_timeout() -> float:
    """The availability probe has its OWN short timeout — independent of the
    read timeout, so a slow CN is reported as down rather than blocking the
    page render with the read timeout."""
    return _tier_config()["timeout_ping_s"]


def _ping_ttl() -> int:
    return int(os.environ.get("CN_PING_TTL", "30"))


def _disabled() -> bool:
    return os.environ.get("MO_DISABLE_CN", "0") == "1"


def _tier() -> str:
    return _tier_config()["tier"]


def _token_key() -> str:
    return _tier_config()["token_key"]


def _cache_ttl_s() -> int:
    return int(_tier_config()["cache_ttl_s"])


def _auth_headers() -> dict:
    """Build the request headers; secure tier adds ``Authorization: Bearer …``.

    The token is resolved lazily (every call), so a token that the operator
    adds to the secret store after the engine starts is picked up on the next
    request without restarting. A missing key in secure mode raises
    ``SecretStoreError`` naming the key (NEVER the value), which the page
    helpers render as a hard, key-naming error — never a silent fall-back to
    the simple tier."""
    headers: dict = {}
    if _tier() != "secure":
        return headers
    # Import here so the simple tier never touches the secret store (and a
    # missing secret store on a laptop does not break simple-tier users).
    from mini_ork.dispatch.secrets import read_secret_exports, secret_store_path
    key = _token_key()
    if not key:
        from mini_ork.dispatch.secrets import SecretStoreError
        raise SecretStoreError(
            "secure tier requires CN_TOKEN_KEY (or token_key in contextnest.yaml)"
        )
    exports = read_secret_exports(secret_store_path())
    if key not in exports or not exports[key]:
        from mini_ork.dispatch.secrets import SecretStoreError
        raise SecretStoreError(
            f"secret store is missing required key {key!r} for the secure CN tier"
        )
    headers["Authorization"] = f"Bearer {exports[key]}"
    return headers


def _cache_home() -> str:
    """The home a *cache* may write to, or ``""`` when there is none.

    ``MINI_ORK_HOME`` wins outright. Without it, honour ``./.mini-ork`` only
    when it already exists — a cache must never *create* a home, because
    ``makedirs`` on a relative default writes into whatever directory the
    process happens to sit in. That is how a ``.mini-ork`` appeared inside the
    shipped ``mini-ork IDE.app/Contents/MacOS`` and invalidated the bundle's
    code signature: an engine page rendered with the app bundle as its cwd.
    """
    home = os.environ.get("MINI_ORK_HOME")
    if home:
        return home
    return ".mini-ork" if os.path.isdir(".mini-ork") else ""


def _ping_cache_file() -> str:
    home = _cache_home()
    if not home:
        return ""
    d = os.path.join(home, "state")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return ""
    return os.path.join(d, "cn_ping.cache")


def _section_cache_dir() -> str:
    """Per-(section, scope) JSON files, one cache slot each.

    The directory is created on demand; cache misses are silent. A separate
    directory from the ping cache keeps a one-off render from clobbering the
    short-lived ping state."""
    home = _cache_home()
    if not home:
        return ""
    d = os.path.join(home, "cache", "cn")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return ""
    return d


def _section_cache_path(section: str, scope: str) -> str:
    directory = _section_cache_dir()
    if not directory:
        return ""
    safe = urllib.parse.quote(scope or "_root", safe="")
    return os.path.join(directory, f"{section}__{safe}.json")


def _get_text(path: str, *, timeout: float | None = None) -> str:
    # Resolve auth headers BEFORE the network try/except so a secure-tier
    # missing-token error escapes as ``SecretStoreError`` — swallowing it
    # into ``""`` would hide the config error behind a silent empty section.
    # The simple tier returns an empty dict from ``_auth_headers`` and never
    # touches the secret store, so the call stays free of side effects.
    headers = _auth_headers()
    try:
        req = urllib.request.Request(_base() + path, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout if timeout is not None else _timeout()) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _get(path: str, *, timeout: float | None = None) -> str:
    # Same auth-first contract as ``_get_text``: secure-tier misconfig must
    # surface as ``SecretStoreError`` (named key, never the value), not as
    # a ``"{}"`` fallback that papers over a missing bearer token.
    headers = _auth_headers()
    try:
        req = urllib.request.Request(_base() + path, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout if timeout is not None else _timeout()) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return "{}"


def _post_json(path: str, body: str, *, timeout: float | None = None) -> str:
    headers = {"Content-Type": "application/json"}
    try:
        headers.update(_auth_headers())
    except Exception:
        # A missing-key error from the secure tier must not be swallowed by
        # the page helpers — ``retrieve`` and ``cached_section`` consult the
        # raised ``SecretStoreError`` to surface the key name (never the
        # value). The read helpers (``_get_text`` / ``_get`` / ``available``)
        # resolve ``_auth_headers`` themselves so the raise propagates from
        # them too.
        raise
    try:
        req = urllib.request.Request(_base() + path, data=body.encode("utf-8"),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout if timeout is not None else _timeout()) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return "{}"


def available() -> bool:
    """0/True if CN reachable (cached for CN_PING_TTL secs)."""
    if _disabled():
        return False
    cache = _ping_cache_file()
    now = int(time.time())
    if cache and os.path.isfile(cache):
        try:
            ts, state = open(cache).read().split()[:2]
            if now - int(ts) < _ping_ttl():
                return state == "up"
        except Exception:
            pass
    code = "000"
    # The availability probe uses the short ping timeout, NOT the read
    # timeout — a slow CN must report down so the page renders the one-line
    # not-running message instead of blocking on the read budget.
    # Resolve the bearer token before opening the socket so a missing key on
    # the secure tier fails closed (SecretStoreError propagates) — a missing
    # token is a config error, NOT a "down" answer, and must never be papered
    # over with a cached ``False`` or a silent simple-tier fallback.
    headers = _auth_headers()
    try:
        req = urllib.request.Request(_base() + "/api/v1/substrate/health",
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=_ping_timeout()) as r:
            code = str(r.status)
    except Exception:
        code = "000"
    if cache:
        try:
            open(cache, "w").write(f"{now} {'up' if code == '200' else 'down'}\n")
        except OSError:
            pass
    return code == "200"


def _enc(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def capsule(query: str = "", since: str = "14d", project: str = "") -> str:
    if _disabled() or not available():
        return ""
    qs = f"since={since}"
    if query:
        qs += f"&query={_enc(query)}"
    if project:
        qs += f"&project={_enc(project)}"
    return _get_text(f"/api/v1/prompt-context/capsule?{qs}")


def retrieve(query: str, limit: int = 8) -> str:
    """POST /api/v1/tools/retrieve.

    The wire body uses ``top_k`` (the server reads that key — see
    ``src/api/tools.rs``). The Python parameter is kept as ``limit`` so every
    existing call site and test continues to work unchanged. A secure-tier
    request attaches the bearer token via ``_auth_headers``; if the token
    key is missing from the secret store, the ``_post_json`` call raises
    ``SecretStoreError`` (fail closed — never a silent simple-tier fallback).
    """
    if _disabled() or not available():
        return "{}"
    return _post_json("/api/v1/tools/retrieve",
                      json.dumps({"query": query, "top_k": int(limit)}))


def sessions_by_file(path: str) -> str:
    if _disabled() or not available():
        return "{}"
    return _get(f"/api/v1/sessions/by-file?path={_enc(path)}")


def sessions_by_feature(q: str) -> str:
    if _disabled() or not available():
        return "{}"
    return _get(f"/api/v1/sessions/by-feature?q={_enc(q)}")


def sessions_by_intent(q: str) -> str:
    if _disabled() or not available():
        return "{}"
    return _get(f"/api/v1/sessions/by-intent?q={_enc(q)}")


def inbox(limit: int = 10) -> str:
    if _disabled() or not available():
        return "{}"
    return _get(f"/api/v1/inbox?limit={limit}")


def features_recent(since: str = "24h", layer: str = "") -> str:
    if _disabled() or not available():
        return "{}"
    q = f"since={since}"
    if layer:
        q += f"&layer={layer}"
    return _get(f"/api/v1/features?{q}")


def basins(project: str = "", limit: int = 20) -> str:
    if _disabled() or not available():
        return "{}"
    q = f"limit={limit}"
    if project:
        q += f"&project={_enc(project)}"
    return _get(f"/api/v1/field/basins?{q}")


def graph_neighbors(node_id: str, limit: int = 8) -> str:
    if _disabled() or not available():
        return "{}"
    return _get(f"/api/v1/graph/neighbors?node_id={_enc(node_id)}&limit={limit}")


def graph_path(src: str, dst: str) -> str:
    if _disabled() or not available():
        return "{}"
    return _get(f"/api/v1/graph/path?from={_enc(src)}&to={_enc(dst)}")


def inbox_filtered(urgency: str = "", limit: int = 10) -> str:
    if _disabled() or not available():
        return "{}"
    q = f"limit={limit}"
    if urgency:
        q += f"&urgency={urgency}"
    return _get(f"/api/v1/inbox?{q}")


# ── section cache (one JSON file per (section, scope), short TTL) ───────────
#
# The IDE page calls into CN many times per render. The section cache is the
# "rendering still works while CN is slow or briefly down" mechanism: a
# successful render writes its payload + as_of timestamp; a render that hits
# a timeout, a connection error, or a missing-token error reads the cached
# payload and renders the "as of HH:MM" line. The TTL is short (default 5
# minutes) so an operator who fixes the server sees fresh data on the next
# page refresh without waiting out a long cache.


def _read_section_cache(section: str, scope: str) -> tuple[dict | None, int]:
    """Return ``(payload, age_seconds)`` for a cache slot, or ``(None, 0)``
    when the slot is absent or unreadable. ``age_seconds`` is the wall-clock
    age of the slot when readable, else ``0`` so the caller treats the slot
    as a fresh cache miss and does NOT render an as-of label."""
    path = _section_cache_path(section, scope)
    if not path or not os.path.isfile(path):
        return None, 0
    try:
        raw = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError):
        return None, 0
    if not isinstance(raw, dict):
        return None, 0
    ts = int(raw.get("_as_of", 0) or 0)
    if not ts:
        return None, 0
    return raw, int(time.time()) - ts


def _write_section_cache(section: str, scope: str, payload: dict) -> None:
    """Persist one cache slot. Failures are silent — the cache is best-effort
    graceful degradation, never a critical path."""
    path = _section_cache_path(section, scope)
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except OSError:
        pass


def cached_section(section: str, scope: str, fetch: callable) -> dict:
    """Run ``fetch()`` with graceful degradation.

    * ``fetch()`` returns a dict → write the cache slot, return the payload.
    * ``fetch()`` raises ``SecretStoreError`` (secure-tier misconfig) → the
      error is surfaced verbatim with the KEY name; cache is NOT consulted
      (a config error must be loud, not papered over with stale data).
    * ``fetch()`` raises anything else, or returns a non-dict / empty dict →
      fall back to the cached slot if it is younger than the TTL; if no
      cache or the cache is older, return ``{"_unreachable": True}`` so the
      page renders the one-line "ContextNest is not running" message.

    A successful call ALWAYS refreshes the cache, so the operator never sees
    a TTL-stale value on a healthy read.
    """
    if _disabled():
        return {"_disabled": True}
    try:
        payload = fetch()
    except Exception as exc:  # noqa: BLE001 — see the two clauses above
        from mini_ork.dispatch.secrets import SecretStoreError
        if isinstance(exc, SecretStoreError):
            # Re-raise with the key name; the page renders this as a hard
            # config error, never as a silent simple-tier fallback.
            raise
        payload, age = _read_section_cache(section, scope)
        if payload is not None and age < _cache_ttl_s():
            return dict(payload, _from_cache=True, _age_seconds=age)
        return {"_unreachable": True}
    if not isinstance(payload, dict) or not payload:
        cached, age = _read_section_cache(section, scope)
        if cached is not None and age < _cache_ttl_s():
            return dict(cached, _from_cache=True, _age_seconds=age)
        return {"_unreachable": True}
    out = dict(payload)
    out["_as_of"] = int(time.time())
    out["_from_cache"] = False
    _write_section_cache(section, scope, out)
    return out


# ── scoping helpers (project path filter, run-id extraction, recent sessions) ──


def _coerce_path(value) -> str:
    """A value is a project path when it is a non-empty string that looks
    like a path — starts with ``/`` (absolute) or contains a path separator.
    ``None``, ints, booleans, empty strings, single tokens, and bare names
    without separators are NOT project paths and must be dropped by the
    project filter."""
    if not isinstance(value, str):
        return ""
    s = value.strip()
    if not s:
        return ""
    if s.startswith("/") or "/" in s or "\\" in s:
        return s
    return ""


#: A project's linked worktrees live in a sibling of the checkout, named
#: ``<repo>-worktrees/<slug>/``. That is where this repo does all of its work,
#: so a memory recorded from a worktree names the *worktree* path (verified
#: live 2026-10-16: ``metadata.project_cwd`` =
#: ``/Volumes/docker-ssd/ps/mini-ork-worktrees/ide-node-stream``) and never the
#: checkout the caller asked about.
_WORKTREES_SUFFIX = "-worktrees"


def project_roots(project: str) -> tuple[str, ...]:
    """Every root that counts as ``project``: the checkout and its worktrees.

    ContextNest attributes a session to its launch directory, and here that is
    almost always a linked worktree, so a matcher that accepted only the
    checkout itself would drop every memory this repo produces — ``cn search``
    and ``cn seen-before`` both returned zero project-scoped hits against the
    live server for exactly that reason.

    The separator boundary is still the callers' job (they match ``root + "/"``),
    so widening the set cannot leak ``/ps/mini-ork2`` into ``/ps/mini-ork``.

    ``MINI_ORK_WORKTREES_DIR`` overrides the location. It is honoured only when
    it keeps the conventional per-repo shape (``<name>-worktrees``), so pointing
    one shared directory at several repos' worktrees cannot widen the scope.
    """
    norm = (project or "").rstrip("/")
    if not norm:
        return ()
    roots = [norm]
    wanted = norm + _WORKTREES_SUFFIX
    for candidate in (wanted, os.environ.get("MINI_ORK_WORKTREES_DIR", "")):
        cand = (candidate or "").rstrip("/")
        if not cand or cand in roots:
            continue
        if os.path.basename(cand) == os.path.basename(norm) + _WORKTREES_SUFFIX:
            roots.append(cand)
    return tuple(roots)


def filter_by_project_path(items, project_path: str) -> list:
    """Drop items that carry no path inside ``project_path``.

    ``items`` is an iterable of dicts. An item is kept when any of the
    string fields (``path``, ``file``, ``cwd``, ``project_cwd``, ``uri``)
    starts with one of :func:`project_roots` — the checkout, or a worktree of
    it (``project_path`` is normalised so a trailing slash does not silently
    reject ``/ps/mini-ork/foo`` when the caller asked for ``/ps/mini-ork``).

    Items without any recognisable path field are dropped — the run-id
    extraction then has nothing to misread, and a foreign project leak is
    impossible by construction.
    """
    if not project_path:
        return list(items) if items else []
    prefixes = tuple(r + "/" for r in project_roots(project_path))
    keys = ("path", "file", "cwd", "project_cwd", "uri")
    out: list = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for k in keys:
            v = item.get(k)
            if isinstance(v, str) and v.startswith(prefixes):
                out.append(item)
                break
    return out


_RUN_ID_KEYS = ("run_id", "run", "task_run_id", "trace_id")


def extract_run_id(item) -> str:
    """Best-effort run-id from an item, or ``""`` when no field matches.

    Only the canonical ``run-<digits>-<short>`` shape is accepted; anything
    that does not byte-match is rejected so the rest of the page does not
    build a hyperlink to a non-existent run.
    """
    if not isinstance(item, dict):
        return ""
    import re as _re
    pattern = _re.compile(r"^run-\d{6,}-[A-Za-z0-9]{4,}$")
    for k in _RUN_ID_KEYS:
        v = item.get(k)
        if isinstance(v, str) and pattern.match(v):
            return v
    # ``id`` last — too noisy to be a primary lookup, but some payloads put
    # the run id there too. Same shape rule applies.
    v = item.get("id")
    if isinstance(v, str) and pattern.match(v):
        return v
    return ""


def sessions_by_file_recent(path: str, project_path: str = "",
                            limit: int = 5) -> list:
    """Recent sessions for ``path``, scoped to ``project_path`` when set.

    The raw payload is sorted newest-first by ``last_seen`` (falling back to
    ``started_at`` / ``created_at``), the project filter is applied, and at
    most ``limit`` items are returned as plain dicts. The string the
    underlying HTTP call returns is parsed defensively: a non-JSON or empty
    payload is treated as "no data", not as an error — the caller's
    ``cached_section`` already arbitrates between the live read and the
    cache slot."""
    if not path or _disabled() or not available():
        return []
    raw = _get(f"/api/v1/sessions/by-file?path={_enc(path)}")
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    items = []
    if isinstance(data, dict):
        items = data.get("items") or data.get("sessions") or []
    elif isinstance(data, list):
        items = data
    if project_path:
        items = filter_by_project_path(items, project_path)

    def _key(it):
        for k in ("last_seen", "started_at", "created_at", "ts"):
            v = it.get(k) if isinstance(it, dict) else None
            if isinstance(v, (int, float)):
                return -float(v)
        return 0

    items = sorted([it for it in items if isinstance(it, dict)],
                   key=_key)
    return items[: int(limit)]


def _fire(path: str, body: str) -> None:
    def _go():
        try:
            req = urllib.request.Request(_base() + path, data=body.encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=_hook_timeout()).read()
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()


def hook_post(event: str, session_id: str, cwd: str | None = None, transcript: str = "") -> int:
    if _disabled() or not available():
        return 0
    cwd = os.environ.get("PWD", "") if cwd is None else cwd
    p = {"session_id": session_id, "hook_event_name": event}
    if cwd:
        p["cwd"] = cwd
    if transcript:
        p["transcript_path"] = transcript
    _fire(f"/api/v1/cc/hook/{event}", json.dumps(p))
    return 0


def outcome_post(outcome: str, atom_ids_csv: str = "", evidence: str = "", session_id: str = "") -> int:
    if _disabled() or not atom_ids_csv:
        return 0
    ids = [s.strip() for s in atom_ids_csv.split(",") if s.strip()]
    if not ids or not available():
        return 0
    p = {"atom_ids": ids, "outcome": outcome}
    if evidence:
        p["evidence"] = evidence
    if session_id:
        p["session_id"] = session_id
    _fire("/api/v1/agent/outcome", json.dumps(p))
    return 0


# ── synchronous outcome settle (run-verdict path) ─────────────────────────────
#
# The hook's outcome_post is fire-and-forget on a daemon thread and discards
# the response. The run-verdict settle path needs the opposite: a single
# POST whose response body (``{updated, skipped_unknown, delta_applied}``) is
# inspected so a settle that landed at CN can be recorded as such, and a
# settle that CN refused (timeout, 5xx, missing key) is recorded as pending
# instead of being lost. CN-down must NOT raise — the verdict bytes are the
# cost of truth and the settle is audit metadata.
#
# ``_settle_outcome`` returns one of:
#
#   {"status": "ok",      "updated": int, "skipped_unknown": int,
#                           "delta_applied": int}
#   {"status": "pending",  "reason": str}   # CN disabled / unavailable / error
#   {"status": "skipped",  "reason": str}   # no atom_ids to settle
#
# The hook path is unchanged (``outcome_post`` keeps its fire-and-forget
# semantics so the subagent transcript post is not coupled to a synchronous
# outcome call).


def _settle_outcome(outcome: str, atom_ids: list[str], *, evidence: str = "",
                    session_id: str = "") -> dict:
    """Synchronous outcome POST for the run-verdict settle path.

    Returns the parsed JSON body when CN accepts the request, or a
    ``{"status": "pending", ...}`` dict when CN is disabled / unavailable /
    errors out. Empty / missing atom_ids short-circuits to
    ``{"status": "skipped", ...}`` without an HTTP call.
    """
    if _disabled():
        return {"status": "pending", "reason": "cn_disabled"}
    ids = [s for s in (a.strip() for a in atom_ids) if s]
    if not ids:
        return {"status": "skipped", "reason": "no_atom_ids"}
    if not available():
        return {"status": "pending", "reason": "cn_unavailable"}
    body = {"atom_ids": ids, "outcome": outcome}
    if evidence:
        body["evidence"] = evidence[:480]
    if session_id:
        body["session_id"] = session_id
    # Same auth-first contract as ``_post_json``: a secure-tier misconfig
    # surfaces as ``SecretStoreError`` (named key, never the value). The
    # settle path catches that and records it as pending so the verdict
    # bytes stay untouched and the operator sees the missing-key signal.
    try:
        raw = _post_json("/api/v1/agent/outcome", json.dumps(body))
    except Exception as exc:
        return {"status": "pending", "reason": f"send_error:{type(exc).__name__}"}
    try:
        parsed = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        return {"status": "pending", "reason": "bad_response_body"}
    if not isinstance(parsed, dict):
        return {"status": "pending", "reason": "bad_response_shape"}
    # ``updated`` is the contract the live CN serves (2026-10-15 shape
    # {updated, skipped_unknown, delta_applied}); fall back to None when
    # the body omits it so the caller can still record "ok" against an
    # older server.
    return {
        "status": "ok",
        "updated": parsed.get("updated"),
        "skipped_unknown": parsed.get("skipped_unknown"),
        "delta_applied": parsed.get("delta_applied"),
    }


def settle_outcome_batch(outcome: str, atom_ids: list[str], *,
                          evidence: str = "", session_id: str = "") -> dict:
    """Settle one outcome per atom (one CN call covers the whole batch).

    Used by the run-verdict settle: every atom the run was given gets the
    SAME outcome string (``"success"`` for a published run, ``"failure"``
    for a rejected / rolled-back run), so the batch endpoint is exactly
    what the substrate wants — and one POST is cheaper than N. The return
    shape mirrors :func:`_settle_outcome` so the caller can record the
    batch result against every atom in the run-dir record.
    """
    return _settle_outcome(outcome, list(atom_ids), evidence=evidence,
                           session_id=session_id)


# ── graph projection (PR-6): mini-ork learning entities → CN's durable graph ──
#
# The server whitelists node labels and edge triples (anything else is a 400)
# and matches an edge endpoint against node ids, so the id convention and the
# triple table live here rather than being re-spelled at each call site.

_GRAPH_CHUNK_MAX = 500
_GRAPH_EDGE_TRIPLES = frozenset({
    ("HAS_TRACE", "Run", "Trace"),
    ("LINKED_TO", "Trace", "GradientTarget"),
    ("OF_CLASS", "Trace", "TaskClass"),
    ("OF_CLASS", "GradientTarget", "TaskClass"),
})


def _graph_ids(trace_id=None, task_class=None, gradient_id=None, run_id=None) -> dict:
    """Build the ``{label: node}`` map for one projection, dropping empty keys.

    Ids are the raw keys — no ``run:``/``trace:`` prefix — and ``TaskClass``
    carries the bare class name because that is what the server stores in
    ``name``. An empty/NULL key yields no node: an empty-string id would create
    a junk node that every read route then returns.

    An edge endpoint that does not byte-equal a node id silently matches
    nothing server-side (200 plus a plausible-looking count), so callers must
    build edges with :func:`_graph_edge` from this same map.
    """
    out = {}
    for label, key in (("Run", run_id), ("Trace", trace_id),
                       ("GradientTarget", gradient_id), ("TaskClass", task_class)):
        if key is None or not str(key):
            continue
        out[label] = {"id": str(key), "label": label, "props": {}}
    return out


def _graph_edge(edge_type: str, from_label: str, to_label: str, nodes: dict,
                props: dict | None = None) -> dict:
    """Build an edge whose endpoints are the ids of `nodes` — never a hand-built
    string, which is how a projection silently loses edges."""
    if (edge_type, from_label, to_label) not in _GRAPH_EDGE_TRIPLES:
        raise ValueError(
            f"graph edge triple not whitelisted: {edge_type}/{from_label}/{to_label}"
        )
    return {"from": nodes[from_label]["id"], "from_label": from_label,
            "type": edge_type, "to": nodes[to_label]["id"],
            "to_label": to_label, "props": props or {}}


def graph_upsert(nodes: list, edges: list, source: str = "mini-ork") -> int:
    """Project learning entities into ContextNest's graph. Best-effort: returns 0
    and does nothing when CN is disabled or unreachable."""
    if _disabled() or not available():
        return 0
    if not nodes and not edges:
        return 0
    _fire("/api/v1/graph/upsert", json.dumps({"nodes": nodes, "edges": edges,
                                              "source": source}))
    return 0


def graph_upsert_batched(nodes: list, edges: list, source: str = "mini-ork") -> int:
    """Chunked :func:`graph_upsert`; returns the number of requests fired.

    The server 413s above 2000 combined items and a full ``failure_links``
    backfill is 2023 rows, so a single request would be rejected. Slicing on the
    COMBINED count matters: 500 nodes plus 500 edges is 1000 items, not two
    chunks.
    """
    if _disabled() or not available():
        return 0
    sent = 0
    n_i = e_i = 0
    while n_i < len(nodes) or e_i < len(edges):
        room = _GRAPH_CHUNK_MAX
        chunk_nodes = nodes[n_i:n_i + room]
        room -= len(chunk_nodes)
        chunk_edges = edges[e_i:e_i + room]
        n_i += len(chunk_nodes)
        e_i += len(chunk_edges)
        graph_upsert(chunk_nodes, chunk_edges, source)
        sent += 1
    return sent


# --- render_* : transcribed verbatim from the bash's embedded python ---

def render_atoms_md(payload: str, limit: int = 5) -> str:
    try:
        data = json.loads(payload)
    except Exception:
        return ""
    hits = data.get("hits") or []
    if not hits:
        return ""
    hits = hits[:int(limit)]
    out = ["--- ContextNest atoms (fresh substrate retrieval) ---",
           "Cross-session memory the planner should weigh before deciding:"]
    for h in hits:
        sim = h.get("similarity", 0)
        meta = h.get("metadata") or {}
        kind = meta.get("kind", "atom")
        ts = (meta.get("ts") or "")[:10]
        sid = h.get("session_id") or h.get("id", "")
        content = (h.get("content") or "").strip().replace("\n", " ")
        if len(content) > 280:
            content = content[:277] + "..."
        aid = h.get("id")
        id_tok = f" id={aid}" if isinstance(aid, str) and aid.startswith("cn-") else ""
        out.append(f"- [{kind} sim={sim:.2f} {ts} sess={sid[:8]}{id_tok}] {content}")
    out.append("--- /ContextNest atoms ---")
    return "\n".join(out) + "\n"


def render_features_md(payload: str, cwd: str = "", limit: int = 6) -> str:
    try:
        data = json.loads(payload)
    except Exception:
        return ""
    features = data.get("features") or data.get("items") or []
    if not features:
        return ""
    out = ["--- ContextNest features delivered recently ---"]
    for f in features[:int(limit)]:
        name = (f.get("feature") or f.get("name") or "").strip()
        layer = f.get("layer", "?")
        htt = (f.get("how_to_test") or "").strip()
        pcwd = (f.get("project_cwd") or "")
        here = " [this project]" if cwd and pcwd and (cwd in pcwd or pcwd in cwd) else ""
        out.append(f"- ({layer}){here} {name}")
        if htt:
            out.append(f"  test: {htt[:160]}")
    out.append("--- /ContextNest features ---")
    return "\n".join(out) + "\n"


def render_inbox_md(payload: str, limit: int = 5) -> str:
    try:
        d = json.loads(payload)
    except Exception:
        return ""
    items = d.get("items") or d.get("inbox") or []
    if not items:
        return ""
    out = ["--- ContextNest attention inbox ---"]
    for it in items[:int(limit)]:
        kind = it.get("kind", "?")
        sid = (it.get("session_id") or it.get("id", ""))[:8]
        text = (it.get("content") or it.get("subject") or it.get("action") or "").strip().replace("\n", " ")
        if len(text) > 160:
            text = text[:157] + "..."
        out.append(f"- [{kind} {sid}] {text}")
    out.append("--- /ContextNest attention inbox ---")
    return "\n".join(out) + "\n"


def render_basins_md(payload: str, limit: int = 5) -> str:
    try:
        d = json.loads(payload)
    except Exception:
        return ""
    bs = d.get("basins") or d.get("items") or []
    if not bs:
        return ""
    out = ["--- ContextNest topic clusters (basins) ---"]
    for b in bs[:int(limit)]:
        bid = (b.get("basin_id") or b.get("id", ""))[:8]
        mass = b.get("active_mass") or b.get("mass") or b.get("size") or 0
        rep = (b.get("representative") or b.get("centroid_text") or "").strip().replace("\n", " ")
        if len(rep) > 160:
            rep = rep[:157] + "..."
        out.append(f"- [{bid} mass={mass}] {rep}")
    out.append("--- /ContextNest topic clusters ---")
    return "\n".join(out) + "\n"


def render_graph_neighbors_md(payload: str, limit: int = 5) -> str:
    try:
        d = json.loads(payload)
    except Exception:
        return ""
    ns = d.get("neighbors") or []
    if not ns:
        return ""
    out = ["--- ContextNest graph — neighbours of the top retrieved memory ---"]
    for n in ns[:int(limit)]:
        nid = (n.get("id") or "")[:8]
        try:
            w = float(n.get("weight") or 0.0)
        except (TypeError, ValueError):
            w = 0.0
        out.append(f"- {nid} w={w:.2f}")
    out.append("--- /graph neighbours ---")
    return "\n".join(out) + "\n"


def render_graph_path_md(payload: str, limit: int = 3) -> str:
    try:
        d = json.loads(payload)
    except Exception:
        return ""
    if not d.get("found"):
        return ""
    nodes = [n for n in (d.get("nodes") or []) if n]
    if not nodes:
        return ""
    # `limit` decides whether the intermediate hops are worth spelling out — it
    # must NOT be applied before picking the endpoints, or a path longer than
    # the limit would print a middle hop as the destination. Truncating to the
    # endpoint pair keeps both ends truthful.
    if len(nodes) > int(limit):
        nodes = [nodes[0], nodes[-1]]
    hops = d.get("hops") or 0
    try:
        total_weight = float(d.get("total_weight") or 0.0)
    except (TypeError, ValueError):
        total_weight = 0.0
    algorithm = d.get("algorithm") or "?"
    chain = " -> ".join(n[:8] for n in nodes)
    out = ["--- ContextNest graph — how the top two memories connect ---",
           f"- {chain} ({hops} hops, w={total_weight:.2f}, {algorithm})",
           "--- /graph path ---"]
    return "\n".join(out) + "\n"


# ── Concord P0 coord helpers (raise-based; the opposite of the fail-soft helpers above) ──
#
# The fail-soft helpers above return {} / "" when ContextNest is down so
# mini-ork never blocks on CN. The `mini-ork concord` CLI needs the opposite:
# it maps a connection error to exit code 3 and an HTTP 4xx/5xx to exit code 4,
# so these helpers raise instead of falling back. They deliberately do NOT
# consult `available()` (concord run must fail open and keep retrying the
# upsert on each heartbeat even when the ping cache says "down") and do NOT
# route through `_fire` (which would swallow the very errors the CLI needs).


class CoordUnavailable(RuntimeError):
    """ContextNest is unreachable (connection refused, DNS, or a timeout)."""


class CoordConfigError(CoordUnavailable):
    """The secure tier's bearer token could not be resolved.

    A ``CoordUnavailable`` so every caller that already degrades on an
    unreachable ContextNest (the CLI's exit 3, ``concord run`` failing open)
    degrades here too. The message names the missing key, never its value.
    """


class CoordHTTPError(RuntimeError):
    """ContextNest answered with a 4xx/5xx status."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"ContextNest returned {status}")
        self.status = status
        self.body = body


def _coord_timeout() -> float:
    return float(os.environ.get("CN_COORD_TIMEOUT_SEC", "3"))


def _coord_request(method: str, path: str, body: dict | None = None, *,
                   timeout: float | None = None) -> dict:
    """One raise-based send path for the coord helpers.

    Unlike ``_get``/``_post_json`` this raises instead of returning a fallback,
    so the CLI can map failures to exit codes. Returns the parsed JSON body
    (``{}`` on an empty or undecodable body). ``timeout`` overrides
    ``CN_COORD_TIMEOUT_SEC`` for one call, so an IDE page can bound its read
    without changing the environment a long-lived ``serve`` shares.
    """
    try:
        # The coord routes sit behind the same bearer as every other read: a
        # secure-tier deployment that gates /api/v1 would refuse them otherwise.
        headers: dict = _auth_headers()
    except Exception as exc:  # noqa: BLE001 — only SecretStoreError is converted
        from mini_ork.dispatch.secrets import SecretStoreError
        if isinstance(exc, SecretStoreError):
            raise CoordConfigError(str(exc)) from exc
        raise
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(_base() + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout or _coord_timeout()) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", "replace")
        except Exception:
            err_body = ""
        raise CoordHTTPError(exc.code, err_body) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise CoordUnavailable(str(exc)) from exc
    try:
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def coord_upsert_principal(principal_id: str, fields: dict) -> dict:
    """PUT /api/v1/coord/principals/{id} — register (and heartbeat) a principal."""
    return _coord_request("PUT", f"/api/v1/coord/principals/{_enc(principal_id)}", fields)


def coord_list_principals(status: str = "active") -> dict:
    """GET /api/v1/coord/principals?status=active|all — newest last_seen first."""
    return _coord_request("GET", f"/api/v1/coord/principals?status={_enc(status)}")


def coord_get_principal(principal_id: str) -> dict:
    """GET /api/v1/coord/principals/{id} — a single principal."""
    return _coord_request("GET", f"/api/v1/coord/principals/{_enc(principal_id)}")


def coord_end_principal(principal_id: str) -> dict:
    """DELETE /api/v1/coord/principals/{id} — mark the principal ended."""
    return _coord_request("DELETE", f"/api/v1/coord/principals/{_enc(principal_id)}")


def coord_send(principal_id: str, sender: str, body: str) -> dict:
    """POST /api/v1/coord/principals/{id}/messages — send a message to a principal."""
    return _coord_request(
        "POST", f"/api/v1/coord/principals/{_enc(principal_id)}/messages",
        {"from": sender, "body": body},
    )


def coord_inbox(principal_id: str, unacked: bool = True, *,
                timeout: float | None = None) -> dict:
    """GET /api/v1/coord/principals/{id}/messages?unacked=true|false.

    ``unacked=False`` returns every message, acked ones included, oldest first.
    """
    flag = "true" if unacked else "false"
    return _coord_request(
        "GET", f"/api/v1/coord/principals/{_enc(principal_id)}/messages?unacked={flag}",
        timeout=timeout,
    )


def coord_outbox(sender: str, limit: int = 200, *, timeout: float | None = None) -> dict:
    """GET /api/v1/coord/messages?from=<sender> — what ``sender`` sent, oldest first.

    Each message's ``principal_id`` is its recipient. A ContextNest that
    predates the route answers 404, raised as ``CoordHTTPError``.
    """
    return _coord_request(
        "GET", f"/api/v1/coord/messages?from={_enc(sender)}&limit={int(limit)}",
        timeout=timeout,
    )


def coord_hot_claims() -> dict:
    """GET /api/v1/coord/hot-claims — live activity-derived claims on hot files."""
    return _coord_request("GET", "/api/v1/coord/hot-claims")


def coord_owns_violations(since: int = 0) -> dict:
    """GET /api/v1/coord/owns-violations?since=<seq> — recorded --owns scope violations."""
    return _coord_request("GET", f"/api/v1/coord/owns-violations?since={int(since)}")


def coord_ack(principal_id: str, msg_id: str, by: str) -> dict:
    """POST /api/v1/coord/principals/{id}/messages/{msg_id}/ack — acknowledge."""
    return _coord_request(
        "POST", f"/api/v1/coord/principals/{_enc(principal_id)}/messages/{_enc(msg_id)}/ack",
        {"by": by},
    )
