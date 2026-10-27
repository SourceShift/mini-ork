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

import io
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET

_ROOT = None

# ── git-oracle probe kind (default off) ────────────────────────────────────
#
# A git-oracle probe is a normal probe kickoff whose YAML frontmatter carries
# ``oracle: git``. Its data lives in a sidecar (``probes/oracles/<stem>.json``)
# because the ``.md`` is the kickoff the solver reads. The target is a
# history-free snapshot of ``base_sha``, and the probe counts as solved only
# when the run publishes AND the hidden tests from ``fix_sha`` pass against the
# result. Everything is gated on ``MO_APPLY_PROBE_GIT_ORACLE == "1"`` so the
# existing probe sets behave exactly as before.
GIT_ORACLE_FLAG = "MO_APPLY_PROBE_GIT_ORACLE"
ORACLE_DIR = "oracles"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Same pattern as PYTEST_FILE in scripts/mine_heldout_tasks.py.
_PYTEST_FILE_RE = re.compile(r"(^|/)tests/(.*/)?test_[^/]*\.py$")


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


# ── per-recipe probe config ─────────────────────────────────────────────────
#
# The global MO_APPLY_PROBE_* defaults fit the small code-fix probes; a
# framework-edit run costs $3-12 and takes 20-60 minutes, so its probe set
# needs its own budget/timeout. A recipe may ship
# ``probes/probe-config.yaml`` with any of the four knobs below; those values
# become the DEFAULTS for that recipe, and an explicitly set env var still
# wins. A config that exists but does not parse (or carries a non-numeric
# value) FAILS CLOSED with ValueError naming the file — silently falling back
# to the $2 global would score a real run against an impossible budget.

_PROBE_CONFIG_KEYS = ("budget_usd", "timeout_s", "control_n", "max_tasks")


def _probe_config(recipe: str | None) -> dict:
    """The parsed ``probes/probe-config.yaml`` for a recipe dir, or ``{}``.

    Raises ``ValueError`` naming the file when it exists but cannot be parsed
    or holds a non-numeric value — never falls back to the global default.
    """
    if not recipe:
        return {}
    path = os.path.join(recipe, "probes", "probe-config.yaml")
    if not os.path.isfile(path):
        return {}
    try:
        import yaml  # deferred: keeps module import cheap
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:  # malformed YAML, unreadable file, ...
        raise ValueError(
            f"probe config {path} does not parse: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            f"probe config {path} must be a mapping, got {type(data).__name__}")
    resolved: dict = {}
    for key in _PROBE_CONFIG_KEYS:
        if key not in data or data[key] is None:
            continue
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"probe config {path} has non-numeric {key}: {value!r}")
        resolved[key] = value
    return resolved


def _recipe_config(recipe_name: str) -> dict:
    """Config for ``recipe_name``, following probe-arm temp-copy names back to
    the recipe they were copied from.

    ``_materialize_arm`` runs directive arms as
    ``<recipe>__probe_<pid>_<idx>`` and strips the copy's ``probes/`` dir so a
    nested scorer cannot re-discover the probe set. Its config lives in that
    stripped directory, so resolve the original recipe's config instead.
    """
    recipe = _recipe_dir(recipe_name)
    config = _probe_config(recipe)
    if config or recipe is None:
        return config
    base = re.sub(r"__probe_\d+_\d+$", "", os.path.basename(recipe))
    if base == os.path.basename(recipe):
        return config
    return _probe_config(_recipe_dir(base))


def _probe_setting(recipe_name: str, env_name: str, config_key: str,
                   default: float, *, as_int: bool = False) -> float | int:
    """Resolve one probe knob: explicit env > recipe config > global default.

    A malformed env value keeps the historical silent fallback to the global
    default; only the config file fails closed (see ``_probe_config``).
    """
    raw = os.environ.get(env_name)
    if raw is not None:
        try:
            value = float(raw)
        except ValueError:
            value = float(default)
    else:
        value = float(_recipe_config(recipe_name).get(config_key, default))
    return max(1, int(value)) if as_int else value


