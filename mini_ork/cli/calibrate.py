"""``mini-ork calibrate`` — the router's calibration error and the gate's blind spot.

Runs the calibration backtest (``mini_ork.learning.calibration_backtest``) over
the persisted predictions in ``execution_traces.predicted_error`` and prints the
two numbers the cost-down claim depends on:

  * reliability — ECE and Brier over every recorded prediction;
  * blind spot — of the rows the router declined to escalate on, the fraction
    that errored anyway.

Human output is the reliability table, then ECE / Brier, then the blind-spot
line. ``--json`` emits the summarize dict as the only stdout so callers can pipe
it. Diagnostics go to stderr.

A database with no ``predicted_error`` column (every DB the moment 0059 lands)
prints "no predictions recorded yet" and exits 0 — that is the expected first
run, not an error.
"""
from __future__ import annotations

import json
import os
import sys

from mini_ork.learning import calibration_backtest


def _usage() -> str:
    return (
        "Usage: mini-ork calibrate [--task-class <name>] [--bins <n>] [--json] [--help]\n"
        "\n"
        "Measure the router's calibration error (ECE/Brier) and the escalation\n"
        "gate's blind-spot rate over execution_traces.predicted_error.\n"
        "\n"
        "Options:\n"
        "  --task-class <name>  Restrict to one task class (default: all)\n"
        "  --bins <n>           Reliability-diagram bin count (default: 10)\n"
        "  --json               Emit the summarize dict as JSON (the only stdout)\n"
        "  --help               This message\n"
    )


def _parse(argv):
    """Returns (task_class, bins, json_flag, help_flag, unknown, error)."""
    if argv is None:
        argv = sys.argv[1:]
    task_class = ""
    bins = 10
    json_flag = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            return task_class, bins, json_flag, True, None, None
        if a == "--task-class":
            if i + 1 >= len(argv):
                return task_class, bins, json_flag, False, None, "missing value for --task-class"
            task_class = argv[i + 1]
            i += 2
        elif a == "--bins":
            if i + 1 >= len(argv):
                return task_class, bins, json_flag, False, None, "missing value for --bins"
            try:
                bins = int(argv[i + 1])
            except ValueError:
                return task_class, bins, json_flag, False, None, f"invalid bin count: {argv[i+1]}"
            if bins < 1:
                return task_class, bins, json_flag, False, None, f"bin count must be >= 1: {bins}"
            i += 2
        elif a == "--json":
            json_flag = True
            i += 1
        else:
            return task_class, bins, json_flag, False, a, None
    return task_class, bins, json_flag, False, None, None


def main(argv=None, *, stdout=None, stderr=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    task_class, bins, json_flag, help_flag, unknown, parse_err = _parse(argv)
    if help_flag:
        out.write(_usage())
        return 0
    if parse_err:
        err.write(f"{parse_err}\n")
        out.write(_usage())
        return 2
    if unknown is not None:
        err.write(f"Unknown flag: {unknown}\n")
        out.write(_usage())
        return 2

    db = os.environ.get("MINI_ORK_DB", "")
    # A missing DB or missing file is "no predictions" too — load_prediction_rows
    # fails open, so summarize(db) yields n=0 rather than a traceback.
    summary = calibration_backtest.summarize(db, task_class, bins=bins)

    if json_flag:
        out.write(json.dumps(summary) + "\n")
        return 0

    if summary["n"] == 0:
        out.write("no predictions recorded yet\n")
        return 0

    out.write("reliability\n")
    out.write(f"{'bin':>4} {'range':>15} {'n':>6} {'mean_pred':>10} {'obs_rate':>9}\n")
    for b in summary["reliability"]:
        rng = f"[{b['lo']:.2f}, {b['hi']:.2f})"
        out.write(f"{'':>4} {rng:>15} {b['n']:>6} "
                  f"{b['mean_predicted']:>10.4f} {b['observed_rate']:>9.4f}\n")
    out.write(f"ECE: {summary['ece']:.4f}\n")
    out.write(f"Brier: {summary['brier']:.4f}\n")
    bs = summary["blind_spot"]
    rate = bs["blind_spot_rate"]
    rate_s = f"{rate:.4f}" if rate is not None else "n/a"
    out.write(f"blind spot: kept={bs['n_kept']} errored={bs['n_kept_errored']} rate={rate_s}\n")
    out.write(f"escalated={bs['n_escalated']} errored={bs['n_escalated_errored']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
