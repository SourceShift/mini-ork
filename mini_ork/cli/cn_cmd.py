"""``mini-ork cn`` — the operator-facing ContextNest verbs.

``cn status`` answers "is it on, where, and how fast?" every refresh. The
ContextNest IDE panels ask the same question and share the same fields. The
intentionally never reveals the bearer token; the secure-tier key is named
so the operator knows which one to export.

On the secure tier a missing token is a CONFIG error, not a "down" answer:
the command exits non-zero, names the missing KEY (never the value) on
stderr, and refuses to probe the server. Scripts and CI can therefore
distinguish "fix the secret store" (exit 5) from "server is down" (exit 0).

``cn ack-overlap <id>`` posts to ``/api/v1/coord/overlaps/<id>/ack`` through
the raise-based :func:`mini_ork.cn_client._coord_request` seam. It maps a
``CoordUnavailable`` to exit 3 and a ``CoordHTTPError`` to exit 4, so the
IDE panel can render a precise failure next to the Ack button rather than a
generic stderr trace.

``cn search <mode> <query>`` writes a cache slot the Search tab on the
Context IDE page reads verbatim — never re-hitting CN on the render path.
Four modes (``memories``/``sessions``/``features``/``files``) compose the
existing :mod:`mini_ork.cn_client` seams (``retrieve``,
``sessions_by_feature``, ``sessions_by_file``, ``features_recent``, plus
``filter_by_project_path``); project scoping is **off by default** with an
explicit ``--all-projects`` opt-in. The ``memories`` mode raises the read
budget via ``CN_TIMEOUT_SEC`` because ``/tools/retrieve`` takes ~20 s and
the render-path budget is 8 s.

Wire: ``GET /api/v1/substrate/health`` (ping),
``POST /api/v1/tools/retrieve`` (memories), ``GET /api/v1/sessions/by-file``
and ``/api/v1/sessions/by-feature`` and ``/api/v1/sessions/:id/summary``
(files), ``GET /api/v1/features?project=...`` (features). Exit codes: 0 on
a successful search write; 2 on a usage error; 3 when ContextNest is
unreachable; 4 on an HTTP 4xx/5xx; 5 on a secure-tier secret-store
misconfiguration.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from mini_ork import cn_client
from mini_ork.dispatch.secrets import SecretStoreError

# Exit codes — kept small and named for the caller, not the implementation.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_COORD_UNREACHABLE = 3
EXIT_COORD_HTTP = 4
EXIT_SECRET_CONFIG = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-ork cn",
        description="ContextNest client status",
        add_help=True,
    )
    sub = parser.add_subparsers(dest="subcommand")
    status = sub.add_parser("status", help="show tier, endpoint, reachability, latency")
    status.add_argument("--json", action="store_true",
                        help="emit JSON instead of a one-line text")
    ack = sub.add_parser("ack-overlap",
                         help="POST /api/v1/coord/overlaps/<id>/ack")
    ack.add_argument("overlap_id",
                     help="the overlap id returned by /api/v1/coord/overlaps")
    ack.add_argument("--by", default=None,
                     help="principal acknowledging; defaults to $CONCORD_PRINCIPAL "
                          "or human:$USER")
    ack.add_argument("--json", action="store_true",
                     help="emit the server response as JSON")
    search = sub.add_parser(
        "search",
        help="search ContextNest (memories|sessions|features|files) and "
             "write the cache slot the Search IDE tab reads",
    )
    search.add_argument(
        "mode",
        choices=("memories", "sessions", "features", "files"),
        help="what to search: memories (semantic), sessions (by feature), "
             "features (project-scoped), or files (by path)",
    )
    search.add_argument(
        "query",
        help="the search query: free text for memories/sessions/features; "
             "a path or partial path for files",
    )
    search.add_argument(
        "--all-projects", action="store_true",
        help="include other projects' rows; default is this-project only",
    )
    search.add_argument(
        "--limit", type=int, default=20,
        help="max rows to keep per mode (default 20)",
    )
    search.add_argument(
        "--json", action="store_true",
        help="emit the result envelope as JSON instead of writing the "
             "cache slot",
    )
    return parser


def _probe(base: str, timeout: float, headers: dict | None = None) -> tuple[bool, float, int]:
    """One availability probe with the short ping timeout.

    Returns ``(reachable, latency_seconds, http_status)``. ``http_status`` is
    the wire status code, or ``0`` on a connection / timeout error. The
    probe never raises — ``cn status`` returns 0 whether the server is up or
    not. ``headers`` is attached to the request so the secure-tier probe
    sends the bearer token; the simple tier passes ``None`` and gets a bare
    GET.
    """
    url = f"{base.rstrip('/')}/api/v1/substrate/health"
    start = time.monotonic()
    if headers:
        req = urllib.request.Request(url, headers=headers)
    else:
        req = url
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            latency = time.monotonic() - start
            return True, latency, int(r.status)
    except urllib.error.HTTPError as exc:
        latency = time.monotonic() - start
        return False, latency, int(exc.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        latency = time.monotonic() - start
        return False, latency, 0


def _report_secret_error(exc: SecretStoreError, *, token_key: str,
                         base: str, tier: str, as_json: bool) -> int:
    """Surface a secure-tier missing-token error on stderr (key name only,
    never the value), emit a structured payload on stdout for ``--json``,
    and return a non-zero exit code. The token value is never read here
    because ``SecretStoreError`` only carries the key name."""
    # Use the resolved key name when the exception message is too generic;
    # the operator needs to know which ``export`` to add to their store.
    key = token_key or "<unset>"
    msg = f"mini-ork cn: secure tier secret store is missing key {key!r}: {exc}"
    sys.stderr.write(msg + "\n")
    if as_json:
        # Structured payload on stdout so ``--json`` callers can still parse
        # the diagnostic programmatically. ``token_key`` is the only field
        # from the misconfigured state — no value is ever present.
        payload = {
            "tier": tier,
            "endpoint": base,
            "token_key": token_key or None,
            "reachable": False,
            "latency_ms": 0,
            "http_status": 0,
            "disabled": False,
            "error": "secret_store_missing_key",
            "error_key": key,
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return EXIT_SECRET_CONFIG


def _ack_default_by() -> str:
    """The default ``--by`` for ``cn ack-overlap``: $CONCORD_PRINCIPAL else human:$USER."""
    return os.environ.get("CONCORD_PRINCIPAL") or f"human:{os.environ.get('USER', 'unknown')}"


def _cmd_ack_overlap(args: argparse.Namespace) -> int:
    """POST ``/api/v1/coord/overlaps/<id>/ack`` through the raise-based seam.

    :func:`mini_ork.cn_client._coord_request` raises ``CoordUnavailable`` on a
    connection error (mapped to :data:`EXIT_COORD_UNREACHABLE`, exit 3) and
    ``CoordHTTPError`` on a 4xx/5xx (:data:`EXIT_COORD_HTTP`, exit 4). On a
    successful ack the parsed JSON body is echoed on stdout, either as JSON
    with ``--json`` or as a one-line ``ack-overlap: <id> acknowledged`` text.
    The id is URL-escaped through :func:`mini_ork.cn_client._enc` so an overlap
    id carrying ``/`` or another reserved character does not break the path.
    """
    by = args.by if args.by else _ack_default_by()
    try:
        payload = cn_client._coord_request(
            "POST",
            f"/api/v1/coord/overlaps/{cn_client._enc(args.overlap_id)}/ack",
            {"by": by},
        )
    except cn_client.CoordUnavailable as exc:
        sys.stderr.write(
            f"mini-ork cn ack-overlap: ContextNest unavailable: {exc}\n"
        )
        return EXIT_COORD_UNREACHABLE
    except cn_client.CoordHTTPError as exc:
        sys.stderr.write(
            f"mini-ork cn ack-overlap: ContextNest returned "
            f"{exc.status}: {exc.body}\n"
        )
        return EXIT_COORD_HTTP
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    else:
        overlap_id = (
            payload.get("id") if isinstance(payload, dict) else None
        ) or args.overlap_id
        sys.stdout.write(f"ack-overlap: {overlap_id} acknowledged\n")
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = cn_client._tier_config()
    base = cfg["base_url"]
    tier = cfg["tier"]
    token_key = cfg["token_key"]
    timeout_ping = float(cfg["timeout_ping_s"])

    if os.environ.get("MO_DISABLE_CN", "0") == "1":
        payload = {
            "tier": tier,
            "endpoint": base,
            "token_key": token_key if tier == "secure" else None,
            "reachable": False,
            "latency_ms": 0,
            "http_status": 0,
            "disabled": True,
            "note": "MO_DISABLE_CN=1",
        }
        if args.json:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        else:
            sys.stdout.write(f"cn: {tier} · {base} · disabled (MO_DISABLE_CN=1)\n")
        return EXIT_OK

    # Resolve the bearer token BEFORE the network probe so a secure-tier
    # misconfiguration fails closed here too — a missing key is a config
    # error, not a "server is down" answer, and ``cn status`` is the one
    # place an operator runs first when something looks wrong. The
    # ``SecretStoreError`` carries the KEY name only, never the value.
    try:
        headers = cn_client._auth_headers()
    except SecretStoreError as exc:
        return _report_secret_error(exc, token_key=token_key, base=base,
                                    tier=tier, as_json=args.json)

    reachable, latency, http_status = _probe(base, timeout_ping, headers=headers or None)
    payload = {
        "tier": tier,
        "endpoint": base,
        "token_key": token_key if tier == "secure" else None,
        "reachable": reachable,
        "latency_ms": int(round(latency * 1000)),
        "http_status": http_status,
        "disabled": False,
    }
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    else:
        tag = "reachable" if reachable else "not reachable"
        sys.stdout.write(
            f"cn: {tier} · {base} · {tag} · {payload['latency_ms']} ms "
            f"(http {http_status})\n"
        )
    return EXIT_OK


# ── search verb ────────────────────────────────────────────────────────────
#
# ``cn search <mode> <query>`` resolves a project, calls one matching
# :mod:`cn_client` seam, applies the this-project scope client-side (or
# passes it to the endpoint URL when the endpoint honours it), and writes
# the result envelope to the cache slot the Search IDE tab reads verbatim.
# The page renders from the slot — it never re-hits CN — so the only HTTP
# traffic this command produces is the single mode-appropriate GET / POST.
#
# Each mode honours the verified wire shape on 2026-10-15:
#  - retrieve            → {hits: [{id, content, importance, similarity,
#                                   metadata{}, session_id}]}
#  - sessions_by_feature → {query, hits: [{session_id, feature, ts,
#                                          files[], refs[], layer}]}
#  - sessions_by_file    → {query, matches: [{session_id, matched_files[],
#                                            total_files}]}  (no timestamp)
#  - features_recent     → {count, since, layer,
#                           features: [{feature, files[], how_to_test,
#                                       layer, refs[], defs[], session_id,
#                                       ts}]}                   (no session filter)

_SEARCH_CACHE_FILE = "context.search__latest.json"
_SEARCH_DEFAULT_LIMIT = 20
_SEARCH_MAX_LIMIT = 100
_SEARCH_FEATURES_SINCE = "30d"
_VALID_MODES = ("memories", "sessions", "features", "files")


def _project_root() -> str:
    """The git toplevel of cwd, then ``MINI_ORK_ROOT``, then ``""``.

    The CLI lives outside an IDE process, so it walks ``git rev-parse``
    on every invocation (cheap, sub-second); the page-side reads
    ``home.parent`` so the two sides agree on whose CN rows are
    in-scope. A read failure (no git, no env) returns ``""`` and the
    verb falls back to unfiltered rows — the explicit ``--all-projects``
    flag is the safe opt-in to that unfiltered state.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        if out:
            return out
    except Exception:  # noqa: BLE001 — git missing or hung; fall through
        pass
    return os.environ.get("MINI_ORK_ROOT", "")


