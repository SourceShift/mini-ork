"""Models, lanes & cost — which model does what, how work escalates, where the money goes.

Everything is read from the project's own config (``agents.yaml``,
``providers.yaml``) and ledger (``llm_calls``). Credential *names* are shown,
never values.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

TITLE = "Models, lanes & cost"
SUB = ("Which model does what, how work escalates, and where the money goes. "
       "The ledger is billed envelopes, not estimates.")
TABS = [("lanes", "Lanes & providers"), ("routing", "Routing"), ("cost", "Cost & budget")]

_LEARNING = {"gradient-extract", "pattern-induct", "reflect"}
_NATIVE_KINDS = {"anthropic-native", "codex-native", "opencode-native", "executable-script"}


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    tab = tab if tab in dict(TABS) else TABS[0][0]
    errors: dict[str, str] = {}
    spent = _spent_today(home)
    chips = [S.chip(f"{S.money(spent)} today")]
    if tab == "lanes":
        sections = (S.guarded(errors, "Lanes", lambda: _lanes_table(home))
                    + S.guarded(errors, "Credentials", lambda: _credentials(home))
                    + S.guarded(errors, "Bring your own provider", lambda: _byo(home)))
    elif tab == "routing":
        sections = (S.guarded(errors, "Role → lane ladder", lambda: _ladder(home))
                    + S.guarded(errors, "GRPO bandit · implementer arms", lambda: _bandit(home))
                    + S.guarded(errors, "Router calibration", lambda: _calibration(home))
                    + S.guarded(errors, "Cost advisor · per turn", lambda: _advisor(home)))
    else:
        sections = (S.guarded(errors, "Budget caps", lambda: _caps(home, spent))
                    + S.guarded(errors, "Today by stage", lambda: _by_stage(home))
                    + S.guarded(errors, "Last 7 days", lambda: _last_7_days(home))
                    + S.guarded(errors, "Guards", lambda: _guards(home))
                    + S.guarded(errors, "Ledger · latest calls", lambda: _ledger(home)))
    return S.page("lanes", TITLE, SUB, chips_=chips, actions=[], tabs=TABS, tab=tab, args=args,
                  sections=sections, errors=errors)


# ── config (read-only mirrors of what dispatch reads) ──────────────────────

def _root() -> Path:
    from mini_ork.dispatch.providers import mini_ork_root

    return mini_ork_root()


def _yaml(path: Path) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _agents(home: Path) -> dict[str, Any]:
    """agents.yaml as dispatch sees it: HOME's, else ROOT's, plus the personal overlay."""
    from mini_ork.dispatch import agents_config

    path = home / "config" / "agents.yaml"
    if not path.is_file():
        path = _root() / "config" / "agents.yaml"
    base = _yaml(path) if path.is_file() else {}
    overlay = agents_config.personal_path(home=str(home))
    return agents_config.merge(base, _yaml(Path(overlay))) if overlay else base


def _providers_path(home: Path) -> Path | None:
    candidates = []
    if os.environ.get("MINI_ORK_PROVIDERS"):
        candidates.append(Path(os.environ["MINI_ORK_PROVIDERS"]))
    candidates += [home / "config" / "providers.yaml", _root() / "config" / "providers.yaml"]
    return next((p for p in candidates if p.is_file()), None)


def _providers(home: Path) -> dict[str, dict[str, Any]]:
    """The first providers.yaml that exists — the same shadowing dispatch applies."""
    path = _providers_path(home)
    if path is None:
        return {}
    providers = _yaml(path).get("providers") or {}
    return {str(k): v for k, v in providers.items() if isinstance(v, dict)}


def _chains(home: Path) -> dict[str, list[str]]:
    lanes = _agents(home).get("lanes") or {}
    out: dict[str, list[str]] = {}
    for role, value in lanes.items():
        chain = [p.strip() for p in str(value or "").split(",") if p.strip()]
        if chain:
            out[str(role)] = chain
    return out


def _db(home: Path):
    from mini_ork.web.deps import db_for

    return db_for(home)


def _since(hours: int) -> str:
    when = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)
    return when.strftime("%Y-%m-%dT%H:%M:%S")


def _spent_today(home: Path) -> float:
    from mini_ork import cost_ledger

    db = home / "state.db"
    return cost_ledger.spent_last_24h(db if db.is_file() else None)


def _cap() -> float:
    try:
        return float(os.environ.get("MO_DAILY_BUDGET_USD", "50") or 50)
    except ValueError:
        return 50.0


# ── Lanes & providers ──────────────────────────────────────────────────────

def _secret_names(home: Path) -> tuple[set[str], str]:
    """Credential names with a non-empty value in the env or the project's secret store."""
    from mini_ork.dispatch.secrets import read_secret_exports, secret_store_path

    env = {"MINI_ORK_HOME": str(home), "MINI_ORK_SECRETS": os.environ.get("MINI_ORK_SECRETS", "")}
    store = secret_store_path(env)
    names = {k for k, v in os.environ.items() if v}
    try:
        names |= {k for k, v in read_secret_exports(store).items() if v}
    except Exception as exc:  # noqa: BLE001 — an unreadable store is a finding, not a crash
        return names, f"{type(exc).__name__}: {exc}"
    return names, ""


