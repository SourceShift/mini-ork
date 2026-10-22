"""SDE-5b: the supervisor applies proposed graph patches and records outcomes.

Hermetic: a temp mini-ork home with a sqlite ``state.db``, a tiny workflow
pinned through ``MINI_ORK_WORKFLOW`` (what ``graph_patch.base_workflow`` falls
back to), and patches created with ``graph_patch.propose``. The sweep's
injectable ``spawn(run_id, from_node)`` seam stands in for a detached revive.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from mini_ork.recovery import graph_patch, supervisor

TINY_WORKFLOW = """\
nodes:
  - name: planner
    type: planner
    model_lane: glm
  - name: implementer
    type: implementer
    model_lane: glm
  - name: reviewer
    type: reviewer
    model_lane: glm
edges:
  - from: planner
    to: implementer
    edge_type: depends_on
  - from: implementer
    to: reviewer
    edge_type: depends_on
"""

SCHEMA = """
CREATE TABLE task_runs (
    id TEXT PRIMARY KEY, status TEXT, updated_at INTEGER,
    created_at INTEGER, kickoff_path TEXT, notes TEXT DEFAULT '');
CREATE TABLE epics (
    id TEXT PRIMARY KEY, status TEXT, last_run_id TEXT,
    notes TEXT DEFAULT '');
CREATE TABLE node_checkpoints (run_id TEXT, node_id TEXT, status TEXT);
CREATE TABLE diagnosis_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, fix_epic_id TEXT,
    label TEXT, label_reason TEXT, labelled_at INTEGER);
CREATE TABLE mo_inbox_gates (gate_id TEXT, feature TEXT, status TEXT);
CREATE TABLE epic_attempts (epic_id TEXT, run_id TEXT, attempt INTEGER);
"""

HERMETIC_ENV = (
    "MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_ROOT", "MINI_ORK_RUN_DIR",
    "MINI_ORK_RUN_ID", "MINI_ORK_PLAN_PATH", "MINI_ORK_RECIPE",
    "MINI_ORK_TASK_CLASS", "MO_SUPERVISE_MAX_PATCHES_PER_RUN",
    "MO_REPLAN_LANE",
)

RERUN = {"kind": "rerun", "from_node": "implementer",
         "reason": "node failed", "source": "orchestrator"}


class Home:
    """A temp mini-ork home plus the two tables the sweep's rules touch."""

    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        (self.home / "runs").mkdir(parents=True)
        self.db = self.home / "state.db"
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        con.commit()
        con.close()
        self.engine = root / "engine"
        self.engine.mkdir()
        self.workflow = root / "workflow.yaml"
        self.workflow.write_text(TINY_WORKFLOW, encoding="utf-8")

    def sql(self, sql: str, args: tuple = ()) -> None:
        con = sqlite3.connect(self.db)
        try:
            con.execute(sql, args)
            con.commit()
        finally:
            con.close()

    def add_run(self, run_id: str, status: str, updated_at: int = 2_000) -> None:
        self.sql(
            "INSERT INTO task_runs (id, status, updated_at, created_at,"
            " kickoff_path, notes) VALUES (?,?,?,?,?,?)",
            (run_id, status, updated_at, 1_000, None, ""))

    def run_dir(self, run_id: str) -> Path:
        path = self.home / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def patch_dir(self, run_id: str) -> Path:
        path = self.run_dir(run_id) / graph_patch.PATCH_DIRNAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_repair(self, run_id: str, payload: dict) -> None:
        (self.run_dir(run_id) / "repair.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")

    def read_repair(self, run_id: str) -> dict | None:
        path = self.home / "runs" / run_id / "repair.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def propose(self, run_id: str, patch: dict) -> int:
        return graph_patch.propose(self.run_dir(run_id), dict(patch))

    def mark_applied(self, run_id: str, n: int) -> None:
        (self.patch_dir(run_id) / f"{n:03d}.applied.json").write_text(
            json.dumps({"applied_at": "2026-01-01T00:00:00Z", "pid": 1,
                        "engine_sha": "", "from_node": "implementer"}) + "\n",
            encoding="utf-8")

    def write_outcome(self, run_id: str, n: int, passed: bool,
                      status: str = "failed") -> None:
        (self.patch_dir(run_id) / f"{n:03d}.outcome.json").write_text(
            json.dumps({"passed": passed, "status": status,
                        "labelled_at": 111}) + "\n",
            encoding="utf-8")

    def read_outcome(self, run_id: str, n: int) -> dict | None:
        path = self.patch_dir(run_id) / f"{n:03d}.outcome.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


