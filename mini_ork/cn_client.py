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


def connections_for(node_id: str, limit: int = 8) -> str:
    if _disabled() or not available():
        return "{}"
    return _get(f"/api/v1/connections?node_id={_enc(node_id)}&limit={limit}")


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