def _parse_probe_frontmatter(path: str) -> dict:
    """YAML frontmatter at the top of a probe kickoff, or ``{}`` when absent.

    Anchor probes carry ``anchor: true`` in their frontmatter so the apply loop
    can split the frozen probe set into (a) the metric the gate compares and
    (b) the metric the loop is NOT allowed to optimize. The frontmatter is
    optional: a probe without one is a gate probe (kickoff rule #1 — anchor is
    opt-in, never the default).
    """
    try:
        with open(path, encoding="utf-8") as fh:
            head = fh.read(4096)
    except OSError:
        return {}
    if not head.startswith("---\n"):
        return {}
    # Frontmatter is the FIRST ``---``-delimited block at the top of the file.
    end = head.find("\n---\n", 4)
    if end < 0:
        return {}
    body = head[4:end]
    try:
        import yaml  # deferred: keeps module import cheap
        parsed = yaml.safe_load(body) or {}
    except Exception:
        # Malformed frontmatter (typo, missing colon, indented block) falls
        # back to a gate probe — anchor is opt-in, never inferred.
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _valid_oracle_rel_path(path: object) -> bool:
    """A repo-relative pytest file path that cannot escape the target.

    ``_PYTEST_FILE_RE`` alone accepts ``/abs/tests/test_x.py`` and
    ``../../tests/test_x.py`` (its alternation starts with ``(^|/)``); the
    overlay in ``_grade_git_oracle`` writes to ``os.path.join(target, path)``,
    so an absolute path would discard ``target`` and ``..`` would climb out of
    it. The sidecar schema says "repo-relative", so reject both here.
    """
    if not isinstance(path, str):
        return False
    if os.path.isabs(path) or ".." in path.split("/"):
        return False
    return _PYTEST_FILE_RE.search(path) is not None


def _valid_oracle_id(value: object) -> bool:
    """A ``<junit classname>::<name>`` test id string."""
    return isinstance(value, str) and "::" in value