class Spawn:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def __call__(self, run_id, from_node):
        self.calls.append((run_id, from_node))
        return 4321


@pytest.fixture()
def sh(tmp_path, monkeypatch):
    home = Home(tmp_path)
    for key in HERMETIC_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MINI_ORK_WORKFLOW", str(home.workflow))
    return home


def _sweep(sh: Home, spawn=None, now: int = 5_000, engine=None, **kwargs):
    return supervisor.sweep(str(sh.home), str(sh.db),
                            engine_root=str(engine or sh.engine), now=now,
                            spawn=spawn, **kwargs)


def _applies(actions):
    return [a for a in actions if a.get("action") == "apply_patch"]


def _candidate(sh: Home, run_id: str, *, status: str = "failed",
               repair: dict | None = {"state": "gave_up"},
               patch: dict | None = None) -> int:
    sh.add_run(run_id, status)
    if repair is not None:
        sh.write_repair(run_id, repair)
    return sh.propose(run_id, dict(patch or RERUN))


def _git_engine(root: Path, message: str) -> Path:
    engine = root / "engine-git"
    engine.mkdir()
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", "-C", str(engine), *args], check=True, capture_output=True,
        text=True)
    run("init", "-q")
    run("-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-q",
        "--allow-empty", "-m", message)
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    return engine


# ── R2: outcome files ───────────────────────────────────────────────────────


def test_r2_writes_outcome_once_and_never_rewrites(sh):
    run_id = "run-published"
    sh.add_run(run_id, "published")
    sh.propose(run_id, dict(RERUN))
    sh.mark_applied(run_id, 1)
    sh.write_repair(run_id, {"state": "reviving", "revived_at": 1_000,
                             "revive_from_node": "implementer",
                             "revive_patch": "001"})

    actions = _sweep(sh)

    labels = [a for a in actions if a.get("action") == "label"]
    assert len(labels) == 1, actions
    assert labels[0]["patch"] == "001"
    outcome = sh.read_outcome(run_id, 1)
    assert outcome == {"passed": True, "status": "published",
                       "labelled_at": 5_000}

    path = sh.patch_dir(run_id) / "001.outcome.json"
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    assert not [a for a in _sweep(sh, now=9_000) if a.get("action") == "label"]
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


def test_r2_outcome_uses_computed_correct_not_status(sh):
    run_id = "run-checkpoint"
    sh.add_run(run_id, "failed")
    sh.sql("INSERT INTO node_checkpoints VALUES (?,?,?)",
           (run_id, "implementer", "success"))
    sh.propose(run_id, dict(RERUN))
    sh.mark_applied(run_id, 1)
    sh.write_repair(run_id, {"state": "reviving", "revived_at": 1_000,
                             "revive_from_node": "implementer",
                             "revive_patch": "001"})

    _sweep(sh)

    outcome = sh.read_outcome(run_id, 1)
    assert outcome["passed"] is True
    assert outcome["status"] == "failed"


def test_r2_without_revive_patch_writes_no_outcome(sh):
    run_id = "run-no-patch-key"
    sh.add_run(run_id, "published")
    sh.write_repair(run_id, {"state": "reviving", "revived_at": 1_000,
                             "revive_from_node": "implementer"})

    _sweep(sh)

    assert list(sh.patch_dir(run_id).glob("*.outcome.json")) == []