def _search_cache_path() -> str:
    """The same slot path :func:`cn_client._section_cache_dir` writes.

    The verb reaches into ``cn_client``'s directory so the page's own
    ``_section_cache_dir()`` resolves to the same file with no second
    convention to keep in sync. ``MINI_ORK_HOME`` is always set on the
    IDE action path; the CLI defaults to ``.mini-ork`` in cwd for
    hands-on usage.
    """
    return os.path.join(cn_client._section_cache_dir(), _SEARCH_CACHE_FILE)


def _last_ts_from_summary(payload) -> int:
    """Extract ``last_ts`` / ``last_activity`` from a session summary.

    The wireshape wraps everything under ``summary.summary``; older CN
    builds returned the flat shape. Either form returns the int value
    (epoch seconds, or whatever unit the server records — files mode
    only uses this for sorting, never rendering), or ``0`` when the
    field is absent. A non-dict payload returns ``0``.
    """
    if not isinstance(payload, dict):
        return 0
    inner = payload.get("summary")
    if not isinstance(inner, dict):
        inner = payload
    for key in ("last_ts", "last_activity"):
        v = inner.get(key)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _project_path_match(item, project: str) -> str:
    """First path inside ``item`` that lies under ``project``, or ``""``.

    Each verified wire shape carries the path in a different place —
    ``metadata.cwd`` / ``metadata.project_cwd`` for memories,
    ``files[]`` for sessions-by-feature and features, ``matched_files[]``
    for files mode. The helper walks the common shapes so a foreign
    project leak is impossible by construction (an item with no path
    under ``project`` is dropped by the caller of this helper).
    """
    if not isinstance(item, dict) or not project:
        return ""
    norm = project.rstrip("/")
    # Top-level keys first (matches :func:`cn_client.filter_by_project_path`).
    for key in ("path", "file", "cwd", "project_cwd", "uri"):
        v = item.get(key)
        if isinstance(v, str) and v.startswith(norm + "/"):
            return v
    # ``metadata`` is the canonical wire shape — verified 2026-10-15.
    meta = item.get("metadata")
    if isinstance(meta, dict):
        for key in ("project_cwd", "cwd", "path", "file"):
            v = meta.get(key)
            if isinstance(v, str) and v.startswith(norm + "/"):
                return v
    # List-of-path fields: ``files`` / ``matched_files`` ship as a
    # list of strings (or one-level dicts whose values include a path).
    for key in ("files", "matched_files"):
        v = item.get(key)
        if isinstance(v, list):
            for entry in v:
                if isinstance(entry, str) and entry.startswith(norm + "/"):
                    return entry
                if isinstance(entry, dict):
                    for vv in entry.values():
                        if isinstance(vv, str) and vv.startswith(norm + "/"):
                            return vv
    return ""


