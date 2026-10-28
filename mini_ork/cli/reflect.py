"""Sole runtime implementation of the mini-ork reflect command.

Mirrors the bash CLI byte-for-byte: same flags, same stdout/stderr lines,
same env-var opt-out toggles (MO_PATTERN_MINER, MO_CROSS_EPIC_GRADIENTS,
MO_BUG_REPORT_SWEEP, MO_RHO_AGGREGATE, MO_LANE_ROUTER), same SQLite writes.

This module owns the CLI surface and dispatches
the load-bearing pipeline call (`reflection_run`) to the ported Python
implementation in `mini_ork.learning.reflection_pipeline`. The rho aggregator,
lane router, trace-store writes, pattern-store, cross-epic-gradient, and
bug-report side channels are all native now (bash side-channel libs retired).
Parity is enforced by
`tests/unit/test_mini_ork_reflect_py.py` (eight standalone golden and
behavioral contracts captured after pre-retirement byte parity passed).

GEPA optimizer block (MO_OPTIMIZER=gepa) is intentionally NOT ported: the
default path (MO_OPTIMIZER unset) leaves the block fully skipped, which
matches the kickoff's "byte-identical to the pre-R4b reflect" invariant.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import io
import json
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

from mini_ork.context import scoped_environ

__all__ = ["main", "_resolve_reflect_model"]


# ── Defaults (mirror bash) ───────────────────────────────────────────────────
MINI_ORK_ROOT = Path(__file__).resolve().parents[2]


def _usage() -> str:
    return (
        "Usage: mini-ork reflect [--since <timestamp>] [--task-class <name>] [--dry-run]\n"
        "\n"
        "Run the reflection pipeline over recent execution traces to extract gradient\n"
        "signals, recurring patterns, and suggested workflow promotions.\n"
        "\n"
        "Options:\n"
        "  --since <timestamp>   Start of analysis window (ISO-8601 or unix ts, default: 24h ago)\n"
        "  --task-class <name>   Limit reflection to traces of this task class\n"
        "  --lane <lane>          Resolve reflection model from agents.yaml (default: reflector)\n"
        "  --dry-run             Show trace count that would be analyzed; skip LLM\n"
        "  --help                Show this help\n"
    )


def _resolve_reflect_model(lane: str, mini_ork_home: str) -> str:
    """Resolve a reflect model from the effective lane policy.

    Reads the same policy dispatch honours: the run-dir snapshot
    (``$MINI_ORK_RUN_DIR/config/agents.yaml``) > ``<home>/config/agents.yaml``
    > ``<root>/config/agents.yaml``, with the per-user overlay
    (``$MINI_ORK_AGENTS`` or ``<home>/config/agents.local.yaml``) merged on
    top. The raw template alone ignored the overlay and the run snapshot, so
    a reflect pass could land on a lane no other node was using.

    Falls back to the lane name verbatim when no policy is readable. A broken
    overlay is announced on stderr and also falls back to the lane name rather
    than taking the reflect pass down.
    """
    from mini_ork.dispatch.llm_dispatch import _effective_lanes  # noqa: PLC0415

    try:
        lanes = _effective_lanes(str(MINI_ORK_ROOT), mini_ork_home) or {}
    except Exception as exc:  # noqa: BLE001 — an overlay typo must not kill reflect
        sys.stderr.write(
            f"reflect: lane policy unreadable ({exc}); using lane {lane!r}\n")
        return lane
    return str(lanes.get(lane) or lanes.get("reflector") or lane)


# ── Argument parsing ─────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mini-ork-reflect",
        description="Reflection pipeline orchestrator (Python runtime).",
        add_help=False,  # bash prints its own help text; mirror it
    )
    p.add_argument("--since", type=str, default="")
    p.add_argument("--run-id", type=str, default="")
    p.add_argument("--task-class", type=str, default="")
    p.add_argument("--lane", type=str, default="")
    p.add_argument("--dry-run", action="store_true")
    # Scheduled cadence (see docs/operator/reflect-schedule.md). Deliberately
    # absent from _usage(): the help text is a byte-pinned golden.
    p.add_argument("--scheduled", action="store_true")
    p.add_argument("--config", type=str, default="")
    p.add_argument("--interval", action="store_true")
    p.add_argument("--help", "-h", action="store_true")
    return p


def _run_scheduled(args) -> int:
    """Dispatch the scheduled cadence tiers; never touch the reflect pipeline's
    own env/trace setup (this process is the scheduler, not a reflect pass)."""
    from mini_ork.learning import reflect_schedule  # noqa: PLC0415

    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    db = os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
    try:
        cfg = reflect_schedule.load_config(home, args.config or None)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if args.interval:
        return reflect_schedule.run_forever(home, db, cfg)
    return reflect_schedule.run_once(home, db, cfg)


# ── Trace writes (native trace_store; bash lib/trace_store.sh retired) ──────
@contextlib.contextmanager
def _temporary_environ(env: dict):
    """Expose a subprocess-style environment to an in-process native call."""
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _trace_write(payload_json: str, env: dict) -> None:
    """Write a trace via the native mini_ork.trace_store (was: bash
    lib/trace_store.sh::trace_write). Mirrors bash's
    `trace_write "$payload" >/dev/null 2>&1 || true` semantics — errors are
    swallowed so reflect never aborts on a tracing failure. Runs inside the
    given env so the lineage fallbacks (MINI_ORK_DB, MINI_ORK_RUN_ID, …)
    resolve exactly as they did in the former bash subprocess env."""
    from mini_ork import trace_store
    try:
        with _temporary_environ(env):
            trace_store.trace_write(payload_json)
    except Exception:
        pass


def _trace_finalizer(trace_id: str, trace_env: dict, state: dict) -> None:
    """atexit guard: land a terminal 'failure' row if the pipeline exits
    without writing its own terminal trace.

    Covers the exception/crash path only. It CANNOT cover the parent's
    subprocess-timeout path: that SIGKILLs this process and atexit never runs
    on SIGKILL. That path is covered parent-side by
    ``trace_store.finalize_reflect_traces(run_id)``."""
    if state.get("terminal"):
        return
    from mini_ork import trace_store
    try:
        payload = json.dumps(trace_store.enrich_stage_trace(
            {"trace_id": trace_id, "task_class": "__reflect__", "status": "failure"},
            node_type="reflector"))
        _trace_write(payload, trace_env)
    except Exception:
        pass


# ── Run-scoped trace selection ───────────────────────────────────────────────
@contextlib.contextmanager
def _run_scoped_trace_selection(rp, run_id: str):
    """Restrict the pipeline's trace selection to ``run_id`` for one pass.

    ``reflection_pipeline`` has a single selection point —
    ``_extract_trace_ids_sql``, looked up on the module at call time — and no
    run-id parameter. Rather than duplicate its extractor loop here (which
    would drift from its watermark and dispatch-failure handling), swap that
    one function for a run-scoped query for the duration of the pass and
    restore it afterwards. The restore matters: ``reflect.main`` is callable
    in-process, and a leaked scope would make the next un-scoped call silently
    analyse only the previous run's traces.

    The ``__reflect__`` exclusion mirrors the pipeline's own query, so this
    pass never analyses another reflect pass either.
    """
    original = rp._extract_trace_ids_sql

    def _scoped(db_path: str, since_ts: int, batch: int) -> list[str]:
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA busy_timeout=5000")
        try:
            rows = con.execute(
                "SELECT trace_id FROM execution_traces "
                "WHERE CAST(strftime('%s', created_at) AS INTEGER) >= ? "
                "AND run_id = ? "
                "AND (task_class IS NULL OR substr(task_class, 1, 2) != '__') "
                "ORDER BY created_at LIMIT ?",
                (int(since_ts), run_id, int(batch)),
            ).fetchall()
        finally:
            con.close()
        return [r[0] for r in rows]

    rp._extract_trace_ids_sql = _scoped
    try:
        yield
    finally:
        rp._extract_trace_ids_sql = original


# ── Dry-run branch ───────────────────────────────────────────────────────────
def _dry_run(since: int, task_class_filter: str, reflect_lane: str,
             gradient_model: str, db_path: str, run_id: str = "") -> None:
    """Mirror bash lines 122-134. sqlite3 from `MINI_ORK_DB`, optional
    task_class/run-id filters. Echoes 2-3 lines to stdout depending on filter."""
    count = 0
    if os.path.isfile(db_path):
        where = "created_at >= ?"
        params: list = [int(since)]
        if task_class_filter:
            where += " AND task_class = ?"
            params.append(task_class_filter)
        if run_id:
            where += " AND run_id = ?"
            params.append(run_id)
        try:
            con = sqlite3.connect(db_path)
            count = con.execute(
                f"SELECT COUNT(*) FROM execution_traces WHERE {where};", params
            ).fetchone()[0]
            con.close()
        except Exception:
            count = 0
    print(f"[dry-run] would analyze {count} trace(s) since {since}")
    print(f"[dry-run] lane: {reflect_lane} -> {gradient_model}")
    if task_class_filter:
        print(f"[dry-run] filter: task_class={task_class_filter}")
    if run_id:
        print(f"[dry-run] filter: run_id={run_id}")


# ── Detached-pass deadline ───────────────────────────────────────────────────
def _reflect_deadline_seconds() -> int | None:
    """Read ``MO_REFLECT_DEADLINE_S``; only a positive integer arms an alarm."""
    raw = os.environ.get("MO_REFLECT_DEADLINE_S", "")
    try:
        deadline = int(raw)
    except (TypeError, ValueError):
        return None
    return deadline if deadline > 0 else None


def _arm_reflect_deadline() -> int | None:
    """Bound the pass with ``SIGALRM`` when the lifecycle supplied a deadline.

    The detached post-run child has no parent's blocking-call timeout
    watching it, so a pass that blocks outside an LLM call (a DB lock wait,
    say) would otherwise live forever. POSIX only: where ``SIGALRM`` or
    ``alarm`` is unavailable this is skipped silently. Returns the armed bound
    (or ``None`` when nothing was installed).
    """
    deadline = _reflect_deadline_seconds()
    if deadline is None:
        return None
    sigalrm = getattr(signal, "SIGALRM", None)
    alarm = getattr(signal, "alarm", None)
    if sigalrm is None or alarm is None:
        return None

    def _on_deadline(_signum, _frame):
        sys.stderr.write(f"reflect: deadline {deadline}s reached; stopping\n")
        # SystemExit (not os._exit) so atexit handlers — in particular
        # _trace_finalizer — still land a terminal trace row.
        raise SystemExit(124)

    try:
        signal.signal(sigalrm, _on_deadline)
        alarm(deadline)
    except (OSError, ValueError):
        # Not the main thread, or the platform refuses the signal: fail soft.
        return None
    return deadline


def _disarm_reflect_deadline() -> None:
    """Cancel a previously armed deadline alarm (no-op when unarmed)."""
    alarm = getattr(signal, "alarm", None)
    if alarm is not None:
        with contextlib.suppress(Exception):
            alarm(0)


# ── Main ─────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = _build_parser()
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        arg = unknown[0]
        if arg.startswith("-"):
            sys.stderr.write(f"Unknown flag: {arg}. Try --help\n")
        else:
            sys.stderr.write(f"Unexpected argument: {arg}. Try --help\n")
        return 2

    if args.help:
        sys.stdout.write(_usage())
        return 0

    # Scheduled cadence: branch before any reflect-pass side effect (deadline
    # alarm, __reflect__ trace write, atexit registration) — the scheduler is
    # not itself a pass. --config/--interval without --scheduled stay unknown
    # flags so the historical CLI surface is unchanged.
    if not args.scheduled and (args.config or args.interval):
        flag = "--config" if args.config else "--interval"
        sys.stderr.write(f"Unknown flag: {flag}. Try --help\n")
        return 2
    if args.scheduled:
        return _run_scheduled(args)

    # A detached post-run pass has no parent timeout; arm the child's own
    # deadline before any work that could block. Disarmed at every normal
    # return so a callable ``main`` cannot leave a stray alarm behind.
    _arm_reflect_deadline()

    # ── env setup (mirror bash lines 38-72) ────────────────────────────────
    since = args.since or ""
    task_class_filter = args.task_class or ""
    run_id = args.run_id or os.environ.get("MINI_ORK_REFLECT_RUN_ID", "")
    reflect_lane = args.lane or os.environ.get("MINI_ORK_REFLECT_LANE", "")
    dry_run_env = os.environ.get("MINI_ORK_DRY_RUN", "0")
    dry_run = args.dry_run or (dry_run_env == "1")

    mini_ork_home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    db_path = os.environ.get("MINI_ORK_DB") or os.path.join(mini_ork_home, "state.db")
    os.environ["MINI_ORK_HOME"] = mini_ork_home
    os.environ["MINI_ORK_DB"] = db_path
    os.environ["MINI_ORK_ROOT"] = os.environ.get("MINI_ORK_ROOT", str(MINI_ORK_ROOT))

    if reflect_lane:
        os.environ["MINI_ORK_REFLECT_LANE"] = reflect_lane
    else:
        reflect_lane = "reflector"

    if not os.environ.get("MINI_ORK_GRADIENT_MODEL"):
        gradient_model = _resolve_reflect_model(reflect_lane, mini_ork_home)
        os.environ["MINI_ORK_GRADIENT_MODEL"] = gradient_model
    else:
        gradient_model = os.environ["MINI_ORK_GRADIENT_MODEL"]

    # Default since = 24h ago
    if not since:
        since = str(int(time.time()) - 86400)
    try:
        since_int = int(since)
    except ValueError:
        since_int = int(time.time()) - 86400
        since = str(since_int)

    # ── trace start ────────────────────────────────────────────────────────
    trace_id = f"tr-reflect-{int(time.time())}-{os.getpid()}"
    trace_env = {
        **os.environ,
        "MINI_ORK_ROOT": os.environ["MINI_ORK_ROOT"],
        "MINI_ORK_HOME": mini_ork_home,
        "MINI_ORK_DB": db_path,
    }

    _trace_state: dict = {"terminal": False}
    if not dry_run:
        # Heal the rows a hard-killed reflect left 'running' BEFORE opening our
        # own. Until now the only sweep lived at the run-start boundary, so a
        # home whose runs have stopped (the researcher's) kept a phantom pass
        # forever and every reader of execution_traces counted it. Age-guarded
        # (the helper's 12 min default), so a reflect genuinely in flight is
        # never touched; best-effort, because reflect must run even when the DB
        # is mid-write.
        with contextlib.suppress(Exception):
            from mini_ork import trace_store  # noqa: PLC0415
            trace_store.finalize_reflect_traces(db=db_path)
        _trace_write(
            json.dumps({
                "trace_id": trace_id,
                "task_class": "__reflect__",
                "status": "running",
            }),
            trace_env,
        )
        # Defensive terminal write if the pipeline exits without finalizing
        # (exception/crash). The SIGKILL path is the parent's to finalize.
        atexit.register(_trace_finalizer, trace_id, trace_env, _trace_state)

    # ── dry-run branch ─────────────────────────────────────────────────────
    if dry_run:
        _dry_run(since_int, task_class_filter, reflect_lane, gradient_model,
                 db_path, run_id)
        _disarm_reflect_deadline()
        return 0

    # ── dispatch to reflection_pipeline (ported Python) ────────────────────
    from mini_ork.learning import reflection_pipeline as rp

    # Inject a deterministic gradient-extraction stub if the env var names a
    # function defined in `reflection_pipeline`'s globals. This is the Python
    # mirror of bash's MINI_ORK_GRADIENT_EXTRACTOR_FN override hook (see
    # the native extractor). The test fixture defines `_rfl_stub` in this
    # module so happy-path cases can run without a live LLM.
    fn_name = os.environ.get("MINI_ORK_GRADIENT_EXTRACTOR_FN", "")
    if fn_name == "_rfl_stub":
        def _rfl_stub(trace_id: str):
            if trace_id == "trace-A":
                return [
                    '{"gradient_id":"g-A-0","target":"t","signal":"s0","suggested_change":"c0","evidence":"trace-A","confidence":0.5}',
                    '{"gradient_id":"g-A-1","target":"t","signal":"s1","suggested_change":"c1","evidence":"trace-A","confidence":0.4}',
                ]
            if trace_id == "trace-B":
                return [
                    '{"gradient_id":"g-B-0","target":"t","signal":"s0","suggested_change":"c0","evidence":"trace-B","confidence":0.7}',
                ]
            return []

        rp.set_gradient_extract(_rfl_stub)
        rp.set_gradient_store(lambda _g: None)
        rp.set_gradient_ensure_table(lambda: None)
    elif fn_name:
        candidate = getattr(rp, fn_name, None)
        if candidate is not None and callable(candidate):
            rp.set_gradient_extract(candidate)
            rp.set_gradient_store(getattr(rp, "_rfl_store_noop", lambda _g: None))
            rp.set_gradient_ensure_table(getattr(rp, "_rfl_ensure_noop", lambda: None))

    sys.stdout.write("=== mini-ork reflect ===\n")
    sys.stdout.write(f"    since:   {since}\n")
    sys.stdout.write(f"    filter:  {task_class_filter or 'all'}\n")
    sys.stdout.write(f"    lane:    {reflect_lane} -> {gradient_model}\n")
    sys.stdout.write("\n")

    if task_class_filter:
        sys.stderr.write("  [warn] --task-class filter not yet implemented in reflection_run; ignoring\n")

    reflect_start_ts = int(time.time())

    # ── side-channel: code findings (MO_CODE_FINDINGS) ──────────────────────
    # Harvest reviewer/verifier findings out of the run dirs into the
    # code_findings tables, so the "Your code" tab can group what reviews keep
    # finding in the same file.
    #
    # FIRST, before the pipeline and before every other channel. This is the
    # only producer of that tab's data and the cheapest step in the file (it
    # reads run dirs; it calls no model), yet as the eighth of eight it was the
    # first casualty of every truncation — an exception, a timeout, or the
    # SIGKILL a killed reflect leaves behind — which is how a home can show
    # 1930 leaked 'running' reflect rows and a tab that has been empty since
    # August. It also depends on nothing below it: it reads the run dirs'
    # review and verdict artifacts, never another channel's output.
    # Default ON with the same `get(name, "1") != "0"` opt-out form as its
    # neighbours. harvest() calls ensure_schema itself, so there is no schema
    # step here, and the whole block is fail-soft — a side-channel must never
    # crash reflect.
    #
    # The import sits INSIDE the try, unlike the neighbouring side-channels
    # that import above theirs (they fail-soft on the *call* only). This block
    # pulls a heavier dependency chain (sqlite/glob/subprocess + the learning
    # package), so an ImportError here must degrade to the
    # `[code_findings] skipped:` line rather than crash reflect — do not
    # "restore" it to match the neighbours.
    if os.environ.get("MO_CODE_FINDINGS", "1") != "0":
        try:
            from mini_ork.learning import code_findings
            cf_stats = code_findings.harvest(mini_ork_home, db=db_path)
            # Harvest dedupes within a run, but rows written before that rule
            # linger as verdict echoes of a reviewer's own row. Prune them right
            # after the harvest that would have skipped them, inside the same
            # fail-soft try (a prune failure is as harmless to reflect as a
            # harvest failure), and stay quiet unless it actually removed rows.
            cf_pruned = code_findings.prune_duplicates(db_path)
            if cf_pruned > 0:
                sys.stdout.write(
                    f"  [code_findings] pruned {cf_pruned} duplicate(s)\n"
                )
            # LF-R5: rows the OLD parser wrote (before the note classifier and
            # the artifact-file rule) linger because the harvest is
            # INSERT OR IGNORE by fingerprint. Drop the non-code ones and NULL
            # the file on rows filed under a run artifact, right after the
            # duplicate prune and inside the same fail-soft try (a maintenance
            # failure is as harmless to reflect as a harvest failure). Both stay
            # quiet unless they actually changed a row. ``reharvest`` is a
            # manual command and is deliberately NOT run here.
            cf_receipts = code_findings.prune_receipts(db_path)
            if cf_receipts > 0:
                sys.stdout.write(
                    f"  [code_findings] pruned {cf_receipts} non-code row(s)\n"
                )
            cf_artifacts = code_findings.clear_artifact_files(db_path)
            if cf_artifacts > 0:
                sys.stdout.write(
                    f"  [code_findings] cleared {cf_artifacts} artifact file path(s)\n"
                )
        except Exception as exc:  # a side-channel must never crash reflect
            sys.stderr.write(f"  [code_findings] skipped: {exc}\n")
            cf_stats = None
        if cf_stats is not None:
            sys.stdout.write(
                f"  [code_findings] harvested {cf_stats.get('runs', 0)} run(s), "
                f"{cf_stats.get('findings', 0)} finding(s)\n"
            )
            # A harvest that changed nothing because it *could not* is not the
            # same as one that found nothing, and only ``reason`` says which —
            # so a pass that stopped short says so on stderr, one line, with the
            # two settings the stop depends on. The stdout line above stays
            # byte-identical on the happy path.
            if cf_stats.get("reason") not in (None, "ok"):
                sys.stderr.write(
                    f"  [code_findings] nothing harvested: {cf_stats.get('reason')} "
                    f"(home={cf_stats.get('home')}, db={cf_stats.get('db')})\n"
                )

    run_stdout = io.StringIO()
    # Name this pass for its pipeline stages (the gradient_extract pass-stats
    # row reads it back). Scoped so the variable cannot leak into a caller's
    # environment; `scoped_environ` is the only sanctioned way to change
    # process env.
    with scoped_environ({"MO_REFLECT_PASS_ID": trace_id}):
        if run_id:
            with _run_scoped_trace_selection(rp, run_id):
                with contextlib.redirect_stdout(run_stdout):
                    suggestions_json = rp.reflection_run(since_int)
        else:
            with contextlib.redirect_stdout(run_stdout):
                suggestions_json = rp.reflection_run(since_int)
    sys.stdout.write(f"{suggestions_json.rstrip(chr(10))}\n")

    # ── side-channel: pattern_store (MO_PATTERN_MINER) ──────────────────────
    # Native: mine_from_traces() reimplements pattern_store_mine_from_traces in-
    # process (byte-parity verified on the real state.db — both return 9). Unlike
    # cross_epic's promote(), it returns without printing, so no stdout capture is
    # needed. try/except mirrors the retired bash wrapper's `|| echo 0`.
    patterns_written = 0
    if os.environ.get("MO_PATTERN_MINER", "1") != "0":
        window = os.environ.get("MO_PATTERN_MINER_WINDOW", "7d")
        min_cluster = os.environ.get("MO_PATTERN_MINER_MIN_CLUSTER", "3")
        from mini_ork.stores import pattern_store
        try:
            patterns_written = pattern_store.mine_from_traces(
                db_path=db_path, window=window, min_cluster=int(min_cluster)
            )
        except Exception:
            patterns_written = 0
        sys.stdout.write(f"  [pattern_miner] wrote {patterns_written or 0} pattern_records rows\n")

    # ── side-channel: contrastive lane mining (MO_MGM_MINER) ───────────────
    # Mines one run/node where lane W succeeded and lane L failed into
    # pattern_records, so the existing induction → promotion → verify path can
    # author and approve a lesson in the same pass. Infra failures are
    # report-only. Placed after the pattern_store miner and before induction so
    # a freshly-mined row is eligible for a lesson immediately.
    mgm_report = None
    if os.environ.get("MO_MGM_MINER", "1") != "0":
        try:
            from mini_ork.learning import contrastive_lanes
            mgm_report = contrastive_lanes.mine_into_patterns(
                db_path=db_path,
                window=os.environ.get("MO_MGM_MINER_WINDOW") or None,
                min_runs=os.environ.get("MO_MGM_MINER_MIN_RUNS", "3"),
            )
        except Exception as exc:  # a side-channel must never crash reflect
            sys.stderr.write(f"  [mgm_miner] skipped: {exc}\n")
            mgm_report = None
    if mgm_report is not None:
        sys.stdout.write(
            f"  [mgm_miner] {mgm_report.get('pairs', 0)} contrastive pair(s), "
            f"wrote {mgm_report.get('written', 0)} pattern_records row(s), "
            f"{len(mgm_report.get('routing') or [])} routing-only key(s)\n"
        )

    # ── side-channel: pattern induction (MO_PATTERN_INDUCE) ────────────────
    # The miner above names clusters; it does not understand them. This is the
    # step that reads the member traces and authors the lesson, following
    # Trace2Skill 2603.25158 stage 2 (parallel analyst proposal) and stage 3
    # (merge). Runs after the miner so a freshly-mined cluster is eligible in
    # the same pass. Default ON with an opt-out, like every other new gate here;
    # a cluster that already carries a lesson is never re-authored.
    induce_report = None
    try:
        from mini_ork.learning import pattern_induction
        if pattern_induction._induct_enabled():
            # Propagate the live reflector lane so induction stops defaulting to
            # ``codex`` (dead on chatgpt auth — see kickoff) and so a configured
            # ``MINI_ORK_INDUCE_MODEL`` overrides the gradient lane per site.
            induce_report = pattern_induction.induce_pending(
                db_path=db_path,
                limit=int(os.environ.get("MO_PATTERN_INDUCE_LIMIT", "20")),
                min_cluster=int(os.environ.get("MO_PATTERN_MINER_MIN_CLUSTER", "3")),
                model=os.environ.get("MINI_ORK_INDUCE_MODEL") or gradient_model,
            )
    except Exception as exc:  # a side-channel must never crash reflect
        sys.stderr.write(f"  [pattern_induct] skipped: {exc}\n")
        induce_report = None
    if induce_report is not None:
        sys.stdout.write(
            f"  [pattern_induct] authored {induce_report.get('induced', 0)} lesson(s), "
            f"{len(induce_report.get('skipped') or [])} cluster(s) left without one\n"
        )
        # Surface an every-call-failed cluster as one stderr line so the
        # operator can see WHY the cluster was skipped (and which lane was
        # actually dispatched). Reading the model/error off the entry — not the
        # env — keeps the line consistent with what the dispatch attempted.
        # The error tail is whatever the provider wrote to stderr (often a
        # multi-line 400/401 dump); collapse whitespace so the line stays
        # one line, matching the "ONE line" contract for an operator scan.
        for skipped in induce_report.get("skipped") or []:
            if skipped.get("reason") == "dispatch failed":
                err = " ".join((skipped.get("error") or "").split())
                sys.stderr.write(
                    f"  [pattern_induct] every analyst call failed on lane "
                    f"{skipped.get('model', '?')}: {err}\n"
                )
                break

    # Record this pass's induction health for `ledger.stage_health` and the
    # IDE Learn overview. A report of None means the stage was turned off or
    # raised, so there is nothing honest to record. Fail-soft like every other
    # side-channel: a ledger error must never fail reflect.
    if induce_report is not None and not dry_run:
        try:
            from mini_ork.learning.ledger import record_pass_stat

            skipped_entries = induce_report.get("skipped") or []
            induced = int(induce_report.get("induced", 0) or 0)
            failing = [
                s for s in skipped_entries
                if s.get("reason") == "dispatch failed"
            ]
            lane = ""
            last_error = ""
            if failing:
                lane = failing[-1].get("model") or ""
                last_error = failing[-1].get("error") or ""
            record_pass_stat(
                trace_id,
                "pattern_induction",
                inputs=induced + len(skipped_entries),
                outputs=induced,
                failures=len(failing),
                lane=lane,
                last_error=last_error,
                db=db_path,
            )
        except Exception:  # a side-channel must never crash reflect
            pass

    # ── side-channel: lesson themes (MO_THEMES) ───────────────────────────
    # Group gradient_records into themes so the prompt block stops carrying
    # 10k near-duplicates of the same observation. Runs AFTER pattern_induct
    # (which produces the gradient store this reads) and BEFORE the
    # learning-loop write-back (which prints `[learning] persisted`).
    # Default ON with the same `get(name, "1") != "0"` opt-out form as the
    # other side-channels; a side-channel must never crash reflect, so the
    # whole block is wrapped in a bare try/except that writes ONE stderr
    # line on failure and continues. See mini_ork/learning/themes.py for the
    # exact behaviour of assign_new and rollup_framework_bugs.
    themes_report: dict | None = None
    bugs_updated = 0
    promoted = 0
    if os.environ.get("MO_THEMES", "1") != "0":
        try:
            from mini_ork.learning import themes

            themes_report = themes.assign_new(db_path)
            bugs_updated = themes.rollup_framework_bugs(db_path)
            # Author + verify lessons for supported themes, so the v1/v2 lesson
            # blocks get a producer (a candidate-only table renders nothing).
            promoted = themes.promote(db_path).get("promoted", 0)
        except Exception as exc:  # a side-channel must never crash reflect
            sys.stderr.write(f"  [themes] skipped: {exc}\n")
            themes_report = None
            bugs_updated = 0
            promoted = 0
    if themes_report is not None:
        sys.stdout.write(
            f"  [themes] assigned {themes_report.get('assigned', 0)} gradient(s) "
            f"→ {themes_report.get('themes_total', 0)} theme(s) "
            f"({themes_report.get('themes_new', 0)} new), "
            f"{bugs_updated} framework bug(s) updated, "
            f"{promoted} lesson(s) promoted\n"
        )

    # ── learning-loop write-back ───────────────────────────────────────────
    suggestions_written = 0
    try:
        con = sqlite3.connect(db_path)
        suggestions_written = con.execute(
            "SELECT COUNT(*) FROM emergent_patterns "
            "WHERE status='proposed' AND detected_at >= ?;",
            (reflect_start_ts,),
        ).fetchone()[0]
        con.close()
    except Exception:
        suggestions_written = 0
    sys.stdout.write(
        f"[learning] persisted {patterns_written or 0} patterns, "
        f"{suggestions_written} suggestions\n"
    )

    # ── side-channel: cross_epic_gradient (MO_CROSS_EPIC_GRADIENTS) ────────
    # Native: promote() reimplements cross_epic_gradient_promote in-process (byte-
    # parity verified on the real state.db). The try/except mirrors the retired bash wrapper's
    # `|| echo 0` — promote() itself propagates sqlite errors, but a side-channel
    # must never crash reflect.
    if os.environ.get("MO_CROSS_EPIC_GRADIENTS", "1") != "0":
        min_classes = os.environ.get("MO_CROSS_EPIC_MIN_CLASSES", "2")
        min_conf = os.environ.get("MO_CROSS_EPIC_MIN_CONF", "0.7")
        window = os.environ.get("MO_CROSS_EPIC_WINDOW", "14d")
        from mini_ork.learning import cross_epic_gradient
        try:
            # promote() prints the count (a bash-heredoc parity artifact its own test
            # asserts) AND returns it; capture the print so it doesn't leak into
            # reflect's stdout — exactly what the retired bash wrapper did with the subprocess's.
            with contextlib.redirect_stdout(io.StringIO()):
                cross_written = cross_epic_gradient.promote(
                    min_classes=int(min_classes),
                    min_confidence=float(min_conf),
                    window=window,
                    db=db_path,
                )
        except Exception:
            cross_written = 0
        sys.stdout.write(f"  [cross_epic_gradient] promoted {cross_written or 0} cross-class gradients\n")

    # ── side-channel: bug_report (MO_BUG_REPORT_SWEEP) ──────────────────────
    # Native: bug_report_sweep()/bug_report_promote() reimplement the bash lib in-
    # process (both return-only, no stdout to capture). sweep resolves the db from
    # MINI_ORK_DB (set above) and the runs dir from `home`; promote's repo_root=None
    # falls back to $MINI_ORK_ROOT — matching the former bash wrapper env. try/except
    # mirrors `|| echo 0`.
    if os.environ.get("MO_BUG_REPORT_SWEEP", "1") != "0":
        from mini_ork.observability import bug_report
        try:
            bugs_swept = bug_report.bug_report_sweep(since=int(since or 0), home=mini_ork_home)
        except Exception:
            bugs_swept = 0
        sys.stdout.write(f"  [bug_report_sweep] swept {bugs_swept or 0} new noticed bug(s)\n")
        auto_promote = os.environ.get("MO_BUG_REPORT_AUTO_PROMOTE", "0")
        if auto_promote != "0":
            try:
                promoted = bug_report.bug_report_promote(top=int(auto_promote))
            except Exception:
                promoted = 0
            sys.stdout.write(f"  [bug_report_promote] promoted {promoted or 0} bug(s) to epics\n")

    # ── side-channel: rho_aggregator (MO_RHO_AGGREGATE) ────────────────────
    # Native: aggregate_win_rates() reimplements rho_aggregate_win_rates in-process
    # (byte-parity verified vs live bash on the real state.db — 114/114 rows). The
    # try/except mirrors the retired bash wrapper's `|| echo 0`: a side-channel failure must
    # never crash reflect.
    if os.environ.get("MO_RHO_AGGREGATE", "1") != "0":
        from mini_ork.learning import rho_aggregator
        try:
            rho_updated = rho_aggregator.aggregate_win_rates(db_path, since=int(since or 0))
        except Exception:
            rho_updated = 0
        sys.stdout.write(f"  [rho_aggregate] upserted {rho_updated or 0} prompt_win_rates row(s)\n")

    # ── side-channel: lane_router (MO_LANE_ROUTER) ──────────────────────────
    if os.environ.get("MO_LANE_ROUTER", "1") != "0":
        from mini_ork import lane_router

        rp.reflection_apply_per_node_credit(db_path)
        try:
            try:
                lanes_updated = lane_router.recompute_advantages(
                    since=int(since or 0), db=db_path
                )
            except Exception:
                lanes_updated = 0
        finally:
            rp.reflection_restore_per_node_credit(db_path)
        sys.stdout.write(
            f"  [lane_router] recomputed advantage for {lanes_updated or 0} "
            f"(lane, task_class) pair(s)\n"
        )

    # ── GEPA optimizer block intentionally NOT ported (default path skipped) ──

    # ── trace end ──────────────────────────────────────────────────────────
    reflect_end_ts = int(time.time())
    traces_analyzed = 0
    gradients_written = 0
    try:
        con = sqlite3.connect(db_path)
        where = ("CAST(strftime('%s', created_at) AS INTEGER) >= ? "
                 "AND task_class != '__reflect__'")
        params: list = [since_int]
        if run_id:
            where += " AND run_id = ?"
            params.append(run_id)
        traces_analyzed = con.execute(
            f"SELECT COUNT(*) FROM execution_traces WHERE {where};", params
        ).fetchone()[0]
        gradients_written = con.execute(
            "SELECT COUNT(*) FROM gradient_records WHERE created_at >= ?;",
            (reflect_start_ts,),
        ).fetchone()[0]
        con.close()
    except Exception:
        pass

    from mini_ork import trace_store  # noqa: PLC0415
    verifier_output = {
        "traces_analyzed": int(traces_analyzed or 0),
        "gradients_written": int(gradients_written or 0),
        "since": int(since_int),
    }
    # `_reflect_timeout` sizes its bound from post-run passes only. Mark this
    # row when the lifecycle spawned us as that tail; in-DAG reflector passes
    # run the same command without the marker and must not ratchet the bound.
    if os.environ.get("MO_REFLECT_POST_RUN") == "1":
        verifier_output["caller"] = "post_run"
    payload = json.dumps(trace_store.enrich_stage_trace({
        "trace_id": trace_id,
        "task_class": "__reflect__",
        "status": "success",
        "duration_ms": (reflect_end_ts - reflect_start_ts) * 1000,
        "verifier_output": verifier_output,
    }, node_type="reflector"))
    _trace_write(payload, trace_env)
    _trace_state["terminal"] = True

    sys.stdout.write(
        f"reflect: analyzed {traces_analyzed or 0} traces, "
        f"wrote {gradients_written or 0} gradients (trace {trace_id})\n"
    )

    # ── cost reconcile (last step; post-run child) ──────────────────────────
    # The lifecycle's own reconcile ran before reflect, so reflect's spend —
    # gradient extraction is the expensive part — never reached
    # ``task_runs.cost_usd`` (16 runs, $3.46 short; RB-32). A run-scoped pass
    # reconciles that run now that every dispatch is recorded. Best-effort: a
    # ledger/db hiccup must not fail a pass whose learning output is already
    # persisted and traced.
    if run_id:
        try:
            from mini_ork import cost_ledger  # noqa: PLC0415
            cost_ledger.reconcile_run_cost(db_path, run_id)
        except Exception as exc:  # a side-channel must never crash reflect
            sys.stderr.write(f"  [cost] reconcile skipped: {exc}\n")
    _disarm_reflect_deadline()
    return 0


if __name__ == "__main__":
    sys.exit(main())
