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
existing :mod:`mini_ork.cn_client` seams (``_post_json`` for memories so the
verb reads ``CN_SEARCH_TIMEOUT_SEC`` directly; ``sessions_by_feature``,
``sessions_by_file``, ``features_recent``, plus ``filter_by_project_path``
for the others). Project scoping resolves through ``MINI_ORK_HOME``'s
parent (the operator's project root) — never ``MINI_ORK_ROOT`` (the engine
install). Unresolved scope fails CLOSED: an envelope with ``"error": "…set
MINI_ORK_HOME, or pass --all-projects…"`` and the verb exits 3. The
memories mode reads ``CN_SEARCH_TIMEOUT_SEC`` (default ``"90"``) for the
read budget; a body without a ``hits`` list (or ``available()`` returning
False) writes an error envelope and exits 4 — callers distinguish scope
vs. retrieve by the envelope's ``error`` field.

``cn seen-before <run-id> [--node <node-id>]`` caches one
``/api/v1/tools/retrieve`` POST per failing run/node into
``<run_dir>/contextnest-seen-before.json`` — the immutable per-run file
the failed outcome and failed node pages read on render. The verb reaches
into :func:`mini_ork.cn_client._post_json` directly (same wire as
:func:`mini_ork.cn_client.retrieve`, but with the ``CN_SEARCH_TIMEOUT_SEC``
budget the verb reads) and never touches ``CN_TIMEOUT_SEC``; project
scoping is the same fail-closed gate ``cn search`` honours
(:data:`SCOPE_ERROR` → exit 3). A body without a ``hits`` list is treated
as a retrieve failure (exit 4) and stores nothing — a partial write would
poison the page's render cache. The envelope is written ONCE: a second
call for the same run does NOT overwrite the stored file (immutable
cache, never mutated), and the page always reads the file verbatim — the
``mini-ork cn seen-before`` button is the only path that writes.

``cn outcome <atom-id> success|failure --run <run-id>`` posts to
``/api/v1/agent/outcome`` through the synchronous
:func:`mini_ork.cn_client._settle_outcome` seam and reports the response's
``updated`` field (NOT ``delta_applied`` — the documented live shape
verified 2026-10-15; the verb never reads ``delta_applied``). The verb is
confirm-gated: the IDE button passes a ``confirm`` through
:func:`mini_ork.ide_pages.spec.cli`, and the CLI requires ``--yes`` (or an
affirmative on the TTY prompt) before posting so a typed ``mini-ork cn
outcome`` invocation does not silently mutate CN's memory on a yes-means-
yes assumption. A secure-tier missing write-scoped key surfaces as a
named :class:`SecretStoreError` (exit 5, key name only, never the value);
CN disabled / unreachable returns the same pending reason the settle path
records and exits 0 — the audit log captures the failure, the operator
sees the same missing-key signal.

Wire: ``GET /api/v1/substrate/health`` (ping),
``POST /api/v1/tools/retrieve`` (memories / seen-before),
``GET /api/v1/sessions/by-file`` and ``/api/v1/sessions/by-feature`` and
``/api/v1/sessions/:id/summary`` (files), ``GET /api/v1/features?project=...``
(features), ``POST /api/v1/agent/outcome`` (settle). Exit codes: 0 on a
successful search / seen-before write / outcome settle; 2 on a usage
error; 3 on a scope-resolution failure OR ContextNest-unreachable; 4 on
a retrieve-failure OR an HTTP 4xx/5xx; 5 on a secure-tier secret-store
misconfiguration. Both 3 and 4 are overloaded — the envelope's ``error``
field (search) / the stderr reason (seen-before / outcome) is the only
discriminator.
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
from pathlib import Path

from mini_ork import cn_client
from mini_ork.dispatch.secrets import SecretStoreError

# Exit codes — kept small and named for the caller, not the implementation.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_COORD_UNREACHABLE = 3
EXIT_COORD_HTTP = 4
EXIT_SECRET_CONFIG = 5

# The fail-closed scope envelope's verbatim error text. Worded exactly so
# test assertions, documentation, and the page's red row all see the same
# string. Kept short so it fits in a chip and matches the CLI's stderr
# signature.
SCOPE_ERROR = (
    "cannot resolve this project; set MINI_ORK_HOME, "
    "or pass --all-projects to search every project"
)


