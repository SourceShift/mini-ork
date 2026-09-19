"""Pure helpers for the goal-loop recipe.

The recipe's `goal_state` transform runs the units lister + predicate command
once per wave, classifying each unit as pass/fail with a one-line reason.
All subprocess invocations pass unit ids as argv elements (NOT interpolated
into a shell string) so a malicious or oddball unit id can't escape the call.
"""
from __future__ import annotations

import shlex
import subprocess
from collections.abc import Iterable
from typing import Any


UnitState = dict[str, Any]


def list_units(target_cwd: str, units_cmd: str) -> list[str]:
    """Run `units_cmd` inside `target_cwd` and return one unit id per line.

    Empty lines and trailing whitespace are stripped. The command is run via
    shell (units_cmd is a free-form operator-supplied expression like
    ``find . -name '*.md' -not -path './node_modules/*'``), so it MUST come
    from a trusted env var — never from a kickoff body.
    """
    proc = subprocess.run(
        units_cmd,
        cwd=target_cwd,
        shell=True,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"units_cmd failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    units: list[str] = []
    for raw in proc.stdout.splitlines():
        unit = raw.strip()
        if unit:
            units.append(unit)
    return units


def evaluate_units(
    target_cwd: str,
    predicate_cmd: str,
    units: Iterable[str],
) -> dict[str, UnitState]:
    """Run `predicate_cmd <unit_id>` for every unit. argv, not shell.

    The predicate prints one reason line on stdout regardless of pass/fail;
    we capture the reason text alongside the exit code. A non-zero exit
    classifies the unit as failing; the predicate is the source of truth
    for "what counts as a goal failure" so we don't second-guess its rc.
    """
    units_list = list(units)
    if not units_list:
        return {}
    argv = shlex.split(predicate_cmd)
    results: dict[str, UnitState] = {}
    for unit in units_list:
        proc = subprocess.run(
            [*argv, unit],
            cwd=target_cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        reason_lines = (proc.stdout or proc.stderr or "").strip().splitlines()
        reason_text = (
            reason_lines[0]
            if reason_lines
            else (proc.stderr.strip() or f"rc={proc.returncode}")
        )
        results[unit] = {
            "pass": proc.returncode == 0,
            "reason": reason_text,
        }
    return results


def harvest_evidence(
    target_cwd: str,
    evidence_cmd: str,
    units: Iterable[str],
    *,
    max_chars: int = 12000,
) -> dict[str, str]:
    """Run ``evidence_cmd <unit_id>`` per unit and capture its FULL output.

    This is the deep-evidence counterpart to :func:`evaluate_units`. The
    predicate is deliberately cheap (one-line reason, exit code) because it runs
    on every poll of every wave; it therefore hands the fix child a thin,
    often-truncated signal. When a goal-loop is armed with
    ``MO_GOAL_EVIDENCE_CMD``, this runs ONCE per wave for only the units actually
    being dispatched, and captures the whole diagnostic payload (e.g. the
    produced-vs-required artifact diff, the failing node, the relevant log tail)
    so the child can diagnose the real root cause instead of guessing from an
    opaque status string.

    argv, not shell — the unit id is always the final argument, never
    interpolated into a command string. A non-zero exit is tolerated: partial
    evidence still helps, so stdout (then stderr) is captured regardless of rc
    and clamped to ``max_chars`` to keep the child's kickoff bounded.
    """
    units_list = list(units)
    if not units_list:
        return {}
    argv = shlex.split(evidence_cmd)
    out: dict[str, str] = {}
    for unit in units_list:
        proc = subprocess.run(
            [*argv, unit],
            cwd=target_cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        text = proc.stdout or ""
        if proc.stderr:
            text = f"{text}\n[stderr]\n{proc.stderr}" if text else proc.stderr
        out[unit] = text.strip()[:max_chars]
    return out