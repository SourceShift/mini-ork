"""Suggest a healthy lane when a lane fails on quota/auth.

Consumed by :func:`mini_ork.recovery.retry_hint._case_lane_unavailable` to build
the ``mini-ork recover <run> --lane <alias>=<lane>`` command, and by
``retry_notify.fix_steps`` to render the switch instruction for the operator.

Pure read, no network, no writes. Every disk/DB read is fail-soft — a missing
providers.yaml, a missing state.db, a missing table, or a missing row all
degrade to ``[]`` / zeros rather than raising, matching the discipline in
:mod:`mini_ork.recovery.retry_hint`.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Node types whose nodes write code. A node is a code node when its ``type`` is
# one of these, OR when its lane alias is shared with such a node in the same
# workflow (``prior_art_lens`` shares ``codex_lens`` with the implementer, so
# switching that alias moves the implementer too).
CODE_ROLES = {"implementer", "worker", "publisher", "rollback", "bdd_runner", "healer"}

_DEFAULT_CODE_LANES = "minimax,codex,deepseek,opus"


def code_lanes() -> list[str]:
    """The lane names allowed for code nodes — ``MO_CODE_LANES`` (comma list),
    default ``minimax,codex,deepseek,opus``. ``glm`` is never allowed for code
    (house policy: glm is analysis-only)."""
    raw = os.environ.get("MO_CODE_LANES", _DEFAULT_CODE_LANES)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _providers(home: Path) -> dict[str, Any]:
    """The effective ``providers.yaml`` ``providers:`` mapping (first hit wins):
    ``$MINI_ORK_PROVIDERS`` → ``<home>/config/providers.yaml`` →
    ``<root>/config/providers.yaml`` — the same shadowing order the dispatcher
    applies. ``{}`` when no file resolves or the document is malformed."""
    candidates: list[Path] = []
    if override := os.environ.get("MINI_ORK_PROVIDERS"):
        candidates.append(Path(override))
    if home:
        candidates.append(Path(home) / "config" / "providers.yaml")
    root = os.environ.get("MINI_ORK_ROOT")
    if root:
        candidates.append(Path(root) / "config" / "providers.yaml")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            import yaml
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, ImportError, ValueError):
            continue
        if isinstance(doc, dict):
            providers = doc.get("providers")
            if isinstance(providers, dict):
                return providers
    return {}


def known_lanes(home) -> list[str]:
    """Lane names from the effective providers.yaml (the providers: keys)."""
    return sorted(_providers(Path(home)).keys())


def _since(hours: int) -> str:
    when = datetime.now(timezone.utc) - timedelta(hours=hours)
    return when.strftime("%Y-%m-%dT%H:%M:%S")


def lane_health(db, lane, hours: int = 6) -> dict[str, Any]:
    """Health window for ``lane`` from ``llm_calls`` (``model_id = lane`` OR
    ``provider = lane``), the last ``hours`` hours.

    Returns ``{"ok", "quota", "auth", "other_fail", "last_error", "last_ts"}``.
    Fail-soft: a missing db/table/row returns the all-zero dict."""
    out: dict[str, Any] = {"ok": 0, "quota": 0, "auth": 0, "other_fail": 0,
                           "last_error": "", "last_ts": ""}
    if db is None:
        return out
    try:
        if not db.has_table("llm_calls"):
            return out
    except Exception:  # noqa: BLE001 — a dead handle is "no data", never a raise
        return out
    lane_l = str(lane or "").lower()
    try:
        rows = db.rows(
            "SELECT status, error_category, error_message, ts FROM llm_calls "
            "WHERE ts >= ? AND (lower(model_id) = ? OR lower(provider) = ?) "
            "ORDER BY ts ASC",
            (_since(hours), lane_l, lane_l),
        )
    except Exception:  # noqa: BLE001 — schema drift: never raise
        return out
    last_error = ""
    last_ts = ""
    for r in rows:
        status = str(r.get("status") or "")
        cat = str(r.get("error_category") or "")
        ts = str(r.get("ts") or "")
        err = str(r.get("error_message") or "")
        if status == "success":
            out["ok"] += 1
        elif cat == "quota":
            out["quota"] += 1
        elif cat == "auth":
            out["auth"] += 1
        else:
            out["other_fail"] += 1
        if ts:
            last_ts = ts
        if err:
            last_error = err
    out["last_error"] = last_error
    out["last_ts"] = last_ts
    return out


def suggest(home, *, failed_lane, alias, node_types, db=None,
            limit: int = 3) -> list[dict[str, str]]:
    """Candidate lanes to switch ``alias`` to, best first.

    * candidates = ``known_lanes`` minus ``failed_lane``, restricted to
      ``code_lanes()`` when any of ``node_types`` is a code node;
    * drop lanes with any quota/auth failure in the window;
    * rank tested lanes first; for code, in ``MO_CODE_LANES`` order (the house
      preference: opus, the reviewer family, last); then ``ok`` desc, fewer
      ``other_fail``, name; unseen lanes (no recent calls) rank last;
    * return ``[{"lane", "reason"}]`` (≤ ``limit``), e.g.
      ``"14 successful calls in the last 6h"`` / ``"no recent calls (untested)"``.
    """
    failed_lane = str(failed_lane or "")
    alias = str(alias or "")
    del alias  # kept in the signature for the caller's clarity; unused here
    node_types = list(node_types or [])
    is_code = any(nt in CODE_ROLES for nt in node_types)

    known = known_lanes(home)
    candidates = [ln for ln in known if ln != failed_lane]
    if is_code:
        allowed = set(code_lanes())
        candidates = [ln for ln in candidates if ln in allowed]

    scored: list[tuple[str, dict[str, Any]]] = []
    for ln in candidates:
        h = lane_health(db, ln, hours=6)
        if h["quota"] or h["auth"]:
            continue  # unhealthy — drop
        scored.append((ln, h))

    # For code, the MO_CODE_LANES order is the house preference (implementer on
    # a different family than the Opus reviewer, opus last): it outranks raw
    # success counts among healthy lanes. Untested lanes still rank last.
    policy = {ln: i for i, ln in enumerate(code_lanes())} if is_code else {}

    def _key(item: tuple[str, dict[str, Any]]):
        ln, h = item
        unseen = h["ok"] == 0 and h["other_fail"] == 0
        return (unseen, policy.get(ln, len(policy)), -int(h["ok"]), int(h["other_fail"]), ln)

    scored.sort(key=_key)
    out: list[dict[str, str]] = []
    for ln, h in scored[:limit]:
        if h["ok"] == 0 and h["other_fail"] == 0:
            reason = "no recent calls (untested)"
        else:
            reason = f"{h['ok']} successful calls in the last 6h"
        out.append({"lane": ln, "reason": reason})
    return out