def _probe_cache(home: Path) -> dict[str, Any]:
    try:
        data = json.loads((home / "lane-probe-cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _calls_by_lane(home: Path, providers: dict[str, dict[str, Any]]) -> dict[str, dict[str, float]]:
    db = _db(home)
    if not db.has_table("llm_calls"):
        return {}
    rows = db.rows(
        "SELECT provider, model_id, status, COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS usd "
        "FROM llm_calls WHERE ts >= ? GROUP BY provider, model_id, status", (_since(24),))
    out: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "usd": 0.0, "errors": 0})
    for lane, entry in providers.items():
        names = {lane.lower(), str(entry.get("model") or "").lower()} - {""}
        for r in rows:
            model = str(r.get("model_id") or "").lower()
            provider = str(r.get("provider") or "").lower()
            if model in names or provider == lane.lower():
                bucket = out[lane]
                bucket["calls"] += int(r.get("n") or 0)
                bucket["usd"] += float(r.get("usd") or 0.0)
                if str(r.get("status") or "success") != "success":
                    bucket["errors"] += int(r.get("n") or 0)
    return out


def _lanes_table(home: Path) -> dict[str, Any]:
    providers = _providers(home)
    chains = _chains(home)
    primary: dict[str, list[str]] = defaultdict(list)
    retry: dict[str, list[str]] = defaultdict(list)
    for role, chain in chains.items():
        primary[chain[0]].append(role)
        for lane in chain[1:]:
            retry[lane].append(role)
    names, _ = _secret_names(home)
    probes = _probe_cache(home)
    calls = _calls_by_lane(home, providers)
    cols = [S.col(90), S.col(fr=1, min=140), S.col(fr=1, min=120), S.col(50), S.col(54), S.col(80)]
    head = ["lane", "provider", "roles", "calls", "today", "state"]
    if not providers:
        return S.table("Lanes", cols, head, [[S.muted("No providers.yaml found"), "", "", "", "", ""]],
                       full=True)
    ordered = sorted(providers, key=lambda lane: (not primary.get(lane) and not retry.get(lane), lane))
    rows = []
    for lane in ordered:
        entry = providers[lane]
        kind = str(entry.get("kind") or "?")
        model = str(entry.get("model") or "")
        provider_text = " · ".join(p for p in (kind, model) if p)
        roles = primary.get(lane, [])
        roles_text = ", ".join(roles[:3]) + (f" +{len(roles) - 3}" if len(roles) > 3 else "")
        if not roles and retry.get(lane):
            roles_text = f"retry for {len(retry[lane])} role{'s' if len(retry[lane]) != 1 else ''}"
        stats = calls.get(lane, {"calls": 0, "usd": 0.0, "errors": 0})
        rows.append([S.cell(lane, f"fam:{lane}", b=True), S.muted(provider_text), roles_text or S.muted("—"),
                     S.mono(int(stats["calls"])),
                     S.mono(S.money(stats["usd"]) if stats["calls"] else "—"),
                     _state(entry, names, probes.get(lane), int(stats["errors"]))])
    return S.table("Lanes", cols, head, rows, full=True,
                   note="Roles map to lanes in .mini-ork/config/agents.yaml. "
                        "Swap vendors without touching workflows. Calls and spend cover the last 24 h.")