def _do_memories(query: str, project: str, all_projects: bool, limit: int) -> list:
    """POST ``/api/v1/tools/retrieve`` with a raised read budget.

    The 8 s default read budget drops ``retrieve`` (which takes ~20 s on
    real CN); the verb raises ``CN_TIMEOUT_SEC`` for the duration of the
    call and restores the prior value on exit. The page never asks for
    memories (it reads the cache slot), so the raised budget is local to
    the verb. Filtering accepts the verified wire shape's nested
    ``metadata.cwd`` / ``metadata.project_cwd``; an item with no
    path under ``project`` is dropped by :func:`_project_path_match`.
    """
    prior = os.environ.get("CN_TIMEOUT_SEC")
    os.environ["CN_TIMEOUT_SEC"] = "30"
    try:
        raw = cn_client.retrieve(query, limit=max(1, int(limit)))
    finally:
        if prior is None:
            os.environ.pop("CN_TIMEOUT_SEC", None)
        else:
            os.environ["CN_TIMEOUT_SEC"] = prior
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return []
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        return []
    if all_projects or not project:
        return hits
    return [h for h in hits if _project_path_match(h, project)]


def _do_sessions(query: str, project: str, all_projects: bool, limit: int) -> list:
    """GET ``/api/v1/sessions/by-feature?q=<query>``.

    The endpoint wraps rows under ``hits`` (verified 2026-10-15); the
    client-side filter keeps only rows whose ``files[]`` (or
    ``metadata.cwd`` / ``metadata.project_cwd``) lies under the
    project root. An item with no path under ``project`` is dropped by
    :func:`_project_path_match` — the cross-project leak guard.
    """
    raw = cn_client.sessions_by_feature(query)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return []
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        return []
    if all_projects or not project:
        return hits
    return [h for h in hits if _project_path_match(h, project)]


