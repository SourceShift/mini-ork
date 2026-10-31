"""``mini-ork rsi-cycle`` — the operator surface of the self-improvement loop.

Two halves. The read-only half (``plan``/``list``/``show``/``doctor``) harvests
the three domains and prints the ranked queue: useful on day one, at zero spend,
and the reference output every later stage is tested against. The spending half
(the default verb, plus ``quarantine``/``revert``/``halt``/``resume``) runs one
cycle end to end and gives an operator the levers to inspect, keep, release or
undo what it produced.

The verbs an operator reaches for when a cycle misbehaves:

* ``plan``       — what would be fixed next, and why everything else was dropped.
* ``list``       — what the ledger already knows (candidates, or ``--cycles``).
* ``show``       — one candidate or cycle in full, including the gate results.
* ``quarantine`` — list/show/release/clean what a red gate kept.
* ``revert``     — undo a merge with a gated revert commit, never a reset.
* ``halt``       — write the durable stop file; ``resume`` removes it.
* ``doctor``     — is this checkout able to run a cycle at all, right now.

Exit codes: ``0`` ok / nothing to do / halted by budget or a kill switch,
``1`` a check failed, a cycle failed or an internal error, ``2`` usage, config
or an unusable database.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

_USAGE = """\
mini-ork rsi-cycle — harvest gaps in learning, routing and verification, and fix one.

Usage:
  mini-ork rsi-cycle [run] [--once | --interval N] [--config PATH] [--dry-run]
                     [--suggest] [--probe-run] [--max-cycles N] [--budget USD]
                     [--candidate ID] [--domain D] [--json]
  mini-ork rsi-cycle plan [--json] [--no-probes] [--domain D] [--max N]
  mini-ork rsi-cycle list [--status S] [--limit N] [--cycles] [--json]
  mini-ork rsi-cycle show <cycle_id|candidate_id> [--json]
  mini-ork rsi-cycle quarantine [list | show <id> | release <id> | clean <id>]
  mini-ork rsi-cycle revert <cycle_id>
  mini-ork rsi-cycle halt | resume
  mini-ork rsi-cycle doctor [--json]
  mini-ork rsi-cycle research [--domain D] [--refresh] [--json]
  mini-ork rsi-cycle impact [status | rotate <benchmark> | canary] [--json]

Global: --db PATH  --home DIR  --root DIR

The default verb runs one cycle. ``--dry-run`` prints the would-be kickoff and
writes nothing; ``--suggest`` runs the paid SUGGEST stage but dispatches nothing.
A cycle merges only when all twelve gates pass and an exogenous critic panel is
unanimous — otherwise it quarantines the branch and names the failing gate.

``plan`` is read-only and spends nothing: it runs the deterministic harvest
(SQLite predicates, AST rules, and — unless --no-probes — the cheap probes)
and prints the queue a cycle would work from. ``doctor`` is the first thing to
run when a cycle misbehaves: it checks the DB, the migration state, the probe
binaries, the worktree driver and the origin remote.