def _state(entry: dict[str, Any], names: set[str], probe: Any, errors: int) -> dict[str, Any]:
    key = entry.get("api_key_env")
    if isinstance(key, str) and key and key not in names:
        return S.cell("no key", "red")
    if isinstance(probe, dict) and probe.get("dead"):
        return S.cell(str(probe["dead"]).split(" (")[0], "red")
    if errors:
        return S.cell(f"{errors} error{'s' if errors != 1 else ''}", "yellow")
    return S.cell("ok", "green")


def _credentials(home: Path) -> dict[str, Any]:
    providers = _providers(home)
    names, store_error = _secret_names(home)
    declared: dict[str, list[str]] = defaultdict(list)
    native: list[str] = []
    for lane, entry in providers.items():
        key = entry.get("api_key_env")
        if isinstance(key, str) and key:
            declared[key].append(lane)
        elif str(entry.get("kind") or "") in _NATIVE_KINDS:
            native.append(lane)
    items = []
    if native:
        items.append(S.dot(f"Local logins: {', '.join(sorted(native))}",
                           "native CLIs (claude, codex, opencode) use their own sign-in"))
    present = sorted(k for k in declared if k in names)
    if present:
        items.append(S.ok(", ".join(present), "secrets.local.sh or the environment · values never shown"))
    for key in sorted(k for k in declared if k not in names):
        lanes = ", ".join(sorted(declared[key]))
        items.append(S.bad(f"{key} missing", f"lane {lanes} disabled · "
                                             f"run mini-ork providers configure {declared[key][0]} in a terminal",
                           [S.btn("Configure", None, "primary")]))
    if store_error:
        items.append(S.warn("Secret store unreadable", store_error))
    if not items:
        items.append(S.dot("No providers declare credentials"))
    return S.lst("Credentials", items, full=True,
                 note="mini-ork providers configure <lane> prompts securely; keys never pass as CLI flags.")


def _byo(home: Path) -> dict[str, Any]:
    from mini_ork.dispatch.providers import PROVIDER_KIND_BUILDERS

    path = _providers_path(home) or (home / "config" / "providers.yaml")
    kinds = ", ".join(sorted(PROVIDER_KIND_BUILDERS))
    act = [S.btn("Add provider", S.open_path(str(path)))] if path.is_file() else []
    return S.lst("Bring your own provider", [S.dot("providers.yaml", f"{path} · kinds: {kinds}", act)],
                 full=True)


# ── Routing ────────────────────────────────────────────────────────────────

def _ladder(home: Path) -> dict[str, Any]:
    chains = _chains(home)
    rows = [[S.mono(role), " → ".join(chain)] for role, chain in sorted(chains.items())]
    if not rows:
        rows = [[S.muted("No lanes in agents.yaml"), ""]]
    return S.table("Role → lane ladder", [S.col(110), S.col(fr=1, min=0)], ["role", "starts on → escalates to"],
                   rows, note="From agents.yaml lanes: a comma list is the ladder — the first lane runs, "
                              "the next takes over when it fails.")