def _probe_oracle(probe_path: str) -> dict | None:
    """The parsed sidecar for a git-oracle probe, or None when absent/invalid.

    Returns None unless the probe's frontmatter says ``oracle: git``. The
    sidecar is ``probes/oracles/<stem>.json`` beside the probe; malformed JSON,
    a non-object, or any field that breaks the schema also yields None so
    ``_load_probes`` can simply skip the probe.
    """
    fm = _parse_probe_frontmatter(probe_path)
    if fm.get("oracle") != "git":
        return None
    stem = os.path.splitext(os.path.basename(probe_path))[0]
    sidecar = os.path.join(
        os.path.dirname(probe_path), ORACLE_DIR, stem + ".json")
    try:
        with open(sidecar, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("base_sha", "fix_sha"):
        value = data.get(key)
        if not isinstance(value, str) or _SHA_RE.match(value) is None:
            return None
    test_files = data.get("test_files")
    if (not isinstance(test_files, list) or not test_files
            or not all(_valid_oracle_rel_path(p) for p in test_files)):
        return None
    fail_to_pass = data.get("fail_to_pass")
    if (not isinstance(fail_to_pass, list) or not fail_to_pass
            or not all(_valid_oracle_id(t) for t in fail_to_pass)):
        return None
    pass_to_pass = data.get("pass_to_pass")
    if (not isinstance(pass_to_pass, list)
            or not all(_valid_oracle_id(t) for t in pass_to_pass)):
        return None
    return data


class _ProbePair(tuple):
    """A ``(path, is_anchor)`` pair that stringifies to its path.

    ``_load_probes`` has always returned pairs and every caller unpacks them,
    but a caller that only wants the probe name naturally reaches for
    ``os.path.basename(str(entry))``. Keeping ``str(pair)`` equal to the path
    preserves that reading without changing the unpacking contract.
    """

    __slots__ = ()

    def __new__(cls, path: str, is_anchor: bool) -> "_ProbePair":
        return super().__new__(cls, (path, is_anchor))

    def __str__(self) -> str:
        return self[0]


def _load_probes(task_class: str) -> list[tuple[str, bool]]:
    """Sorted, capped probe list as ``(path, is_anchor)`` pairs.

    The split is what makes the anchor signal meaningful: the gate's
    before/after vectors cover ONLY non-anchor probes, so a candidate that
    only gains on an anchor probe cannot game its way past the gate. Anchors
    are still run for both arms (control + candidate) like any probe; their
    outcome is reported as ``anchor_solved_frac`` for the candidate arm
    (kickoff rule #1). ``MO_APPLY_PROBE_MAX_TASKS`` caps the GATE probes
    only; every anchor probe is always kept. Capping the combined, name-sorted
    list silently dropped the anchor (probe-3 sorts after probe-1/2 under the
    default cap of 2), so ``anchor_solved_frac`` was never measured and
    ``collapse_history`` never written — found by the 2026-09-29 live smoke.

    Frontmatter is read once per probe file and its gating rules applied in
    order: holdout probes are never scored by apply; git-oracle probes are
    loaded only behind ``MO_APPLY_PROBE_GIT_ORACLE=1`` AND with a valid
    sidecar; any other non-empty ``oracle`` value is unknown and skipped.
    """
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
    pairs: list[tuple[str, bool]] = []
    for name in names:
        path = os.path.join(probes_dir, name)
        fm = _parse_probe_frontmatter(path)
        if bool(fm.get("holdout")):
            continue
        if fm.get("oracle") == "git":
            if os.environ.get(GIT_ORACLE_FLAG, "") != "1":
                continue
            if _probe_oracle(path) is None:
                continue
        elif fm.get("oracle"):
            continue
        is_anchor = bool(fm.get("anchor"))
        pairs.append(_ProbePair(path, is_anchor))
    cap = int(_probe_setting(task_class, "MO_APPLY_PROBE_MAX_TASKS",
                             "max_tasks", 2, as_int=True))
    gate = [pr for pr in pairs if not pr[1]][:cap]
    anchors = [pr for pr in pairs if pr[1]]
    return gate + anchors


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
    "not_needed",
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
    timeout_s = float(_probe_setting(recipe_name, "MO_APPLY_PROBE_TIMEOUT_S",
                                     "timeout_s", 600.0))
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
    # Probe arms run against a private fixture tmpdir on THIS host, so local
    # placement is the only correct one. Without the explicit flag, a host
    # whose env profile defaults to Daytona would ship every arm to the cloud.
    proc = subprocess.Popen(
        [sys.executable, "-m", "mini_ork.cli.main", "run",
         "--placement", "local", recipe_name, kickoff],
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


def probe_scratch_parent(root: str | None = None) -> str:
    """The directory that holds probe scratch trees, created if needed.

    ``MO_APPLY_PROBE_SCRATCH_DIR`` wins when set and non-empty. Otherwise the
    scratch lives BESIDE the repo, not under ``$TMPDIR``: colima's docker mount
    cannot see ``$TMPDIR`` (the same rule as ``scripts/run_heldout.py``), so a
    target there would be invisible to a containerized arm.
    """
    r = os.path.abspath(root or _root())
    parent = os.environ.get("MO_APPLY_PROBE_SCRATCH_DIR", "")
    if not parent:
        parent = os.path.join(
            os.path.dirname(r), os.path.basename(r) + "-probe-scratch")
    os.makedirs(parent, exist_ok=True)
    return parent


def _ensure_scratch_parent() -> None:
    """Materialize an explicitly configured scratch parent up front.

    A run with no git-oracle probe never creates a snapshot, so a caller that
    inspects ``MO_APPLY_PROBE_SCRATCH_DIR`` should see an empty directory rather
    than "missing". Nothing is created when the variable is unset (the default
    parent stays lazy), and a read-only location is best-effort: it must not
    turn a measurement that needs no scratch into a failure.
    """
    if not os.environ.get("MO_APPLY_PROBE_SCRATCH_DIR"):
        return
    try:
        probe_scratch_parent()
    except OSError:
        pass


def materialize_git_tree(sha: str, *, root: str | None = None) -> str:
    """A history-free snapshot of ``sha`` as a one-commit repo.

    ``git archive`` is deterministic and narrow (no ``.git``, no untracked
    files) — the same reasoning as ``mini_ork/certify/image.py``. Extracting it
    and committing gives a target whose ``base_sha`` content is intact but from
    which no later commit (notably ``fix_sha``) is reachable, so the solver
    cannot recover the hidden tests from object history.

    Fail loud: an unknown sha raises ``RuntimeError`` and removes the scratch
    directory it made. Any later failure (extraction, repo creation) is cleaned
    up the same way before propagating.
    """
    r = root or _root()
    dst = tempfile.mkdtemp(
        prefix="mo-probe-git-", dir=probe_scratch_parent(r))
    archived = subprocess.run(
        ["git", "-C", r, "archive", "--format=tar", sha],
        capture_output=True)
    if archived.returncode != 0:
        shutil.rmtree(dst, ignore_errors=True)
        tail = archived.stderr[-300:].decode("utf-8", "replace")
        raise RuntimeError(f"git archive {sha!r} failed: {tail}")
    try:
        with tarfile.open(fileobj=io.BytesIO(archived.stdout)) as tar:
            tar.extractall(dst, filter="data")
        _make_target_repo(dst)
    except BaseException:
        shutil.rmtree(dst, ignore_errors=True)
        raise
    return dst


def _materialize_target(probe_path: str) -> str | None:
    """A fresh scratch copy of the probe's target, or None when it has none.

    A git-oracle probe's target is a history-free snapshot of its ``base_sha``.
    Otherwise it is a fresh copy of the probe's fixture — fresh per (probe, arm),
    not per arm: the two recipe arms are materialized once and reused across the
    whole probe loop, so a target shared between runs would let the mutating
    arm's edits — or one probe's edits — be observed by the next launch. Each
    run starts from the frozen base.
    """
    oracle = _probe_oracle(probe_path)
    if oracle is not None:
        return materialize_git_tree(oracle["base_sha"])
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


def run_pytest_junit(target: str, test_files: list[str], *,
                     timeout: float) -> dict[str, str] | None:
    """Run ``test_files`` inside ``target`` and return ``{id: outcome}``.

    The id format matches ``scripts/mine_heldout_tasks.py:_run_pytest``
    (``<junit classname>::<name>``), and a missing report means the run told us
    nothing — ``None``, never an empty dict that would read as "all absent".
    ``PYTHONSAFEPATH`` is removed because ``python -m pytest`` must be able to
    import the target's top-level modules (see ``mini_ork/cli/main.py``).
    """
    tmp = tempfile.mkdtemp(prefix="mo-probe-grade-")
    try:
        report = os.path.join(tmp, "junit.xml")
        home = os.path.join(tmp, "home")
        env = {**os.environ}
        env.pop("PYTHONSAFEPATH", None)
        env["MINI_ORK_ROOT"] = target
        env["MINI_ORK_HOME"] = home
        env["MINI_ORK_DB"] = os.path.join(home, "state.db")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 f"--junitxml={report}", *test_files],
                cwd=target, capture_output=True, timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            return None
        if not os.path.isfile(report):
            return None
        try:
            tree = ET.parse(report)
        except ET.ParseError:
            return None
        results: dict[str, str] = {}
        for case in tree.iter("testcase"):
            classname = case.get("classname") or ""
            name = case.get("name") or ""
            outcome = "passed"
            if case.find("failure") is not None:
                outcome = "failed"
            elif case.find("error") is not None:
                outcome = "error"
            elif case.find("skipped") is not None:
                outcome = "skipped"
            results[f"{classname}::{name}"] = outcome
        return results
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Files the solver may NOT bring into the grading tree: anything that steers
# pytest or Python startup (a forged conftest/plugin/config can turn every test
# green), plus the solver's own tests. Base-snapshot versions are kept instead.
_GRADE_PROTECTED_NAMES = frozenset({
    "conftest.py", "pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg",
    "pyproject.toml", "sitecustomize.py", "usercustomize.py",
})


def _grade_protected(rel: str, hidden_dirs: set[str]) -> bool:
    """Whether a solver-changed path stays at its base-snapshot version."""
    base = os.path.basename(rel)
    if base in _GRADE_PROTECTED_NAMES or rel.endswith(".pth"):
        return True
    if base.endswith(".py") and (base.startswith("test_") or base.endswith("_test.py")):
        return True
    return base == "__init__.py" and os.path.dirname(rel) in hidden_dirs


def _solver_changes(target: str) -> tuple[list[str], list[str]] | None:
    """``(changed_or_new, deleted)`` paths in the solver's tree vs its first commit.

    ``target`` is the one-commit repo ``_make_target_repo`` built from the base
    snapshot (the run may add commits on top). ``None`` when git cannot answer,
    which the caller treats as "cannot grade" (fail closed).
    """
    def _git_lines(*args: str) -> list[str] | None:
        proc = subprocess.run(["git", "-C", target, *args],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            return None
        return [line for line in proc.stdout.splitlines() if line.strip()]

    roots = _git_lines("rev-list", "--max-parents=0", "HEAD")
    if not roots:
        return None
    first = roots[-1]
    changed = _git_lines("diff", "--name-only", "--diff-filter=d", first)
    deleted = _git_lines("diff", "--name-only", "--diff-filter=D", first)
    untracked = _git_lines("ls-files", "--others", "--exclude-standard")
    if changed is None or deleted is None or untracked is None:
        return None
    return sorted(set(changed) | set(untracked)), sorted(set(deleted))


def _inside_tree(tree: str, rel: str) -> str | None:
    """``tree/rel`` when its parent resolves inside ``tree``, else None."""
    dest = os.path.join(tree, rel)
    real_tree = os.path.realpath(tree)
    parent = os.path.realpath(os.path.dirname(dest))
    if parent != real_tree and not parent.startswith(real_tree + os.sep):
        return None
    return dest


def _grade_git_oracle(target: str, oracle: dict, *,
                      timeout: float | None = None) -> bool:
    """Whether the run's result passes the hidden tests from ``fix_sha``.

    Grading never runs in the solver's tree, so nothing the solver wrote can
    steer the test run:

    1. a fresh history-free snapshot of ``base_sha`` is materialized;
    2. the solver's changed regular files are copied in, except the protected
       ones (``_grade_protected``: conftest/pytest/tox/setup/pyproject config,
       site/usercustomize, ``.pth`` files, the solver's own tests); symlinks are
       never followed or copied, and solver deletions are replayed;
    3. the hidden test files from ``fix_sha`` are overlaid;
    4. pytest runs there, and the snapshot is removed.

    The solver's tree is left untouched. Any failure to build the grading tree
    fails closed (``False``).
    """
    if timeout is None:
        timeout = float(os.environ.get("MO_APPLY_PROBE_GRADE_TIMEOUT_S", "600"))
    hidden: dict[str, bytes] = {}
    for path in oracle["test_files"]:
        shown = subprocess.run(
            ["git", "-C", _root(), "show", f"{oracle['fix_sha']}:{path}"],
            capture_output=True)
        if shown.returncode != 0:
            return False
        hidden[path] = shown.stdout
    changes = _solver_changes(target)
    if changes is None:
        return False
    changed, deleted = changes
    hidden_dirs = {os.path.dirname(p) for p in hidden}
    try:
        tree = materialize_git_tree(oracle["base_sha"])
    except Exception:  # noqa: BLE001 — an unbuildable grading tree is a fail
        return False
    try:
        for rel in changed:
            if rel in hidden or _grade_protected(rel, hidden_dirs):
                continue
            src = os.path.join(target, rel)
            if os.path.islink(src) or not os.path.isfile(src):
                continue
            dest = _inside_tree(tree, rel)
            if dest is None:
                return False
            os.makedirs(os.path.dirname(dest) or tree, exist_ok=True)
            if os.path.islink(dest):
                os.unlink(dest)
            shutil.copyfile(src, dest)
        for rel in deleted:
            if rel in hidden or _grade_protected(rel, hidden_dirs):
                continue
            dest = _inside_tree(tree, rel)
            if dest is not None and os.path.isfile(dest) and not os.path.islink(dest):
                os.unlink(dest)
        for rel, blob in hidden.items():
            dest = _inside_tree(tree, rel)
            if dest is None:
                return False
            os.makedirs(os.path.dirname(dest) or tree, exist_ok=True)
            if os.path.islink(dest):
                os.unlink(dest)
            with open(dest, "wb") as fh:
                fh.write(blob)
        results = run_pytest_junit(tree, oracle["test_files"], timeout=timeout)
    finally:
        shutil.rmtree(tree, ignore_errors=True)
    if results is None:
        return False
    expected = set(oracle["fail_to_pass"]) | set(oracle["pass_to_pass"])
    return all(results.get(t) == "passed" for t in expected)


def _probe_outcome(probe_path: str, run_id: str, target: str | None,
                   db: str | None = None) -> tuple[float, bool | None]:
    """``(outcome, hidden_pass)`` for one arm of one probe.

    Non-git probes are unchanged: the outcome is the publish verdict and
    ``hidden_pass`` is None (so the caller adds no key). A git-oracle probe is
    solved only when the run published AND the hidden tests pass; an
    unpublished run is 0.0 without spending a grading run at all.

    ``db`` is forwarded keyword-only when given, but the no-db path must pass
    exactly one positional argument: ``tests/unit/test_cli_apply_py.py`` patches
    ``_run_outcome`` with a one-argument fake.
    """
    published = _run_outcome(run_id) if db is None else _run_outcome(run_id, db=db)
    oracle = _probe_oracle(probe_path)
    if oracle is None:
        return published, None
    if published < 1.0 or not target:
        return 0.0, False
    hidden = _grade_git_oracle(target, oracle)
    return (1.0 if hidden else 0.0), hidden


def probe_score(task_class: str, target_file: str | None,
                directive: str, *, source_ref: str = "", context: str = "") -> dict | None:
    """Two-arm held-out evaluation of one directive mutation.

    Arms: baseline = the recipe exactly as committed; candidate = the same
    recipe with the directive block appended to the target prompt (identical
    append semantics to ``apply_mutation``, marker included, so what the probe
    measures is exactly what a promote would land).

    Anchor split (kickoff rule #1): probes with ``anchor: true`` in their YAML
    frontmatter are run for both arms like any probe, but their outcomes do
    NOT enter the gate's ``before``/``after`` vectors. The candidate arm's
    mean outcome on anchor probes is reported as ``anchor_solved_frac``; with
    no anchor probes that field is ``None`` so the apply loop knows to skip
    the ``collapse_history`` write.

    Returns a probe-result dict:
        before / after / n     — scalar utilities + probe count (GATE probes
                                 only — anchors never enter this average)
        pertask_json           — JSON with before/after/ids vectors for the
                                 gate's per-task no-regression rule (GATE only)
        anchor_solved_frac     — mean candidate outcome on anchor probes, or
                                 None when no anchor probes exist
        directives             — count (1 for ``probe_score``, the candidate
                                 is one ``suggested_change``)
        runs                   — [{probe, arm, run_id, outcome, cost_usd}]
        cost_usd               — total spend across every launch
    or None when the recipe has no frozen probe set (caller must NOT promote).
    """
    probes = _load_probes(task_class)
    if not probes:
        return None
    _ensure_scratch_parent()
    if not target_file or not directive:
        return None  # no candidate arm can be materialized → nothing to measure
    budget = float(_probe_setting(task_class, "MO_APPLY_PROBE_BUDGET_USD",
                                  "budget_usd", 2.0))
    # MO_APPLY_PROBE_CONTROL_N: how many times to run the unmutated baseline arm
    # per probe task before scoring the candidate. The per-probe baseline outcome
    # becomes the BEST of N retries, so run-to-run noise in the unmutated recipe
    # can no longer be credited to the candidate (G06-T03). Default 3; minimum
    # 1 reproduces the pre-change behaviour byte-identically (max([x]) == x).
    control_n = int(_probe_setting(task_class, "MO_APPLY_PROBE_CONTROL_N",
                                   "control_n", 3, as_int=True))

    # Deferred import: apply imports this module at call time only, and the
    # directive-block builder lives in apply — importing here avoids the cycle.
    from mini_ork.cli.apply import _directive_block
    block = _directive_block(directive, source_ref=source_ref, context=context)

    temp_dirs: list[str] = []
    temp_targets: list[str] = []
    runs: list[dict] = []
    before_v: list[float] = []
    after_v: list[float] = []
    anchor_after_v: list[float] = []  # candidate arm outcomes on anchor probes
    ids: list[str] = []
    control_solved_per_probe: list[int] = []
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
        for probe, is_anchor in probes:
            if spent >= budget or stop_sweep:
                break  # n truncates to probes completed in BOTH arms below
            # ── Control arm: N retries on the unmutated baseline ────────────
            # The per-probe control outcome is the BEST of N retries: a task
            # is solved by control if ANY retry solved it. The candidate gets
            # credit for that task only if it solves one the control did not.
            control_outcomes: list[float] = []
            control_unscored = False
            last_cost = 0.0  # pre-flight estimate for the upcoming retry
            for retry in range(control_n):
                if spent + last_cost > budget or stop_sweep:
                    # Budget cap binds across all arms including the N control
                    # retries. Pre-flight BEFORE spending: refuse the launch
                    # when the previous retry's cost would already exceed the
                    # budget, instead of overspending first. Stop and return
                    # None rather than score against an incomplete control set
                    # (kickoff step 1).
                    control_unscored = True
                    break
                target = _materialize_target(probe)
                if target:
                    temp_targets.append(target)
                try:
                    _stdout, run_id, cost = _launch_run(
                        base_name, probe, target_cwd=target)
                except ProbeArmTimeout as exc:
                    # Mirror the candidate arm: a timeout stops the sweep but
                    # the probes already measured (in BOTH arms) are preserved
                    # — n truncates to min(len(before_v), len(after_v)). An arm
                    # timeout must NOT throw away a whole sweep's evidence.
                    runs.append({"probe": os.path.basename(probe),
                                 "arm": "baseline", "retry": retry,
                                 "run_id": "", "outcome": None,
                                 "cost_usd": 0.0, "timed_out": True,
                                 "error": str(exc)})
                    stop_sweep = True
                    break  # NOT control_unscored — partial results survive
                spent += cost
                last_cost = cost
                outcome, hidden = _probe_outcome(probe, run_id, target, db=None)
                entry = {"probe": os.path.basename(probe),
                         "arm": "baseline", "retry": retry,
                         "run_id": run_id, "outcome": outcome,
                         "cost_usd": cost}
                if hidden is not None:
                    entry["hidden_pass"] = hidden
                runs.append(entry)
                control_outcomes.append(outcome)
            if control_unscored:
                return None
            # A timeout on retry 0 leaves control_outcomes empty: skip the
            # probe — the outer probe loop sees stop_sweep=True and breaks,
            # and n truncates to the probes finished in BOTH arms.
            if not control_outcomes:
                break
            # ── Candidate arm: single run, as today ─────────────────────────
            if spent >= budget or stop_sweep:
                break
            target = _materialize_target(probe)
            if target:
                temp_targets.append(target)
            try:
                _stdout, run_id, cost = _launch_run(
                    cand_name, probe, target_cwd=target)
            except ProbeArmTimeout as exc:
                # No usable outcome for this arm. Record why and stop the
                # sweep: n truncates to the probes finished in BOTH arms,
                # so the probes already measured are kept rather than
                # thrown away with the run that blew the timeout.
                runs.append({"probe": os.path.basename(probe),
                             "arm": "candidate", "run_id": "",
                             "outcome": None, "cost_usd": 0.0,
                             "timed_out": True, "error": str(exc)})
                stop_sweep = True
                break
            spent += cost
            outcome, hidden = _probe_outcome(probe, run_id, target, db=None)
            entry = {"probe": os.path.basename(probe), "arm": "candidate",
                     "run_id": run_id, "outcome": outcome,
                     "cost_usd": cost,
                     "is_anchor": is_anchor}
            if hidden is not None:
                entry["hidden_pass"] = hidden
            runs.append(entry)
            # Anchor split (rule #1): anchor probes never enter the gate's
            # before/after vectors — they exist to feed the collapse
            # detector's anchor column, NOT to be optimized by the apply
            # gate. Gate probes preserve the strict-superset semantics
            # the gate already implements (G06-T03).
            if is_anchor:
                anchor_after_v.append(outcome)
            else:
                before_v.append(max(control_outcomes))
                ids.append(os.path.basename(probe))
                control_solved_per_probe.append(
                    1 if max(control_outcomes) >= 1.0 else 0)
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
    control_solved_per_probe = control_solved_per_probe[:n]
    # anchor_solved_frac == None when no anchor probes exist (kickoff rule
    # #1: "With no anchor probes, anchor_solved_frac is None"). The apply
    # loop uses that as the cue to skip the collapse_history write entirely.
    anchor_solved_frac = (sum(anchor_after_v) / len(anchor_after_v)
                         if anchor_after_v else None)
    if n == 0:
        return {"before": 0.0, "after": 0.0, "n": 0, "pertask_json": "",
                "runs": runs, "cost_usd": round(spent, 4),
                "truncated_by_budget": True,
                "control_n": control_n, "control_solved": [],
                "anchor_solved_frac": anchor_solved_frac,
                "directives": 1}
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
        "control_n": control_n,
        "control_solved": control_solved_per_probe,
        "anchor_solved_frac": anchor_solved_frac,
        "directives": 1,
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

    The anchor split mirrors ``probe_score``: probes with ``anchor: true`` in
    their YAML frontmatter are excluded from the gate's before/after vectors,
    and the candidate arm's mean outcome on those probes is reported as
    ``anchor_solved_frac``. Code-arm candidates have ``directives == 0``
    (the candidate is a patch, not a directive).

    Returns the same shape as ``probe_score``, or None when the recipe has no
    frozen probe set or the patch cannot apply — the caller must not promote.
    """
    probes = _load_probes(task_class)
    recipe = _recipe_dir(task_class)
    if not probes or recipe is None:
        return None
    _ensure_scratch_parent()
    budget = float(_probe_setting(task_class, "MO_APPLY_PROBE_BUDGET_USD",
                                  "budget_usd", 2.0))
    control_n = int(_probe_setting(task_class, "MO_APPLY_PROBE_CONTROL_N",
                                   "control_n", 3, as_int=True))
    recipe_name = os.path.basename(recipe)

    arms: list[tuple[str, str, str]] = []  # (arm, tmpdir, tree)
    temp_targets: list[str] = []
    runs: list[dict] = []
    before_v: list[float] = []
    after_v: list[float] = []
    anchor_after_v: list[float] = []  # candidate arm outcomes on anchor probes
    ids: list[str] = []
    control_solved_per_probe: list[int] = []
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

        base_tree = arms[0][2]
        cand_tree = arms[1][2]
        stop_sweep = False
        for probe, is_anchor in probes:
            if spent >= budget or stop_sweep:
                break  # n truncates to probes completed in BOTH arms below
            # ── Control arm: N retries on the baseline worktree ────────────
            control_outcomes: list[float] = []
            control_unscored = False
            last_cost = 0.0  # pre-flight estimate for the upcoming retry
            for retry in range(control_n):
                if spent + last_cost > budget or stop_sweep:
                    # Budget pre-flight: refuse the launch BEFORE spending when
                    # the previous retry's cost would already exceed the
                    # budget. Stop and return None rather than score against
                    # an incomplete control set (kickoff step 1).
                    control_unscored = True
                    break
                target = _materialize_target(probe)
                if target:
                    temp_targets.append(target)
                try:
                    _stdout, run_id, cost = _launch_run(
                        recipe_name, probe, target_cwd=target, root=base_tree)
                except ProbeArmTimeout as exc:
                    # Mirror the candidate arm: a timeout stops the sweep but
                    # the probes already measured (in BOTH arms) are preserved
                    # — n truncates to min(len(before_v), len(after_v)). An arm
                    # timeout must NOT throw away a whole sweep's evidence.
                    runs.append({"probe": os.path.basename(probe),
                                 "arm": "baseline", "retry": retry,
                                 "run_id": "", "outcome": None,
                                 "cost_usd": 0.0, "timed_out": True,
                                 "error": str(exc)})
                    stop_sweep = True
                    break  # NOT control_unscored — partial results survive
                spent += cost
                last_cost = cost
                outcome, hidden = _probe_outcome(
                    probe, run_id, target, db=_arm_db(base_tree))
                entry = {"probe": os.path.basename(probe),
                         "arm": "baseline", "retry": retry,
                         "run_id": run_id, "outcome": outcome,
                         "cost_usd": cost}
                if hidden is not None:
                    entry["hidden_pass"] = hidden
                runs.append(entry)
                control_outcomes.append(outcome)
            if control_unscored:
                return None
            # A timeout on retry 0 leaves control_outcomes empty: skip the
            # probe — the outer probe loop sees stop_sweep=True and breaks,
            # and n truncates to the probes finished in BOTH arms.
            if not control_outcomes:
                break
            # ── Candidate arm: single run on the candidate worktree ────────
            if spent >= budget or stop_sweep:
                break
            target = _materialize_target(probe)
            if target:
                temp_targets.append(target)
            try:
                _stdout, run_id, cost = _launch_run(
                    recipe_name, probe, target_cwd=target, root=cand_tree)
            except ProbeArmTimeout as exc:
                # No usable outcome for this arm. Record why and stop the
                # sweep: n truncates to the probes finished in BOTH arms,
                # so the probes already measured are kept rather than
                # thrown away with the run that blew the timeout.
                runs.append({"probe": os.path.basename(probe),
                             "arm": "candidate", "run_id": "",
                             "outcome": None, "cost_usd": 0.0,
                             "timed_out": True, "error": str(exc)})
                stop_sweep = True
                break
            spent += cost
            outcome, hidden = _probe_outcome(
                probe, run_id, target, db=_arm_db(cand_tree))
            entry = {"probe": os.path.basename(probe), "arm": "candidate",
                     "run_id": run_id, "outcome": outcome,
                     "cost_usd": cost,
                     "is_anchor": is_anchor}
            if hidden is not None:
                entry["hidden_pass"] = hidden
            runs.append(entry)
            # Anchor split: same semantics as ``probe_score``. Code-arm
            # candidates still write ``anchor_solved_frac`` to collapse_history
            # so the detector sees anchor drift across code mutations too.
            if is_anchor:
                anchor_after_v.append(outcome)
            else:
                before_v.append(max(control_outcomes))
                ids.append(os.path.basename(probe))
                control_solved_per_probe.append(
                    1 if max(control_outcomes) >= 1.0 else 0)
                after_v.append(outcome)
    finally:
        for _arm, tmp, tree in arms:
            _remove_arm(tmp, tree)
        for path in temp_targets:
            shutil.rmtree(path, ignore_errors=True)

    n = min(len(before_v), len(after_v))
    before_v, after_v, ids = before_v[:n], after_v[:n], ids[:n]
    control_solved_per_probe = control_solved_per_probe[:n]
    anchor_solved_frac = (sum(anchor_after_v) / len(anchor_after_v)
                         if anchor_after_v else None)
    if n == 0:
        result = {"before": 0.0, "after": 0.0, "n": 0, "pertask_json": "",
                  "runs": runs, "cost_usd": round(spent, 4),
                  "truncated_by_budget": True,
                  "control_n": control_n, "control_solved": [],
                  "anchor_solved_frac": anchor_solved_frac,
                  "directives": 0}
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
            "control_n": control_n,
            "control_solved": control_solved_per_probe,
            "anchor_solved_frac": anchor_solved_frac,
            "directives": 0,
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