def _do_features(query: str, project: str, all_projects: bool,
                 limit: int, since: str = _SEARCH_FEATURES_SINCE) -> list:
    """GET ``/api/v1/features?since=<since>[&project=<project>]``.

    The endpoint has no server-side session filter (kickoff assumption);
    it DOES honour a ``project=`` query parameter — the verified live
    shape returns ``{count, since, layer, features: [...]}``. Passing
    ``project=`` keeps the response small enough that the page renders
    without a second CN call; ``--all-projects`` skips the param so the
    full list comes back (the all-projects chip on the page is the
    explicit, labelled opt-in). Each row's ``files[]`` (or
    ``metadata.project_cwd``) provides the client-side cross-check.
    """
    if cn_client._disabled():
        return []
    params = [f"since={urllib.parse.quote(since, safe='')}"]
    if not all_projects and project:
        params.append(f"project={urllib.parse.quote(project, safe='')}")
    if query:
        # Features are not free-text match on the wire; the query rides
        # ``query=`` so a future server filter is honoured without code
        # changes here. A pre-feature-filter server returns the full
        # window — the verb caps to ``limit`` after.
        params.append(f"query={urllib.parse.quote(query, safe='')}")
    raw = cn_client._get("/api/v1/features?" + "&".join(params))
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return []
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        return []
    if not all_projects and project:
        features = [f for f in features if _project_path_match(f, project)]
    return features[:max(1, int(limit))]