def _bandit(home: Path) -> dict[str, Any]:
    db = _db(home)
    rows = db.rows(
        "SELECT model, SUM(runs_count) AS runs, SUM(success_count) AS ok, "
        "SUM(relative_advantage * runs_count) AS adv FROM agent_performance_memory "
        "WHERE role = 'implementer' GROUP BY model ORDER BY runs DESC LIMIT 6") \
        if db.has_table("agent_performance_memory") else []
    total = sum(int(r.get("runs") or 0) for r in rows)
    if not total:
        return S.lst("GRPO bandit · implementer arms", [S.dot("No implementer runs recorded yet")])
    items = []
    for r in rows:
        runs = int(r.get("runs") or 0)
        lane = str(r.get("model") or "?")
        adv = float(r.get("adv") or 0.0) / runs if runs else 0.0
        items.append((lane, 100 * runs / total, f"{runs} runs · adv {adv:+.2f}", f"fam:{lane}"))
    return S.bars("GRPO bandit · implementer arms", items,
                  note="Share of implementer runs per arm; adv is the run-weighted relative advantage.")


def _calibration(home: Path) -> dict[str, Any]:
    db = _db(home)

    def count(table: str) -> str:
        if not db.has_table(table):
            return "—"
        row = db.rows(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608 — fixed table names
        return f"{int(row[0]['n']):,}" if row else "0"

    return S.kv("Router calibration", [
        ("Policy", os.environ.get("MO_ROUTING_POLICY") or "default"),
        ("Arm records", count("agent_performance_memory")),
        ("Domain slices", count("lane_domain_advantage")),
        ("Region slices", count("lane_region_advantage")),
    ])


def _advisor(home: Path) -> dict[str, Any]:
    path = home / "config" / "cost-advisor.yaml"
    acts = [S.btn("Open", S.open_path(str(path)), "ghost")] if path.is_file() else []
    sub = ("per-turn decisions are not logged; cost-advisor.yaml sets the policy"
           if path.is_file() else "no cost-advisor.yaml in this project")
    return S.lst("Cost advisor · per turn", [S.dot("Not recorded", sub, acts)])


# ── Cost & budget ──────────────────────────────────────────────────────────

def _caps(home: Path, spent: float) -> dict[str, Any]:
    cap = _cap()
    budget = _agents(home).get("budget") or {}
    ratio = spent / cap if cap else 0.0
    colour = "red" if ratio >= 1 else "yellow" if ratio >= 0.8 else "green"
    per_run = budget.get("per_run_usd")
    per_epic = budget.get("per_epic_usd")
    db = _db(home)
    cache = db.rows("SELECT COALESCE(SUM(times_reused),0) AS hits, COALESCE(SUM(dollars_saved),0) AS saved "
                    "FROM mini_orch_cache_stats") if db.has_table("mini_orch_cache_stats") else []
    hits = int(cache[0]["hits"]) if cache else 0
    saved = float(cache[0]["saved"]) if cache else 0.0
    return S.kv("Budget caps", [
        ("Today", f"{S.money(spent)} of {S.money(cap)}", colour, "MO_DAILY_BUDGET_USD · last 24 h"),
        ("Per run", S.money(per_run) if per_run is not None else "—", "text", "agents.yaml budget.per_run_usd"),
        ("Per epic", S.money(per_epic) if per_epic is not None else "—", "text", "agents.yaml budget.per_epic_usd"),
        ("Stage cache", f"{hits} hits", "green" if hits else "text", f"saved {S.money(saved)}"),
    ], full=True)


def _stage(feature: str) -> str:
    name = feature.split(":", 1)[-1] if feature else ""
    return "learning" if name in _LEARNING else (name or "other")


def _by_stage(home: Path) -> dict[str, Any]:
    db = _db(home)
    rows = db.rows("SELECT feature_name, COALESCE(SUM(cost_usd),0) AS usd FROM llm_calls "
                   "WHERE ts >= ? GROUP BY feature_name", (_since(24),)) if db.has_table("llm_calls") else []
    totals: dict[str, float] = defaultdict(float)
    for r in rows:
        totals[_stage(str(r.get("feature_name") or ""))] += float(r.get("usd") or 0.0)
    totals = {k: v for k, v in totals.items() if v > 0}
    if not totals:
        return S.lst("Today by stage", [S.dot("No spend in the last 24 h")])
    top = max(totals.values())
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])[:8]
    return S.bars("Today by stage", [
        (stage, 100 * usd / top, S.money(usd), "purple" if stage == "learning" else "blue")
        for stage, usd in ranked])


