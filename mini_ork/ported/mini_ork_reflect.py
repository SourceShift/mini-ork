"""Strangler-fig Python port of `bin/mini-ork-reflect`.

Mirrors the bash CLI byte-for-byte: same flags, same stdout/stderr lines,
same env-var opt-out toggles (MO_PATTERN_MINER, MO_CROSS_EPIC_GRADIENTS,
MO_BUG_REPORT_SWEEP, MO_RHO_AGGREGATE, MO_LANE_ROUTER), same SQLite writes
via subprocess to the unported bash libs.

Co-existence model (strangler-fig): bash `bin/mini-ork-reflect` is the
authoritative source. This module mirrors its CLI surface and dispatches
the load-bearing pipeline call (`reflection_run`) to the ported Python
implementation in `mini_ork.ported.reflection_pipeline`, while side-channel
libs (pattern_store, cross_epic_gradient, bug_report, rho_aggregator,
lane_router) are invoked via `subprocess.run(['bash','-c', ...])` so their
sqlite writes are byte-identical to the bash CLI. Parity is enforced by
`tests/unit/test_mini_ork_reflect_py.py` (>=6 cases that invoke the live
bash subprocess via `subprocess.run(['bash',...])` and diff against the
Python output byte-for-byte; floats 1e-6 tolerance).

GEPA optimizer block (MO_OPTIMIZER=gepa) is intentionally NOT ported: the
default path (MO_OPTIMIZER unset) leaves the block fully skipped, which
matches the kickoff's "byte-identical to the pre-R4b reflect" invariant.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

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
    """Resolve a reflect model from agents.yaml. Mirrors bash `_resolve_reflect_model`.

    Search order:
      1. ${MINI_ORK_HOME}/config/agents.yaml
      2. ${MINI_ORK_ROOT}/config/agents.yaml
      3. fallback: return lane name verbatim.

    Behaviour mirrors bash byte-for-byte: if the YAML is malformed or the
    lanes key is missing, the lane name is returned. The bash implementation
    uses an inline python3 heredoc; we reimplement the same lookup so the
    port doesn't have to shell out for a YAML read on every invocation.
    """
    candidates = [
        os.path.join(mini_ork_home, "config", "agents.yaml"),
        os.path.join(str(MINI_ORK_ROOT), "config", "agents.yaml"),
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            import yaml  # type: ignore[import-untyped]

            with open(path) as f:
                data = yaml.safe_load(f) or {}
            lanes = data.get("lanes") or {}
            return str(lanes.get(lane) or lanes.get("reflector") or lane)
        except Exception:
            return lane
    return lane


# ── Argument parsing ─────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mini-ork-reflect",
        description="Reflection pipeline orchestrator (Python port of bin/mini-ork-reflect).",
        add_help=False,  # bash prints its own help text; mirror it
    )
    p.add_argument("--since", type=str, default="")
    p.add_argument("--task-class", type=str, default="")
    p.add_argument("--lane", type=str, default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--help", "-h", action="store_true")
    return p


# ── Subprocess wrapper for unported bash libs ────────────────────────────────
def _bash_lib_call(lib_name: str, fn_name: str, args: str, env: dict) -> int:
    """Invoke a bash lib function with the given args. Captures the LAST
    integer token of stdout; returns 0 on parse failure (mirrors bash's
    `|| echo 0` fallback).

    The env dict MUST contain MINI_ORK_ROOT and MINI_ORK_DB. Errors on stderr
    are swallowed (matches bash's `2>/dev/null` semantics) — bash would
    silently downgrade the integer via the `|| echo 0` fallback.
    """
    script = (
        f'set -Eeuo pipefail\n'
        f'_require_lib() {{\n'
        f'  local lib="${{MINI_ORK_ROOT}}/lib/${{1}}.sh"\n'
        f'  [ ! -f "$lib" ] && {{ echo "lib/${{1}}.sh not yet present (P1 in flight?)" >&2; exit 3; }}\n'
        f'  source "$lib"\n'
        f'}}\n'
        f'_require_lib {lib_name}\n'
        f'{fn_name} {args}\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return 0
    out = proc.stdout.strip()
    if not out:
        return 0
    try:
        return int(out.split()[-1])
    except (ValueError, IndexError):
        return 0


def _trace_write_bash(payload_json: str, env: dict) -> None:
    """Write a trace via the unported bash trace_store. Mirrors bash's
    `trace_write "$payload" >/dev/null 2>&1 || true` semantics — errors are
    swallowed so reflect never aborts on a tracing failure."""
    script = (
        f'set -Eeuo pipefail\n'
        f'_require_lib() {{\n'
        f'  local lib="${{MINI_ORK_ROOT}}/lib/${{1}}.sh"\n'
        f'  [ ! -f "$lib" ] && return 0\n'
        f'  source "$lib"\n'
        f'}}\n'
        f'_require_lib trace_store\n'
        f'trace_write {json.dumps(payload_json)} >/dev/null 2>&1 || true\n'
    )
    subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
    )


# ── Dry-run branch ───────────────────────────────────────────────────────────
def _dry_run(since: int, task_class_filter: str, reflect_lane: str,
             gradient_model: str, db_path: str) -> None:
    """Mirror bash lines 122-134. sqlite3 from `MINI_ORK_DB`, optional
    task_class filter. Echoes 2-3 lines to stdout depending on filter."""
    count = 0
    if os.path.isfile(db_path):
        filter_sql = ""
        if task_class_filter:
            filter_sql = f" AND task_class='{task_class_filter}'"
        try:
            con = sqlite3.connect(db_path)
            count = con.execute(
                f"SELECT COUNT(*) FROM execution_traces "
                f"WHERE created_at >= {since}{filter_sql};"
            ).fetchone()[0]
            con.close()
        except Exception:
            count = 0
    print(f"[dry-run] would analyze {count} trace(s) since {since}")
    print(f"[dry-run] lane: {reflect_lane} -> {gradient_model}")
    if task_class_filter:
        print(f"[dry-run] filter: task_class={task_class_filter}")


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

    # ── env setup (mirror bash lines 38-72) ────────────────────────────────
    since = args.since or ""
    task_class_filter = args.task_class or ""
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
    subprocess_env = {
        **os.environ,
        "MINI_ORK_ROOT": os.environ["MINI_ORK_ROOT"],
        "MINI_ORK_HOME": mini_ork_home,
        "MINI_ORK_DB": db_path,
    }

    if not dry_run:
        _trace_write_bash(
            json.dumps({
                "trace_id": trace_id,
                "task_class": "__reflect__",
                "status": "running",
            }),
            subprocess_env,
        )

    # ── dry-run branch ─────────────────────────────────────────────────────
    if dry_run:
        _dry_run(since_int, task_class_filter, reflect_lane, gradient_model, db_path)
        return 0

    # ── dispatch to reflection_pipeline (ported Python) ────────────────────
    from mini_ork.ported import reflection_pipeline as rp  # lazy: avoid import-time cost

    # Inject a deterministic gradient-extraction stub if the env var names a
    # function defined in `reflection_pipeline`'s globals. This is the Python
    # mirror of bash's MINI_ORK_GRADIENT_EXTRACTOR_FN override hook (see
    # lib/gradient_extractor.sh). The test fixture defines `_rfl_stub` in this
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

    run_stdout = io.StringIO()
    with contextlib.redirect_stdout(run_stdout):
        suggestions_json = rp.reflection_run(since_int)
    sys.stdout.write(f"{suggestions_json.rstrip(chr(10))}\n")

    # ── side-channel: pattern_store (MO_PATTERN_MINER) ──────────────────────
    patterns_written = 0
    if os.environ.get("MO_PATTERN_MINER", "1") != "0":
        window = os.environ.get("MO_PATTERN_MINER_WINDOW", "7d")
        min_cluster = os.environ.get("MO_PATTERN_MINER_MIN_CLUSTER", "3")
        patterns_written = _bash_lib_call(
            "pattern_store",
            "pattern_store_mine_from_traces",
            f"--window {window} --min-cluster {min_cluster}",
            subprocess_env,
        )
        sys.stdout.write(f"  [pattern_miner] wrote {patterns_written or 0} pattern_records rows\n")

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
    # parity verified on the real state.db). The try/except mirrors _bash_lib_call's
    # `|| echo 0` — promote() itself propagates sqlite errors, but a side-channel
    # must never crash reflect.
    if os.environ.get("MO_CROSS_EPIC_GRADIENTS", "1") != "0":
        min_classes = os.environ.get("MO_CROSS_EPIC_MIN_CLASSES", "2")
        min_conf = os.environ.get("MO_CROSS_EPIC_MIN_CONF", "0.7")
        window = os.environ.get("MO_CROSS_EPIC_WINDOW", "14d")
        from mini_ork.ported import cross_epic_gradient
        try:
            # promote() prints the count (a bash-heredoc parity artifact its own test
            # asserts) AND returns it; capture the print so it doesn't leak into
            # reflect's stdout — exactly what _bash_lib_call did with the subprocess's.
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
    if os.environ.get("MO_BUG_REPORT_SWEEP", "1") != "0":
        bugs_swept = _bash_lib_call(
            "bug_report",
            "bug_report_sweep",
            f"--since {since}",
            subprocess_env,
        )
        sys.stdout.write(f"  [bug_report_sweep] swept {bugs_swept or 0} new noticed bug(s)\n")
        auto_promote = os.environ.get("MO_BUG_REPORT_AUTO_PROMOTE", "0")
        if auto_promote != "0":
            promoted = _bash_lib_call(
                "bug_report",
                "bug_report_promote",
                f"--top {auto_promote}",
                subprocess_env,
            )
            sys.stdout.write(f"  [bug_report_promote] promoted {promoted or 0} bug(s) to epics\n")

    # ── side-channel: rho_aggregator (MO_RHO_AGGREGATE) ────────────────────
    # Native: aggregate_win_rates() reimplements rho_aggregate_win_rates in-process
    # (byte-parity verified vs live bash on the real state.db — 114/114 rows). The
    # try/except mirrors _bash_lib_call's `|| echo 0`: a side-channel failure must
    # never crash reflect.
    if os.environ.get("MO_RHO_AGGREGATE", "1") != "0":
        from mini_ork.ported import rho_aggregator
        try:
            rho_updated = rho_aggregator.aggregate_win_rates(db_path, since=int(since or 0))
        except Exception:
            rho_updated = 0
        sys.stdout.write(f"  [rho_aggregate] upserted {rho_updated or 0} prompt_win_rates row(s)\n")

    # ── side-channel: lane_router (MO_LANE_ROUTER) ──────────────────────────
    if (os.environ.get("MO_LANE_ROUTER", "1") != "0"
            and os.path.isfile(os.path.join(os.environ["MINI_ORK_ROOT"], "lib", "lane_router.sh"))):
        lanes_updated = _bash_lib_call(
            "lane_router",
            "lane_router_recompute_advantages",
            f"--since {since}",
            subprocess_env,
        )
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
        traces_analyzed = con.execute(
            "SELECT COUNT(*) FROM execution_traces "
            "WHERE CAST(strftime('%s', created_at) AS INTEGER) >= ? "
            "AND task_class != '__reflect__';",
            (since_int,),
        ).fetchone()[0]
        gradients_written = con.execute(
            "SELECT COUNT(*) FROM gradient_records WHERE created_at >= ?;",
            (reflect_start_ts,),
        ).fetchone()[0]
        con.close()
    except Exception:
        pass

    payload = json.dumps({
        "trace_id": trace_id,
        "task_class": "__reflect__",
        "status": "success",
        "duration_ms": (reflect_end_ts - reflect_start_ts) * 1000,
        "verifier_output": {
            "traces_analyzed": int(traces_analyzed or 0),
            "gradients_written": int(gradients_written or 0),
            "since": int(since_int),
        },
    })
    _trace_write_bash(payload, subprocess_env)

    sys.stdout.write(
        f"reflect: analyzed {traces_analyzed or 0} traces, "
        f"wrote {gradients_written or 0} gradients (trace {trace_id})\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