class RetrieveFailed(RuntimeError):
    """The memories-mode retrieve POST did not return a parseable hits list.

    Raised by :func:`_do_memories` and converted to a standard envelope
    with ``error`` set and exit 4 by :func:`_cmd_search`. Carries the
    user-facing message verbatim — never the budget value as a bare float,
    always the human-readable ``"within <budget> s"`` framing.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


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
    seen = sub.add_parser(
        "seen-before",
        help="cache a ContextNest retrieve result for a failing run into "
             "<run_dir>/contextnest-seen-before.json",
    )
    seen.add_argument(
        "run_id",
        help="the run id whose <run_dir>/contextnest-seen-before.json the "
             "outcome and node pages read",
    )
    seen.add_argument(
        "--node", default=None,
        help="the failing node id; the verb reads the error tail from the "
             "node's log / session transcript. Defaults to the run's "
             "failing node when omitted.",
    )
    seen.add_argument(
        "--all-projects", action="store_true",
        help="include other projects' rows; default is this-project only",
    )
    seen.add_argument(
        "--top-k", type=int, default=5,
        help="the /api/v1/tools/retrieve top_k value (default 5). The page "
             "renders the top 3 by similarity regardless.",
    )
    seen.add_argument(
        "--json", action="store_true",
        help="emit the envelope as JSON instead of writing the stored file",
    )
    outcome = sub.add_parser(
        "outcome",
        help="settle one atom's outcome (success|failure) against "
             "/api/v1/agent/outcome; reports the response's `updated` field",
    )
    outcome.add_argument(
        "atom_id",
        help="the atom id to settle; the run page also accepts the run id "
             "so the Helped/Wrong buttons can pass one stable value",
    )
    outcome.add_argument(
        "outcome",
        choices=("success", "failure"),
        help="the outcome to record; the live 2026-10-15 wire accepts "
             "either, and the response carries `updated`",
    )
    outcome.add_argument(
        "--run", default=None,
        help="the run id, used to scope evidence when the audit log "
             "records the settle; defaults to none",
    )
    outcome.add_argument(
        "--evidence", default="",
        help="an optional evidence string the settle records against the "
             "atom; capped at 480 chars by the cn_client seam",
    )
    outcome.add_argument(
        "--yes", action="store_true",
        help="skip the interactive confirm; the IDE buttons carry a "
             "`confirm` payload that the IDE itself handles, so the CLI "
             "still asks the operator on a typed invocation",
    )
    outcome.add_argument(
        "--json", action="store_true",
        help="emit the settle response as JSON instead of the one-line "
             "`outcome: <atom_id> updated=<n>` text",
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
#
# The memories mode reaches into :func:`mini_ork.cn_client._post_json`
# directly rather than :func:`mini_ork.cn_client.retrieve` — both call the
# same endpoint with the same body, but reading ``CN_SEARCH_TIMEOUT_SEC``
# at the verb level avoids the legacy pattern of mutating ``CN_TIMEOUT_SEC``
# around the call. A body without a ``hits`` list, or ``available()``
# returning False, raises :class:`RetrieveFailed` so the verb can write
# the standard envelope and exit 4.

_SEARCH_CACHE_FILE = "context.search__latest.json"
_SEARCH_DEFAULT_LIMIT = 20
_SEARCH_MAX_LIMIT = 100
_SEARCH_FEATURES_SINCE = "30d"
_VALID_MODES = ("memories", "sessions", "features", "files")


def _project_root() -> str:
    """The project root that scopes CN rows.

    Resolution order:

    1. ``Path(os.environ["MINI_ORK_HOME"]).resolve().parent`` when
       ``MINI_ORK_HOME`` is set. This is the same root the Context IDE page
       reads (``home.parent`` — the project's toplevel, not the home dir).
    2. The git toplevel of cwd. Cheap (``git rev-parse --show-toplevel``,
       sub-second) and the only fallback the hands-on CLI has when no
       ``MINI_ORK_HOME`` is set.
    3. ``""`` — the explicit fail-closed sentinel. NEVER ``MINI_ORK_ROOT``:
       that env is the engine install location, not the user's project.

    Returning ``""`` is the verb's "scope is unresolved" signal. The verb
    turns that into an error envelope and exits 3 unless ``--all-projects``
    is the user's opt-in to the unfiltered state.
    """
    home = os.environ.get("MINI_ORK_HOME")
    if home:
        try:
            return str(Path(home).resolve().parent)
        except OSError:
            # Fall through to the git probe — a broken MINI_ORK_HOME is
            # not the same as the env being unset, but it is the same
            # signal to the rest of the verb.
            pass
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        if out:
            return out
    except Exception:  # noqa: BLE001 — git missing or hung; fall through
        pass
    return ""


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

    "Under ``project``" is :func:`mini_ork.cn_client.project_roots` — the
    checkout *and* its ``<repo>-worktrees`` sibling. ContextNest records a
    session against its launch directory, and this repo launches almost
    everything from a worktree, so matching the checkout alone returned zero
    project-scoped rows against the live server (measured 2026-10-16: 0 hits
    scoped, 5 hits unscoped, every one of them this project's own files).
    """
    if not isinstance(item, dict) or not project:
        return ""
    prefixes = tuple(r + "/" for r in cn_client.project_roots(project))
    if not prefixes:
        return ""
    # Top-level keys first (matches :func:`cn_client.filter_by_project_path`).
    for key in ("path", "file", "cwd", "project_cwd", "uri"):
        v = item.get(key)
        if isinstance(v, str) and v.startswith(prefixes):
            return v
    # ``metadata`` is the canonical wire shape — verified 2026-10-15.
    meta = item.get("metadata")
    if isinstance(meta, dict):
        for key in ("project_cwd", "cwd", "path", "file"):
            v = meta.get(key)
            if isinstance(v, str) and v.startswith(prefixes):
                return v
    # List-of-path fields: ``files`` / ``matched_files`` ship as a
    # list of strings (or one-level dicts whose values include a path).
    for key in ("files", "matched_files"):
        v = item.get(key)
        if isinstance(v, list):
            for entry in v:
                if isinstance(entry, str) and entry.startswith(prefixes):
                    return entry
                if isinstance(entry, dict):
                    for vv in entry.values():
                        if isinstance(vv, str) and vv.startswith(prefixes):
                            return vv
    return ""


def _do_memories(query: str, project: str, all_projects: bool, limit: int) -> list:
    """POST ``/api/v1/tools/retrieve`` through :func:`cn_client._post_json`.

    The memories mode goes through :func:`cn_client._post_json` directly
    rather than :func:`cn_client.retrieve` so the verb can pass the
    ``CN_SEARCH_TIMEOUT_SEC`` budget as the ``timeout`` kwarg and never
    touch ``CN_TIMEOUT_SEC`` (the legacy shared read budget). The two
    functions hit the same endpoint with the same body — only the
    timeout knob differs.

    The same ``_disabled()`` / ``available()`` checks ``retrieve`` makes
    come FIRST so a disabled or unreachable CN is reported as
    "ContextNest is not reachable" rather than a budget-spent error.

    A body that does NOT carry a ``hits`` list — including the empty
    ``{}`` ``_post_json`` returns on a swallowed timeout / connection
    error — raises :class:`RetrieveFailed` so :func:`_cmd_search` can
    write the standard envelope and exit 4. A real ``{"hits": [...]}``
    body is the success path, even when the list is empty.

    The verb fails closed at the scope gate (exit 3) before this helper
    is called with ``(not all_projects and not project)``; a non-empty
    ``project`` is the documented invariant when ``all_projects`` is
    False, and the path-match filter drops items without a path under
    the project root by construction.
    """
    if cn_client._disabled() or not cn_client.available():
        raise RetrieveFailed("ContextNest is not reachable")
    budget = float(os.environ.get("CN_SEARCH_TIMEOUT_SEC", "90"))
    body = json.dumps({"query": query, "top_k": max(1, int(limit))})
    raw = cn_client._post_json("/api/v1/tools/retrieve", body, timeout=budget)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        raise RetrieveFailed(
            f"ContextNest retrieve did not answer within {budget:g} s"
        )
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        raise RetrieveFailed(
            f"ContextNest retrieve did not answer within {budget:g} s"
        )
    if all_projects:
        return hits
    return [h for h in hits if _project_path_match(h, project)]


def _do_sessions(query: str, project: str, all_projects: bool, limit: int) -> list:
    """GET ``/api/v1/sessions/by-feature?q=<query>``.

    The endpoint wraps rows under ``hits`` (verified 2026-10-15); the
    client-side filter keeps only rows whose ``files[]`` (or
    ``metadata.cwd`` / ``metadata.project_cwd``) lies under the
    project root. An item with no path under ``project`` is dropped by
    :func:`_project_path_match` — the cross-project leak guard. The
    verb's scope gate guarantees ``project`` is non-empty whenever
    ``all_projects`` is False, so the unfiltered ``or not project``
    shortcut the legacy mode helpers carried is gone.
    """
    raw = cn_client.sessions_by_feature(query)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return []
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        return []
    if all_projects:
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


def _empty_result_for(mode: str) -> dict:
    """One empty result dict whose key matches the mode's wire shape.

    Centralised so the scope-failure and retrieve-failure envelopes carry
    the same shape as a real ``{"hits": []}`` / ``{"features": []}`` /
    ``{"matches": []}`` result — the page reads ``envelope["result"]``
    blindly when no error is set, and a missing key would render the
    error row but then crash on the follow-up render call."""
    if mode in ("memories", "sessions"):
        return {"hits": []}
    if mode == "features":
        return {"features": []}
    if mode == "files":
        return {"matches": []}
    return {}


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


def _emit_envelope(envelope: dict, args: argparse.Namespace,
                   write_slot: bool) -> int:
    """Write/print the envelope and return the verb's exit code.

    ``--json`` short-circuits the slot write and emits the envelope on
    stdout so callers (and tests) can inspect the wire response without
    touching the home cache. The slot is always written first when
    ``write_slot`` is True — that way the page's render path sees the
    error envelope even if the operator's terminal captured the JSON.

    Returns 0 on a write success, or :data:`EXIT_COORD_UNREACHABLE` when
    the slot cannot be persisted (a write failure is data loss in
    practice). Error exits (3 / 4 / 5) are the caller's responsibility;
    this helper only routes the success / unreachable split.
    """
    if args.json:
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        return EXIT_OK
    if not write_slot:
        return EXIT_OK
    path = _write_search_slot(envelope)
    if not path:
        return EXIT_COORD_UNREACHABLE
    return EXIT_OK


def _cmd_search(args: argparse.Namespace) -> int:
    """Search ContextNest, then write the slot the IDE tab reads.

    The mode dispatches to one of the four ``_do_*`` helpers; each one
    honours the same envelope shape (mode / query / all_projects /
    project / ts / count / result). Failures (scope-unresolved,
    retrieve-failed) are folded into the SAME envelope shape via an
    extra ``error`` field and a non-zero exit. ``--json`` short-circuits
    the slot write and prints the envelope on stdout so callers (and
    tests) can inspect the wire response without touching the home
    cache.

    Fail-closed scope: when ``--all-projects`` is off and the project
    root cannot be resolved (``MINI_ORK_HOME`` unset AND no git toplevel
    in cwd), the verb never calls ContextNest — it writes an error
    envelope, prints the message to stderr, and exits 3. The error
    text is :data:`SCOPE_ERROR`.
    """
    mode = args.mode
    query = args.query
    all_projects = bool(args.all_projects)
    limit = max(1, min(int(args.limit or _SEARCH_DEFAULT_LIMIT), _SEARCH_MAX_LIMIT))
    project = _project_root()
    ts = int(time.time())
    empty_result = _empty_result_for(mode)

    # Fail-closed scope — every mode honours the same gate. ``--all-projects``
    # is the explicit opt-in to the unfiltered state, so it bypasses the
    # gate entirely (the project field stays "" in the envelope).
    if not all_projects and not project:
        envelope: dict = {
            "mode": mode,
            "query": query,
            "all_projects": all_projects,
            "project": project,
            "ts": ts,
            "count": 0,
            "result": empty_result,
            "error": SCOPE_ERROR,
        }
        sys.stderr.write(f"mini-ork cn search: {SCOPE_ERROR}\n")
        rc = _emit_envelope(envelope, args, write_slot=True)
        # The error envelope is the user-visible signal; the slot is the
        # page's signal. Both must reach their destination — a write
        # failure trumps the non-error-exit case above.
        return rc if rc else EXIT_COORD_UNREACHABLE

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
        # Secure-tier misconfig fires BEFORE the timeout detection so a
        # missing CN_TOKEN_KEY cannot be papered over as a retrieve
        # timeout. The CLI echoes the key name (never the value) and
        # returns 5.
        sys.stderr.write(
            f"mini-ork cn search: secure tier secret store error: {exc}\n"
        )
        return EXIT_SECRET_CONFIG
    except RetrieveFailed as exc:
        # Centralised retrieve-failure envelope: same shape as a real
        # empty response plus ``error``, exit 4. Page renders the error
        # as one red row (see ``_render_search_results``).
        envelope = {
            "mode": mode,
            "query": query,
            "all_projects": all_projects,
            "project": project,
            "ts": ts,
            "count": 0,
            "result": empty_result,
            "error": exc.message,
        }
        sys.stderr.write(f"mini-ork cn search: {exc.message}\n")
        rc = _emit_envelope(envelope, args, write_slot=True)
        return rc if rc else EXIT_COORD_HTTP

    envelope = {
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
    # programmatic read. ``label`` is the project path verbatim — the
    # unfiltered opt-in is "all projects", and a non-empty project is
    # the only scope the verb honours.
    label = "all projects" if all_projects else project
    sys.stdout.write(
        f"cn search: {mode} · {envelope['count']} row(s) · "
        f"project={label} · slot={path}\n"
    )
    return EXIT_OK


# ── seen-before verb ───────────────────────────────────────────────────────
#
# ``cn seen-before <run-id> [--node <node-id>]`` resolves the run's failing
# node, pulls an error-tail query from the node's log / session transcript,
# POSTs one ``/api/v1/tools/retrieve`` through ``_post_json`` with
# ``CN_SEARCH_TIMEOUT_SEC`` as the budget, filters the result to the
# project's rows, and writes the envelope to
# ``<run_dir>/contextnest-seen-before.json`` (an immutable per-run cache
# the outcome and node pages read verbatim). The verb never touches
# ``CN_TIMEOUT_SEC``; the search-mode memory path is the one that owns
# that budget. A body without a ``hits`` list (including the empty
# ``{}`` ``_post_json`` returns on a swallowed timeout) is treated as a
# retrieve failure: NO file is written (a partial write would poison the
# page's render cache) and the verb exits 4.
#
# Project scoping is the same fail-closed gate ``cn search`` honours:
# ``--all-projects`` is the explicit opt-in to the unfiltered state. The
# stored envelope carries provenance (``run_id``, ``node_id``, ``query``,
# ``ts``) so a second open reads the file as the page's source of truth —
# the page never re-fetches.
#
# The page renders the top 3 hits by similarity. ``top_k=5`` keeps the
# page's three cards and the no-result case (when fewer than three hits
# are project-scoped) from diverging from a more aggressive fetch.

_SEEN_BEFORE_FILE = "contextnest-seen-before.json"
_SEEN_BEFORE_TOPK = 5
_SEEN_BEFORE_PAGE_LIMIT = 3
_SEEN_BEFORE_TAIL_BYTES = 4096


def _valid_run_id(run_id: str) -> bool:
    """``True`` when *run_id* is one safe path segment.

    The id names a directory under ``$MINI_ORK_HOME/runs``. A value
    carrying a separator, a parent reference, an absolute path, or a NUL
    would let the verb read or write outside that tree — and the
    error-tail read feeds its bytes onto the ContextNest wire, so an
    escaping id is an exfiltration path as well as a clobber. Rejecting
    it keeps every id the operator names inside the runs directory.
    """
    if not run_id or run_id in (".", ".."):
        return False
    if run_id != os.path.basename(run_id):
        return False
    return "/" not in run_id and "\\" not in run_id and "\x00" not in run_id


def _seen_before_path(run_id: str) -> str:
    """Absolute path to the run's stored envelope.

    The same convention the run page uses: ``$MINI_ORK_HOME/runs/<run-id>``.
    ``MINI_ORK_HOME`` is required (the IDE path always sets it); a hands-on
    CLI invocation without it gets an empty path, which :func:`_cmd_seen_before`
    turns into a scope-resolution failure (exit 3). Returning ``""`` here is
    the fail-closed sentinel the rest of the verb expects.
    """
    home = os.environ.get("MINI_ORK_HOME")
    if not home or not _valid_run_id(run_id):
        return ""
    return os.path.join(home, "runs", run_id, _SEEN_BEFORE_FILE)


def _read_error_tail(run_id: str, node_id: str | None) -> str:
    """A short error-tail query from the failing node's artefacts.

    Reads, in order:

    1. ``<run_dir>/impl-<node_id>.log`` — the implementer's last stderr
       tail. Capped to :data:`_SEEN_BEFORE_TAIL_BYTES` to keep the retrieve
       query short.
    2. ``<run_dir>/verifier_<node_id>.log`` — the verifier's output.
    3. ``<run_dir>/evidence/<node_id>.log`` — the gate/shell evidence.
    4. The last ``text`` / ``result`` entry of the node's session
       transcript (``<run_dir>/sessions/<sid>.jsonl``), if discoverable.

    A missing run dir or a missing node log returns ``""``; the verb then
    falls back to the run id itself as the query so the retrieve still
    has SOMETHING to send (a hit on the run id is more useful than a
    refusal to search). The fallback is documented at the call site.
    """
    home = os.environ.get("MINI_ORK_HOME")
    if not home or not _valid_run_id(run_id):
        return ""
    run_dir = Path(home) / "runs" / run_id
    if not run_dir.is_dir():
        return ""
    if node_id:
        for rel in (f"impl-{node_id}.log",
                    f"verifier_{node_id}.log",
                    f"evidence/{node_id}.log"):
            path = run_dir / rel
            if path.is_file():
                try:
                    raw = path.read_bytes()[-_SEEN_BEFORE_TAIL_BYTES:]
                except OSError:
                    continue
                try:
                    text = raw.decode("utf-8", "replace").strip()
                except Exception:  # noqa: BLE001
                    continue
                if text:
                    return text
    sessions = run_dir / "sessions"
    if sessions.is_dir():
        try:
            candidates = sorted(
                sessions.glob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            candidates = []
        for path in candidates:
            try:
                raw = path.read_bytes()[-_SEEN_BEFORE_TAIL_BYTES:]
            except OSError:
                continue
            try:
                lines = raw.decode("utf-8", "replace").splitlines()[-20:]
            except Exception:  # noqa: BLE001
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                for key in ("error", "result", "text", "content"):
                    v = entry.get(key)
                    if isinstance(v, str) and v.strip():
                        return v.strip()[:1024]
                msg = entry.get("message")
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()[:1024]
    return ""


def _do_seen_before_retrieve(query: str, project: str, all_projects: bool,
                             top_k: int) -> list:
    """POST ``/api/v1/tools/retrieve`` for the seen-before verb.

    Mirrors :func:`_do_memories`: same wire, same project filter, same
    fail-closed on a body without a ``hits`` list. ``top_k`` is the value
    the verb passes through (default :data:`_SEEN_BEFORE_TOPK`); the page
    renders the top :data:`_SEEN_BEFORE_PAGE_LIMIT` by similarity
    regardless. The ``_disabled() / available()`` gate fires first so a
    disabled or unreachable CN is reported as "ContextNest is not
    reachable" rather than a budget-spent error.
    """
    if cn_client._disabled() or not cn_client.available():
        raise RetrieveFailed("ContextNest is not reachable")
    budget = float(os.environ.get("CN_SEARCH_TIMEOUT_SEC", "90"))
    body = json.dumps({"query": query, "top_k": max(1, int(top_k))})
    raw = cn_client._post_json("/api/v1/tools/retrieve", body, timeout=budget)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        raise RetrieveFailed(
            f"ContextNest retrieve did not answer within {budget:g} s"
        )
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        raise RetrieveFailed(
            f"ContextNest retrieve did not answer within {budget:g} s"
        )
    if all_projects:
        return hits
    return [h for h in hits if _project_path_match(h, project)]


def _seen_before_exists(path: str) -> bool:
    """``True`` when a stored envelope is already on disk.

    The verb NEVER overwrites an existing file — the per-run cache is
    immutable by contract. A second call for the same run exits 0 with a
    one-line ``seen-before: <run_id> already cached`` text; ``--json``
    echoes the existing envelope so the operator can see what is
    there. This keeps a stale write (e.g. one captured against an
    outdated CN corpus) from being silently replaced by a fresh run.
    """
    return bool(path) and os.path.isfile(path)


def _read_seen_before(path: str) -> dict:
    """Best-effort read of a stored envelope, or ``{}`` on miss / parse error.

    The page treats an unparseable file the same as a missing one (the
    page's render path is the slot, not the wire) — never raises.
    """
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_seen_before(path: str, envelope: dict) -> bool:
    """Persist the envelope atomically; returns ``True`` on success.

    Best-effort: a write failure exits 3 (the verb's data-loss path) and
    prints the path on stderr so the operator can intervene. The page
    reads the file verbatim — a partial write would render a malformed
    JSON error on every refresh, so a failed write is treated like a
    scope failure.
    """
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as exc:
        sys.stderr.write(
            f"mini-ork cn seen-before: failed to create {path}: {exc}\n"
        )
        return False
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, ensure_ascii=False)
    except OSError as exc:
        sys.stderr.write(
            f"mini-ork cn seen-before: failed to write {path}: {exc}\n"
        )
        return False
    return True


def _cmd_seen_before(args: argparse.Namespace) -> int:
    """Cache one retrieve per failing run/node.

    Fail-closed scope: when ``--all-projects`` is off and the project
    root cannot be resolved, the verb never calls CN and exits 3 with
    :data:`SCOPE_ERROR` on stderr. The page's empty-state path is
    different (no project, no rows) — the verb's fail-closed envelope
    writes an error key so the page can render the operator-facing
    message exactly once.

    The stored envelope is written ONCE per run. A second call for the
    same run short-circuits with exit 0 and ``--json`` echoes the
    existing envelope; the file is never overwritten. A retrieve failure
    (no ``hits`` list, CN disabled, CN unreachable, secure-tier
    misconfig) writes nothing and exits 4 / 5 / 3 respectively — the
    page stays on its no-stored-file empty state until the operator
    succeeds.
    """
    run_id = str(args.run_id or "").strip()
    if not run_id:
        sys.stderr.write("mini-ork cn seen-before: run_id is required\n")
        return EXIT_USAGE
    if not _valid_run_id(run_id):
        sys.stderr.write(
            f"mini-ork cn seen-before: invalid run_id {run_id!r} "
            f"(expected one path segment)\n"
        )
        return EXIT_USAGE
    path = _seen_before_path(run_id)
    if not path:
        sys.stderr.write(
            f"mini-ork cn seen-before: cannot resolve {run_id} run dir; "
            f"set MINI_ORK_HOME to the project's .mini-ork directory\n"
        )
        return EXIT_COORD_UNREACHABLE

    if _seen_before_exists(path):
        if args.json:
            existing = _read_seen_before(path)
            sys.stdout.write(json.dumps(existing, ensure_ascii=False) + "\n")
        else:
            sys.stdout.write(
                f"cn seen-before: {run_id} already cached at {path}\n"
            )
        return EXIT_OK

    node_id = str(getattr(args, "node", None) or "").strip() or None
    all_projects = bool(args.all_projects)
    top_k = max(1, min(int(args.top_k or _SEEN_BEFORE_TOPK), 25))

    project = _project_root()
    if not all_projects and not project:
        sys.stderr.write(f"mini-ork cn seen-before: {SCOPE_ERROR}\n")
        return EXIT_COORD_UNREACHABLE

    query = _read_error_tail(run_id, node_id)
    if not query:
        # A run dir without a log / session transcript still gets a
        # useful retrieve: the run id itself is a free-text query that
        # surfaces past runs ContextNest has indexed.
        query = run_id

    ts = int(time.time())
    try:
        hits = _do_seen_before_retrieve(query, project, all_projects, top_k)
    except SecretStoreError as exc:
        sys.stderr.write(
            f"mini-ork cn seen-before: secure tier secret store error: {exc}\n"
        )
        return EXIT_SECRET_CONFIG
    except RetrieveFailed as exc:
        sys.stderr.write(f"mini-ork cn seen-before: {exc.message}\n")
        return EXIT_COORD_HTTP

    sorted_hits = sorted(
        (h for h in hits if isinstance(h, dict)),
        key=lambda h: float(h.get("similarity") or 0.0),
        reverse=True,
    )
    envelope = {
        "run_id": run_id,
        "node_id": node_id,
        "query": query,
        "ts": ts,
        "top_k": top_k,
        "count": len(sorted_hits),
        "all_projects": all_projects,
        "project": project,
        "hits": sorted_hits,
    }

    if args.json:
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        return EXIT_OK

    if not _write_seen_before(path, envelope):
        return EXIT_COORD_UNREACHABLE

    sys.stdout.write(
        f"cn seen-before: {run_id} · {len(sorted_hits)} hit(s) · "
        f"stored={path}\n"
    )
    return EXIT_OK


# ── outcome verb ───────────────────────────────────────────────────────────
#
# ``cn outcome <atom-id> success|failure --run <run-id>`` is confirm-gated
# and posts to ``/api/v1/agent/outcome`` through the synchronous settle
# seam. The verb reports the response's ``updated`` field — NOT
# ``delta_applied`` (the documented live 2026-10-15 shape; ``delta_applied``
# is read by the audit log but is not the success metric the operator
# sees). A secure-tier missing key surfaces as a named
# :class:`SecretStoreError` (exit 5, key name only, never the value);
# CN disabled / unreachable records ``pending`` and exits 0 — the audit
# log captures the failure so the operator sees the missing-key signal.


_OUTCOME_OK_TEXT = "outcome: {atom} {outcome} updated={updated}"


def _confirm_outcome(atom_id: str, outcome: str, run_id: str) -> bool:
    """TTY confirm for a typed ``cn outcome`` invocation.

    The IDE buttons carry a ``confirm`` payload that the IDE itself
    handles, so the IDE does not reach this helper. A typed CLI
    invocation asks the operator on stderr (the same yes/no prompt the
    other confirm-gated mini-ork subcommands use); ``--yes`` skips the
    prompt.

    Only an interactive stdin can answer the prompt. A pipe, /dev/null,
    a daemonised process, or a captured-stdio harness has no human at
    the terminal, so blocking on ``input()`` would either raise
    ``OSError`` or deadlock; both degrade to the safe auto-answer path
    :func:`mini_ork.cli.plan._can_prompt` already takes for the same
    reason. ``MINI_ORK_NONINTERACTIVE=1`` forces that path explicitly.
    Returns ``True`` when the invocation may proceed.
    """
    if os.environ.get("MINI_ORK_NONINTERACTIVE", "0") == "1":
        return True
    try:
        if not sys.stdin.isatty():
            return True
    except (OSError, ValueError):
        # A closed/detached stdin has no isatty — not a human.
        return True
    sys.stderr.write(
        f"About to record outcome {outcome!r} for atom {atom_id!r} "
        f"(run {run_id or '<none>'}).\n"
    )
    sys.stderr.write("Type 'yes' to continue: ")
    sys.stderr.flush()
    try:
        line = input()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        return False
    return line.strip().lower() == "yes"


def _outcome_settle_response(outcome: str, atom_id: str, run_id: str,
                             evidence: str) -> dict:
    """Call :func:`mini_ork.cn_client._settle_outcome` and return the dict.

    Thin wrapper so the verb's body stays one read-the-result path; the
    test layer patches :func:`mini_ork.cn_client._settle_outcome` directly
    to drive success / pending / skipped / secure-tier outcomes.
    """
    return cn_client._settle_outcome(
        outcome, [atom_id], evidence=evidence,
        session_id=run_id or "",
    )


def _cmd_outcome(args: argparse.Namespace) -> int:
    """Settle one atom against ``/api/v1/agent/outcome``.

    Confirm-gated: a typed invocation requires ``--yes`` (or the TTY
    prompt). The IDE buttons carry a ``confirm`` payload the IDE itself
    resolves — they do not reach this prompt.

    Reports the response's ``updated`` field on success; records
    ``pending`` on a CN-down / config-error state and exits 0 so the
    audit log captures the failure rather than the verb blowing up.
    """
    atom_id = str(args.atom_id or "").strip()
    if not atom_id:
        sys.stderr.write("mini-ork cn outcome: atom_id is required\n")
        return EXIT_USAGE
    outcome = str(args.outcome or "").strip()
    if outcome not in ("success", "failure"):
        sys.stderr.write(
            f"mini-ork cn outcome: outcome must be 'success' or 'failure' "
            f"(got {outcome!r})\n"
        )
        return EXIT_USAGE
    run_id = str(args.run or "").strip() or ""
    evidence = str(args.evidence or "")

    if not args.yes and not _confirm_outcome(atom_id, outcome, run_id):
        sys.stderr.write("mini-ork cn outcome: aborted\n")
        return EXIT_USAGE

    try:
        result = _outcome_settle_response(outcome, atom_id, run_id, evidence)
    except SecretStoreError as exc:
        sys.stderr.write(
            f"mini-ork cn outcome: secure tier secret store error: {exc}\n"
        )
        return EXIT_SECRET_CONFIG

    if args.json:
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    else:
        if result.get("status") == "ok":
            updated = result.get("updated")
            sys.stdout.write(
                _OUTCOME_OK_TEXT.format(
                    atom=atom_id, outcome=outcome, updated=updated,
                ) + "\n"
            )
        else:
            reason = str(result.get("reason") or "unknown")
            sys.stdout.write(
                f"outcome: {atom_id} pending ({reason})\n"
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
    if args.subcommand == "seen-before":
        return _cmd_seen_before(args)
    if args.subcommand == "outcome":
        return _cmd_outcome(args)
    sys.stderr.write(f"mini-ork cn: unknown subcommand {args.subcommand!r}\n")
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], root=os.getcwd()))