def _last_7_days(home: Path) -> dict[str, Any]:
    db = _db(home)
    rows = db.rows(
        "SELECT ts, cost_usd FROM llm_calls WHERE ts >= ?", (_since(24 * 8),)) \
        if db.has_table("llm_calls") else []
    today = _dt.date.today()
    days = [today - _dt.timedelta(days=i) for i in range(6, -1, -1)]
    totals = {d: 0.0 for d in days}
    for r in rows:
        try:
            when = _dt.datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")).astimezone().date()
        except (ValueError, TypeError):
            continue
        if when in totals:
            totals[when] += float(r.get("cost_usd") or 0.0)
    top = max(totals.values()) or 1.0
    return S.bars("Last 7 days", [
        ("Today" if d == today else d.strftime("%a"), 100 * totals[d] / top, S.money(totals[d]))
        for d in days])


def _guards(home: Path) -> dict[str, Any]:
    from mini_ork import scheduler

    db = _db(home)
    items = []
    if db.has_table("circuit_breaker_state"):
        rows = db.rows("SELECT scope_key, state, last_reason FROM circuit_breaker_state "
                       "WHERE state != 'CLOSED' ORDER BY updated_at DESC")
        total = db.rows("SELECT COUNT(*) AS n FROM circuit_breaker_state")
        if rows:
            items.append(S.warn(f"Circuit breaker open on {len(rows)} scope{'s' if len(rows) != 1 else ''}",
                                " · ".join(f"{r['scope_key']} ({r.get('last_reason') or r['state']})"
                                           for r in rows[:3])))
        else:
            items.append(S.ok("Circuit breaker armed",
                              f"{int(total[0]['n']) if total else 0} scopes watched · none open"))
    if scheduler.cost_pause_active(str(home)):
        items.append(S.warn("Cost pause active", "dispatch halts until the pause is lifted"))
    else:
        items.append(S.ok("No cost pause", f"dispatch halts once the last 24 h pass {S.money(_cap())}"))
    items.append(S.dot("Dry run", "MINI_ORK_DRY_RUN=1 walks the whole pipeline with zero LLM calls"))
    return S.lst("Guards", items)


def _tokens(n: Any) -> str:
    try:
        v = int(n or 0)
    except (TypeError, ValueError):
        return "—"
    return f"{v / 1000:.1f}K" if v >= 1000 else str(v)


def _ledger(home: Path) -> dict[str, Any]:
    db = _db(home)
    rows = db.rows("SELECT run_id, provider, model_id, feature_name, total_tokens, cost_usd FROM llm_calls "
                   "ORDER BY id DESC LIMIT 12") if db.has_table("llm_calls") else []
    cols = [S.col(fr=1, min=140), S.col(80), S.col(fr=1, min=100), S.col(60), S.col(56)]
    head = ["run", "lane", "node", "tokens", "cost"]
    if not rows:
        out = [[S.muted("No calls recorded"), "", "", "", ""]]
    else:
        out = []
        for r in rows:
            provider = str(r.get("provider") or "")
            lane = str(r.get("model_id") or "") if provider == "gateway" else provider
            out.append({"cells": [S.mono(r.get("run_id") or "—"), S.cell(lane, f"fam:{lane}"),
                                  str(r.get("feature_name") or "").split(":", 1)[-1],
                                  S.mono(_tokens(r.get("total_tokens"))), S.mono(S.money(r.get("cost_usd")))],
                        "do": (S.open_run(str(r["run_id"]))
                               if r.get("run_id") and (home / "runs" / str(r["run_id"])).is_dir()
                               else None)})
    return S.table("Ledger · latest calls", cols, head, out,
                   actions=[S.btn("Usage report", S.cli("usage-report"), "ghost")])
