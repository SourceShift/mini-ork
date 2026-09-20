"""GRASP-style frozen probe-set scorer for the apply gate (task #18).

Design source: arXiv 2605.29668 (GRASP) — score a proposed directive-block
mutation by re-running a FROZEN, balanced probe set of the recipe's tasks with
and without the mutation, then accept only when fix-rate improves and the
per-task no-regression budget holds. This retires the MO_APPLY_UNVETTED
honesty flag for this scorer: the utilities in the gate come from real,
held-out mini-ork runs, not a fabricated mock.

The probe set lives in ``recipes/<recipe>/probes/*.md`` — ordinary kickoff
files. A recipe without a probe directory has no frozen probe set, and
``probe_score`` returns None so the gate quarantines instead of promoting on
a fabricated neutral score.

A probe whose recipe EDITS FILES needs somewhere to edit that is not the
framework tree: each launch gets a private copy of ``probes/fixtures/<stem>/``
handed over as ``MO_TARGET_CWD``. Without it both arms would run in
``MINI_ORK_ROOT`` — refused by the dispatch cwd guard, and shared between arms
even where it is not. Recipes that only write into their own run directory
(obs-smoke) declare no fixture and are unaffected.

run_id capture is deliberately stdout-based: the state.db ``runs`` table is
vestigial (written only by benchmark_suite / auto_merge) while ``task_runs``
is live, so deriving "the run I just launched" from the runs table would
attribute the wrong run. The run CLI prints ``mini_ork_result={...}`` with
``run_id`` in its sink; we parse that and fall back to the deterministic
``run-<epoch>-<pid>`` pattern. A launch that reports no run_id raises — a
score must never be fabricated for a run that did not happen.

What the arm is waited ON is the run, not the process. A run reaches its
terminal status at publish time, but ``mini_ork run`` continues past that into
rubric scoring and eval — work the probe does not score, which can outlast the
run itself by an unbounded margin. ``_await_arm_run`` polls the arm's own
``task_runs`` row (keyed by the run directory's pid) and stops the arm as soon
as the run is decided, so the measurement costs what the run costs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

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


def _probe_fixture(probe_path: str) -> str | None:
    """The probe's target project, or None when it declares none.

    A probe that drives a FILE-EDITING recipe needs somewhere to edit. Each
    arm of the two-arm comparison gets its own copy of this directory, handed
    to the run as ``MO_TARGET_CWD``: without it both arms would run in
    ``MINI_ORK_ROOT``, where the mutating arm's edits are visible to the other
    arm and (via providers.cwd_guard) the dispatch is refused outright.

    Convention: ``probes/fixtures/<probe-stem>/`` sits beside the probe. Kept
    out of the ``*.md`` glob so it is never mistaken for a probe, and shipped
    in-repo so the fixture is frozen with the probe it grades.
    """
    stem = os.path.splitext(os.path.basename(probe_path))[0]
    fixture = os.path.join(os.path.dirname(probe_path), "fixtures", stem)
    return fixture if os.path.isdir(fixture) else None


# ── injectable seams (unit tests monkeypatch these two, never the caller) ──


class ProbeArmTimeout(RuntimeError):
    """One arm of one probe blew the per-run timeout.

    Distinct from RuntimeError-on-no-run_id: the arm produced nothing usable,
    which is a measurement fact to record and step past, not a reason to throw
    away every probe measured so far.
    """


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the arm run AND everything it spawned.

    The arm's verifier chain (``mini_ork.cli.verify`` → the target repo's
    ``pytest``) outlives a bare ``proc.kill()``: only the direct child is
    signalled, so the descendants are reparented to init and keep burning CPU
    against a worktree the caller is about to delete. ``start_new_session=True``
    puts the arm in its own process group, so the whole tree dies together.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


# Statuses that mean the run has decided its own outcome. ``published`` is the
# one the gate scores on (see ``_run_outcome``); the failure statuses are here
# so a run that dies is recognised as finished rather than waited on.
_TERMINAL_STATUSES = frozenset({
    "published", "failed", "stopped", "aborted", "killed", "cancelled", "error",
})


def _run_status(run_id: str, db: str | None) -> str | None:
    """The run's status row, or None while it is absent or unreadable."""
    db = db if db is not None else _db_path()
    if not os.path.isfile(db):
        return None
    try:
        con = sqlite3.connect(db, timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        try:
            row = con.execute(
                "SELECT status FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _arm_run_id_for_pid(proc: subprocess.Popen, home: str) -> str | None:
    """The run this arm started, named ``run-<epoch>-<pid>`` (cli/main.py:458).

    The run's own directory names it, so the arm can be identified while it is
    still running — before it has printed anything to stdout.
    """
    try:
        names = os.listdir(os.path.join(home, "runs"))
    except OSError:
        return None
    suffix = f"-{proc.pid}"
    for name in names:
        if name.startswith("run-") and name.endswith(suffix):
            return name
    return None


def _await_arm_run(proc: subprocess.Popen, home: str, db: str | None,
                   timeout_s: float, poll_s: float = 2.0) -> tuple[str, str | None]:
    """Wait for the arm's RUN to finish, not for its PROCESS to exit.

    ``mini_ork run`` writes the run's terminal status when the publisher lands
    and then keeps working — rubric scoring / eval runs afterwards — on a
    process that may take far longer than the run itself, or never return at
    all. Waiting on process exit therefore turns a three-minute measurement
    into a full-timeout one (observed live: an arm published at $1.40 in ~3
    minutes and was still inside rubric scoring 5 minutes later, at which point
    the old code recorded a timeout and threw the measurement away).

    Returns ``(state, run_id)`` where state is ``"terminal"`` (run decided,
    run_id set), ``"exited"`` (process gone — caller reads stdout, which is
    authoritative) or ``"timeout"``.
    """
    deadline = time.time() + timeout_s
    while True:
        if proc.poll() is not None:
            return "exited", None
        run_id = _arm_run_id_for_pid(proc, home)
        if run_id and _run_status(run_id, db) in _TERMINAL_STATUSES:
            return "terminal", run_id
        if time.time() >= deadline:
            return "timeout", None
        time.sleep(poll_s)


def _launch_run(recipe_name: str, kickoff: str,
                target_cwd: str | None = None,
                root: str | None = None) -> tuple[str, str, float]:
    """Run one probe through the real CLI. Returns (stdout, run_id, cost_usd).

    ``target_cwd`` is the arm's private copy of the probe fixture (see
    ``_probe_fixture``); it becomes the run's ``MO_TARGET_CWD``. Without it a
    mutating recipe would edit ``MINI_ORK_ROOT`` — refused by the dispatch cwd
    guard, and shared between arms even where it is not.

    ``root`` is the arm's own framework tree (``probe_score_code``). When given
    it redirects ``MINI_ORK_ROOT``/``MINI_ORK_HOME``/``MINI_ORK_DB`` at that
    tree, so the arm's runs and their outcome rows live in the arm's database
    and never in the ambient one — a score read from the wrong arm is exactly
    the attribution failure this parameter exists to prevent.

    Fail-loud: a launch that produces no run_id raises RuntimeError — the
    caller must never score a run it cannot identify.
    """
    framework_root = _root()
    try:
        timeout_s = float(os.environ.get("MO_APPLY_PROBE_TIMEOUT_S", "600"))
    except ValueError:
        timeout_s = 600.0
    # MO_STATIC_RECIPE_PLAN freezes planning: the probe measures the EXECUTION
    # effect of the directive, not the planner's mood. A stochastic planner
    # shape-fail (observed twice live on opus) would randomly kill an arm and
    # fabricate a regression signal. recipe_fallback_plan renders the temp
    # recipe's workflow.yaml deterministically, zero LLM.
    env = {**os.environ, "MINI_ORK_ROOT": root or framework_root,
           "MINI_ORK_NONINTERACTIVE": "1", "MO_STATIC_RECIPE_PLAN": "1"}
    # Run-scoped env must NOT leak into the probe launch: an inherited
    # MINI_ORK_RUN_ID makes the nested run REUSE this run's task_runs row
    # (outcome attribution then reads the wrong status), inherited
    # RUN_DIR/PLAN_PATH/WORKFLOW route the nested run into this run's
    # artifacts, and an inherited MO_AUTO_APPLY would fire a sweep inside
    # every probe run — unbounded recursion. MO_TARGET_CWD leaks the OUTER
    # run's target into both arms, which would make the two arms share one
    # working tree; each arm sets its own below.
    for leak in ("MINI_ORK_RUN_ID", "MINI_ORK_TASK_RUN_ID", "MINI_ORK_RUN_DIR",
                 "MINI_ORK_PLAN_PATH", "MINI_ORK_WORKFLOW", "MINI_ORK_RECIPE",
                 "MO_AUTO_APPLY", "MO_TARGET_CWD"):
        env.pop(leak, None)
    if target_cwd:
        env["MO_TARGET_CWD"] = target_cwd
    if root:
        # The arm's OWN tree and database. Set after the leak-strip so the
        # arm's paths win over anything inherited.
        env["MINI_ORK_ROOT"] = root
        env["MINI_ORK_HOME"] = os.path.join(root, ".mini-ork")
        env["MINI_ORK_DB"] = os.path.join(root, ".mini-ork", "state.db")
    proc = subprocess.Popen(
        [sys.executable, "-m", "mini_ork.cli.main", "run", recipe_name, kickoff],
        cwd=root or framework_root, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    home = env.get("MINI_ORK_HOME") or os.path.join(_root(), ".mini-ork")
    state, run_id = _await_arm_run(proc, home, env.get("MINI_ORK_DB"), timeout_s)
    if state == "timeout":
        _kill_process_group(proc)
        raise ProbeArmTimeout(
            f"probe arm exceeded MO_APPLY_PROBE_TIMEOUT_S={timeout_s:.0f}s "
            f"({os.path.basename(kickoff)})"
        ) from None
    if state == "terminal":
        # The run is decided; the process is past the measurement (rubric /
        # eval). Take its stdout if it happens to be gone, else reap it — the
        # run_id and cost come from the arm's own database, not from the exit.
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            out, err = "", ""
    else:
        out, err = proc.communicate()
    run_id = _run_id_from_stdout(out) or run_id
    if not run_id:
        raise RuntimeError(
            f"probe launch reported no run_id (rc={proc.returncode}): "
            f"{(out or '')[-400:]} {(err or '')[-400:]}"
        )
    cost = _run_cost(run_id, db=_arm_db(root) if root else None)
    return out, run_id, cost


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


def _run_outcome(run_id: str, db: str | None = None) -> float:
    """1.0 when the run published, 0.0 otherwise (held-out execution verdict).

    ``db`` names the arm's database explicitly. The code arm runs each arm in
    its own framework tree with its own state.db, so the default (ambient)
    lookup would read the OUTER run's rows and score the wrong arm.
    """
    db = db if db is not None else _db_path()
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


def _run_cost(run_id: str, db: str | None = None) -> float:
    db = db if db is not None else _db_path()
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


def _materialize_target(probe_path: str) -> str | None:
    """A fresh scratch copy of the probe's fixture, or None when it has none.

    Fresh per (probe, arm), not per arm: the two recipe arms are materialized
    once and reused across the whole probe loop, so a target shared between
    runs would let the mutating arm's edits — or one probe's edits — be
    observed by the next launch. Each run starts from the frozen fixture.
    """
    fixture = _probe_fixture(probe_path)
    if not fixture:
        return None
    dst = tempfile.mkdtemp(prefix="mo-probe-target-")
    shutil.copytree(fixture, dst, dirs_exist_ok=True)
    _make_target_repo(dst)
    return dst


def _make_target_repo(dst: str) -> None:
    """Make a fixture copy a git repo, or the edit surface silently escapes it.

    ``_resolve_target_cwd`` (cli/execute.py) honours ``MO_TARGET_CWD`` only when
    ``git rev-parse --show-toplevel`` succeeds on it, and otherwise falls back to
    the kickoff's toplevel — the framework root. A bare directory copy is
    therefore not a target at all: the implementer edits the framework tree, the
    review diff comes back empty, and the probe grades a fixture nothing touched.
    Observed live: an arm run whose rubric read "MO_TARGET_CWD resolved to the
    framework root instead of the probe fixture". The repo is also what makes the
    arm's own diff computable.

    Fail-loud: a target that is not a repo reproduces that silent escape, so it
    raises rather than letting the scorer report a score it did not measure.
    """
    for args in (["init", "-q"],
                 ["config", "user.email", "probe@mini-ork.invalid"],
                 ["config", "user.name", "probe fixture"],
                 ["add", "-A"],
                 ["commit", "-qm", "probe fixture"]):
        subprocess.run(["git", "-C", dst, *args], capture_output=True, text=True)
    back = subprocess.run(["git", "-C", dst, "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    if back.returncode != 0:
        raise RuntimeError(
            f"probe fixture target {dst!r} is not a git repo — MO_TARGET_CWD "
            f"would silently fall back to the framework root"
        )


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
    temp_targets: list[str] = []
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
        stop_sweep = False
        for probe in probes:
            if spent >= budget or stop_sweep:
                break  # n truncates to probes completed in BOTH arms below
            for arm, recipe_name in (("baseline", base_name), ("candidate", cand_name)):
                if spent >= budget or stop_sweep:
                    break
                target = _materialize_target(probe)
                if target:
                    temp_targets.append(target)
                try:
                    _stdout, run_id, cost = _launch_run(recipe_name, probe, target_cwd=target)
                except ProbeArmTimeout as exc:
                    # No usable outcome for this arm. Record why and stop the
                    # sweep: n truncates to the probes finished in BOTH arms,
                    # so the probes already measured are kept rather than
                    # thrown away with the run that blew the timeout.
                    runs.append({"probe": os.path.basename(probe), "arm": arm,
                                 "run_id": "", "outcome": None,
                                 "cost_usd": 0.0, "timed_out": True,
                                 "error": str(exc)})
                    stop_sweep = True
                    break
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
        for path in temp_targets:
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


# ── code arm: vary the framework tree, not the prompt text ──────────────────
#
# ``probe_score`` above can only score a DIRECTIVE: it copies the recipe and
# appends text to a prompt file. A change to gate arithmetic, lane routing, or a
# harness module has no directive to append, so it cannot be scored there at
# all — which is why "run the improvement and see whether it was real" is
# impossible for every candidate that is not a prompt edit.
#
# The code arm varies the FRAMEWORK TREE instead: two git worktrees at the same
# base ref, one of which carries the candidate patch. Everything else is shared
# with ``probe_score`` — the frozen probe set, the per-probe fixture copy, the
# budget knobs, and the finally-cleanup discipline — so the two scorers cannot
# drift on what "a measurement" means.

def _arm_db(root: str) -> str:
    """The arm tree's own database. Never the ambient one."""
    return os.path.join(root, ".mini-ork", "state.db")


def _seed_arm_home(tree: str) -> None:
    """Optionally give the arm tree its own lane config.

    ``MO_APPLY_ARM_CONFIG`` names a config directory to copy (``*.yaml`` only —
    a secrets file is never copied). Unset by default: an arm then resolves
    lanes from the tree's own ``config/``, which is the reproducible choice.
    Set it when the measurement must run on a specific (e.g. cheaper) lane that
    the tree's committed config does not name.
    """
    src = os.environ.get("MO_APPLY_ARM_CONFIG", "").strip()
    if not src or not os.path.isdir(src):
        return
    dst = os.path.join(tree, ".mini-ork", "config")
    os.makedirs(dst, exist_ok=True)
    for name in sorted(os.listdir(src)):
        if not name.endswith(".yaml"):
            continue
        src_file = os.path.join(src, name)
        if os.path.isfile(src_file):
            shutil.copyfile(src_file, os.path.join(dst, name))


def _bootstrap_arm_db(tree: str) -> str:
    """Create the arm's empty state.db so its runs are attributable.

    A fresh worktree carries no ``.mini-ork`` (it is gitignored), and a run
    cannot write its outcome row into a database that does not exist. The
    schema comes from the tree's own migration set — the arm is graded on its
    own code, including its own schema.
    """
    db = _arm_db(tree)
    if os.path.isfile(db):
        return db
    os.makedirs(os.path.dirname(db), exist_ok=True)
    from mini_ork.stores import migrate  # deferred: keeps module import cheap
    migrate.init_db(db=db, root=tree)
    return db


def _code_arm_tree(task_class: str, base_ref: str,
                   patch_path: str | None) -> tuple[str, str] | None:
    """One arm: a detached worktree at ``base_ref``, patch applied when given.

    Returns ``(tmpdir, tree)`` or None when the patch does not apply. An EMPTY
    or absent patch is a legitimate no-op arm — that is the null calibration,
    not a failure — so it is applied as nothing rather than rejected.
    """
    tmp = tempfile.mkdtemp(prefix="mo-code-arm-")
    tree = os.path.join(tmp, "tree")
    add = subprocess.run(
        ["git", "-C", _root(), "worktree", "add", "--detach", tree, base_ref],
        capture_output=True, text=True)
    if add.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"git worktree add {base_ref!r} failed: {add.stderr[-300:]}")

    # Freeze the probe set: the arm's own probes would be re-discovered by a
    # nested scorer if anything ever applied inside an arm run.
    recipe = _recipe_dir(task_class)
    if recipe:
        probes_in_copy = os.path.join(tree, os.path.relpath(recipe, _root()), "probes")
        if os.path.isdir(probes_in_copy):
            shutil.rmtree(probes_in_copy, ignore_errors=True)

    if patch_path and os.path.isfile(patch_path) and os.path.getsize(patch_path) > 0:
        check = subprocess.run(
            ["git", "-C", tree, "apply", "--check", patch_path],
            capture_output=True, text=True)
        if check.returncode != 0:
            _remove_arm(tmp, tree)
            return None  # never a fabricated neutral score for a patch that is not the candidate
        applied = subprocess.run(
            ["git", "-C", tree, "apply", patch_path], capture_output=True, text=True)
        if applied.returncode != 0:
            _remove_arm(tmp, tree)
            return None
    return tmp, tree


def _remove_arm(tmp: str, tree: str) -> None:
    """Remove the worktree registration AND the directory, on every exit path."""
    subprocess.run(["git", "-C", _root(), "worktree", "remove", "--force", tree],
                   capture_output=True, text=True)
    shutil.rmtree(tmp, ignore_errors=True)


def probe_score_code(task_class: str, patch_path: str | None, *,
                     base_ref: str = "HEAD") -> dict | None:
    """Two-arm held-out evaluation of one CODE change to the framework tree.

    Arms: baseline = ``git worktree add --detach <tmp>/base <base_ref>``;
    candidate = the same tree with ``patch_path`` applied. Both carry their own
    ``.mini-ork`` state.db, so each arm's outcome is read from the arm that
    produced it.

    An empty patch is the NULL CALIBRATION: the two arms are identical, so a
    non-zero delta means the instrument is measuring noise rather than the
    change. An instrument that fails that is fixed before it is trusted.

    Returns the same shape as ``probe_score``, or None when the recipe has no
    frozen probe set or the patch cannot apply — the caller must not promote.
    """
    probes = _frozen_probes(task_class)
    recipe = _recipe_dir(task_class)
    if not probes or recipe is None:
        return None
    try:
        budget = float(os.environ.get("MO_APPLY_PROBE_BUDGET_USD", "2.0"))
    except ValueError:
        budget = 2.0
    recipe_name = os.path.basename(recipe)

    arms: list[tuple[str, str, str]] = []  # (arm, tmpdir, tree)
    temp_targets: list[str] = []
    runs: list[dict] = []
    before_v: list[float] = []
    after_v: list[float] = []
    ids: list[str] = []
    spent = 0.0
    try:
        base = _code_arm_tree(task_class, base_ref, None)
        if base is None:
            return None
        arms.append(("baseline", base[0], base[1]))
        cand = _code_arm_tree(task_class, base_ref, patch_path)
        if cand is None:
            # The candidate patch does not apply → nothing was measured.
            return None
        arms.append(("candidate", cand[0], cand[1]))
        for _arm, _tmp, tree in arms:
            _seed_arm_home(tree)
            _bootstrap_arm_db(tree)

        stop_sweep = False
        for probe in probes:
            if spent >= budget or stop_sweep:
                break  # n truncates to probes completed in BOTH arms below
            for arm, _tmp, tree in arms:
                if spent >= budget or stop_sweep:
                    break
                target = _materialize_target(probe)
                if target:
                    temp_targets.append(target)
                try:
                    _stdout, run_id, cost = _launch_run(
                        recipe_name, probe, target_cwd=target, root=tree)
                except ProbeArmTimeout as exc:
                    # No usable outcome for this arm. Record why and stop the
                    # sweep: n truncates to the probes finished in BOTH arms,
                    # so the probes already measured are kept rather than
                    # thrown away with the run that blew the timeout.
                    runs.append({"probe": os.path.basename(probe), "arm": arm,
                                 "run_id": "", "outcome": None,
                                 "cost_usd": 0.0, "timed_out": True,
                                 "error": str(exc)})
                    stop_sweep = True
                    break
                spent += cost
                outcome = _run_outcome(run_id, db=_arm_db(tree))
                runs.append({"probe": os.path.basename(probe), "arm": arm,
                             "run_id": run_id, "outcome": outcome, "cost_usd": cost})
                if arm == "baseline":
                    before_v.append(outcome)
                    ids.append(os.path.basename(probe))
                else:
                    after_v.append(outcome)
    finally:
        for _arm, tmp, tree in arms:
            _remove_arm(tmp, tree)
        for path in temp_targets:
            shutil.rmtree(path, ignore_errors=True)

    n = min(len(before_v), len(after_v))
    before_v, after_v, ids = before_v[:n], after_v[:n], ids[:n]
    if n == 0:
        result = {"before": 0.0, "after": 0.0, "n": 0, "pertask_json": "",
                  "runs": runs, "cost_usd": round(spent, 4),
                  "truncated_by_budget": True}
    else:
        result = {
            "before": sum(before_v) / n,
            "after": sum(after_v) / n,
            "n": n,
            "pertask_json": json.dumps({
                "before": [int(v) for v in before_v],
                "after": [int(v) for v in after_v],
                "ids": ids,
            }),
            "runs": runs,
            "cost_usd": round(spent, 4),
        }
    _write_null_calibration(patch_path, result)
    return result


def _write_null_calibration(patch_path: str | None, result: dict) -> None:
    """Record the calibration when an empty patch was scored.

    ${MINI_ORK_RUN_DIR}/code-arm-null-calibration.json is the evidence artifact:
    the instrument is accepted only when the no-op delta sits within
    MO_APPLY_REGRESSION_TOLERANCE on at least MO_APPLY_MIN_EXAMPLES probes.
    Written only for the null patch — a real candidate is not a calibration.
    """
    try:
        patch = patch_path or ""
        if patch and os.path.isfile(patch) and os.path.getsize(patch) > 0:
            return
        run_dir = os.environ.get("MINI_ORK_RUN_DIR", "").strip()
        if not run_dir or not os.path.isdir(run_dir):
            return
        payload = {
            "patch": "",
            "before": result.get("before", 0.0),
            "after": result.get("after", 0.0),
            "n": result.get("n", 0),
            "delta": result.get("after", 0.0) - result.get("before", 0.0),
        }
        with open(os.path.join(run_dir, "code-arm-null-calibration.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except OSError:
        pass  # evidence is best-effort; the score itself is unaffected