# ── R6: the apply path ──────────────────────────────────────────────────────


def test_r6_applies_rerun_and_marks_reviving(sh):
    run_id = "run-gave-up"
    _candidate(sh, run_id)
    spawn = Spawn()

    actions = _sweep(sh, spawn)

    applied = _applies(actions)
    assert len(applied) == 1, actions
    assert applied[0] == {"run_id": run_id, "action": "apply_patch",
                          "patch": "001", "kind": "rerun",
                          "reason": applied[0]["reason"]}
    assert (sh.patch_dir(run_id) / "001.applied.json").is_file()
    repair = sh.read_repair(run_id)
    assert repair["state"] == "reviving"
    assert repair["revive_patch"] == "001"
    assert spawn.calls == [(run_id, "implementer")]


def test_r6_applies_when_repair_json_absent(sh):
    run_id = "run-no-repair"
    _candidate(sh, run_id, repair=None)

    actions = _sweep(sh, Spawn())

    assert len(_applies(actions)) == 1, actions
    assert sh.read_repair(run_id)["revive_patch"] == "001"


def test_r6_applies_revive_failed(sh):
    run_id = "run-revive-failed"
    _candidate(sh, run_id, repair={"state": "revive_failed"})

    assert len(_applies(_sweep(sh, Spawn()))) == 1


def test_r6_picks_lowest_proposed(sh):
    run_id = "run-lowest"
    sh.add_run(run_id, "failed")
    sh.write_repair(run_id, {"state": "gave_up"})
    assert sh.propose(run_id, dict(RERUN)) == 1
    sh.mark_applied(run_id, 1)
    sh.write_outcome(run_id, 1, passed=False)
    assert sh.propose(run_id, {"kind": "rerun", "from_node": "reviewer",
                               "reason": "also failed",
                               "source": "orchestrator"}) == 2

    applied = _applies(_sweep(sh, Spawn()))

    assert len(applied) == 1 and applied[0]["patch"] == "002"


def test_r6_spawn_adapter_passes_from_node(sh):
    run_id = "run-spawn-seam"
    _candidate(sh, run_id)
    spawn = Spawn()

    _sweep(sh, spawn)

    assert spawn.calls == [(run_id, "implementer")]


# ── R6: skip gates ──────────────────────────────────────────────────────────


def test_r6_skips_when_applied_patch_has_no_outcome(sh):
    run_id = "run-in-flight"
    sh.add_run(run_id, "failed")
    sh.write_repair(run_id, {"state": "gave_up"})
    sh.propose(run_id, dict(RERUN))
    sh.mark_applied(run_id, 1)  # outcome deliberately absent
    sh.propose(run_id, {"kind": "rerun", "from_node": "reviewer",
                        "reason": "also failed", "source": "orchestrator"})

    assert not _applies(_sweep(sh, Spawn()))


def test_r6_skips_at_default_cap_and_env_nine_clamps(sh, monkeypatch):
    monkeypatch.setenv("MO_SUPERVISE_MAX_PATCHES_PER_RUN", "9")
    capped = "run-capped"
    sh.add_run(capped, "failed")
    sh.write_repair(capped, {"state": "gave_up"})
    for node in ("implementer", "reviewer", "planner"):
        sh.propose(capped, {"kind": "rerun", "from_node": node,
                            "reason": "failed", "source": "orchestrator"})
    for n in (1, 2):
        sh.mark_applied(capped, n)
        sh.write_outcome(capped, n, passed=False)
    fresh = "run-fresh"
    _candidate(sh, fresh)
    spawn = Spawn()

    actions = _sweep(sh, spawn)

    assert not [a for a in _applies(actions) if a["run_id"] == capped], actions
    assert not (sh.patch_dir(capped) / "003.applied.json").is_file()
    assert len([a for a in _applies(actions) if a["run_id"] == fresh]) == 1


@pytest.mark.parametrize("status", ["executing", "published", "superseded",
                                    "not_needed"])
