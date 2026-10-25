"""``mini-ork rsi-cycle`` — the operator surface of the self-improvement loop.

At this step the command is **read-only**: ``plan`` harvests the three domains
and prints the ranked queue, and nothing is written to the ledger, no worktree
is created and no model is called. That is deliberate — the queue is useful on
day one, at zero spend, and it gives every later stage a reference output to be
tested against.

The verbs here are the ones an operator reaches for when a cycle misbehaves:

* ``plan``   — what would be fixed next, and why everything else was dropped.
* ``list``   — what the ledger already knows (candidates, or ``--cycles``).
* ``show``   — one candidate or cycle in full, including the gate results.
* ``doctor`` — is this checkout able to run a cycle at all, right now.

Exit codes: ``0`` ok, ``1`` a check failed / internal error, ``2`` usage,
config or an unusable database.
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
mini-ork rsi-cycle — harvest gaps in learning, routing and verification.

Usage:
  mini-ork rsi-cycle plan [--json] [--no-probes] [--domain D] [--max N]
  mini-ork rsi-cycle list [--status S] [--limit N] [--cycles] [--json]
  mini-ork rsi-cycle show <cycle_id|candidate_id> [--json]
  mini-ork rsi-cycle doctor [--json]

Global: --db PATH  --home DIR  --root DIR

``plan`` is read-only and spends nothing: it runs the deterministic harvest
(SQLite predicates, AST rules, and — unless --no-probes — the cheap probes)
and prints the queue a cycle would work from. ``doctor`` is the first thing to
run when a cycle misbehaves: it checks the DB, the migration state, the probe
binaries, the worktree driver and the origin remote.
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
    if db:
        return Path(db).expanduser()
    if home:
        return Path(home).expanduser() / "state.db"
    env_db = os.environ.get("MINI_ORK_DB")
    if env_db:
        return Path(env_db).expanduser()
    env_home = os.environ.get("MINI_ORK_HOME")
    if env_home:
        return Path(env_home).expanduser() / "state.db"
    return root / ".mini-ork" / "state.db"


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


def _merged_ancestor_signatures(root: Path, rows: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Signatures whose ``merged_sha`` is already an ancestor of ``origin/main``.

    Requires git, which is why the queue never does it. If ``origin/main``
    cannot be resolved the set is empty: a merged gap then returns to the queue
    and costs one re-check, rather than a real regression staying hidden
    forever because the remote moved.
    """
    shas = {str(r["merged_sha"]) for r in rows if r.get("merged_sha")}
    if not shas:
        return frozenset()
    try:
        base = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "origin/main"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if base.returncode != 0:
        return frozenset()

    reachable: set[str] = set()
    for sha in shas:
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), "merge-base", "--is-ancestor", sha, "origin/main"],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            reachable.add(sha)
    return frozenset(
        str(r["signature"]) for r in rows if r.get("merged_sha") in reachable
    )


def _ledger_view(con, candidates, *, root: Path):
    """Read what the queue needs. One query per distinct signature is fine here:
    a harvest produces dozens, not thousands, and the alternative is a bulk read
    whose SQL would have to mirror the ledger's own signature index."""
    from mini_ork.rsi import ledger
    from mini_ork.rsi.queue import LedgerView

    by_signature: dict[str, Mapping[str, Any]] = {}
    merged_rows: list[Mapping[str, Any]] = []
    for sig in sorted({c.signature for c in candidates}):
        row = ledger.candidate_by_signature(con, sig)
        if row is None:
            continue
        by_signature[sig] = row
        if row.get("status") == "merged":
            merged_rows.append(row)

    claimed = {
        str(r["candidate_id"])
        for r in con.execute(
            "SELECT candidate_id FROM rsi_cycles"
            " WHERE status = 'running' AND candidate_id IS NOT NULL"
        )
    }
    return LedgerView(
        by_signature=by_signature,
        claimed_ids=frozenset(claimed),
        merged_ancestor_signatures=_merged_ancestor_signatures(root, merged_rows),
    )


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

    now = int(time.time()) if now is None else int(now)
    con = _open_ledger(db)
    try:
        # The probes are subprocesses and would otherwise resolve their own DB
        # from the ambient environment — a `--db` flag that the harvest honours
        # and `metrics` does not is how one cycle reads two databases.
        with scoped_environ({"MINI_ORK_DB": str(db), "MINI_ORK_ROOT": str(root)}):
            probe_results = _run_probes(root, enabled=probes_enabled)
            report = harvest_mod.harvest(root, con, now=now, probes=probe_results)
        view = _ledger_view(con, report.candidates, root=root)
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
        max_candidates=args.max,
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
    return "\n".join(lines)


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
        proc = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError("no `origin` remote — merges must quarantine, not commit locally")
        return proc.stdout.strip()

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

    check("database", db_exists)
    check("migrations", migrations)
    check("rsi_tables", tables)
    check("lease_tables", lease_tables)
    check("binaries", probe_bin)
    check("origin_remote", origin)
    check("harvest_rules", rules)

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
# Parser / entry point
# --------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mini-ork rsi-cycle", add_help=False)
    p.add_argument("verb", nargs="?", default="plan",
                   choices=["plan", "list", "show", "doctor", "help"])
    p.add_argument("ident", nargs="?", default=None)
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-probes", action="store_true",
                   help="plan: skip the deterministic probes (DB + AST only)")
    p.add_argument("--domain", default=None,
                   choices=["learning", "routing", "verification"])
    p.add_argument("--max", type=int, default=_DEFAULTS["max"],
                   help="plan: how many candidates to select")
    p.add_argument("--status", default=None, help="list: filter by status")
    p.add_argument("--limit", type=int, default=_DEFAULTS["limit"])
    p.add_argument("--cycles", action="store_true", help="list: cycles instead of candidates")
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

    base = _engine_root(args.root or root)
    db = _db_path(base, db=args.db, home=args.home)
    try:
        if args.verb == "plan":
            return _cmd_plan(args, base, db)
        if args.verb == "list":
            return _cmd_list(args, base, db)
        if args.verb == "show":
            return _cmd_show(args, base, db)
        return _cmd_doctor(args, base, db)
    except _SetupError as exc:
        sys.stderr.write(f"rsi-cycle: {exc}\n")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
