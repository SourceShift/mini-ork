"""`mini-ork metrics lessons` — does each injected lesson help the node that got it?

Read-only over ``lesson_outcome_exposures`` (migration 0092). Per lesson and
arm it prints how many run-nodes received it, how many succeeded on the first
try, and one of four statuses — ``insufficient_n``, ``insufficient_events``,
``saturated`` or ``measured``. Then the contrasts from
``mini_ork.learning.lesson_outcomes``, each labelled for what it is:

- **clean arm** — delivered minus held-out. Causal-looking and, while the
  withheld slice is thin, ``insufficient_held_out``. It is the number this
  command exists to make possible; it starts reporting by itself when the arm
  fills, and it is the only contrast that gates the rerank penalty in
  ``item_utility`` (value *and* significance both come from the arm).
- **population gap** — the same ``(node_type, task_class)`` stratum in the
  window, direct-standardized to the treated weights. Correlation, printed as
  ``measured_correlational`` and allowed no control over anything.
- **run-level** — the run's publish outcome per lesson, labelled "not
  attribution": every lesson shown to any node of a published run is credited.

Exit codes fail closed rather than print a zero for a store that is not there:
``0`` a report was produced (even if every row is ``insufficient``), ``1`` no
``state.db``, ``2`` the database exists but migration 0092 has not run.

Two tiers, one command. On a laptop it resolves in-process against
``$MINI_ORK_HOME/state.db`` — no server, no daemon, no network. On a control
plane run ``--resolve-only`` as the single writer and let consumers read with
``--no-resolve`` against a shared store; every surface stamps ``as of``
``resolved_at``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from mini_ork.learning import lesson_outcomes as lo

_TOP = 25


def _default_db() -> str:
    if os.environ.get("MINI_ORK_DB"):
        return os.environ["MINI_ORK_DB"]
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    return os.path.join(home, "state.db")


def _fmt_rate(value) -> str:
    return "  —  " if value is None else f"{value:.3f}"


def _fmt_ts(ts) -> str:
    if not ts:
        return "never"
    import datetime  # noqa: PLC0415 — only the human path needs it

    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%MZ")


def _contrasts(db: str, lesson_keys: list[str], *, since_day, window) -> dict:
    """The three contrasts for each lesson named in ``lesson_keys``."""
    out: dict[str, dict] = {}
    for key in lesson_keys:
        kind, _, ident = key.partition(":")
        if not ident:
            continue
        out[key] = {
            "summary": lo.lesson_summary(db, kind=kind, ident=ident),
            "clean_arm": lo.clean_arm_contrast(
                db, kind=kind, ident=ident, since_day=since_day),
            "population": lo.population_gap(
                db, kind=kind, ident=ident, window=window, since_day=since_day),
            "run_level": lo.run_level(db, kind=kind, ident=ident),
            "effect": lo.effect(db, kind=kind, ident=ident),
        }
    return out


def build_parser() -> argparse.ArgumentParser:
    """The argv contract, shared by ``main`` and any in-process caller."""
    p = argparse.ArgumentParser(
        prog="mini-ork metrics lessons",
        description="Per-lesson node outcomes: does a lesson help the node it was given to?",
    )
    p.add_argument("--db", default=None,
                   help="state.db (default: $MINI_ORK_DB, else <home>/state.db)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--since-days", type=int, default=None,
                   help="ignore exposures older than N days (default: all)")
    p.add_argument("--window-days", type=int, default=None,
                   help=f"population-control window (default: {lo.DEFAULT_WINDOW_DAYS})")
    p.add_argument("--min-n", type=int, default=None,
                   help=f"run-nodes per lesson before a rate counts (default: {lo.DEFAULT_MIN_N})")
    p.add_argument("--min-events", type=int, default=None,
                   help="non-clean nodes per arm before a rate counts "
                        f"(default: {lo.DEFAULT_MIN_EVENTS})")
    p.add_argument("--lesson", default=None, metavar="KIND:ID",
                   help="one lesson, e.g. pattern:pat-a7551171398c")
    p.add_argument("--population", action="store_true",
                   help="always print the population contrast, even without --lesson")
    p.add_argument("--top", type=int, default=_TOP,
                   help=f"contrasts for the top N lessons by n (default: {_TOP})")
    p.add_argument("--runs", action="store_true",
                   help="include the per-run resolution records")
    p.add_argument("--no-resolve", action="store_true",
                   help="read only; never fold new runs into the fact table")
    p.add_argument("--resolve-only", action="store_true",
                   help="fold new runs and exit (the control-plane writer)")
    return p


def main(argv: list[str] | None = None, *, stdout=None, stderr=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    a = build_parser().parse_args(argv)
    db = a.db or _default_db()

    if not os.path.exists(db):
        err.write(f"mini-ork metrics lessons: no state.db at {db}\n"
                  "  Nothing is measured yet. `mini-ork init` creates one.\n")
        return 1

    # Checked before resolving: the resolver creates the two tables on a dev
    # DB (``ensure_schema``), and a *read* command must not quietly build
    # schema on someone's shared store. Missing tables is a clear, non-zero
    # answer that names the migration.
    probe = lo.connect(db)
    ok = lo.has_tables(probe) if probe is not None else False
    if probe is not None:
        probe.close()
    if not ok:
        err.write(f"mini-ork metrics lessons: {db} has no lesson_outcome_exposures table.\n"
                  "  Apply db/migrations/0092_lesson_outcome_exposures.sql "
                  "(`mini-ork migrate`).\n")
        return 2

    resolved = None
    if not a.no_resolve:
        resolved = lo.resolve_finished(db)

    if a.resolve_only:
        payload = {"resolved": resolved, "ledger": lo.ledger_stats(db)}
        if a.json:
            out.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        else:
            _print_resolve(payload, out)
        return 0

    stats = lo.ledger_stats(db)
    if not stats["available"]:
        err.write(f"mini-ork metrics lessons: {db} has no lesson_outcome_exposures table.\n"
                  "  Apply db/migrations/0092_lesson_outcome_exposures.sql "
                  "(`mini-ork migrate`).\n")
        return 2

    lo_n = a.min_n if a.min_n is not None else lo.min_n()
    lo_events = a.min_events if a.min_events is not None else lo.min_events()
    since_day = None
    if a.since_days is not None:
        import time  # noqa: PLC0415

        since_day = (int(time.time()) - a.since_days * 86400) // 86400
    rows = lo.exposures(db, since_day=since_day)
    if a.lesson:
        kind, _, ident = a.lesson.partition(":")
        if not ident:
            err.write("--lesson wants KIND:ID, e.g. pattern:pat-a7551171398c\n")
            return 2
        rows = [r for r in rows if r["source_kind"] == kind and r["source_id"] == ident]
    summary = lo.summarize(rows, min_n_value=lo_n, min_events_value=lo_events)
    lessons = sorted(
        summary["lessons"],
        key=lambda entry: -int(entry["arms"].get("delivered", {}).get("n_nodes") or 0),
    )
    if a.lesson:
        keys = [entry["lesson"] for entry in lessons]
    else:
        keys = [entry["lesson"] for entry in lessons[:max(0, a.top)]]
    result = {
        "ledger": stats,
        "resolved": resolved,
        # Is the withheld slice reproducible from today's bucket? This is the
        # honesty check on the clean arm: rows in the ledger that the current
        # predicate would not withhold mean the arm is partly historical, and
        # lessons permanently inside the arm that never yield a held row mean
        # the sink is not wired on their channel.
        "arm": lo.arm_diagnostic(db),
        "min_n": lo_n,
        "min_events": lo_events,
        "unstable": summary["unstable"],
        "lessons": lessons,
        "contrasts": _contrasts(db, keys, since_day=since_day, window=a.window_days),
    }
    if a.runs:
        result["runs"] = lo.resolve_runs(db)
    if a.json:
        out.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    else:
        _print_human(result, out, show_population=bool(a.lesson or a.population))
    return 0


def _print_resolve(payload: dict, out) -> None:
    resolved, stats = payload["resolved"] or {}, payload["ledger"]
    out.write("Lesson outcomes — resolve\n")
    out.write(f"  runs marked {resolved.get('runs', 0)} · exposures written "
              f"{resolved.get('exposures', 0)} · skipped {resolved.get('skipped', 0)}")
    if resolved.get("note"):
        out.write(f" · note={resolved['note']}")
    out.write("\n")
    out.write(f"  ledger now: {stats.get('rows', 0)} rows · {stats.get('runs', 0)} runs · "
              f"{stats.get('lessons', 0)} lessons · held_out {stats.get('held_out_rows', 0)} rows "
              f"({stats.get('held_out_lessons', 0)} lessons)\n")


def _print_arm(arm: dict, out) -> None:
    """One screen for the clean arm's health, printed before the contrasts.

    A thin arm is the reason every clean-arm number reads ``insufficient``, so
    it is stated here rather than inferred from a status string. The two
    disagreements are the ones an operator must act on: a held row today's
    bucket would deliver (stale flag or a second writer) and a lesson inside
    the arm that has never been withheld at volume (missing sink).
    """
    if arm.get("status") != "measured":
        out.write("\n  arm: unavailable (no lesson_injections table)\n")
        return
    out.write(f"\n  arm: rate={arm['rate']:.3f} · {arm['lessons']} lessons · "
              f"held {arm['held_rows']} rows on {arm['held_lessons']} lessons"
              f" since {_fmt_ts(arm.get('arm_start'))}"
              f" · inconsistent {arm['inconsistent_count']}"
              f" · never-withheld {arm['missing_expected_count']}\n")
    for entry in arm.get("inconsistent") or []:
        out.write(f"    ! {entry['lesson']} held {entry['held']}×/{entry['n']} — "
                  "today's bucket would deliver it (stale flag or a second "
                  "writer; the row still counts as control)\n")
    for entry in arm.get("missing_expected") or []:
        out.write(f"    ? {entry['lesson']} n={entry['n']} "
                  f"({entry['since_arm']} since the arm) and never withheld — "
                  "its channel supplies no sink\n")


def _print_human(result: dict, out, *, show_population: bool) -> None:
    stats = result["ledger"]
    out.write("Lessons — node outcomes (per-lesson, node-granular)\n")
    if not stats.get("rows"):
        out.write("  No exposures yet. Terminal runs are folded on the next run of this\n"
                  "  command unless --no-resolve; nothing is measured until then.\n")
        return
    out.write(f"  ledger: {stats['rows']} rows · {stats['runs']} runs · "
              f"{stats['lessons']} lessons · held_out {stats['held_out_rows']} rows "
              f"({stats['held_out_lessons']} lessons)\n")
    out.write(f"  resolved: {stats['resolved_runs']} runs (as of "
              f"{_fmt_ts(stats.get('resolved_at'))})\n")
    out.write(f"  floors: n>={result['min_n']}, non-clean events>={result['min_events']}; "
              "clean = attempt 1 succeeded with no retry\n")
    if result.get("unstable"):
        out.write(f"  excluded: {result['unstable']} rows on positional c:<n> ids "
                  "(kickoff constraints, not lessons)\n")

    header = (f"  {'lesson':38s}  {'arm':9s} {'n':>5s} {'n_prim':>6s} {'clean':>6s}"
              f" {'retry':>6s} {'fail':>5s}  status")
    out.write("\n" + header + "\n")
    shown = 0
    for entry in result["lessons"]:
        for arm, m in entry["arms"].items():
            shown += 1
            if shown > _TOP * 2 and not result.get("_all"):
                continue
            # The status shown is the primary-node verdict when the lesson
            # reached a primary node; otherwise the all-type one (a
            # researcher-only lesson reads `saturated`, not a fake `measured`).
            status = m["primary_status"] if m.get("n_primary") else m["status"]
            out.write(f"  {entry['lesson'][:38]:38s}  {arm:9s} {m['n_nodes']:5d} "
                      f"{m.get('n_primary', 0):6d} "
                      f"{_fmt_rate(m['clean_rate']):>6s} {_fmt_rate(m['retry_rate']):>6s} "
                      f"{m['failed']:5d}  {status}\n")
    if shown > _TOP * 2:
        out.write(f"  … {shown - _TOP * 2} more arm rows; --json for all, "
                  "--lesson KIND:ID for one\n")
    if not shown:
        out.write("  (no stable lesson rows in range)\n")
        return

    _print_arm(result.get("arm") or {}, out)
    out.write("\n  contrasts\n")
    for key, c in result["contrasts"].items():
        summary = c["summary"]
        arm = c["clean_arm"]
        gap = c["population"]
        eff = c["effect"]
        out.write(f"  [{key}]\n")
        if summary.get("clean_rate_primary") is not None:
            out.write(f"    primary nodes: clean {summary['clean_rate_primary']:.3f} "
                      f"n={summary['n_primary']} (all types: "
                      f"{_fmt_rate(summary.get('clean_rate'))} n={summary.get('n_nodes')}) "
                      f"· {summary['status']}\n")
        else:
            out.write(f"    primary nodes: none reached (all types n={summary.get('n_nodes')}) "
                      f"· {summary['status']}\n")
        out.write(f"    clean arm (delivered - held_out): {arm['status']} "
                  f"value={arm.get('value')} p={arm.get('p')} "
                  f"delivered={arm.get('n_delivered')} held_out={arm.get('n_held_out')}\n")
        if show_population or gap.get("status") != "unavailable":
            out.write(f"    population [correlational]: {gap['status']} "
                      f"value={gap.get('value')} treated={gap.get('treated_rate')}"
                      f" control={gap.get('control_rate')} coverage={gap.get('coverage')}\n")
        rl = c["run_level"]
        if rl.get("status") == "measured":
            out.write(f"    run-level (not attribution): publish {rl['rate']} "
                      f"({rl['wins']}/{rl['wins'] + rl['losses']})\n")
        out.write(f"    effect: {eff['value']} ({eff['status']})\n")
    if not result["contrasts"]:
        out.write("  (no lesson carried enough rows to name)\n")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
