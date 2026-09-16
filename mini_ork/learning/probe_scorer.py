"""GRASP-style frozen probe-set scorer for the apply gate (task #18).

Design source: arXiv 2605.29668 (GRASP) — score a proposed directive-block
mutation by re-running a FROZEN, balanced probe set of the recipe's tasks with
and without the mutation, then accept only when fix-rate improves and the
per-task no-regression budget holds. This retires the MO_APPLY_UNVETTED
honesty flag for this scorer: the utilities in the gate come from real,
held-out mini-ork runs, not a fabricated mock.

The probe set lives in ``recipes/<recipe>/probes/*.md`` — ordinary kickoff
files. A recipe without a probe directory has no frozen probe set, and
``probe_score`` returns None so the gate can record a pending_human_approval
instead of promoting on a fabricated neutral score.

run_id capture is deliberately stdout-based: the state.db ``runs`` table is
vestigial (written only by benchmark_suite / auto_merge) while ``task_runs``
is live, so deriving "the run I just launched" from the runs table would
attribute the wrong run. The run CLI prints ``mini_ork_result={...}`` with
``run_id`` in its sink; we parse that and fall back to the deterministic
``run-<epoch>-<pid>`` pattern. A launch that reports no run_id raises — a
score must never be fabricated for a run that did not happen.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys

_ROOT = None


def _root() -> str:
    global _ROOT
    if _ROOT is None:
        _ROOT = os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    return _ROOT


def _recipe_dir(task_class: str) -> str | None:
    root = _root()
    candidates = [task_class, task_class.replace("_", "-"), task_class.replace("-", "_")]
    for name in candidates:
        path = os.path.join(root, "recipes", name)
        if os.path.isdir(path):
            return path
    return None


def _frozen_probes(task_class: str) -> list[str]:
    """Sorted, capped probe kickoff paths for the recipe. [] when none."""
    recipe = _recipe_dir(task_class)
    if not recipe:
        return []
    probes_dir = os.path.join(recipe, "probes")
    if not os.path.isdir(probes_dir):
        return []
    try:
        names = sorted(n for n in os.listdir(probes_dir) if n.endswith(".md"))
    except OSError:
        return []
    try:
        cap = max(1, int(os.environ.get("MO_APPLY_PROBE_MAX_TASKS", "2")))
    except ValueError:
        cap = 2
    return [os.path.join(probes_dir, n) for n in names[:cap]]


# ── injectable seams (unit tests monkeypatch these two, never the caller) ──


def _launch_run(recipe_name: str, kickoff: str) -> tuple[str, str, float]:
    """Run one probe through the real CLI. Returns (stdout, run_id, cost_usd).

    Fail-loud: a launch that produces no run_id raises RuntimeError — the
    caller must never score a run it cannot identify.
    """
    root = _root()
    try:
        timeout_s = float(os.environ.get("MO_APPLY_PROBE_TIMEOUT_S", "600"))
    except ValueError:
        timeout_s = 600.0
    env = {**os.environ, "MINI_ORK_ROOT": root, "MINI_ORK_NONINTERACTIVE": "1"}
    # Run-scoped env must NOT leak into the probe launch: an inherited
    # MINI_ORK_RUN_ID makes the nested run REUSE this run's task_runs row
    # (outcome attribution then reads the wrong status), inherited
    # RUN_DIR/PLAN_PATH/WORKFLOW route the nested run into this run's
    # artifacts, and an inherited MO_AUTO_APPLY would fire a sweep inside
    # every probe run — unbounded recursion.
    for leak in ("MINI_ORK_RUN_ID", "MINI_ORK_TASK_RUN_ID", "MINI_ORK_RUN_DIR",
                 "MINI_ORK_PLAN_PATH", "MINI_ORK_WORKFLOW", "MINI_ORK_RECIPE",
                 "MO_AUTO_APPLY"):
        env.pop(leak, None)
    proc = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.main", "run", recipe_name, kickoff],
        cwd=root, env=env, capture_output=True, text=True, timeout=timeout_s,
    )
    run_id = _run_id_from_stdout(proc.stdout)
    if not run_id:
        raise RuntimeError(
            f"probe launch reported no run_id (rc={proc.returncode}): "
            f"{(proc.stdout or '')[-400:]} {(proc.stderr or '')[-400:]}"
        )
    cost = _run_cost(run_id)
    return proc.stdout, run_id, cost


def _run_id_from_stdout(stdout: str) -> str | None:
    """Sink JSON first (authoritative), deterministic run-<epoch>-<pid> regex
    as fallback (the CLI echoes the run id on its banner lines too)."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("mini_ork_result="):
            try:
                sink = json.loads(line[len("mini_ork_result="):])
            except json.JSONDecodeError:
                continue
            rid = sink.get("run_id") if isinstance(sink, dict) else None
            if rid:
                return str(rid)
    m = re.findall(r"\brun-\d+-\d+\b", stdout or "")
    return m[-1] if m else None


def _db_path() -> str:
    return os.environ.get("MINI_ORK_DB") or os.path.join(
        os.environ.get("MINI_ORK_HOME") or os.path.join(_root(), ".mini-ork"), "state.db"
    )