def _do_files(query: str, project: str, all_projects: bool, limit: int) -> list:
    """GET ``/api/v1/sessions/by-file?path=<query>`` then sort by ``last_ts``.

    The wire response has NO timestamp on each match — it ships
    ``{query, matches: [{session_id, matched_files[], total_files}]}``.
    To get a meaningful "newest first" order, the verb enriches each
    match with ``/api/v1/sessions/:id/summary``'s ``last_ts`` (cheap
    header-only endpoint). Both the project filter (on the per-row
    ``matched_files[]`` path) and the limit are applied AFTER the
    enrichment so a filtered match doesn't reorder the kept rows.
    """
    if cn_client._disabled():
        return []
    raw = cn_client.sessions_by_file(query)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return []
    matches = data.get("matches") if isinstance(data, dict) else None
    if not isinstance(matches, list):
        return []

    enriched: list[dict] = []
    for m in matches:
        if not isinstance(m, dict):
            continue
        sid = m.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        try:
            sum_raw = cn_client._get(
                f"/api/v1/sessions/{urllib.parse.quote(sid, safe='')}/summary"
            )
        except Exception:  # noqa: BLE001 — best-effort enrichment
            sum_raw = "{}"
        try:
            sum_data = json.loads(sum_raw) if sum_raw else {}
        except ValueError:
            sum_data = {}
        enriched.append({**m, "last_ts": _last_ts_from_summary(sum_data)})

    if not all_projects and project:
        enriched = [m for m in enriched if _project_path_match(m, project)]

    enriched.sort(key=lambda r: int(r.get("last_ts") or 0), reverse=True)
    return enriched[:max(1, int(limit))]


