"""Verification & safety — certify, gates, panel independence, human gates, autonomy.

Everything is read from the project's ``state.db``, its ``certificates/`` and
``gate-hackability/`` folders, the engine's recipes and ``docs/SAFETY.md``.
A source that is missing gives an honest empty state, never a made-up number.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

TABS = [("certify", "Certify"), ("gates", "Gates & verifiers"), ("panels", "Panel independence"),
        ("inbox", "Human gates"), ("autonomy", "Autonomy & probes"), ("bugs", "Bug reports")]

DAY = 86400
_CERT_LIMIT = 10
_VERIFIER_LIMIT = 40
_CHIP_LIMIT = 14


# ── shared readers ─────────────────────────────────────────────────────────

def _db(home: Path):
    from mini_ork.web.db import db_for

    if not (home / "state.db").is_file():
        return None
    return db_for(home)


def _rows(db: Any, table: str, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    if db is None or not db.has_table(table):
        return []
    return db.rows(sql, params)


def _json(text: Any) -> dict[str, Any]:
    try:
        out = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _engine_root() -> Path:
    from mini_ork.web.recipes import mini_ork_root

    return mini_ork_root()


def _short(run_id: Any) -> str:
    text = str(run_id or "")
    return text if len(text) <= 24 else text[:10] + "…" + text[-8:]


# ── certify ────────────────────────────────────────────────────────────────

def _certificates(home: Path) -> list[dict[str, Any]]:
    folder = home / "certificates"
    if not folder.is_dir():
        return []
    out = []
    for path in folder.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and str(data.get("schema", "")).startswith("mini-ork.certificate/"):
            data["_path"] = str(path)
            data["_mtime"] = path.stat().st_mtime
            out.append(data)
    out.sort(key=lambda c: c["_mtime"], reverse=True)
    return out[:_CERT_LIMIT]


_VERDICT_COLOUR = {"PROVEN": "green", "REFUTED": "red", "UNVERIFIED": "yellow"}
_VERDICT_EXIT = {"PROVEN": "exit 0", "REFUTED": "exit 1", "UNVERIFIED": "exit 2"}


def _certify_sections(home: Path, args: dict[str, str], errors: dict[str, str]) -> list[dict[str, Any]]:
    claim = (args.get("claim") or "").strip()
    certs: list[dict[str, Any]] = []

    def form() -> dict[str, Any]:
        prompt = f"/certify {claim}" if claim else "/certify "
        return S.kv("Certify", [
            ("Base", "HEAD~1", "text", "commit before the change"),
            ("Head", "HEAD", "text", "commit with the change"),
            ("Claim", claim or "— the bug report, typed in the thread", "text" if claim else "sub"),
        ], full=True,
            note="Proves a change fixes a bug: a probe that fails on base and passes on head, plus "
                 "adversarial invariants. Exit codes: 0 PROVEN · 1 REFUTED · 2 UNVERIFIED — CI can "
                 "gate on it. Any range: mini-ork certify --base B --head H --issue TEXT.",
            actions=[S.btn("Run certify", S.thread(prompt), "primary")])

    def latest() -> list[dict[str, Any]]:
        certs.extend(_certificates(home))
        if not certs:
            return []
        c = certs[0]
        verdict = str(c.get("verdict") or "—")
        evidence = c.get("evidence") or {}
        invariants = [i for i in evidence.get("invariants") or [] if isinstance(i, dict)]
        holding = sum(1 for i in invariants if i.get("holds"))
        method = c.get("method") or {}
        cost = (c.get("cost") or {}).get("usd")
        digest = str(c.get("digest") or "")
        out = [S.kv(f"Certificate {str(c.get('schema', '')).rsplit('/', 1)[-1]}", [
            ("Verdict", verdict, _VERDICT_COLOUR.get(verdict, "text"), _VERDICT_EXIT.get(verdict, "")),
            ("Invariants", f"{holding} of {len(invariants)} hold" if invariants else "none kept",
             "green" if invariants and holding == len(invariants) else "text"),
            ("Probe", "built" if evidence.get("probe") else "not built",
             "green" if evidence.get("probe") else "sub"),
            ("Model", method.get("model") or "—"),
            ("Cost", S.money(cost) if cost is not None else "—"),
            ("Digest", f"sha256:{digest[:4]}…{digest[-4:]}" if digest else "—", "muted"),
        ], full=True, note=str(c.get("reason") or ""),
            actions=[S.btn("Open JSON", S.open_path(c["_path"]), "ghost")])]
        probe = str(evidence.get("probe") or "")
        if probe:
            out.append(S.code("Reproduction probe", [(line, "body") for line in probe.splitlines()[:24]]))
        if invariants:
            out.append(S.lst("Adversarial invariants", [
                S.ok(i.get("mr") or "invariant", i.get("outcome") or "") if i.get("holds")
                else S.warn(i.get("mr") or "invariant", f"on patch: {i.get('on_patch') or '—'}")
                for i in invariants]))
        return out

    def recent() -> dict[str, Any]:
        now = time.time()
        rows = [[(c.get("claim") or {}).get("summary") or "—",
                 S.cell(c.get("verdict") or "—", _VERDICT_COLOUR.get(str(c.get("verdict")), "sub")),
                 S.muted(S.age(c["_mtime"], int(now)))] for c in certs]
        if not rows:
            rows = [[S.muted("No certificates yet — they land in .mini-ork/certificates/"), "", ""]]
        return S.table("Recent certificates", [S.col(fr=1, min=160), S.col(90), S.col(50)],
                       ["claim", "verdict", "age"],
                       [{"cells": r, "do": S.open_path(c["_path"]) if certs else None}
                        for r, c in zip(rows, certs or [{}])], full=True)

    return (S.guarded(errors, "Certify", form) + S.guarded(errors, "Certificate", latest)
            + S.guarded(errors, "Recent certificates", recent))


# ── gates & verifiers ──────────────────────────────────────────────────────

def _node_ends(db: Any, since: int) -> list[dict[str, Any]]:
    return _rows(db, "run_events",
                 "SELECT run_id, payload_json, created_at FROM run_events "
                 "WHERE event_type = 'node_end' AND created_at >= ? ORDER BY created_at DESC",
                 (since,))


def _gate_outcomes(home: Path) -> dict[str, Any]:
    db = _db(home)
    now = int(time.time())
    tally: dict[str, dict[str, Any]] = {
        g: {"pass": 0, "fail": 0, "pending": 0, "latest": ""} for g in
        ("deterministic_verifier", "reviewer_gate", "human_gate", "budget_gate",
         "deployment_gate", "liveness_gate")}

    def note(gate: str, passed: bool, latest: str) -> None:
        t = tally[gate]
        t["pass" if passed else "fail"] += 1
        # Newest first: the first failure wins, else the first outcome.
        if not t["latest"] or (not passed and not t.get("latest_is_fail")):
            t["latest"] = latest
            t["latest_is_fail"] = not passed

    for ev in _node_ends(db, now - DAY):
        p = _json(ev.get("payload_json"))
        node_type, node, reason = p.get("node_type"), p.get("node_id") or "?", p.get("finish_reason")
        run = _short(ev.get("run_id"))
        if reason == "cost_limit":
            note("budget_gate", False, f"{node} · {run} · cost limit")
        if reason == "timeout":
            note("liveness_gate", False, f"{node} · {run} · timeout")
        if node_type == "verifier":
            ok = reason == "done"
            note("deterministic_verifier", ok, f"{node} · {run}" + ("" if ok else f" · {reason}"))
        elif node_type == "reviewer":
            verdict = p.get("verdict") or ("pass" if reason == "done" else reason)
            note("reviewer_gate", verdict == "pass", f"{verdict} · {run}")
        elif node_type == "publisher":
            ok = reason == "done"
            note("deployment_gate", ok, f"publish · {run}" + ("" if ok else f" · {reason}"))
    for g in _rows(db, "mo_inbox_gates",
                   "SELECT gate_id, status, resolved_at, enqueued_at FROM mo_inbox_gates "
                   "WHERE status = 'pending' OR resolved_at >= ?", (now - DAY,)):
        t = tally["human_gate"]
        if g["status"] == "pending":
            t["pending"] += 1
            t["latest"] = t["latest"] or f"{g['gate_id']} waiting"
        else:
            t["pass" if g["status"] == "approved" else "fail"] += 1
    # Budget and liveness record only their trips; a pass leaves no event.
    for gate in ("budget_gate", "liveness_gate"):
        tally[gate]["pass"] = None
    rows = []
    for gate, t in tally.items():
        passed = "—" if t["pass"] is None else t["pass"]
        rows.append([S.cell(gate, "text"), S.mono(passed, "green" if t["pass"] else "sub"),
                     S.mono(t["fail"], "red" if t["fail"] else "sub"),
                     S.mono(t["pending"], "yellow" if t["pending"] else "sub"),
                     S.muted(t["latest"] or "—")])
    return S.table("Gate outcomes · last 24 h",
                   [S.col(fr=1, min=150), S.col(50), S.col(50), S.col(60), S.col(fr=1, min=120)],
                   ["gate", "pass", "fail", "pending", "latest"], rows, full=True,
                   note="From node_end events: verifiers, reviewers and the publisher; cost limits and "
                        "timeouts count against budget and liveness; human gates from mo_inbox_gates.")


def _specialised_gates(home: Path) -> dict[str, Any]:
    db = _db(home)
    items = []
    for g in _rows(db, "gate_registry",
                   "SELECT gate_id, gate_type, condition, safety, active FROM gate_registry "
                   "ORDER BY active DESC, gate_id"):
        cond = str(g.get("condition") or "")
        cond = cond.rsplit("/", 1)[-1] if "/" in cond else cond
        sub = f"{g.get('gate_type')} · {cond}" + (" · safety" if g.get("safety") else "")
        items.append(S.ok(g["gate_id"], sub) if g.get("active") else S.dot(g["gate_id"], sub + " · inactive"))
    if not items:
        items = [S.dot("No gates registered", "mini-ork gate register adds one to gate_registry")]
    return S.lst("Specialised gates", items, note="Rows of gate_registry, evaluated by mini_ork.gates.")


def _verifier_registry(home: Path) -> dict[str, Any]:
    from mini_ork import recipes_catalog

    db = _db(home)
    scopes: dict[str, list[str]] = {}
    nodes: dict[tuple[str, str], str] = {}
    for entry in recipes_catalog.list_recipes(home):
        contract = _yaml(entry.path / "artifact_contract.yaml")
        for ref in contract.get("success_verifiers") or []:
            name = str(ref).rsplit("/", 1)[-1]
            scopes.setdefault(name, []).append(entry.id)
        for node in _yaml(entry.path / "workflow.yaml").get("nodes") or []:
            if isinstance(node, dict) and node.get("verifier_ref"):
                nodes[(entry.id, str(node["verifier_ref"]).rsplit("/", 1)[-1])] = str(node.get("name"))
    # Latest verifier outcome per (recipe, node), newest first.
    latest: dict[tuple[str, str], str] = {}
    for ev in _rows(db, "run_events",
                    "SELECT re.payload_json, tr.recipe FROM run_events re "
                    "JOIN task_runs tr ON tr.id = re.run_id "
                    "WHERE re.event_type = 'node_end' AND re.created_at >= ? "
                    "ORDER BY re.created_at DESC", (int(time.time()) - 30 * DAY,)):
        p = _json(ev.get("payload_json"))
        if p.get("node_type") != "verifier":
            continue
        key = (str(ev.get("recipe")), str(p.get("node_id")))
        latest.setdefault(key, "pass" if p.get("finish_reason") == "done" else "fail")
    rows = []
    for name, recipes in sorted(scopes.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:_VERIFIER_LIMIT]:
        last = next((latest[(r, nodes[(r, name)])] for r in recipes
                     if (r, name) in nodes and (r, nodes[(r, name)]) in latest), None)
        scope = ", ".join(recipes[:3]) + (f" +{len(recipes) - 3}" if len(recipes) > 3 else "")
        rows.append([S.mono(name), S.muted(scope),
                     S.cell(last or "—", {"pass": "green", "fail": "red"}.get(last or "", "sub"))])
    if not rows:
        rows = [[S.muted("No recipe declares success_verifiers"), "", ""]]
    return S.table("Verifier registry", [S.col(fr=1, min=140), S.col(fr=1, min=120), S.col(60)],
                   ["verifier", "scope", "last"], rows,
                   note="success_verifiers of every recipe; last = newest outcome in 30 days.")


def _minimum_evidence(home: Path) -> dict[str, Any]:
    from mini_ork import recipes_catalog

    db = _db(home)
    vacuous = {e.id for e in recipes_catalog.list_recipes(home)
               if not (_yaml(e.path / "artifact_contract.yaml").get("success_verifiers") or [])}
    counts: dict[str, int] = {}
    for r in _rows(db, "task_runs",
                   "SELECT recipe, COUNT(*) AS n FROM task_runs WHERE status = 'published' "
                   "AND created_at >= ? GROUP BY recipe", (int(time.time()) - 7 * DAY,)):
        if r.get("recipe") in vacuous:
            counts[str(r["recipe"])] = int(r.get("n") or 0)
    if not counts:
        items = [S.ok("Every run published this week declared verifiers",
                      f"{len(vacuous)} recipes declare none; none of them published this week")]
    else:
        total = sum(counts.values())
        items = [S.warn(f"{total} run{'s' if total != 1 else ''} this week passed with no verifiers",
                        f"{', '.join(sorted(counts))} — 0 success_verifiers declared, so it is not a pass")]
        items += [S.dot(r, f"{n} published · no success_verifiers",
                        [S.btn("Add success_verifiers", S.thread(f"/recipe edit {r} add success_verifiers"))])
                  for r, n in sorted(counts.items(), key=lambda kv: -kv[1])[:5]]
    return S.lst("Minimum evidence", items, full=True)


def _yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml

        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)  # noqa: S506 — a safe loader
    except Exception:  # noqa: BLE001 — a broken recipe file costs its row only
        return {}
    return data if isinstance(data, dict) else {}


# ── panel independence ─────────────────────────────────────────────────────

_COALITION = {"heterogeneous": "green", "low": "green", "medium": "yellow", "high": "red"}


def _families(fp: dict[str, Any]) -> dict[str, int]:
    return {str(k): int(v) for k, v in (fp.get("families_used") or {}).items()}


def _fam_colour(family: str) -> str:
    return "fam:" + family.split(",")[0].strip() if family else "sub"


def _panel_sections(home: Path, args: dict[str, str], errors: dict[str, str]) -> list[dict[str, Any]]:
    from mini_ork.web import recipes as web_recipes

    fps: dict[str, dict[str, Any]] = {}

    def chooser() -> dict[str, Any]:
        for name in web_recipes.list_recipes():
            try:
                fp = web_recipes.fingerprint(name, home)
            except Exception:  # noqa: BLE001 — skip a recipe that does not load
                continue
            if sum(_families(fp).values()) >= 2:
                fps[name] = fp
        ranked = sorted(fps, key=lambda n: (-sum(_families(fps[n]).values()), n))[:_CHIP_LIMIT]
        sel = args.get("recipe") if args.get("recipe") in fps else ("code-fix" if "code-fix" in fps
                                                                     else (ranked[0] if ranked else ""))
        if sel and sel not in ranked:
            ranked.append(sel)
        args["recipe"] = sel
        return S.chips("Recipe", [{"t": n, "on": n == sel, "do": S.set_args(recipe=n)} for n in ranked],
                       full=True, note="" if ranked else "No recipe has two or more model nodes.")

    def verdict() -> list[dict[str, Any]]:
        fp = fps.get(args.get("recipe") or "")
        if not fp:
            return []
        fams = _families(fp)
        llm_nodes = [n for n in fp.get("nodes") or [] if n.get("family")]
        distinct = sorted({f.strip() for fam in fams for f in fam.split(",") if f.strip()})
        coalition = str(fp.get("coalition") or "—")
        rho = _realised_rho(home, fp)
        out = [S.kv("Coalition verdict", [
            ("Verdict", coalition, _COALITION.get(coalition, "text")),
            ("Families", str(len(distinct)), "text", " · ".join(distinct)),
            ("Dominant family", fp.get("dominant_family") or "—"),
            ("Dominant share", f"{float(fp.get('dominant_share') or 0) * 100:.0f}%"),
            ("Model nodes", str(len(llm_nodes))),
            ("ρ realised", rho[0], "text", rho[1]),
        ], full=True, note="Verdict from lane families in workflow.yaml + agents.yaml: four or more "
                           "families is heterogeneous; one family on ≥75% of model nodes is high.")]
        top = max(fams.values()) if fams else 1
        out.append(S.bars("Family distribution", [
            (fam, n / top * 100, f"{n} node{'s' if n != 1 else ''}", _fam_colour(fam))
            for fam, n in sorted(fams.items(), key=lambda kv: -kv[1])]))
        out.append(S.table("Receipt · who judged this",
                           [S.col(130), S.col(fr=1, min=90), S.col(90), S.col(fr=1, min=90)],
                           ["node", "family", "role", "lane"],
                           [[S.mono(n.get("name")), S.cell(n.get("family"), _fam_colour(str(n.get("family")))),
                             S.muted(n.get("type")), S.muted(n.get("lane"))] for n in llm_nodes]))
        if coalition == "high":
            out.append(S.lst("What to do", [S.bad(
                "Single-family quorum",
                "Agreement here is one disposition sampled several times. Reassign at least one "
                "reviewer lane to another family in agents.yaml.",
                [S.btn("Open lanes", S.page_link("lanes", "lanes"))])], full=True))
        return out

    return S.guarded(errors, "Recipe", chooser) + S.guarded(errors, "Coalition verdict", verdict)


def _realised_rho(home: Path, fp: dict[str, Any]) -> tuple[str, str]:
    from mini_ork.web.recipes import load_recipe

    task_class = str((load_recipe(str(fp.get("recipe"))).get("task_class") or {}).get("task_class") or "")
    names = {task_class, str(fp.get("recipe")), str(fp.get("recipe")).replace("-", "_")} - {""}
    rows = _rows(_db(home), "panel_topology_telemetry",
                 "SELECT recipe, rho, quadrant, n_traces, computed_at FROM panel_topology_telemetry "
                 "ORDER BY computed_at DESC LIMIT 400")
    row = next((r for r in rows if r.get("recipe") in names and int(r.get("n_traces") or 0) > 0), None)
    if row is None:
        return "not measured", "no panel telemetry with traces"
    return f"{float(row.get('rho') or 0):.2f}", f"{row.get('quadrant') or ''} · {str(row.get('computed_at'))[:10]}"


# ── human gates ────────────────────────────────────────────────────────────

def _inbox_sections(home: Path, errors: dict[str, str]) -> list[dict[str, Any]]:
    def pending() -> dict[str, Any]:
        db = _db(home)
        items = []
        for g in _rows(db, "mo_inbox_gates",
                       "SELECT inbox_id, gate_id, feature, phase, enqueued_at FROM mo_inbox_gates "
                       "WHERE status = 'pending' ORDER BY enqueued_at"):
            sub = " · ".join(x for x in (str(g.get("feature") or ""), str(g.get("phase") or ""),
                                         f"waiting {S.age(g.get('enqueued_at'), int(time.time()))}") if x)
            item_id = g["inbox_id"]
            items.append(S.item(g.get("gate_id") or "gate", sub, m="⚑", mc="purple", acts=[
                S.btn("Approve", S.cli("board", "gate", "approve", str(item_id),
                                       confirm=f"Approve oversight item {item_id}?"),
                      "primary"),
                S.btn("Reject", S.cli("board", "gate", "reject", str(item_id),
                                      confirm=f"Reject oversight item {item_id}?"),
                      "danger")]))
        for p in _rows(db, "promotion_records",
                       "SELECT promotion_id, candidate_id, utility_before, utility_after, decided_at "
                       "FROM promotion_records WHERE decision = 'pending_human_approval' "
                       "ORDER BY decided_at DESC"):
            items.append(S.item(f"promote candidate {str(p['candidate_id'])[:12]}",
                                f"promotion_gate · utility {p.get('utility_before'):.2f} → "
                                f"{p.get('utility_after'):.2f}", m="⚑", mc="purple"))
        for e in _rows(db, "inbox",
                       "SELECT id, epic_id, kind, body_md FROM inbox WHERE resolved_at IS NULL "
                       "ORDER BY opened_at"):
            first = next((ln.strip("# ").strip() for ln in str(e.get("body_md") or "").splitlines()
                          if ln.strip()), "")
            items.append(S.item(f"{e.get('kind')} · {e.get('epic_id')}", first[:160], m="⚑", mc="yellow",
                                acts=[S.btn("Open epics", S.page_link("recipes", "epics"), "ghost")]))
        if not items:
            items = [S.ok("Nothing waiting for you", "No pending oversight items, promotions or epic escalations")]
        return S.lst("Pending human gates", items, full=True,
                     note="mo_inbox_gates, promotions waiting on a human, and open epic escalations. "
                          "Resolutions are recorded with your note and never auto-resolve.")

    def calibration() -> dict[str, Any]:
        db = _db(home)
        since = int(time.time()) - 30 * DAY
        rows = _rows(db, "mo_inbox_gates",
                     "SELECT status, enqueued_at, resolved_at FROM mo_inbox_gates WHERE enqueued_at >= ?",
                     (since,))
        decided = [r for r in rows if r["status"] != "pending" and r.get("resolved_at")]
        waits = sorted(int(r["resolved_at"]) - int(r["enqueued_at"]) for r in decided)
        median = S.duration(waits[len(waits) // 2]) if waits else "—"
        risk = "—"
        if (home / "state.db").is_file():
            from mini_ork.learning.oversight_calibration import calibrate

            report = calibrate(since=float(since), db_path=str(home / "state.db"))
            if report.get("n"):
                gates = report.get("gates") or []
                risk = "high" if any(g.get("fatigue") for g in gates) else (
                    "medium" if any(g.get("stale") or g.get("abandoned") for g in gates) else "low")
        return S.kv("Oversight calibration · 30 days", [
            ("Decisions", str(len(decided))),
            ("Approved", str(sum(1 for r in decided if r["status"] == "approved"))),
            ("Median time", median),
            ("Fatigue risk", risk, {"low": "green", "high": "red", "medium": "yellow"}.get(risk, "text"),
             "rule-by-fatigue"),
        ], full=True)

    return S.guarded(errors, "Pending human gates", pending) + S.guarded(errors, "Oversight calibration", calibration)


# ── autonomy & probes ──────────────────────────────────────────────────────

_RUNG_OF_KIND = {"prompt_change": 1, "context_change": 2, "retrieval_change": 2, "edge_change": 3,
                 "edge_add": 3, "edge_remove": 3, "role_change": 4, "lane_change": 4,
                 "tools_change": 4, "verifier_change": 5, "gate_change": 5, "code_change": 6}


def _ladder() -> list[tuple[str, str, str]]:
    text = (_engine_root() / "docs" / "SAFETY.md").read_text(encoding="utf-8")
    out = []
    for line in text.splitlines():
        m = re.match(r"\|\s*(\d)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$", line)
        if m:
            mutation = m.group(2).split(" — ")[0].strip()
            out.append((m.group(1), mutation, m.group(3).strip()))
    return out


def _autonomy_sections(home: Path, errors: dict[str, str]) -> list[dict[str, Any]]:
    def ladder() -> dict[str, Any]:
        db = _db(home)
        in_flight: dict[str, int] = {}
        unmapped = 0
        for c in _rows(db, "workflow_candidates",
                       "SELECT mutations FROM workflow_candidates WHERE status IN ('candidate','shadow')"):
            try:
                kinds = [m.get("kind") for m in json.loads(c.get("mutations") or "[]") if isinstance(m, dict)]
            except ValueError:
                kinds = []
            rungs = [_RUNG_OF_KIND[k] for k in kinds if k in _RUNG_OF_KIND]
            if rungs:
                in_flight[str(max(rungs))] = in_flight.get(str(max(rungs)), 0) + 1
            else:
                unmapped += 1
        waiting = len(_rows(db, "promotion_records",
                            "SELECT 1 FROM promotion_records WHERE decision = 'pending_human_approval'"))
        if waiting:
            in_flight["7"] = in_flight.get("7", 0) + waiting
        rows = []
        for rung, mutation, gate in _ladder():
            n = in_flight.get(rung, 0)
            rows.append([S.cell(rung, "purple" if rung == "7" else "body"), mutation, S.muted(gate),
                         S.cell(str(n), "yellow" if n and rung == "7" else ("body" if n else "sub"))])
        note = "From docs/SAFETY.md; in flight = workflow_candidates still candidate or shadow."
        if unmapped:
            note += f" {unmapped} candidate(s) carry mutation kinds outside the ladder."
        return S.table("Bounded autonomy ladder", [S.col(40), S.col(fr=1, min=140), S.col(fr=1, min=140),
                                                   S.col(90)],
                       ["rung", "mutation", "gate required", "in flight"], rows, full=True, note=note)

    def quarantine() -> dict[str, Any]:
        db = _db(home)
        items = []
        for v in _rows(db, "version_registry",
                       "SELECT name, kind, quarantine_reason, quarantined_at FROM version_registry "
                       "WHERE status = 'quarantined' ORDER BY quarantined_at DESC LIMIT 10"):
            items.append(S.warn(Path(str(v["name"])).name or v["name"], v.get("quarantine_reason") or v.get("kind")))
        for c in _rows(db, "workflow_candidates",
                       "SELECT candidate_id, utility_delta, created_at FROM workflow_candidates "
                       "WHERE status = 'quarantined' ORDER BY created_at DESC LIMIT 10"):
            items.append(S.warn(f"candidate {str(c['candidate_id'])[:12]}",
                                f"utility Δ {float(c.get('utility_delta') or 0):+.2f} · {str(c.get('created_at'))[:10]}"))
        n = len(_rows(db, "promotion_records",
                      "SELECT 1 FROM promotion_records WHERE decision = 'quarantined'"))
        if not items:
            items = [S.dot("Nothing quarantined right now",
                           f"{n} promotion decision(s) were quarantined in the past" if n else "")]
        return S.lst("Quarantine", items)

    def probes() -> dict[str, Any]:
        db = _db(home)
        items = []
        folder = home / "gate-hackability"
        reports = []
        if folder.is_dir():
            for path in folder.glob("*.json"):
                try:
                    reports.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        if reports:
            worst = max(reports, key=lambda r: float(r.get("hackability") or 0))
            passed_bad = sum(int(r.get("passed_bad") or 0) for r in reports)
            line = (f"{len(reports)} gates measured · worst {worst.get('gate_id')} "
                    f"hackability {float(worst.get('hackability') or 0):.2f}")
            items.append((S.warn if passed_bad else S.ok)(
                "gate hackability", line + (f" · {passed_bad} bad input(s) passed" if passed_bad else "")))
        else:
            items.append(S.dot("gate hackability", "not measured yet — .mini-ork/gate-hackability/ is empty"))
        for name, what in (("gate-fuzz", "false-pass rate across hermetic cases"),
                           ("hack-probe", "reward hacking across a history.json"),
                           ("harness-audit", "tampered harness edits in an edits.json")):
            items.append(S.dot(name, f"on demand · mini-ork {name} — {what}"))
        open_events = _rows(db, "safety_events",
                            "SELECT tripwire_id, severity FROM safety_events WHERE status = 'open'")
        if open_events:
            items.append(S.bad("RSP tripwires", f"{len(open_events)} open: "
                               + ", ".join(sorted({str(e['tripwire_id']) for e in open_events}))[:160]))
        else:
            items.append(S.ok("RSP tripwires", "no open safety_events"))
        return S.lst("Safety probes", items)

    def audit() -> dict[str, Any]:
        db = _db(home)
        lines = []
        for a in _rows(db, "audit_log",
                       "SELECT event_type, actor, target, occurred_at FROM audit_log "
                       "ORDER BY occurred_at DESC LIMIT 12"):
            when = str(a.get("occurred_at") or "")[5:16].replace("T", " ")
            colour = "yellow" if a.get("event_type") in ("quarantine", "safety_hit") else "muted"
            lines.append((f"{when}  {str(a.get('event_type')):<10} {a.get('target')}  by {a.get('actor')}", colour))
        if not lines:
            for p in _rows(db, "promotion_records",
                           "SELECT decision, to_version_id, decided_by, decided_at FROM promotion_records "
                           "ORDER BY decided_at DESC LIMIT 12"):
                when = str(p.get("decided_at") or "")[5:16].replace("T", " ")
                colour = "yellow" if p.get("decision") == "quarantined" else "muted"
                lines.append((f"{when}  {str(p.get('decision')):<11} {str(p.get('to_version_id'))[:24]}"
                              f"  by {p.get('decided_by')}", colour))
            title = "audit_log · empty — promotion_records instead" if lines else "audit_log"
        else:
            title = "audit_log"
        if not lines:
            lines = [("No audit events recorded yet.", "dim")]
        return S.code(title, lines, full=True)

    return (S.guarded(errors, "Bounded autonomy ladder", ladder) + S.guarded(errors, "Quarantine", quarantine)
            + S.guarded(errors, "Safety probes", probes) + S.guarded(errors, "audit_log", audit))


# ── page ───────────────────────────────────────────────────────────────────

def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    tab = tab if tab in {k for k, _ in TABS} else "certify"
    errors: dict[str, str] = {}
    if tab == "certify":
        sections = _certify_sections(home, args, errors)
    elif tab == "gates":
        sections = (S.guarded(errors, "Gate outcomes · last 24 h", lambda: _gate_outcomes(home))
                    + S.guarded(errors, "Specialised gates", lambda: _specialised_gates(home))
                    + S.guarded(errors, "Verifier registry", lambda: _verifier_registry(home))
                    + S.guarded(errors, "Minimum evidence", lambda: _minimum_evidence(home)))
    elif tab == "panels":
        sections = _panel_sections(home, args, errors)
    elif tab == "inbox":
        sections = _inbox_sections(home, errors)
    elif tab == "bugs":
        sections = S.guarded(errors, "Bug reports", lambda: _bugs(home))
    else:
        sections = _autonomy_sections(home, errors)
    return S.page("verify", "Verification & safety",
                  "Executable checks first, independent panels second, and hard limits on what changes itself.",
                  tabs=TABS, tab=tab, args=args, sections=sections, errors=errors)


# ── bug reports ────────────────────────────────────────────────────────────

def _short_title(text: str, limit: int) -> str:
    """Truncate a bug-report title; ``verify._short`` already owns run_id truncation."""
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _bugs(home: Path) -> dict[str, Any]:
    db = _db(home)
    rows = db.rows("SELECT id, title, observed_in, confidence, status, severity FROM bug_reports "
                   "ORDER BY CASE WHEN status = 'open' THEN 0 ELSE 1 END, confidence DESC LIMIT 20") \
        if (db is not None and db.has_table("bug_reports")) else []
    cols = [S.col(60), S.col(fr=1, min=0), S.col(70), S.col(56)]
    out = [[S.mono(f"b-{r['id']}"), S.cell(_short_title(str(r.get("title") or ""), 110), "text"),
            S.muted(Path(str(r.get("observed_in") or "—")).name), S.mono(f"{float(r.get('confidence') or 0):.2f}")]
           for r in rows]
    if not out:
        out = [[S.muted("—"), S.muted("No bug reports yet"), "", ""]]
    return S.table("Bug reports", cols, ["id", "report", "source", "score"], out, full=True,
                   note="Sweep runs for bug reports, prioritise, and promote the top ones into kickoffs.",
                   actions=[S.btn("Sweep runs", S.cli("bugs", "sweep", home=False)),
                            S.btn("Promote top 3", S.cli("bugs", "promote", "--top", "3",
                                                         confirm="Write kickoffs for the top 3 bug reports?",
                                                         home=False),
                                  "primary")])