def _run_outcome(run_id: str) -> float:
    """1.0 when the run published, 0.0 otherwise (held-out execution verdict)."""
    db = _db_path()
    if not os.path.isfile(db):
        return 0.0
    try:
        con = sqlite3.connect(db, timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        try:
            row = con.execute(
                "SELECT status FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return 0.0
    return 1.0 if row and row[0] == "published" else 0.0


def _run_cost(run_id: str) -> float:
    db = _db_path()
    if not os.path.isfile(db):
        return 0.0
    try:
        con = sqlite3.connect(db, timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        try:
            row = con.execute(
                "SELECT cost_usd FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return 0.0
    try:
        return float(row[0]) if row and row[0] is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _materialize_arm(task_class: str, target_file: str | None, directive_block: str | None,
                     idx: int) -> tuple[str, str | None]:
    """Copy the recipe to ``recipes/<recipe>__probe_<pid>_<idx>`` and, for the
    candidate arm, append the directive block to the target prompt file.
    Returns (recipe_dir_name, mutated_abs_path)."""
    src = _recipe_dir(task_class)
    if src is None:
        raise RuntimeError(f"no recipe directory for task_class {task_class!r}")
    name = os.path.basename(src) + f"__probe_{os.getpid()}_{idx}"
    dst = os.path.join(_root(), "recipes", name)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    # The temp recipe's own probes would be re-discovered by a nested probe
    # scorer if anything ever applied inside the probe run — strip them so the
    # probe set stays frozen to the original recipe.
    probes_in_copy = os.path.join(dst, "probes")
    if os.path.isdir(probes_in_copy):
        shutil.rmtree(probes_in_copy)
    mutated = None
    if target_file and directive_block:
        rel = target_file.replace("\\", "/")
        if os.path.isabs(rel):
            # An absolute target must land inside the TEMP copy, never the
            # original recipe (auto_sweep passes absolute paths): relativize
            # against the source recipe dir; outside it → nothing to measure.
            try:
                rel = os.path.relpath(os.path.abspath(rel), os.path.abspath(src))
            except ValueError:
                rel = "../../unreachable"
            if rel.startswith(".."):
                return name, None
        cand = os.path.join(dst, rel)
        if not os.path.isfile(cand):
            cand = os.path.join(dst, "prompts", os.path.basename(rel))
        if os.path.isfile(cand):
            with open(cand, "a", encoding="utf-8") as fh:
                fh.write(directive_block)
            mutated = cand
    return name, mutated


def probe_score(task_class: str, target_file: str | None,
                directive: str, *, source_ref: str = "", context: str = "") -> dict | None:
    """Two-arm held-out evaluation of one directive mutation.

    Arms: baseline = the recipe exactly as committed; candidate = the same
    recipe with the directive block appended to the target prompt (identical
    append semantics to ``apply_mutation``, marker included, so what the probe
    measures is exactly what a promote would land).

    Returns a probe-result dict:
        before / after / n     — scalar utilities + probe count
        pertask_json           — JSON with before/after/ids vectors for the
                                 gate's per-task no-regression rule
        runs                   — [{probe, arm, run_id, outcome, cost_usd}]
        cost_usd               — total spend across every launch
    or None when the recipe has no frozen probe set (caller must NOT promote).
    """
    probes = _frozen_probes(task_class)
    if not probes:
        return None
    if not target_file or not directive:
        return None  # no candidate arm can be materialized → nothing to measure
    try:
        budget = float(os.environ.get("MO_APPLY_PROBE_BUDGET_USD", "2.0"))
    except ValueError:
        budget = 2.0

    # Deferred import: apply imports this module at call time only, and the
    # directive-block builder lives in apply — importing here avoids the cycle.
    from mini_ork.cli.apply import _directive_block
    block = _directive_block(directive, source_ref=source_ref, context=context)

    temp_dirs: list[str] = []
    runs: list[dict] = []
    before_v: list[float] = []
    after_v: list[float] = []
    ids: list[str] = []
    spent = 0.0
    try:
        base_name, _ = _materialize_arm(task_class, None, None, 0)
        temp_dirs.append(base_name)
        cand_name, mutated = _materialize_arm(task_class, target_file, block, 1)
        temp_dirs.append(cand_name)
        if target_file and not mutated:
            # Candidate arm could not land the directive → nothing to measure.
            return None
        for probe in probes:
            if spent >= budget:
                break  # n truncates to probes completed in BOTH arms below
            for arm, recipe_name in (("baseline", base_name), ("candidate", cand_name)):
                if spent >= budget:
                    break
                _stdout, run_id, cost = _launch_run(recipe_name, probe)
                spent += cost
                outcome = _run_outcome(run_id)
                runs.append({"probe": os.path.basename(probe), "arm": arm,
                             "run_id": run_id, "outcome": outcome, "cost_usd": cost})
                if arm == "baseline":
                    before_v.append(outcome)
                    ids.append(os.path.basename(probe))
                else:
                    after_v.append(outcome)
    finally:
        root = _root()
        for name in temp_dirs:
            path = os.path.join(root, "recipes", name)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)

    n = min(len(before_v), len(after_v))
    before_v, after_v, ids = before_v[:n], after_v[:n], ids[:n]
    if n == 0:
        return {"before": 0.0, "after": 0.0, "n": 0, "pertask_json": "",
                "runs": runs, "cost_usd": round(spent, 4),
                "truncated_by_budget": True}
    pertask = json.dumps({
        "before": [int(v) for v in before_v],
        "after": [int(v) for v in after_v],
        "ids": ids,
    })
    return {
        "before": sum(before_v) / n,
        "after": sum(after_v) / n,
        "n": n,
        "pertask_json": pertask,
        "runs": runs,
        "cost_usd": round(spent, 4),
    }