def _write_search_slot(envelope: dict) -> str:
    """Persist the envelope to ``$MINI_ORK_HOME/cache/cn/context.search__latest.json``.

    Returns the absolute path on success so the operator can ``cat`` it.
    Failures surface as ``""`` — the verb prints an error and exits 3
    when the slot cannot be written (a write failure is data loss in
    practice, so the verb does not return 0 on it).
    """
    path = _search_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, ensure_ascii=False)
        return path
    except OSError as exc:
        sys.stderr.write(f"mini-ork cn search: failed to write {path}: {exc}\n")
        return ""


def _cmd_search(args: argparse.Namespace) -> int:
    """Search ContextNest, then write the slot the IDE tab reads.

    The mode dispatches to one of the four ``_do_*`` helpers; each one
    honours the same envelope shape (mode / query / all_projects /
    project / ts / count / result). ``--json`` short-circuits the slot
    write and prints the envelope on stdout so callers (and tests) can
    inspect the wire response without touching the home cache.
    """
    mode = args.mode
    query = args.query
    all_projects = bool(args.all_projects)
    limit = max(1, min(int(args.limit or _SEARCH_DEFAULT_LIMIT), _SEARCH_MAX_LIMIT))
    project = _project_root()
    ts = int(time.time())

    try:
        if mode == "memories":
            rows = _do_memories(query, project, all_projects, limit)
            result = {"hits": rows}
        elif mode == "sessions":
            rows = _do_sessions(query, project, all_projects, limit)
            result = {"hits": rows}
        elif mode == "features":
            rows = _do_features(query, project, all_projects, limit)
            result = {"features": rows}
        elif mode == "files":
            rows = _do_files(query, project, all_projects, limit)
            result = {"matches": rows}
        else:  # argparse ``choices``; defensive
            sys.stderr.write(f"mini-ork cn search: unknown mode {mode!r}\n")
            return EXIT_USAGE
    except SecretStoreError as exc:
        sys.stderr.write(
            f"mini-ork cn search: secure tier secret store error: {exc}\n"
        )
        return EXIT_SECRET_CONFIG
    except cn_client.CoordUnavailable as exc:
        sys.stderr.write(f"mini-ork cn search: ContextNest unavailable: {exc}\n")
        return EXIT_COORD_UNREACHABLE

    envelope: dict = {
        "mode": mode,
        "query": query,
        "all_projects": all_projects,
        "project": project,
        "ts": ts,
        "count": len(rows),
        "result": result,
    }

    if args.json:
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        return EXIT_OK

    path = _write_search_slot(envelope)
    if not path:
        return EXIT_COORD_UNREACHABLE
    # Mirror ``cn status``: one-line text summary so the CLI still
    # succeeds under eyes-on / hands-on usage; ``--json`` is the
    # programmatic read.
    label = "all projects" if all_projects else (project or "unscoped")
    sys.stdout.write(
        f"cn search: {mode} · {envelope['count']} row(s) · "
        f"project={label} · slot={path}\n"
    )
    return EXIT_OK


def main(rest: list[str], root: str) -> int:
    del root
    parser = build_parser()
    try:
        args = parser.parse_args(rest)
    except SystemExit as exc:
        return int(exc.code or EXIT_USAGE)
    if args.subcommand is None or args.subcommand == "status":
        return _cmd_status(args)
    if args.subcommand == "ack-overlap":
        return _cmd_ack_overlap(args)
    if args.subcommand == "search":
        return _cmd_search(args)
    sys.stderr.write(f"mini-ork cn: unknown subcommand {args.subcommand!r}\n")
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], root=os.getcwd()))
