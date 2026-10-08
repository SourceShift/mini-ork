"""``mini_ork.recovery.auto_repair`` — the bounded self-repair loop.

Fixture pattern mirrors ``test_ide_pages_outcome.py``: a temp mini-ork home with
an initialised DB and a seeded ``task_runs`` row. ``retry_hint.load_or_compute``
is stubbed (the hint is an *input* to the policy, not its subject) and
``subprocess.Popen`` is stubbed so no ``recover`` is ever really spawned.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mini_ork.recovery import auto_repair as ar
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))

RUN = "run-1791000000-abc123"
T0 = 1_791_000_000

# A framework-edit-shaped workflow (the implementer is ``codex_lens``).
_NODES = [
    {"name": "planner", "type": "planner"},
    {"name": "implementer", "type": "implementer", "model_lane": "codex_lens"},
    {"name": "static_check_verifier", "type": "verifier",
     "verifier_ref": "verifiers/static-check.py"},
    {"name": "test_verifier", "type": "verifier", "verifier_ref": "verifiers/test.py"},
    {"name": "reviewer", "type": "reviewer"},
]
_EDGES = [
    {"from": "planner", "to": "implementer", "edge_type": "depends_on"},
    {"from": "implementer", "to": "static_check_verifier", "edge_type": "verifies"},
    {"from": "implementer", "to": "test_verifier", "edge_type": "verifies"},
    {"from": "static_check_verifier", "to": "reviewer", "edge_type": "depends_on"},
    {"from": "test_verifier", "to": "reviewer", "edge_type": "depends_on"},
]


@pytest.fixture(autouse=True)
def _stub_workflow(monkeypatch):
    import mini_ork.recovery.retry_hint as rh
    monkeypatch.setattr(rh, "_recipe_workflow", lambda home, recipe: (list(_NODES), list(_EDGES)))
    # tests/conftest.py turns auto-repair off suite-wide; these tests exercise
    # the loop itself, with spawning stubbed or asserted per test.
    monkeypatch.delenv("MO_AUTO_REPAIR", raising=False)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed(home: Path, *, status: str = "failed", recipe: str = "framework-edit",
          run_id: str = RUN, cost: float = 0.5, created_at: int = T0) -> Path:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_profile.json").write_text(json.dumps({"recipe": recipe}))
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT OR REPLACE INTO task_runs (id, recipe, status, cost_usd, created_at, "
        "updated_at, ended_at, task_class, kickoff_path, workflow_version, trace_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, recipe, status, cost, created_at, created_at + 120, created_at + 100,
         "framework_edit", "", "latest", "tr-1"),
    )
    con.commit()
    con.close()
    return run_dir


def _hint(**nc) -> dict:
    base = {
        "version": 2, "run_id": RUN, "failed_node": None, "retryable": True,
        "strategy": "none", "from_node": None, "needs_change": None,
        "notes": [], "command": "", "computed_at": "2026-01-01T00:00:00Z",
    }
    if nc:
        base["needs_change"] = nc
    return base


def _stub_hint(monkeypatch, hint: dict) -> None:
    import mini_ork.recovery.retry_hint as rh
    monkeypatch.setattr(rh, "load_or_compute", lambda *a, **k: hint)


def _seed_attempts(run_dir: Path, attempts: list[dict], **extra) -> None:
    data = {"attempts": attempts}
    data.update(extra)
    (run_dir / "repair.json").write_text(json.dumps(data))


class _FakePopen:
    """A ``subprocess.Popen`` stand-in that also satisfies ``subprocess.run``.

    ``subprocess.run`` calls ``Popen`` internally, so the stub must support the
    context-manager + ``communicate``/``poll`` protocol — otherwise patching
    ``Popen`` globally would break every ``run`` (e.g. ``retry_notify``'s
    ``git config`` owner probe).
    """

    def __init__(self, argv, **kw):
        self.argv = list(argv)
        self.args = self.argv  # subprocess.run reads ``process.args``
        self.kw = kw
        self.pid = 4242
        self.returncode = 0
        self.stdout = None
        self.stderr = None
        _CALLS.append((self.argv, dict(kw.get("env") or {})))

    def communicate(self, input=None, timeout=None):
        return b"", b""

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_CALLS: list[tuple[list[str], dict]] = []


@pytest.fixture
def spawned(monkeypatch):
    _CALLS.clear()
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    return _CALLS


def _recover_spawns(calls):
    return [c for c in calls if "mini_ork.recovery.planner" in c[0]]


# ── lane ─────────────────────────────────────────────────────────────────────


def test_lane_hint_switches_lane_and_spawns_recover(home, monkeypatch, spawned):
    _seed(home)
    hint = _hint(
        kind="lane", summary="minimax out of quota", detail="", alias="codex_lens",
        suggestions=[{"lane": "deepseek", "reason": "19 ok"}],
    )
    hint["failed_node"] = "codex_lens"
    hint["from_node"] = "codex_lens"
    _stub_hint(monkeypatch, hint)

    decision = ar.decide(home, RUN)
    assert decision["action"] == "lane"
    assert decision["lanes"] == {"codex_lens": "deepseek"}
    assert decision["from_node"] == "codex_lens"

    ar.apply(home, RUN, decision)
    argv = _recover_spawns(spawned)[0][0]
    assert argv[1:3] == ["-m", "mini_ork.recovery.planner"]
    assert "--lane" in argv and "codex_lens=deepseek" in argv
    assert "--from-node" in argv and "codex_lens" in argv


# ── reviewer revise ──────────────────────────────────────────────────────────


def test_reviewer_needs_revision_revises_from_implementer(home, monkeypatch, spawned):
    run_dir = _seed(home)
    (run_dir / "review-reviewer.json").write_text(json.dumps({
        "verdict": "needs_revision",
        "findings": [{"file": "mini_ork/x.py", "line": 3, "severity": "high",
                      "snippet": "bad()", "issue": "calls bad()"}],
        "reasons": ["the change is wrong"],
    }))
    _stub_hint(monkeypatch, _hint(kind="code", summary="needs a revision", detail=""))

    decision = ar.decide(home, RUN)
    assert decision["action"] == "revise"
    assert decision["from_node"] == "implementer"
    assert decision["ack_change"] is True

    ar.apply(home, RUN, decision)
    argv = _recover_spawns(spawned)[0][0]
    assert argv[2:4] == ["mini_ork.recovery.planner", RUN]

    current = json.loads((run_dir / "revise" / "current.json").read_text())
    feedback_path = Path(current["feedback"])
    assert feedback_path.is_file()
    assert current["round"] == 1
    text = feedback_path.read_text()
    assert "calls bad()" in text
    assert "mini_ork/x.py:3" in text
    assert "--ack-change" in argv
    # The hint written for a *human* marks a code-shaped failure
    # ``retryable: false``; without --force the spawned recover refuses with
    # rc=1 and never dispatches, so the main repair case could never resume.
    assert "--force" in argv


def test_second_revise_escalates_the_implementer_lane(home, monkeypatch, spawned):
    run_dir = _seed(home)
    (run_dir / "review-reviewer.json").write_text(json.dumps({
        "verdict": "needs_revision",
        "findings": [{"file": "a.py", "line": 1, "severity": "high",
                      "snippet": "s", "issue": "still wrong"}],
    }))
    _seed_attempts(run_dir, [{
        "n": 1, "ts": "t", "action": "revise", "from_node": "implementer",
        "lanes": {}, "reason": "first round", "signature": "deadbeef0000",
    }])
    _stub_hint(monkeypatch, _hint(kind="code", summary="x", detail=""))
    monkeypatch.setattr(ar, "_role_lane", lambda home, run_id, node: "codex_lens")

    decision = ar.decide(home, RUN)
    assert decision["action"] == "revise"
    assert decision["lanes"] == {"codex_lens": "opus"}


# ── run_events ───────────────────────────────────────────────────────────────


def _auto_repair_rows(home: Path) -> list[tuple[str, str, str]]:
    con = sqlite3.connect(home / "state.db")
    try:
        return con.execute(
            "SELECT event_id, run_id, payload_json FROM run_events "
            "WHERE event_type = 'auto_repair'").fetchall()
    finally:
        con.close()


def test_apply_emits_an_auto_repair_run_event(home, monkeypatch, spawned):
    """Every apply records one ``auto_repair`` row.

    ``web.db.StateDB`` opens ``PRAGMA query_only = ON``, so writing through it
    fails with "attempt to write a readonly database" — silently, because the
    emit helper swallows ``sqlite3.Error``. The row must go through a plain
    read-write connection, as ``cli/plan.py`` does for ``asks_blocked``.
    """
    _seed(home)
    _stub_hint(monkeypatch, _hint(kind="code", summary="x", detail=""))

    decision = ar.decide(home, RUN)
    assert decision["action"] == "revise"
    ar.apply(home, RUN, decision)

    rows = _auto_repair_rows(home)
    assert len(rows) == 1
    assert rows[0][1] == RUN
    payload = json.loads(rows[0][2])
    assert payload["action"] == "revise" and payload["n"] == 1
    assert RUN in rows[0][0]  # event_id embeds the run id (UNIQUE column)


def test_run_event_ids_do_not_collide_across_runs(home, monkeypatch, spawned):
    """A sweep repairing two runs in the same second must not collide on the PK."""
    _seed(home, run_id=RUN)
    _seed(home, run_id="run-second")
    _stub_hint(monkeypatch, _hint(kind="code", summary="x", detail=""))

    for run_id in (RUN, "run-second"):
        ar.apply(home, run_id, ar.decide(home, run_id))

    rows = _auto_repair_rows(home)
    assert {r[1] for r in rows} == {RUN, "run-second"}
    assert len({r[0] for r in rows}) == 2  # two PKs, no collision


def test_human_handoff_state_not_claimed_before_an_attempt(home, monkeypatch, spawned):
    """``gave_up`` claims the loop tried; a first-failure hand-off has not.

    The kickoff's stop rules are "after at least one attempt OR the stop rules
    fired" — an environment/owner-only first failure hands the run over without
    the attempt claim, so it must not be labelled a give-up.
    """
    run_dir = _seed(home)
    _stub_hint(monkeypatch, _hint(kind="environment", summary="unreachable", detail=""))

    decision = ar.decide(home, RUN)
    assert decision["action"] == "human" and decision["stop"] is False
    ar.apply(home, RUN, decision)
    assert "state" not in json.loads((run_dir / "repair.json").read_text())


def test_a_stop_rule_is_labelled_gave_up(home, monkeypatch):
    run_dir = _seed(home)
    _stub_hint(monkeypatch, _hint(kind="code", summary="x", detail=""))
    _seed_attempts(run_dir, [
        {"n": 1, "ts": "t", "action": "revise", "from_node": "implementer",
         "lanes": {}, "reason": "r", "signature": "a"},
        {"n": 2, "ts": "t", "action": "revise", "from_node": "implementer",
         "lanes": {}, "reason": "r", "signature": "b"},
    ])

    decision = ar.decide(home, RUN)
    assert decision["action"] == "human" and decision["stop"] is True
    ar.apply(home, RUN, decision)
    assert json.loads((run_dir / "repair.json").read_text())["state"] == "gave_up"


# ── spawn env / hand-off ─────────────────────────────────────────────────────


def test_lifecycle_spawn_pins_the_child_env(home, monkeypatch, spawned):
    """The spawned recover must land on the SAME run, home and owner.

    ``cwd`` is ``MINI_ORK_ROOT`` (not the mini-ork home), so an inherited
    ``MINI_ORK_HOME`` — or its absence — would strand the child on the wrong
    state.db; an unset ``MO_AUTO_REPAIR`` reads as off for execute's triage
    gate, giving one failure two owners; and a lifecycle-owned spawn must wait
    for this process to exit before it dispatches.
    """
    _seed(home)
    _stub_hint(monkeypatch, _hint(kind="code", summary="x", detail=""))

    ar.apply(home, RUN, ar.decide(home, RUN), wait_for_exit=True)
    _argv, env = _recover_spawns(spawned)[0]
    assert env["MINI_ORK_HOME"] == str(home)
    assert env["MO_AUTO_REPAIR"] == "1"
    assert env["MO_AUTO_REPAIR_RUN_ID"] == RUN
    assert env["MO_AUTO_REPAIR_ATTEMPT"] == "1"
    assert env["MO_AUTO_REPAIR_WAIT_PID"] == str(os.getpid())


def test_a_non_lifecycle_spawn_is_not_told_to_wait(home, monkeypatch, spawned):
    _seed(home)
    _stub_hint(monkeypatch, _hint(kind="code", summary="x", detail=""))
    ar.apply(home, RUN, ar.decide(home, RUN))  # sweep / board: no run record held
    _argv, env = _recover_spawns(spawned)[0]
    assert "MO_AUTO_REPAIR_WAIT_PID" not in env


def test_wait_for_spawner_is_a_noop_when_unmarked(monkeypatch):
    monkeypatch.delenv("MO_AUTO_REPAIR_WAIT_PID", raising=False)
    started = time.monotonic()
    ar.wait_for_spawner()
    assert time.monotonic() - started < 1.0


def test_wait_for_spawner_ignores_a_pid_that_is_not_its_parent(monkeypatch):
    monkeypatch.setenv("MO_AUTO_REPAIR_WAIT_PID", str(os.getpid() + 1))
    started = time.monotonic()
    ar.wait_for_spawner()
    assert time.monotonic() - started < 1.0


def test_wait_for_spawner_blocks_until_the_spawner_exits(tmp_path):
    """The spawned recover waits for the lifecycle that still owns the run.

    A child that dispatched immediately would set ``status='executing'`` and be
    flipped to ``failed`` by the spawner's teardown (``_close_run_record`` runs
    after reflect). Spawner exits ~0.5 s in; the child wakes on the reparent,
    well before the wait bound, and never early.
    """
    child = tmp_path / "child.py"
    child.write_text(
        "import sys, time\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from mini_ork.recovery import auto_repair\n"
        "t0 = time.monotonic()\n"
        "auto_repair.wait_for_spawner()\n"
        "open(sys.argv[2], 'w').write('%.3f' % (time.monotonic() - t0))\n",
        encoding="utf-8")
    spawner = tmp_path / "spawner.py"
    spawner.write_text(
        "import os, subprocess, sys, time\n"
        "env = dict(os.environ)\n"
        "env['MO_AUTO_REPAIR_WAIT_PID'] = str(os.getpid())\n"
        "env['MO_AUTO_REPAIR_WAIT_S'] = '10'\n"  # bound: a broken wait fails, not hangs
        # argv: <child> <out> <repo> — the child wants (repo, out) as 1,2.
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[3], sys.argv[2]],\n"
        "                 cwd=sys.argv[3], env=env, start_new_session=True)\n"
        "time.sleep(0.5)\n",
        encoding="utf-8")
    out = tmp_path / "woke.txt"

    subprocess.run([sys.executable, str(spawner), str(child), str(out), str(REPO)],
                   capture_output=True, timeout=60)

    deadline = time.monotonic() + 30
    while not out.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert out.exists(), "the spawned child never woke"
    waited = float(out.read_text())
    assert 0.3 <= waited < 5, f"child waited {waited:.2f}s (expected ~0.5s)"


# ── withheld ─────────────────────────────────────────────────────────────────


def _seed_withheld(run_dir: Path) -> None:
    (run_dir / "verdict.json").write_text(json.dumps({
        "levels_decision": "abstain",
        "levels": {"target": "UNVERIFIED"},
        "levels_reasons": {"target": "no test exercises the change"},
    }))


def test_withheld_reverifies_then_proves(home, monkeypatch, spawned):
    run_dir = _seed(home)
    _seed_withheld(run_dir)
    _stub_hint(monkeypatch, _hint())  # no needs_change; strategy irrelevant

    first = ar.decide(home, RUN)
    assert first["action"] == "reverify"
    assert first["from_node"] == "test_verifier"

    # A second, identical attempt escalates to prove.
    _seed_attempts(run_dir, [{
        "n": 1, "ts": "t", "action": "reverify", "from_node": "test_verifier",
        "lanes": {}, "reason": "withheld", "signature": first["signature"],
    }])
    second = ar.decide(home, RUN)
    assert second["action"] == "prove"
    assert second["from_node"] == "implementer"
    assert "target" in second["feedback"]
    assert "no test exercises the change" in second["feedback"]


def test_withheld_run_published_anyway_still_reverifies(home, monkeypatch):
    """A withheld run whose status reads ``published`` is still repairable.

    ``publisher.py`` forces ``status='failed'`` on abstain; a later status
    rewrite (a manual republish that bypassed the level gate) must not blind the
    loop to it — rule 1 admits it, rule 5 owns it.
    """
    run_dir = _seed(home, status="published")
    _seed_withheld(run_dir)
    _stub_hint(monkeypatch, _hint())

    decision = ar.decide(home, RUN)
    assert decision["action"] == "reverify"
    assert decision["from_node"] == "test_verifier"


# ── human give-up ────────────────────────────────────────────────────────────


def test_environment_kind_is_human_and_arms_one_gate(home, monkeypatch, spawned):
    run_dir = _seed(home)
    _stub_hint(monkeypatch, _hint(kind="environment", summary="unreachable", detail=""))
    from mini_ork.recovery import retry_notify
    calls = {"n": 0}
    real = retry_notify.notify

    def _counting(h, r):
        calls["n"] += 1
        return real(h, r)

    monkeypatch.setattr(retry_notify, "notify", _counting)

    decision = ar.decide(home, RUN)
    assert decision["action"] == "human"
    res = ar.apply(home, RUN, decision)
    assert res["gate"] is True
    assert calls["n"] == 1
    assert not _recover_spawns(spawned)  # no recover spawned

    # A second give-up must not duplicate the gate.
    ar.apply(home, RUN, decision)
    assert calls["n"] == 1
    assert retry_notify.pending_fix_for_run(home, run_dir) is not None


def test_stop_rules_yield_human(home, monkeypatch):
    run_dir = _seed(home)
    _stub_hint(monkeypatch, _hint(kind="code", summary="x", detail=""))

    # No progress: the same signature repeats.
    sig = ar._signature(None, "", "")
    _seed_attempts(run_dir, [{"n": 1, "ts": "t", "action": "revise",
                              "from_node": "implementer", "lanes": {},
                              "reason": "r", "signature": sig}])
    assert ar.decide(home, RUN)["action"] == "human"
    assert "no progress" in ar.decide(home, RUN)["reason"]

    # Max attempts.
    _seed_attempts(run_dir, [
        {"n": 1, "ts": "t", "action": "revise", "from_node": "implementer",
         "lanes": {}, "reason": "r", "signature": "a"},
        {"n": 2, "ts": "t", "action": "revise", "from_node": "implementer",
         "lanes": {}, "reason": "r", "signature": "b"},
    ])
    assert ar.decide(home, RUN)["action"] == "human"
    assert "exhausted" in ar.decide(home, RUN)["reason"]


def test_budget_exceeded_yields_human(home, monkeypatch):
    run_dir = _seed(home, cost=25.0)
    _seed_attempts(run_dir, [{"n": 1, "ts": "t", "action": "revise",
                              "from_node": "implementer", "lanes": {},
                              "reason": "r", "signature": "z"}])
    _stub_hint(monkeypatch, _hint(kind="code", summary="x", detail=""))
    decision = ar.decide(home, RUN)
    assert decision["action"] == "human"
    assert "budget" in decision["reason"]


def test_auto_repair_off_returns_none(home, monkeypatch):
    _seed(home)
    monkeypatch.setenv("MO_AUTO_REPAIR", "0")
    _stub_hint(monkeypatch, _hint(kind="code", summary="x", detail=""))
    assert ar.decide(home, RUN)["action"] == "none"
    assert ar.maybe_repair(home, RUN) is None


# ── triage on give-up ────────────────────────────────────────────────────────


def test_give_up_triages_once(home, monkeypatch):
    _seed(home)
    _stub_hint(monkeypatch, _hint(kind="environment", summary="x", detail=""))
    monkeypatch.setenv("MO_FAILURE_TRIAGE", "1")
    import mini_ork.triage.failures as failures
    calls = {"n": 0}
    monkeypatch.setattr(failures, "triage_run",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))

    decision = ar.decide(home, RUN)
    assert decision["action"] == "human"
    ar.apply(home, RUN, decision)
    assert calls["n"] == 1
    # A second give-up decision must not call triage again.
    ar.apply(home, RUN, ar.decide(home, RUN))
    assert calls["n"] == 1


# ── sweep ────────────────────────────────────────────────────────────────────


def _snapshot(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_mtime_ns for p in root.rglob("*")}


def test_sweep_dry_run_skips_experiments_and_superseded(home, monkeypatch, capsys):
    now = int(time.time())
    _seed(home, run_id=RUN, created_at=now)
    _seed(home, run_id="vt5-weak-probe", created_at=now)
    run_dir = _seed(home, run_id="run-superseded", created_at=now)
    (run_dir / "retry-gate.json").write_text(json.dumps(
        {"inbox_id": 1, "superseded": "merged", "abandoned": True}))
    _seed(home, run_id="run-published", status="published", created_at=now)
    _stub_hint(monkeypatch, _hint(kind="code", summary="x", detail=""))

    from mini_ork.cli import repair_cmd
    before = _snapshot(home / "runs")
    rc = repair_cmd.main([
        "--sweep", "--since-days", "2", "--dry-run", "--home", str(home)])
    out = capsys.readouterr().out
    assert rc == 0
    assert RUN in out
    assert "vt5-weak-probe" not in out
    assert "run-superseded" not in out
    assert "run-published" not in out
    assert _snapshot(home / "runs") == before  # nothing written


def test_created_at_epoch_keeps_the_strings_offset():
    """An ISO stamp's own offset is data — ``replace(tzinfo=utc)`` discards it.

    02:00+02:00 IS 00:00Z; forcing UTC would place it two hours later and pull
    an older run out of (or into) the sweep window.
    """
    from mini_ork.cli.repair_cmd import _created_at_epoch
    assert _created_at_epoch("2026-01-01T02:00:00+02:00") == 1767225600
    assert _created_at_epoch("2026-01-01T00:00:00Z") == 1767225600
    assert _created_at_epoch("2026-01-01T00:00:00") == 1767225600  # naive == UTC
    assert _created_at_epoch("1767225600") == 1767225600
    assert _created_at_epoch(None) == 0


def test_cached_catalog_is_idempotent_and_restores():
    """The swap is process-wide, so a nested scope must not stack wrappers."""
    from mini_ork import recipes_catalog
    from mini_ork.cli import repair_cmd

    original = recipes_catalog.list_recipes
    with repair_cmd._cached_catalog():
        memo = recipes_catalog.list_recipes
        assert memo is not original
        with repair_cmd._cached_catalog():
            assert recipes_catalog.list_recipes is memo
        assert recipes_catalog.list_recipes is memo
    assert recipes_catalog.list_recipes is original


def test_sweep_includes_published_but_withheld_runs(home, monkeypatch, capsys):
    """The sweep revives a withheld run even when its status reads ``published``.

    ``run-plain-published`` (no level report) stays out — only a withheld run
    (abstain) is a failed run a status rewrite hid.
    """
    now = int(time.time())
    run_dir = _seed(home, run_id="run-withheld-pub", status="published", created_at=now)
    _seed_withheld(run_dir)
    _seed(home, run_id="run-plain-published", status="published", created_at=now)
    _stub_hint(monkeypatch, _hint())

    from mini_ork.cli import repair_cmd
    rc = repair_cmd.main([
        "--sweep", "--since-days", "2", "--dry-run", "--home", str(home)])
    out = capsys.readouterr().out
    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.startswith("run-withheld-pub")]
    assert lines and "reverify" in lines[0]
    assert "run-plain-published" not in out


# ── hooks ────────────────────────────────────────────────────────────────────


def _planner_setup(tmp_path: Path, monkeypatch):
    from test_recover_lease_wiring import SCHEMA_SQL, _seed_success, _workflow
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_SQL)
    con.close()
    run_id, recipe = "run-cli-1", "framework-edit"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({"recipe": recipe}))
    workflow = tmp_path / "wf.yaml"
    _workflow(workflow)
    for node in ("A", "B", "C"):
        _seed_success(str(db), run_id, node, recipe, "framework_edit", str(run_dir))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_TASK_CLASS", "framework_edit")
    monkeypatch.setenv("MINI_ORK_RECIPE", recipe)
    for key in ("MINI_ORK_LEASE_TOKEN", "MINI_ORK_RECOVERY_REQUEST", "MINI_ORK_WORKFLOW",
                "MO_AUTO_REPAIR_ATTEMPT", "MO_AUTO_REPAIR_RUN_ID",
                "MO_AUTO_REPAIR_WAIT_PID"):
        monkeypatch.delenv(key, raising=False)
    return run_id, str(db), str(workflow)


_NONRETRYABLE_CODE_HINT = {
    "version": 2, "failed_node": "B", "retryable": False, "strategy": "none",
    "from_node": "B",
    "needs_change": {"kind": "code", "summary": "needs a revision", "detail": ""},
    "notes": [], "command": "", "computed_at": "2026-01-01T00:00:00Z",
}


def _stub_nonretryable_hint(monkeypatch, run_id: str) -> None:
    import mini_ork.recovery.retry_hint as rh
    monkeypatch.setattr(rh, "load_or_compute",
                        lambda *a, **k: {**_NONRETRYABLE_CODE_HINT, "run_id": run_id})


def test_planner_hook_calls_maybe_repair_once(tmp_path, monkeypatch):
    from mini_ork.recovery import planner as rp
    run_id, db, workflow = _planner_setup(tmp_path, monkeypatch)
    calls = {"n": 0}
    monkeypatch.setattr(ar, "maybe_repair",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
    rc = rp.cli_main([run_id, "--workflow", workflow, "--db", db],
                     execute_fn=lambda argv: 0)
    assert rc == 0
    assert calls["n"] == 1


def test_planner_hook_swallows_exceptions(tmp_path, monkeypatch):
    from mini_ork.recovery import planner as rp
    run_id, db, workflow = _planner_setup(tmp_path, monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ar, "maybe_repair", _boom)
    rc = rp.cli_main([run_id, "--workflow", workflow, "--db", db],
                     execute_fn=lambda argv: 3)
    assert rc == 3  # the hook's failure does not change the exit code


def test_main_hook_runs_after_retry_notify():
    src = Path(REPO / "mini_ork" / "cli" / "main.py").read_text(encoding="utf-8")
    notify_at = src.index("retry_notify.notify")
    repair_at = src.index("auto_repair.maybe_repair")
    assert notify_at < repair_at


def test_force_resumes_the_run_the_hint_refuses(tmp_path, monkeypatch):
    """HIGH 1: the main repair case (revise) could never resume.

    A code-shaped hint is ``retryable: false`` — the retry hint is written for
    a *human* deciding whether to retry — so a recover invoked without
    ``--force`` refuses with rc=1 and dispatches nothing. The loop's own spawn
    carries ``--force``, so the same argv resumes the run.
    """
    from mini_ork.recovery import planner as rp
    run_id, db, workflow = _planner_setup(tmp_path, monkeypatch)
    _stub_nonretryable_hint(monkeypatch, run_id)
    calls = {"n": 0}

    def _exec(argv):
        calls["n"] += 1
        return 0

    refused = rp.cli_main([run_id, "--from-node", "B", "--ack-change",
                           "--workflow", workflow, "--db", db], execute_fn=_exec)
    assert refused == 1 and calls["n"] == 0

    resumed = rp.cli_main([run_id, "--from-node", "B", "--ack-change", "--force",
                           "--workflow", workflow, "--db", db], execute_fn=_exec)
    assert calls["n"] == 1, "the forced recover never dispatched"
    assert resumed == 0


def test_a_refused_recover_hands_the_run_to_the_human(tmp_path, monkeypatch):
    """MEDIUM: a recover that bails before dispatching must not strand the run.

    The spawn wrote ``state='repairing'``; nothing is running now. Without the
    hand-off ``repair.json`` would stay ``repairing`` forever with no owner.
    """
    from mini_ork.recovery import planner as rp
    run_id, db, workflow = _planner_setup(tmp_path, monkeypatch)
    stuck_dir = tmp_path / "runs" / run_id  # ``_run_dir(MINI_ORK_HOME, run_id)``
    stuck_dir.mkdir(parents=True)
    (stuck_dir / "repair.json").write_text(json.dumps({
        "state": "repairing",
        "attempts": [{"n": 3, "ts": "t", "action": "revise", "from_node": "B",
                      "lanes": {}, "reason": "r", "signature": "s"}],
    }))
    monkeypatch.setenv("MO_AUTO_REPAIR_ATTEMPT", "3")
    monkeypatch.setenv("MO_AUTO_REPAIR_RUN_ID", run_id)
    _stub_nonretryable_hint(monkeypatch, run_id)

    called = {"n": 0}
    rc = rp.cli_main([run_id, "--from-node", "B", "--ack-change",
                      "--workflow", workflow, "--db", db],
                     execute_fn=lambda argv: called.__setitem__("n", called["n"] + 1))

    assert rc == 1 and called["n"] == 0  # refused before dispatching
    data = json.loads((stuck_dir / "repair.json").read_text())
    assert data["state"] == "gave_up"
    assert data["attempts"][-1]["action"] == "human"
    assert "could not resume" in data["attempts"][-1]["reason"]


def test_note_stuck_ignores_an_attempt_it_no_longer_owns(home, monkeypatch):
    """A concurrent repair that already moved on owns the run, not this process."""
    run_dir = _seed(home)
    _seed_attempts(run_dir, [{"n": 4, "ts": "t", "action": "revise",
                              "from_node": "B", "lanes": {}, "reason": "r",
                              "signature": "s"}], state="repairing")
    monkeypatch.setenv("MO_AUTO_REPAIR_ATTEMPT", "3")  # a superseded attempt
    monkeypatch.setenv("MO_AUTO_REPAIR_RUN_ID", RUN)

    assert ar.note_stuck(home, RUN) is False
    assert json.loads((run_dir / "repair.json").read_text())["state"] == "repairing"


def test_spawn_markers_do_not_leak_into_the_dispatched_execute(tmp_path, monkeypatch):
    """The repair markers belong to the spawned recover, not its execute subtree.

    A verifier that runs the recover tests (or any nested recover) inside the
    dispatched execute must not be able to act as this repair attempt.
    """
    from mini_ork.recovery import planner as rp
    run_id, db, workflow = _planner_setup(tmp_path, monkeypatch)
    _stub_nonretryable_hint(monkeypatch, run_id)
    monkeypatch.setenv("MO_AUTO_REPAIR_ATTEMPT", "1")
    monkeypatch.setenv("MO_AUTO_REPAIR_RUN_ID", run_id)
    seen: dict = {}

    def _exec(argv):
        seen["attempt"] = os.environ.get("MO_AUTO_REPAIR_ATTEMPT")
        seen["run_id"] = os.environ.get("MO_AUTO_REPAIR_RUN_ID")
        return 0

    rc = rp.cli_main([run_id, "--from-node", "B", "--ack-change", "--force",
                      "--workflow", workflow, "--db", db], execute_fn=_exec)
    assert rc == 0
    assert seen == {"attempt": None, "run_id": None}