With ``--json`` a single cycle prints one object; a multi-cycle run prints
``{"cycles": [...]}`` so the output stays one JSON document either way.
"""

_DEFAULTS = {"limit": 20, "max": 3, "status": None}


# --------------------------------------------------------------------------
# Paths and connections
# --------------------------------------------------------------------------


def _engine_root(root: str | Path | None) -> Path:
    """Prefer the explicit/env root, else the checkout that owns this package.

    Falling back to ``cwd`` would silently harvest whichever tree the shell
    happens to be in — and a wrong tree produces a plausible-looking queue of
    somebody else's gaps.
    """
    if root:
        return Path(root).resolve()
    env = os.environ.get("MINI_ORK_ROOT")
    if env:
        return Path(env).resolve()
    import mini_ork

    return Path(mini_ork.__file__).resolve().parents[1]


def _db_path(root: Path, *, db: str | None, home: str | None) -> Path:
    from mini_ork.rsi.view import resolve_db

    return resolve_db(root, db=db, home=home)


class _SetupError(RuntimeError):
    """Anything that must exit 2: missing DB, missing tables, bad flags."""


def _open_ledger(db: Path):
    from mini_ork.rsi import ledger

    if not db.exists():
        raise _SetupError(
            f"state DB not found at {db} — run `mini-ork init` (or pass --db/--home)"
        )
    con = ledger.connect(str(db))
    if not ledger.tables_present(con):
        con.close()
        raise _SetupError(
            "rsi ledger tables are missing from this state DB — run `mini-ork update`"
        )
    return con


# --------------------------------------------------------------------------
# Harvest + queue (shared by plan and doctor)
# --------------------------------------------------------------------------


def _run_probes(root: Path, *, enabled: bool):
    from mini_ork.rsi import probes as probe_mod

    if not enabled:
        return ()
    return probe_mod.run_probes(probe_mod.default_specs(root=root), root=root)


def _harvest_and_queue(
    root: Path,
    db: Path,
    *,
    probes_enabled: bool,
    max_candidates: int,
    domains: Sequence[str] | None,
    now: int | None = None,
):
    from mini_ork.context import scoped_environ
    from mini_ork.rsi import harvest as harvest_mod
    from mini_ork.rsi import ledger, queue as queue_mod
    from mini_ork.rsi.view import ledger_view

    now = int(time.time()) if now is None else int(now)
    con = _open_ledger(db)
    try:
        # The probes are subprocesses and would otherwise resolve their own DB
        # from the ambient environment — a `--db` flag that the harvest honours
        # and `metrics` does not is how one cycle reads two databases.
        with scoped_environ({"MINI_ORK_DB": str(db), "MINI_ORK_ROOT": str(root)}):
            probe_results = _run_probes(root, enabled=probes_enabled)
            report = harvest_mod.harvest(root, con, now=now, probes=probe_results)
        view = ledger_view(con, report.candidates, root=root)
        plan = queue_mod.build_queue(
            report.candidates,
            now=now,
            view=view,
            cfg=queue_mod.QueueConfig(max_candidates=max_candidates),
            credits=ledger.credit_stats(con),
            domains=domains,
        )
    finally:
        con.close()
    return report, plan


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _table(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    lines.extend(fmt.format(*row) for row in rows)
    return "\n".join(lines)


def _render_plan(report, plan) -> str:
    out: list[str] = []
    status = "ok" if report.harvest_ok else "INCOMPLETE"
    probes_bad = [p.probe_id for p in report.probes if p.failed]
    out.append(f"RSI queue — harvest {status}; {plan.examined} candidate(s) examined")
    if report.errors:
        out.append(f"  rule errors: {', '.join(report.errors)}")
    if probes_bad:
        out.append(f"  probe errors: {', '.join(probes_bad)}")
    if report.skipped:
        out.append(f"  skipped (not applicable): {', '.join(report.skipped)}")
    out.append("")

    if plan.selected:
        rows = [
            (
                str(item.rank + 1),
                str(item.effective_severity),
                item.candidate.domain,
                item.candidate.kind,
                item.candidate.title,
            )
            for item in plan.selected
        ]
        out.append(_table(rows, ("#", "sev", "domain", "kind", "title")))
        out.append("")
        top = plan.top
        out.append(f"next cycle targets: {top.candidate_id}")
        out.append(f"  files: {', '.join(top.files) or '(none declared)'}")
        if top.success_command:
            out.append(f"  success command: {top.success_command}")
    else:
        out.append("no candidate is eligible right now")

    if plan.rejected:
        counts: dict[str, int] = {}
        for rej in plan.rejected:
            counts[rej.code] = counts.get(rej.code, 0) + 1
        summary = ", ".join(f"{n} {code}" for code, n in sorted(counts.items()))
        out.append("")
        out.append(f"dropped: {summary}")
    if plan.holes:
        out.append(f"starved domains: {', '.join(plan.holes)}")
    if plan.backlog:
        out.append(f"backlog: {len(plan.backlog)} ranked candidate(s) not selected")
    return "\n".join(out) + "\n"


def _render_candidates(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "no candidates recorded\n"
    table = _table(
        [
            (
                str(r["candidate_id"]),
                str(r["severity"]),
                str(r["domain"]),
                str(r["kind"]),
                str(r["status"]),
                str(r["times_seen"]),
                str(r["title"]),
            )
            for r in rows
        ],
        ("candidate_id", "sev", "domain", "kind", "status", "seen", "title"),
    )
    return table + "\n"


def _render_cycles(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "no cycles recorded\n"
    return _table(
        [
            (
                str(r["cycle_id"]),
                str(r["status"]),
                str(r["domain"] or "-"),
                str(r["halt_reason"] or "-"),
                f"{float(r['cost_usd'] or 0):.2f}",
                str(r["merged_sha"] or "-")[:12],
            )
            for r in rows
        ],
        ("cycle_id", "status", "domain", "halt", "usd", "merged"),
    ) + "\n"


# --------------------------------------------------------------------------
# Verbs
# --------------------------------------------------------------------------


def _cmd_plan(args, root: Path, db: Path) -> int:
    report, plan = _harvest_and_queue(
        root,
        db,
        probes_enabled=not args.no_probes,
        max_candidates=args.max if args.max is not None else _DEFAULTS["max"],
        domains=[args.domain] if args.domain else None,
    )
    if args.json:
        payload = {
            "harvest_ok": report.harvest_ok,
            "errors": list(report.errors),
            "skipped": list(report.skipped),
            "probes": [
                {"probe_id": p.probe_id, "status": p.status, "rc": p.rc,
                 "duration_ms": p.duration_ms, "findings": len(p.findings)}
                for p in report.probes
            ],
            "queue": plan.to_dict(),
        }
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_render_plan(report, plan))
    return 0 if report.harvest_ok else 1


def _cmd_list(args, root: Path, db: Path) -> int:
    from mini_ork.rsi import ledger

    con = _open_ledger(db)
    try:
        try:
            if args.cycles:
                rows = ledger.list_cycles(con, status=args.status, limit=args.limit)
            else:
                rows = ledger.list_candidates(con, status=args.status, limit=args.limit)
        except ValueError as exc:  # bad --status for this table
            raise _SetupError(str(exc)) from exc
    finally:
        con.close()
    if args.json:
        sys.stdout.write(json.dumps(rows, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_render_cycles(rows) if args.cycles else _render_candidates(rows))
    return 0


def _cmd_show(args, root: Path, db: Path) -> int:
    from mini_ork.rsi import ledger

    ident = args.ident
    con = _open_ledger(db)
    try:
        if ident.startswith("rsi-"):
            row = ledger.get_candidate(con, ident)
            kind = "candidate"
            related = [
                dict(r) for r in con.execute(
                    "SELECT cycle_id, status, started_at, merged_sha, quarantine_reason"
                    " FROM rsi_cycles WHERE candidate_id = ? ORDER BY started_at DESC LIMIT 20",
                    (ident,),
                )
            ]
            if row is None:
                raise _SetupError(f"no such candidate: {ident}")
            payload = {"candidate": row, "cycles": related}
        else:
            row = ledger.get_cycle(con, ident)
            if row is None:
                raise _SetupError(f"no such cycle: {ident}")
            kind = "cycle"
            payload = {"cycle": row, "probes": ledger.list_probe_runs(con, ident)}
    finally:
        con.close()
    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_render_show(kind, payload) + "\n")
    return 0


def _render_show(kind: str, payload: Mapping[str, Any]) -> str:
    lines: list[str] = []
    if kind == "candidate":
        row = payload["candidate"]
        for key in ("candidate_id", "title", "severity", "domain", "kind", "source",
                    "status", "times_seen", "evidence_key", "success_command",
                    "merged_sha", "outcome_reason"):
            lines.append(f"{key:<15} {row.get(key)}")
        lines.append(f"{'files':<15} {row.get('files_json')}")
        evidence = row.get("evidence_json") or "{}"
        try:
            evidence = json.dumps(json.loads(evidence), indent=2, sort_keys=True)
        except (TypeError, ValueError):
            pass
        lines.append(f"{'evidence':<15} {evidence}")
        for cyc in payload.get("cycles", ()):
            lines.append(
                f"cycle {cyc['cycle_id']}: {cyc['status']}"
                + (f" — {cyc['quarantine_reason']}" if cyc.get("quarantine_reason") else "")
            )
    else:
        row = payload["cycle"]
        for key in ("cycle_id", "status", "started_at", "finished_at", "mode",
                    "halt_reason", "domain", "candidate_id", "cost_usd",
                    "metric_before", "metric_after", "merged_sha",
                    "quarantine_reason", "quarantine_path"):
            lines.append(f"{key:<18} {row.get(key)}")
        for probe in payload.get("probes", ()):
            lines.append(
                f"  probe {probe['probe_id']:<16} {probe['status']:<9} "
                f"rc={probe['rc']} {probe['duration_ms']}ms"
            )
        lines.extend(_render_critics(row.get("critic_json")))
    return "\n".join(lines)


def _render_critics(raw: Any) -> list[str]:
    """One line per critic, with the first blocking finding and its `why` cut.

    The full text is in ``--json``; a split has to be legible without it, since
    the question "did the panel split on the fix or on the question?" is what
    decides whether the next cycle retries this candidate at all.
    """
    try:
        doc = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        return []
    lines: list[str] = []
    for critic in (doc.get("critics") or ()) if isinstance(doc, dict) else ():
        verdict = "approve" if critic.get("approve") else "REJECT "
        blocking = critic.get("blocking_findings") or [""]
        lines.append(f"  critic {str(critic.get('alias', '')):<14} {verdict} "
                     f"({critic.get('family', '')}) {str(blocking[0])[:110]}")
    return lines


def _cmd_doctor(args, root: Path, db: Path) -> int:
    from mini_ork.rsi import harvest as harvest_mod
    from mini_ork.rsi import ledger

    checks: list[dict[str, Any]] = []

    def check(name: str, fn) -> None:
        try:
            detail = fn()
            checks.append({"name": name, "ok": True, "detail": str(detail)})
        except Exception as exc:  # noqa: BLE001 - doctor reports, it does not raise
            checks.append({"name": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}"})

    def db_exists() -> str:
        if not db.exists():
            raise _SetupError(f"{db} does not exist")
        return f"{db} ({db.stat().st_size} bytes)"

    def migrations() -> str:
        from mini_ork.stores.migrate import migrate_status

        applied, pending, drifted, total = migrate_status(
            str(root / "db" / "migrations"), str(db)
        )
        if drifted:
            raise RuntimeError(f"{drifted} migrated file(s) changed on disk")
        if pending:
            raise RuntimeError(f"{pending} of {total} migration(s) pending — run `mini-ork update`")
        return f"{applied}/{total} applied"

    def tables() -> str:
        con = ledger.connect(str(db))
        try:
            ledger.require_tables(con)
        finally:
            con.close()
        return "all four rsi_* tables present"

    def lease_tables() -> str:
        from mini_ork.stores.lease import lease_tables_present

        if not lease_tables_present(str(db)):
            raise RuntimeError("0052 lease tables missing — run `mini-ork update`")
        return "run_leases present"

    def probe_bin() -> str:
        for rel in ("bin/mini-ork", "scripts/mini_ork_worktree.py"):
            path = root / rel
            if not path.exists():
                raise FileNotFoundError(rel)
        return "bin/mini-ork + scripts/mini_ork_worktree.py present"

    def origin() -> str:
        cfg = loaded.get("cfg")
        proc = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        found = proc.returncode == 0 and bool(proc.stdout.strip())
        if found:
            return proc.stdout.strip()
        if cfg is not None and not cfg.merge.require_remote:
            return "absent, and merge.require_remote is false"
        raise RuntimeError("no `origin` remote — merges must quarantine, not commit locally")

    def rules() -> str:
        con = ledger.connect(str(db))
        try:
            report = harvest_mod.harvest(root, con, now=int(time.time()))
        finally:
            con.close()
        if not report.harvest_ok:
            raise RuntimeError(f"harvest errors: {', '.join(report.errors)}")
        drift = [c for c in report.candidates if c.kind == "probe_broken"]
        if drift:
            names = ", ".join(c.evidence_key for c in drift)
            raise RuntimeError(f"schema drift: {names}")
        return f"{len(report.candidates)} candidate(s), {len(report.skipped)} rule(s) skipped"

    loaded: dict[str, Any] = {}

    def config() -> str:
        cfg = _load_config(args, root)
        loaded["cfg"] = cfg
        return (f"mode={cfg.mode} max_cycles={cfg.max_cycles} "
                f"auto_merge={cfg.decide.auto_merge} "
                f"per_cycle=${cfg.budget.per_cycle_cap_usd:.0f}")

    def critic_heterogeneity() -> str:
        from mini_ork.verify.panel_heterogeneity import lane_family

        cfg = loaded.get("cfg")
        if cfg is None:
            raise RuntimeError("config did not load; cannot check lane families")
        implementer = lane_family(cfg.implement.lane)
        families = [lane_family(lane) for lane in cfg.critic.lanes]
        if implementer in families:
            raise RuntimeError(
                f"implement.lane {cfg.implement.lane} ({implementer}) shares a family "
                f"with a critic {families} — every cycle would quarantine")
        return f"{cfg.implement.lane}={implementer} vs {dict(zip(cfg.critic.lanes, families))}"

    def worktrees() -> str:
        base = os.environ.get("MINI_ORK_WORKTREES_DIR")
        path = Path(base) if base else root.parent / f"{root.name}-worktrees"
        path.mkdir(parents=True, exist_ok=True)
        if not os.access(path, os.W_OK):
            raise PermissionError(f"{path} is not writable")
        return str(path)

    def budget() -> str:
        from mini_ork.cost_ledger import spent_last_24h

        cfg = loaded.get("cfg")
        if cfg is None:
            raise RuntimeError("config did not load; cannot compute budget headroom")
        spent = float(spent_last_24h(str(db)))
        cap = cfg.budget.daily_cap_usd
        if spent >= cap:
            raise RuntimeError(
                f"spent ${spent:.2f} of the ${cap:.2f} daily cap — every cycle halts")
        return f"${spent:.2f} of ${cap:.2f} used in the last 24h"

    def stop_switch() -> str:
        stop = Path(db).parent / "rsi" / "DISABLED"
        if not stop.exists():
            return "not set"
        try:
            first = stop.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            first = ""
        raise RuntimeError(f"{stop} present — {first.splitlines()[0] if first else 'operator stop'}")

    check("config", config)
    check("database", db_exists)
    check("migrations", migrations)
    check("rsi_tables", tables)
    check("lease_tables", lease_tables)
    check("binaries", probe_bin)
    check("origin_remote", origin)
    check("harvest_rules", rules)
    check("critic_families", critic_heterogeneity)
    check("worktrees", worktrees)
    check("budget", budget)
    check("stop_switch", stop_switch)

    ok = all(c["ok"] for c in checks)
    if args.json:
        sys.stdout.write(json.dumps({"ok": ok, "checks": checks}, indent=2, sort_keys=True) + "\n")
    else:
        for item in checks:
            mark = "ok  " if item["ok"] else "FAIL"
            sys.stdout.write(f"[{mark}] {item['name']:<15} {item['detail']}\n")
        sys.stdout.write("\n" + ("ready to run a cycle\n" if ok else "not ready — fix the FAIL lines\n"))
    return 0 if ok else 1


# --------------------------------------------------------------------------
# The spending half: run one cycle, and the operator levers around it
# --------------------------------------------------------------------------


def _load_config(args, root: Path):
    """Parse the config, then apply the command-line overrides that are allowed.

    ``--budget`` can only *lower* the per-cycle cap: a flag that raised it would
    let one invocation outspend the file an operator wrote down.
    """
    from dataclasses import replace

    from mini_ork.rsi import config as cfg_mod

    env = dict(os.environ)
    # So the default config path (``<MINI_ORK_ROOT>/config/rsi-cycle.yaml``)
    # resolves to the checkout being operated on, not the shell's cwd.
    env["MINI_ORK_ROOT"] = str(root)
    try:
        cfg = cfg_mod.load_config(args.config, env=env, strict=True)
    except cfg_mod.ConfigError as exc:
        raise _SetupError(str(exc)) from exc

    if args.budget is not None:
        cap = float(args.budget)
        if cap <= 0:
            raise _SetupError("--budget must be positive")
        if cap > cfg.budget.daily_cap_usd:
            raise _SetupError(
                f"--budget {cap:.2f} exceeds the daily cap "
                f"{cfg.budget.daily_cap_usd:.2f}; lower the daily cap in the config instead")
        cfg = replace(cfg, budget=replace(cfg.budget, per_cycle_cap_usd=cap))

    max_cycles = cfg.max_cycles if args.max_cycles is None else int(args.max_cycles)
    mode, interval = cfg.mode, cfg.interval_s
    if args.once:
        # ``--once`` beats a config that says loop: an operator's flag is the
        # more recent statement of intent, and "run once" must never run twice.
        mode, max_cycles = "once", 1
    elif args.interval is not None:
        mode, interval = "loop", int(args.interval)
    # ``--max-cycles`` above one is a loop by definition; the config's own
    # ``mode: once`` is not contradicted, it is overridden for this invocation.
    if mode == "once" and max_cycles != 1:
        mode = "loop"
    cfg = replace(cfg, mode=mode, interval_s=interval, max_cycles=max_cycles)
    return cfg


def _require_origin(root: Path) -> None:
    """``merge.require_remote: true`` with no remote is exit 2, not a late quarantine."""
    proc = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise _SetupError(
            "merge.require_remote is true but this repository has no 'origin' "
            "remote — every merge would quarantine after the child had already "
            "spent. Add an origin, or set merge.require_remote: false to allow "
            "local-only merging")


def _cmd_run(args, root: Path, db: Path) -> int:
    from dataclasses import replace

    from mini_ork.rsi import cycle as cycle_mod

    cfg = _load_config(args, root)
    if cfg.merge.require_remote:
        # Checked here as well as at merge time: discovering it *after* a paid
        # child run turns a $10 cycle into a guaranteed quarantine.
        _require_origin(root)
    if args.probe_run and not cfg.probes.allow_live and not args.dry_run:
        # ``allow_live`` in the file is a permission, not a trigger: a config
        # alone must never start a paid run.
        cfg = replace(cfg, probes=replace(cfg.probes, allow_live=True))

    outcomes: list[Any] = []
    rc = 0
    while True:
        req = cycle_mod.CycleRequest(
            root=root, db=db, config=cfg,
            dry_run=args.dry_run, propose_only=args.suggest,
            candidate_id=args.candidate, max_candidates=args.max,
            domains=[args.domain] if args.domain else None,
            probe_run=bool(args.probe_run),
        )
        try:
            outcome = cycle_mod.run_cycle(req)
        except cycle_mod.CycleSetupError as exc:
            raise _SetupError(str(exc)) from exc
        except cycle_mod.CycleFailure as exc:
            sys.stderr.write(f"rsi-cycle: {exc}\n")
            return 1
        outcomes.append(outcome)
        rc = outcome.rc

        looping = cfg.mode == "loop" and not args.dry_run
        if not looping or outcome.status == "halted":
            break
        if cfg.max_cycles and len(outcomes) >= cfg.max_cycles:
            break
        time.sleep(cfg.interval_s)

    if args.json:
        payload: Any = (_outcome_dict(outcomes[0]) if len(outcomes) == 1
                        else {"cycles": [_outcome_dict(o) for o in outcomes]})
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        for outcome in outcomes:
            sys.stdout.write(_render_outcome(outcome))
    return rc


def _outcome_dict(outcome) -> dict[str, Any]:
    return {
        "cycle_id": outcome.cycle_id,
        "status": outcome.status,
        "rc": outcome.rc,
        "halt_reason": outcome.halt_reason,
        "candidate_id": outcome.candidate_id,
        "detail": outcome.detail,
        "merged_sha": outcome.merged_sha,
        "kickoff": outcome.kickoff,
        "gates": outcome.gates.to_dict() if outcome.gates is not None else None,
    }


def _render_outcome(outcome) -> str:
    lines = [f"cycle {outcome.cycle_id} — {outcome.status}"]
    if outcome.candidate_id:
        lines.append(f"  candidate  {outcome.candidate_id}")
    if outcome.merged_sha:
        lines.append(f"  merged     {outcome.merged_sha}")
    if outcome.halt_reason:
        lines.append(f"  reason     {outcome.halt_reason}")
    if outcome.detail:
        lines.append(f"  detail     {outcome.detail}")
    if outcome.gates is not None:
        results = outcome.gates.results
        passed = sum(1 for r in results if r.ok)
        lines.append(f"  gates      {passed}/{len(results)} passed")
        for result in outcome.gates.failing:
            lines.append(f"    FAIL {result.gate} {result.code}: {result.detail}")
    if outcome.kickoff:
        lines.append("")
        lines.append(outcome.kickoff.rstrip())
    return "\n".join(lines) + "\n"


def _operator_request(root: Path, db: Path, cfg):
    """A request used only to rebuild worktree policy for an operator verb."""
    from mini_ork.rsi.cycle import CycleRequest

    return CycleRequest(root=root, db=db, config=cfg, dry_run=True,
                        cycle_id="rsi-operator")


def _quarantine_record(row: Mapping[str, Any], db: Path) -> Path:
    explicit = str(row.get("quarantine_path") or "")
    if explicit:
        return Path(explicit)
    return Path(db).parent / "rsi" / "quarantine" / f"{row['cycle_id']}.json"


def _cmd_quarantine(args, root: Path, db: Path) -> int:
    from mini_ork.rsi import ledger

    action = (args.ident or "list").lower()
    if action not in ("list", "show", "release", "clean"):
        raise _SetupError("quarantine takes one of: list, show <id>, release <id>, clean <id>")
    con = _open_ledger(db)
    try:
        if action == "list":
            rows = [
                dict(r) for r in con.execute(
                    "SELECT cycle_id, candidate_id, status, quarantine_reason,"
                    " quarantine_path, cost_usd, started_at FROM rsi_cycles"
                    " WHERE quarantine_path IS NOT NULL AND quarantine_path <> ''"
                    " ORDER BY started_at DESC LIMIT ?", (int(args.limit),))
            ]
        else:
            ident = args.sub
            if not ident:
                raise _SetupError(f"quarantine {action} needs a cycle id")
            row = ledger.get_cycle(con, ident)
            if row is None:
                raise _SetupError(f"no such cycle: {ident}")
    finally:
        con.close()

    if action == "list":
        if args.json:
            sys.stdout.write(json.dumps(rows, indent=2, sort_keys=True) + "\n")
        elif not rows:
            sys.stdout.write("no quarantined cycles\n")
        else:
            sys.stdout.write(_table(
                [(r["cycle_id"], str(r["status"]), r["candidate_id"] or "-",
                  f"{float(r['cost_usd'] or 0):.2f}", r["quarantine_reason"] or "-")
                 for r in rows],
                ("cycle_id", "status", "candidate", "usd", "reason")) + "\n")
        return 0

    if action == "show":
        path = _quarantine_record(row, db)
        if not path.is_file():
            raise _SetupError(f"quarantine record missing: {path}")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _SetupError(f"cannot read {path}: {exc}") from exc
        if args.json:
            sys.stdout.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
        else:
            gates = (record.get("gates") or {}).get("results") or []
            lines = [f"cycle {row['cycle_id']} — {row['status']}",
                     f"  candidate  {row['candidate_id'] or '-'}",
                     f"  reason     {row['quarantine_reason'] or '-'}",
                     f"  record     {path}"]
            for gate in gates:
                if not gate.get("ok"):
                    lines.append(f"    FAIL {gate['gate']} {gate['code']}: {gate['detail']}")
            if record.get("changed_files"):
                lines.append(f"  files      {', '.join(record['changed_files'])}")
            lines.append("  worktree   "
                         f"{_worktree_path(root, row['cycle_id'])} (branch wt/{row['cycle_id']})")
            sys.stdout.write("\n".join(lines) + "\n")
        return 0

    from mini_ork.rsi import cycle as cycle_mod

    con = _open_ledger(db)
    try:
        candidate = ledger.get_candidate(con, row["candidate_id"] or "")
        if candidate is None:
            raise _SetupError(f"no candidate row for cycle {row['cycle_id']}")
        files = json.loads(candidate.get("files_json") or "[]")
        if action == "release":
            from mini_ork.rsi.worktree import release

            # Drop the file claims first. They, not the branch, are what the
            # next cycle collides with — a claim left behind turns a re-attempt
            # into a `worktree_claim_conflict` rejection after the paid SUGGEST
            # step, which is precisely the thing "release" exists to prevent.
            # Deliberately config-free, like `halt`: cleanup must survive the
            # broken config that made the cycle quarantine in the first place.
            release(root, row["cycle_id"])
            # Re-open it: the operator is taking the branch, and the next cycle
            # may still re-attempt the gap once the failed-signature TTL lapses.
            ledger.set_candidate_status(
                con, candidate["candidate_id"], "open", now=int(time.time()),
                reason=f"released by operator (cycle {row['cycle_id']})")
        else:
            from mini_ork.rsi.worktree import clean

            # ``clean`` rebuilds the whole worktree policy, so it does parse the
            # config — and must not invent a scope the run never had.
            cfg = _load_config(args, root)
            clean(cycle_mod.worktree_config(
                _operator_request(root, db, cfg), slug=row["cycle_id"], files=files))
    finally:
        con.close()

    if action == "release":
        path = _worktree_path(root, row["cycle_id"])
        sys.stdout.write(
            f"released claims for {row['cycle_id']} and reopened "
            f"{candidate['candidate_id']}; branch wt/{row['cycle_id']} kept at {path}\n"
            f"  the next cycle may claim these files again; merge by hand with: "
            f"git -C {path} checkout wt/{row['cycle_id']}\n")
    else:
        sys.stdout.write(f"cleaned worktree and branch for {row['cycle_id']}\n")
    return 0


def _worktree_path(root: Path, slug: str) -> Path:
    from mini_ork.rsi.cycle import _worktrees_dir

    return _worktrees_dir(root) / slug


def _cmd_revert(args, root: Path, db: Path) -> int:
    from mini_ork.rsi import ledger
    from mini_ork.rsi import cycle as cycle_mod
    from mini_ork.rsi.worktree import revert

    ident = args.ident
    if not ident:
        raise _SetupError("revert needs a merged cycle id")
    con = _open_ledger(db)
    try:
        row = ledger.get_cycle(con, ident)
        if row is None:
            raise _SetupError(f"no such cycle: {ident}")
        if not row.get("merged_sha"):
            raise _SetupError(f"cycle {ident} has no merged_sha — nothing to revert")
        candidate = ledger.get_candidate(con, row["candidate_id"] or "")
    finally:
        con.close()

    cfg = _load_config(args, root)
    files = json.loads((candidate or {}).get("files_json") or "[]")
    result = revert(
        cycle_mod.worktree_config(_operator_request(root, db, cfg), slug=ident, files=files),
        str(row["merged_sha"]))
    if args.json:
        sys.stdout.write(json.dumps(
            {"cycle_id": ident, "reverted_sha": row["merged_sha"],
             "status": result.status, "sha": result.sha, "detail": result.detail},
            indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(f"revert {ident}: {result.status}"
                         + (f" {result.sha}" if result.sha else "")
                         + (f"\n  {result.detail}" if result.detail else "") + "\n")
    return 0 if result.landed else 1


def _cmd_halt(args, root: Path, db: Path) -> int:
    """Write the durable stop, then release the lease.

    Deliberately does not read the config: a loop whose config is broken is
    exactly when an operator needs to stop it, and ``halt`` must not be the
    verb that fails to parse. The price of that choice is that a cycle
    configured with a non-default ``lease.key`` cannot be found here, so the
    release is best effort and says which key it looked for.

    Order matters. The stop file is written **first**: it is what refuses the
    next cycle, so releasing afterwards leaves no window in which a fresh cycle
    could claim the freed key. A cycle that is still alive re-checks the file
    before every stage and halts on its own; its own ``release_lease`` then
    finds no row, which the cycle already tolerates.
    """
    from mini_ork.rsi.config import LeaseConfig
    from mini_ork.stores import lease

    key = LeaseConfig().key
    stop = Path(db).parent / "rsi" / "DISABLED"
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text(f"halted by operator at {int(time.time())}\n", encoding="utf-8")
    sys.stdout.write(f"halted: wrote {stop}\n")
    try:
        held = lease.current_lease(str(db), key)
    except Exception:  # noqa: BLE001 - reporting must not fail the stop
        held = None
    if not held:
        return 0
    token = str(held.get("owner_token") or "")
    released = False
    try:
        released = bool(token) and lease.release_lease(str(db), key, token)
    except Exception:  # noqa: BLE001 - the TTL is the backstop
        released = False
    if released:
        sys.stdout.write(
            f"  released lease {key} (held by {token}); a cycle that is still "
            f"running stops at its next stage check\n")
    else:
        sys.stdout.write(
            f"  lease {key} held by {token or '?'} was not released here; it "
            f"expires on its own TTL\n")
    return 0


def _cmd_resume(args, root: Path, db: Path) -> int:
    stop = Path(db).parent / "rsi" / "DISABLED"
    if stop.exists():
        stop.unlink()
        sys.stdout.write(f"resumed: removed {stop}\n")
    else:
        sys.stdout.write(f"resumed: {stop} was not present\n")
    return 0


# --------------------------------------------------------------------------
# Research + impact: the operator surface of the research-driven loop
# --------------------------------------------------------------------------


def _technique_status_counts(db: Path, domain: str) -> dict[str, int]:
    """``{status: count}`` of one domain's unadopted techniques, from the DB.

    Read-only and table-tolerant: a fresh home has no ``state.db`` and an
    un-migrated one has no ``rsi_candidates`` table. Neither is an error for a
    *reporting* verb — both simply mean "nothing recorded".
    """
    import sqlite3

    if not Path(db).exists():
        return {}
    try:
        con = sqlite3.connect(str(db))
        try:
            rows = con.execute(
                "SELECT status, COUNT(*) FROM rsi_candidates"
                " WHERE kind='technique_unadopted' AND domain=? GROUP BY status",
                (domain,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return {str(status): int(count) for status, count in rows}


def _render_research(payload: Mapping[str, Any]) -> str:
    domains = payload.get("domains") or []
    if not domains:
        return "no research domains\n"
    lines: list[str] = []
    for entry in domains:
        lines.append(f"domain {entry['domain']}")
        review = entry.get("review_id") or "(no review)"
        lines.append(f"  review      {review} (age {entry.get('age_days')} day(s))")
        lines.append(f"  ranked      {entry.get('ranked')} row(s); "
                     f"{entry.get('eligible')} eligible")
        statuses = entry.get("techniques_by_status") or {}
        if statuses:
            rendered = ", ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
            lines.append(f"  unadopted   {rendered}")
        if entry.get("error"):
            lines.append(f"  error       {entry['error']}")
        for row in entry.get("refresh") or []:
            lines.append(f"  refresh     {row['action']} ({row.get('reason')})")
    return "\n".join(lines) + "\n"


def _cmd_research(args, root: Path, db: Path) -> int:
    from mini_ork.rsi import monitor, research

    cfg = _load_config(args, root)
    home = Path(args.home) if args.home else Path(db).parent
    now = int(time.time())
    wanted = [args.domain] if args.domain else None

    entries: list[dict[str, Any]] = []
    for domain in sorted(research.DEFAULT_SPECS):
        if wanted is not None and domain not in wanted:
            continue
        spec = research.DEFAULT_SPECS[domain]
        path = research.latest_review(home, spec.review_glob)
        rows: list[dict] = []
        error = None
        if path is not None:
            try:
                rows = research.load_ranked(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                error = f"unreadable review {path}: {exc}"
        eligible = sum(
            1 for row in rows
            if research.eligible(
                row,
                verdicts=cfg.research.verdicts,
                skip_disagreement=cfg.research.skip_disagreement,
                skip_single_rater=cfg.research.skip_single_rater))
        entry: dict[str, Any] = {
            "domain": domain,
            "review": str(path) if path is not None else None,
            "review_id": path.parent.name if path is not None else None,
            "age_days": monitor.review_age_days(home, spec, now=now),
            "ranked": len(rows),
            "eligible": eligible,
            "techniques_by_status": _technique_status_counts(db, domain),
        }
        if error:
            entry["error"] = error
        if args.refresh:
            entry["refresh"] = monitor.maybe_refresh(
                root, home, cfg, now=now, force=True, domains=[domain])
        entries.append(entry)

    payload = {"domains": entries}
    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_render_research(payload))
    return 0


def _impact_status(home: Path, args) -> int:
    from mini_ork.rsi import impact_state, monitor

    try:
        state = impact_state.load_state(home)
    except impact_state.ImpactStateError as exc:
        sys.stderr.write(f"rsi-cycle: {exc}\n")
        return 1
    halt = monitor.adoption_halted(home)

    rows: list[dict[str, Any]] = []
    for bench, entry in sorted((state.get("benchmarks") or {}).items()):
        canaries = entry.get("canary") or []
        scores = [float(item["score"]) for item in canaries if "score" in item]
        rows.append({
            "benchmark": bench,
            "epoch": entry.get("epoch"),
            "looks": entry.get("looks"),
            "accepted": entry.get("accepted"),
            "rejected": entry.get("rejected"),
            "canaries": len(canaries),
            "peak": max(scores) if scores else None,
            "latest": scores[-1] if scores else None,
        })

    if args.json:
        sys.stdout.write(json.dumps(
            {"benchmarks": rows, "halted": halt}, indent=2, sort_keys=True) + "\n")
        return 0

    if rows:
        sys.stdout.write(_table(
            [(r["benchmark"], str(r["epoch"]), str(r["looks"]), str(r["accepted"]),
              str(r["rejected"]), str(r["canaries"]), str(r["peak"]),
              str(r["latest"])) for r in rows],
            ("benchmark", "epoch", "looks", "accepted", "rejected", "canaries",
             "peak", "latest")) + "\n")
    if halt is not None:
        sys.stdout.write("adoption halted: "
                         + json.dumps(halt, sort_keys=True) + "\n")
    if not rows and halt is None:
        sys.stdout.write("no benchmarks recorded\n")
    return 0


def _impact_canary(args, root: Path, home: Path) -> int:
    from mini_ork.rsi import config as cfg_mod
    from mini_ork.rsi import monitor, research

    try:
        cfg = _load_config(args, root)
    except _SetupError as exc:
        # ``--root`` may name the checkout being scored, which need not carry the
        # operator's config. The canary is a measurement, so fall back to the
        # default knobs and every domain that has a benchmark instead of
        # refusing to measure at all — loudly, so it is never a silent default.
        if args.config:
            raise
        sys.stderr.write(
            f"rsi-cycle: no config at {root} ({exc}); canary uses default "
            "knobs and every known benchmark domain\n")
        cfg = cfg_mod.CycleConfig(
            budget=cfg_mod.BudgetConfig(daily_cap_usd=1.0, per_cycle_cap_usd=1.0),
            decide=cfg_mod.DecideConfig(auto_merge=False),
            research=cfg_mod.ResearchConfig(
                enabled=True, domains=tuple(sorted(research.DEFAULT_SPECS))))

    if not cfg.research.enabled:
        sys.stdout.write("research is disabled; no canary run\n")
        return 0

    now = int(time.time())
    results: list[dict[str, Any]] = []
    for domain in cfg.research.domains:
        spec = research.DEFAULT_SPECS.get(domain)
        if spec is None:
            continue
        results.append(monitor.run_canary(
            root, home, cfg, spec.benchmark, now=now))

    if args.json:
        sys.stdout.write(json.dumps(
            {"canaries": results}, indent=2, sort_keys=True) + "\n")
        return 0
    if not results:
        sys.stdout.write("no enabled research domain has a benchmark\n")
    for result in results:
        if result.get("status") == "error":
            sys.stdout.write(
                f"{result['bench']}: error ({result.get('reason')})\n")
        else:
            sys.stdout.write(
                f"{result['bench']}: {result['status']} "
                f"score={result.get('score')}\n")
    return 0


def _cmd_impact(args, root: Path, db: Path) -> int:
    from mini_ork.rsi import impact_state

    home = Path(db).parent
    action = (args.ident or "status").lower()

    if action == "status":
        return _impact_status(home, args)

    if action == "rotate":
        bench = args.sub
        if not bench:
            sys.stderr.write(
                "usage: mini-ork rsi-cycle impact rotate <benchmark>\n")
            return 2
        try:
            state = impact_state.load_state(home)
            entry = impact_state.rotate(state, bench)
            impact_state.save_state(home, state)
        except impact_state.ImpactStateError as exc:
            sys.stderr.write(f"rsi-cycle: {exc}\n")
            return 1
        sys.stdout.write(f"{bench}: epoch {entry['epoch']}\n")
        return 0

    if action == "canary":
        return _impact_canary(args, root, home)

    sys.stderr.write(
        "usage: mini-ork rsi-cycle impact [status | rotate <benchmark> | canary] [--json]\n")
    return 2


# --------------------------------------------------------------------------
# Parser / entry point
# --------------------------------------------------------------------------


_VERBS = ("run", "plan", "list", "show", "doctor", "quarantine", "revert",
          "halt", "resume", "research", "impact", "help")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mini-ork rsi-cycle", add_help=False)
    p.add_argument("verb", nargs="?", default="run", choices=_VERBS)
    p.add_argument("ident", nargs="?", default=None,
                   help="show <id> | quarantine <action> | revert <id>")
    p.add_argument("sub", nargs="?", default=None,
                   help="quarantine show|release|clean <cycle_id>")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-probes", action="store_true",
                   help="plan: skip the deterministic probes (DB + AST only)")
    p.add_argument("--domain", default=None,
                   choices=["learning", "routing", "verification"])
    p.add_argument("--max", type=int, default=None,
                   help="plan: how many candidates to select")
    p.add_argument("--status", default=None, help="list: filter by status")
    p.add_argument("--limit", type=int, default=_DEFAULTS["limit"])
    p.add_argument("--cycles", action="store_true", help="list: cycles instead of candidates")
    # --- the run verb
    p.add_argument("--config", default=None,
                   help="run: config file (else MO_RSI_CONFIG, else config/rsi-cycle.yaml)")
    p.add_argument("--once", action="store_true", help="run: exactly one cycle (default)")
    p.add_argument("--interval", type=int, default=None,
                   help="run: seconds between cycles; implies a loop")
    p.add_argument("--dry-run", action="store_true",
                   help="run: print the would-be kickoff; write and dispatch nothing")
    p.add_argument("--suggest", action="store_true",
                   help="run: run SUGGEST but do not dispatch an implementer")
    p.add_argument("--probe-run", action="store_true",
                   help="run: include the paid live probes (opt-in, charged to the budget)")
    p.add_argument("--max-cycles", type=int, default=None,
                   help="run: stop after N cycles (0 = unlimited, loop only)")
    p.add_argument("--budget", type=float, default=None,
                   help="run: per-cycle USD cap; may only lower the config's value")
    p.add_argument("--candidate", default=None,
                   help="run: work on this candidate id instead of the queue's top")
    p.add_argument("--refresh", action="store_true",
                   help="research: launch the domain's review recipe now")
    p.add_argument("--db", default=None)
    p.add_argument("--home", default=None)
    p.add_argument("--root", default=None)
    p.add_argument("-h", "--help", action="store_true")
    return p


def build_parser() -> argparse.ArgumentParser:
    """Exposed under the name the subcommand walkers look for."""
    return _parser()


def main(argv: Sequence[str] | None = None, *, root: str | Path | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse already printed the reason
        return int(exc.code or 2)
    if args.help or args.verb == "help":
        sys.stdout.write(_USAGE)
        return 0
    if args.verb == "show" and not args.ident:
        sys.stderr.write("usage: mini-ork rsi-cycle show <cycle_id|candidate_id>\n")
        return 2
    if args.verb == "revert" and not args.ident:
        sys.stderr.write("usage: mini-ork rsi-cycle revert <cycle_id>\n")
        return 2
    if args.verb == "quarantine" and args.ident and args.ident.lower() not in (
            "list", "show", "release", "clean"):
        sys.stderr.write("usage: mini-ork rsi-cycle quarantine [list|show|release|clean] <id>\n")
        return 2

    base = _engine_root(args.root or root)
    db = _db_path(base, db=args.db, home=args.home)
    verbs = {
        "plan": _cmd_plan,
        "list": _cmd_list,
        "show": _cmd_show,
        "quarantine": _cmd_quarantine,
        "revert": _cmd_revert,
        "halt": _cmd_halt,
        "resume": _cmd_resume,
        "doctor": _cmd_doctor,
        "research": _cmd_research,
        "impact": _cmd_impact,
    }
    try:
        handler = verbs.get(args.verb, _cmd_run)
        return handler(args, base, db)
    except _SetupError as exc:
        sys.stderr.write(f"rsi-cycle: {exc}\n")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
