"""Python port of lib/cn_client.sh — ContextNest HTTP client (read + hook push).

Strangler-fig parity port. Same design rules as the bash: never block mini-ork
on CN being down (every call has a timeout + fallback to ``{}`` / ``""``), never
write memories (only fire-and-forget event/outcome posts), cite-tag every atom.
The render_* functions are transcribed verbatim from the bash's embedded python
so their markdown output byte-matches.

Env: CN_BASE_URL, CN_TIMEOUT_SEC (8), CN_HOOK_TIMEOUT_SEC (3), CN_PING_TTL (30),
MO_DISABLE_CN (1 → reads return {} / "", posts no-op).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def _base() -> str:
    return os.environ.get("CN_BASE_URL", "http://127.0.0.1:28080")


def _timeout() -> float:
    return float(os.environ.get("CN_TIMEOUT_SEC", "8"))


def _hook_timeout() -> float:
    return float(os.environ.get("CN_HOOK_TIMEOUT_SEC", "3"))


def _ping_ttl() -> int:
    return int(os.environ.get("CN_PING_TTL", "30"))


def _disabled() -> bool:
    return os.environ.get("MO_DISABLE_CN", "0") == "1"


def _ping_cache_file() -> str:
    d = os.path.join(os.environ.get("MINI_ORK_HOME", ".mini-ork"), "state")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return os.path.join(d, "cn_ping.cache")


def _get_text(path: str) -> str:
    try:
        with urllib.request.urlopen(_base() + path, timeout=_timeout()) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _get(path: str) -> str:
    try:
        with urllib.request.urlopen(_base() + path, timeout=_timeout()) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return "{}"


def _post_json(path: str, body: str) -> str:
    try:
        req = urllib.request.Request(_base() + path, data=body.encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=_timeout()) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return "{}"


def available() -> bool:
    """0/True if CN reachable (cached for CN_PING_TTL secs)."""
    if _disabled():
        return False
    cache = _ping_cache_file()
    now = int(time.time())
    if os.path.isfile(cache):
        try:
            ts, state = open(cache).read().split()[:2]
            if now - int(ts) < _ping_ttl():
                return state == "up"
        except Exception:
            pass
    code = "000"
    try:
        req = urllib.request.Request(_base() + "/api/v1/substrate/health")
        with urllib.request.urlopen(req, timeout=_timeout()) as r:
            code = str(r.status)
    except Exception:
        code = "000"
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
    if _disabled() or not available():
        return "{}"
    return _post_json("/api/v1/tools/retrieve", json.dumps({"query": query, "limit": int(limit)}))


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
        out.append(f"- [{kind} sim={sim:.2f} {ts} sess={sid[:8]}] {content}")
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


class CoordHTTPError(RuntimeError):
    """ContextNest answered with a 4xx/5xx status."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"ContextNest returned {status}")
        self.status = status
        self.body = body


def _coord_timeout() -> float:
    return float(os.environ.get("CN_COORD_TIMEOUT_SEC", "3"))


def _coord_request(method: str, path: str, body: dict | None = None) -> dict:
    """One raise-based send path for the coord helpers.

    Unlike ``_get``/``_post_json`` this raises instead of returning a fallback,
    so the CLI can map failures to exit codes. Returns the parsed JSON body
    (``{}`` on an empty or undecodable body).
    """
    data = None
    headers: dict = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(_base() + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_coord_timeout()) as r:
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


def coord_inbox(principal_id: str, unacked: bool = True) -> dict:
    """GET /api/v1/coord/principals/{id}/messages?unacked=true — unacked messages."""
    flag = "true" if unacked else "false"
    return _coord_request(
        "GET", f"/api/v1/coord/principals/{_enc(principal_id)}/messages?unacked={flag}",
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