def test_r6_skips_non_failed_statuses(sh, status):
    _candidate(sh, f"run-{status}", status=status)

    assert not _applies(_sweep(sh, Spawn()))


@pytest.mark.parametrize("state", ["waiting_on_fix", "repairing", "reviving"])
def test_r6_skips_repair_states_owned_by_a_loop(sh, state):
    _candidate(sh, f"run-{state}", repair={"state": state})

    assert not _applies(_sweep(sh, Spawn()))


_REPLAN = {"kind": "replan", "from_node": "implementer",
           "reason": "re-plan it", "source": "orchestrator"}


def test_r6_skips_replan_when_flag_absent(sh, monkeypatch):
    monkeypatch.delattr(graph_patch, "REPLAN_SUPPORTED", raising=False)
    _candidate(sh, "run-replan-absent", patch=_REPLAN)

    assert not _applies(_sweep(sh, Spawn()))


def test_r6_skips_replan_when_flag_false(sh, monkeypatch):
    monkeypatch.setattr(graph_patch, "REPLAN_SUPPORTED", False)
    _candidate(sh, "run-replan-false", patch=_REPLAN)

    assert not _applies(_sweep(sh, Spawn()))


def test_r6_replan_skip_falls_through_to_next_proposed(sh, monkeypatch):
    monkeypatch.setattr(graph_patch, "REPLAN_SUPPORTED", False)
    run_id = "run-replan-then-rerun"
    sh.add_run(run_id, "failed")
    sh.write_repair(run_id, {"state": "gave_up"})
    assert sh.propose(run_id, {"kind": "replan", "from_node": "implementer",
                               "reason": "re-plan it",
                               "source": "orchestrator"}) == 1
    assert sh.propose(run_id, {"kind": "rerun", "from_node": "reviewer",
                               "reason": "retry the reviewer",
                               "source": "orchestrator"}) == 2

    applied = _applies(_sweep(sh, Spawn()))

    assert len(applied) == 1 and applied[0]["patch"] == "002"


# ── R6: control arm ─────────────────────────────────────────────────────────


def _history(sh: Home, run_id: str, kind: str, passed: list[bool]) -> None:
    for index, ok in enumerate(passed):
        node = f"lane-node-{index}"
        if kind == "swap_lane":
            patch = {"kind": kind, "from_node": node, "node": node,
                     "lane": "glm", "reason": "history",
                     "source": "orchestrator"}
        else:
            patch = {"kind": kind, "from_node": node, "reason": "history",
                     "source": "orchestrator"}
        number = sh.propose(run_id, patch)
        sh.mark_applied(run_id, number)
        sh.write_outcome(run_id, number, passed=ok)


def _swap_candidate(sh: Home, run_id: str) -> None:
    _candidate(sh, run_id, patch={
        "kind": "swap_lane", "from_node": "reviewer", "node": "reviewer",
        "lane": "opus", "reason": "a costlier lane may do better",
        "source": "orchestrator"})


def test_control_arm_skips_kind_not_better_than_rerun(sh):
    _history(sh, "hist-swap", "swap_lane", [True] + [False] * 4)
    _history(sh, "hist-rerun", "rerun", [True] * 4 + [False])
    _swap_candidate(sh, "run-swap")
    spawn = Spawn()

    actions = _sweep(sh, spawn)

    assert not _applies(actions)
    assert spawn.calls == []
    skips = [a for a in actions if a.get("run_id") == "run-swap"
             and "not better than a blind rerun" in str(a.get("reason"))]
    assert len(skips) == 1 and "swap_lane" in skips[0]["reason"]


def test_control_arm_applies_below_five_samples(sh):
    _history(sh, "hist-swap", "swap_lane", [True, True, False])
    _history(sh, "hist-rerun", "rerun", [True] * 4 + [False])
    _swap_candidate(sh, "run-swap")

    applied = _applies(_sweep(sh, Spawn()))

    assert len(applied) == 1 and applied[0]["kind"] == "swap_lane"


