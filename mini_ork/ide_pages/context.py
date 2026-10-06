"""Context (ContextNest) — what each agent is given to read, and why.

The context pack is the planner's ``context-pack.json`` from the newest run
that has one: every item carries the ``cite`` tag that put it there.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mini_ork.ide_pages import spec as S

_PACK_ROWS = 30
_PACK_SCAN = 40

# context-pack.json key → the kind shown in the table.
_KINDS = {
    "task_brief": "brief",
    "verifier_contract": "contract",
    "prior_similar_runs": "prior run",
    "known_failure_modes": "failure mode",
    "verified_emergent_patterns": "pattern",
    "similar_lessons": "lesson",
    "user_preferences": "preference",
    "constraints": "constraint",
    "forbidden_fallbacks": "forbidden",
    "linked_gradients": "gradient",
}
_WHY_KEYS = ("title", "signal", "cluster_label", "suggested_change", "suggested_fix", "text",
             "summary", "target")


def _rows(home: Path, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    db = home / "state.db"
    if not db.is_file():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def _packs(home: Path, limit: int) -> list[tuple[str, Path]]:
    """``(run_id, context-pack.json)`` for the newest runs that have one."""
    out = []
    for r in _rows(home, "SELECT id FROM task_runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                   (_PACK_SCAN,)):
        path = home / "runs" / str(r["id"]) / "context-pack.json"
        if path.is_file():
            out.append((str(r["id"]), path))
            if len(out) >= limit:
                break
    return out


def _short(run_id: str) -> str:
    parts = run_id.split("-")
    if len(parts) == 3 and parts[0] == "run" and parts[1].isdigit():
        return f"run-{parts[2][:6]}"
    return run_id if len(run_id) <= 32 else run_id[:31] + "…"


def _why(entry: dict[str, Any]) -> str:
    for key in _WHY_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:160]
    if entry.get("status"):
        cost = entry.get("cost_usd")
        extra = f" · {S.money(cost)}" if isinstance(cost, (int, float)) and cost else ""
        return f"{entry['status']}{extra}"
    content = entry.get("content")
    if isinstance(content, dict):
        kickoff = content.get("kickoff")
        if isinstance(kickoff, str) and kickoff.strip():
            first = next((ln.strip().lstrip("#").strip() for ln in kickoff.splitlines() if ln.strip()), "")
            return f"the kickoff · {first}"[:160]
        return f"{len(content)} field(s)" if content else "empty"
    return ""


def _pack_rows(pack: dict[str, Any]) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []

    def add(kind: str, entry: Any) -> None:
        if isinstance(entry, dict):
            cite = entry.get("cite")
            if cite:
                rows.append([S.mono(f"[{cite}]"), S.muted(kind), _why(entry) or "—"])
            # nested lists (graph_context.linked_gradients, …)
            for key, value in entry.items():
                if isinstance(value, list) and key != "cite":
                    for item in value:
                        if isinstance(item, dict) and item.get("cite"):
                            add(_KINDS.get(key, key.replace("_", " ")), item)

    for key, value in pack.items():
        kind = _KINDS.get(key, key.replace("_", " "))
        if isinstance(value, list):
            for item in value:
                add(kind, item)
        elif isinstance(value, dict):
            add(kind, value)
    return rows


def _context_pack(home: Path) -> dict[str, Any]:
    found = _packs(home, 1)
    if not found:
        return S.table("Context pack", [S.col(150), S.col(90), S.col(fr=1)],
                       ["cite", "kind", "why it was included"],
                       [[S.muted("No run has assembled a context pack yet"), "", ""]], full=True)
    run_id, path = found[0]
    pack = json.loads(path.read_text(encoding="utf-8"))
    node = pack.get("workflow_node") or "planner"
    rows = _pack_rows(pack)
    note = f"{len(rows)} cited item(s)"
    if isinstance(pack.get("tokens_estimated"), (int, float)):
        budget = pack.get("budget_tokens")
        note += f" · ~{int(pack['tokens_estimated']):,} tokens"
        if isinstance(budget, (int, float)):
            note += f" of a {int(budget):,} budget"
    if len(rows) > _PACK_ROWS:
        note += f" · showing {_PACK_ROWS}"
    return S.table(f"Context pack · {_short(run_id)} {node}", [S.col(150), S.col(90), S.col(fr=1)],
                   ["cite", "kind", "why it was included"],
                   rows[:_PACK_ROWS] or [[S.muted("The pack cites nothing"), "", ""]], full=True,
                   note=note, actions=[S.btn("Open pack", S.open_path(str(path)), "ghost"),
                                       S.btn("Open run", S.open_run(run_id), "ghost")])


def _prefetch(home: Path, cn_up: bool, cn_disabled: bool) -> dict[str, Any]:
    items = []
    packs = _packs(home, 1)
    if packs:
        run_id, _ = packs[0]
        items.append(S.ok("Planner pre-fetch", f"context assembler → context-pack.json · latest {_short(run_id)}"))
    else:
        items.append(S.dot("Planner pre-fetch", "context assembler writes context-pack.json; no run has one yet"))
    if cn_disabled:
        items.append(S.warn("ContextNest packs off", "MO_DISABLE_CN=1 — role packs and capsules degrade to empty"))
    elif cn_up:
        items.append(S.ok("Planner role pack", "ContextNest capsule + atoms appended to the planner prompt"))
    else:
        items.append(S.warn("Planner role pack", "ContextNest unreachable — the pack degrades to empty"))
    use_packs = os.environ.get("MO_USE_ROLE_PACKS", "1") == "1"
    items.append(S.warn("Role packs for other roles",
                        ("wired (MO_USE_ROLE_PACKS=1), not yet implemented — implementer, reviewer and "
                         "verifier prompts get none") if use_packs else "off (MO_USE_ROLE_PACKS=0)"))
    items.append(S.dot("Prompt pre-fetch hook", "mini_ork.cli.cn_hook prefetch — called by the ContextNest "
                                                "shell hooks configured in your agent"))
    return S.lst("Pre-fetch & hooks", items)


def _cache(home: Path) -> dict[str, Any]:
    items: list[tuple] = []
    stage = _rows(home, "SELECT COUNT(*) AS n, COALESCE(SUM(reused_count), 0) AS hits FROM mini_orch_sessions")
    if stage:
        hits = int(stage[0]["hits"] or 0)
        items.append(("Stage memo hits", f"{hits:,}", "green" if hits else "text",
                      f"{int(stage[0]['n'] or 0):,} cached stages"))
    week = time.time() - 7 * 86400
    uses = _rows(home, "SELECT COUNT(*) AS n FROM semantic_memory_uses WHERE retrieved_at >= ?", (week,))
    if uses:
        items.append(("Memory retrievals · 7d", f"{int(uses[0]['n'] or 0):,}", "text", "semantic_memory_uses"))
    sizes = []
    for _run_id, path in _packs(home, 10):
        try:
            tokens = json.loads(path.read_text(encoding="utf-8")).get("tokens_estimated")
        except (OSError, ValueError):
            continue
        if isinstance(tokens, (int, float)):
            sizes.append(float(tokens))
    if sizes:
        avg = sum(sizes) / len(sizes)
        items.append(("Avg pack size", f"{avg / 1000:.1f}K tokens", "text", f"last {len(sizes)} pack(s)"))
    if not items:
        items.append(("Counters", "none yet", "sub"))
    return S.kv("Cache", items)


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    del tab
    cn_disabled = os.environ.get("MO_DISABLE_CN", "0") == "1"
    base = os.environ.get("CN_BASE_URL", "http://127.0.0.1:28080")
    netloc = urlparse(base).netloc or base
    cn_up = False
    if not cn_disabled:
        os.environ["CN_TIMEOUT_SEC"] = "0.5"
        if args.get("ping"):
            os.environ["CN_PING_TTL"] = "0"  # a fresh probe, not the cached one
        try:
            from mini_ork import cn_client

            cn_up = bool(cn_client.available())
        except Exception:  # noqa: BLE001
            cn_up = False
    if cn_disabled:
        chip = S.chip("disabled · MO_DISABLE_CN=1", "sub")
    elif cn_up:
        chip = S.chip(f"connected · {netloc}", "green")
    else:
        chip = S.chip(f"not reachable · {netloc}", "yellow")
    errors: dict[str, str] = {}
    sections: list[dict[str, Any]] = []
    sections += S.guarded(errors, "Context pack", lambda: _context_pack(home))
    sections += S.guarded(errors, "Pre-fetch & hooks", lambda: _prefetch(home, cn_up, cn_disabled))
    sections += S.guarded(errors, "Cache", lambda: _cache(home))
    return S.page("context", "Context (ContextNest)",
                  "What each agent is given to read, with the cite tag that put it there.",
                  chips_=[chip], actions=[S.btn("Ping", S.set_args(ping=str(int(time.time()))), "ghost")],
                  args=args, sections=sections, errors=errors)