def test_control_arm_needs_rerun_at_five_too(sh):
    _history(sh, "hist-swap", "swap_lane", [False] * 5)
    _swap_candidate(sh, "run-swap")

    assert len(_applies(_sweep(sh, Spawn()))) == 1  # exploration


# ── R6: budget, dry run, failure containment ────────────────────────────────


def test_r6_shares_revive_budget_with_r1(sh, tmp_path):
    engine = _git_engine(tmp_path, "fix epic EP-9 landed")
    sh.sql("INSERT INTO epics (id, status) VALUES (?,?)", ("EP-9", "done"))
    sh.write_repair("run-r1-parked", {
        "state": "waiting_on_fix", "fix_epic_id": "EP-9",
        "attempts": [{"ts": 4_000, "from_node": "implementer",
                      "reason": "node failed"}]})
    _candidate(sh, "run-r6-candidate")
    spawn = Spawn()

    actions = _sweep(sh, spawn, engine=engine, max_revives=1)

    acted = [a for a in actions
             if a.get("action") in ("revive", "apply_patch")]
    assert len(acted) == 1, actions
    assert len(spawn.calls) == 1


def test_r6_dry_run_computes_same_action_and_applies_nothing(sh):
    run_id = "run-dry"
    _candidate(sh, run_id)
    spawn = Spawn()
    before = (sh.run_dir(run_id) / "repair.json").read_bytes()

    dry = _applies(_sweep(sh, spawn, dry_run=True))
    assert len(dry) == 1, dry
    assert not (sh.patch_dir(run_id) / "001.applied.json").is_file()
    assert spawn.calls == []
    assert (sh.run_dir(run_id) / "repair.json").read_bytes() == before

    wet = _applies(_sweep(sh, spawn))
    assert len(wet) == 1
    for key in ("action", "run_id", "patch", "kind", "reason"):
        assert dry[0][key] == wet[0][key]


def test_r6_materialize_failure_is_error_and_sweep_continues(sh):
    broken = "run-broken"
    _candidate(sh, broken, patch={
        "kind": "insert_before", "from_node": "fresh_node",
        "node": "ghost_node",
        "new_node": {"name": "fresh_node", "type": "researcher"},
        "reason": "insert before a missing node",
        "source": "orchestrator"})
    _candidate(sh, "run-healthy")

    actions = _sweep(sh, Spawn())

    errors = [a for a in actions
              if a.get("action") == "error" and a.get("run_id") == broken]
    assert len(errors) == 1, actions
    assert "ghost_node" in errors[0]["reason"]
    assert not (sh.patch_dir(broken) / "001.applied.json").is_file()
    assert sh.read_repair(broken)["state"] == "gave_up"
    assert len([a for a in _applies(actions) if a["run_id"] == "run-healthy"]) == 1


def test_r6_failed_apply_is_recorded_once_and_never_retried(sh):
    # Review addition: a patch that cannot materialize must not be retried on
    # every sweep (a replan re-asks the model each time), and the failed
    # attempt must not consume the sweep's revive budget.
    broken = "run-broken-once"
    _candidate(sh, broken, patch={
        "kind": "insert_before", "from_node": "fresh_node",
        "node": "ghost_node",
        "new_node": {"name": "fresh_node", "type": "researcher"},
        "reason": "insert before a missing node",
        "source": "orchestrator"})
    _candidate(sh, "run-healthy-once")

    first = _sweep(sh, Spawn(), max_revives=1)
    marker = sh.patch_dir(broken) / "001.apply-error.json"
    assert marker.is_file()
    assert "ghost_node" in json.loads(marker.read_text(encoding="utf-8"))["error"]
    # The failed attempt gave its budget back, so the healthy run still applied.
    assert [a["run_id"] for a in _applies(first)] == ["run-healthy-once"]

    second = _sweep(sh, Spawn())
    assert not [a for a in second if a.get("run_id") == broken], second
