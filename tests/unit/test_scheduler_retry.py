"""Contracts for the scheduler's opt-in retry loop (RSI across many epics):
retry with a cap, least-attempted-first fairness, carry-over kickoffs,
required verifiers, pre/post hooks, per-epic recipe, roadmap settings."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import scheduler  # noqa: E402
from mini_ork.cli import epics  # noqa: E402


def _init_db(home: Path) -> str:
    home.mkdir(parents=True, exist_ok=True)
    db = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
        capture_output=True, text=True, check=True,
    )
    scheduler.ensure_priority_column(db)
    return db


def _seed(db: str, epic_id: str, created_at: str = "2026-01-01T00:00:00Z", priority: int = 0) -> None:
    con = sqlite3.connect(db)
    con.execute("INSERT INTO epics (id,title,status,priority,created_at) VALUES (?,?,?,?,?)",
                (epic_id, epic_id, "not started", priority, created_at))
    con.commit()
    con.close()


def _row(db: str, epic_id: str) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = dict(con.execute("SELECT * FROM epics WHERE id=?", (epic_id,)).fetchone())
    con.close()
    return row


def _attempts(db: str, epic_id: str) -> list[tuple]:
    con = sqlite3.connect(db)
    rows = con.execute("SELECT attempt, outcome, verdict, reason FROM epic_attempts "
                       "WHERE epic_id=? ORDER BY attempt", (epic_id,)).fetchall()
    con.close()
    return rows


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> dict:
    for var in ("MO_SCHED_MAX_ATTEMPTS", "MO_SCHED_REQUIRED_VERIFIERS", "MO_SCHED_CARRY_OVER",
                "MO_SCHED_PRE_DISPATCH_HOOK", "MO_SCHED_POST_VERDICT_HOOK"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "root"
    home = root / ".mini-ork"
    (home / "runs").mkdir(parents=True)
    db = _init_db(home)
    (root / "kickoffs").mkdir()
    (root / "recipes" / "epic-runner").mkdir(parents=True)
    for epic_id in ("A", "B"):
        _seed(db, epic_id, created_at=f"2026-01-01T00:00:0{'AB'.index(epic_id)}Z")
        (root / "kickoffs" / f"{epic_id}.md").write_text(f"# epic {epic_id}\n\nDo the thing.\n", encoding="utf-8")
    return {"root": str(root), "home": str(home), "db": db, "tmp": tmp_path}


def _runner(world: dict, script: list[dict]) -> list[str]:
    """A fake `mini-ork run`: each call pops the next scripted result, writes
    runs/<run_id>/{verdict,verifier_*}.json + reflection.md, echoes run_id=,
    and records the kickoff path it was handed (the LAST argv)."""
    queue = world["tmp"] / "script.json"
    queue.write_text(json.dumps(script), encoding="utf-8")
    seen = world["tmp"] / "kickoffs-seen.txt"
    stub = world["tmp"] / "fake-runner.py"
    stub.write_text(f"""#!/usr/bin/env python3
import json, os, sys, time
q = json.load(open({str(queue)!r}))
step = q.pop(0); json.dump(q, open({str(queue)!r}, "w"))
run_id = "run-" + str(time.time_ns())
d = os.path.join({world['home']!r}, "runs", run_id); os.makedirs(d)
json.dump({{"verdict": step["verdict"]}}, open(os.path.join(d, "verdict.json"), "w"))
for name, payload in step.get("verifiers", {{}}).items():
    json.dump(payload, open(os.path.join(d, "verifier_" + name + ".json"), "w"))
open(os.path.join(d, "reflection.md"), "w").write(step.get("reflection", "nothing learned"))
open({str(seen)!r}, "a").write(sys.argv[-1] + "\\n")
print("run_id=" + run_id)
""", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return [str(stub)]


def _kickoffs_seen(world: dict) -> list[str]:
    p = world["tmp"] / "kickoffs-seen.txt"
    return p.read_text().splitlines() if p.exists() else []


def _hook(world: dict, name: str, rc: int, stdout: str = "") -> str:
    path = world["tmp"] / name
    env_dump = world["tmp"] / f"{name}.env"
    path.write_text(f"#!/bin/bash\nenv | grep '^MO_' > {env_dump}\necho '{stdout}'\nexit {rc}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_default_is_one_shot_escalation(world):
    runner = _runner(world, [{"verdict": "fail"}])
    scheduler.dispatch_epic("A", world["root"], world["home"], "epic-runner", db=world["db"], runner_cmd=runner)
    row = _row(world["db"], "A")
    assert row["status"] == "escalated"          # historic behaviour preserved
    assert row["attempts"] == 1
    assert _attempts(world["db"], "A")[0][1] == "failed"


def test_retry_until_cap_then_escalate(world, monkeypatch):
    monkeypatch.setenv("MO_SCHED_MAX_ATTEMPTS", "3")
    runner = _runner(world, [{"verdict": "fail"}] * 3)
    for expected in ("not started", "not started", "escalated"):
        scheduler.dispatch_epic("A", world["root"], world["home"], "epic-runner", db=world["db"], runner_cmd=runner)
        assert _row(world["db"], "A")["status"] == expected
    assert [a[0] for a in _attempts(world["db"], "A")] == [1, 2, 3]


def test_success_on_retry_marks_done(world, monkeypatch):
    monkeypatch.setenv("MO_SCHED_MAX_ATTEMPTS", "3")
    runner = _runner(world, [{"verdict": "fail"}, {"verdict": "success"}])
    scheduler.dispatch_epic("A", world["root"], world["home"], "epic-runner", db=world["db"], runner_cmd=runner)
    scheduler.dispatch_epic("A", world["root"], world["home"], "epic-runner", db=world["db"], runner_cmd=runner)
    assert _row(world["db"], "A")["status"] == "done"


def test_least_attempted_first_within_a_priority_tier(world, monkeypatch):
    monkeypatch.setenv("MO_SCHED_MAX_ATTEMPTS", "5")
    runner = _runner(world, [{"verdict": "fail"}])
    # A is older, so it would always win on created_at alone; after one failed
    # attempt it must yield to the never-attempted B.
    assert scheduler.pick_ready(db=world["db"]) == ["A", "B"]
    scheduler.dispatch_epic("A", world["root"], world["home"], "epic-runner", db=world["db"], runner_cmd=runner)
    assert scheduler.pick_ready(db=world["db"]) == ["B", "A"]


def test_carry_over_kickoff_carries_the_failure(world, monkeypatch):
    monkeypatch.setenv("MO_SCHED_MAX_ATTEMPTS", "3")
    runner = _runner(world, [
        {"verdict": "fail", "reflection": "the smoke got a 400: unsupported_currency",
         "verifiers": {"live-smoke": {"pass": False, "reason": "status 400 not in [200]"}}},
        {"verdict": "success"},
    ])
    scheduler.dispatch_epic("A", world["root"], world["home"], "epic-runner", db=world["db"], runner_cmd=runner)
    scheduler.dispatch_epic("A", world["root"], world["home"], "epic-runner", db=world["db"], runner_cmd=runner)
    first, second = _kickoffs_seen(world)
    assert first.endswith("kickoffs/A.md")
    text = Path(second).read_text()
    assert "Do the thing." in text                       # original kickoff kept
    assert "## Previous attempts (this is attempt 2)" in text
    assert "`live-smoke`: status 400 not in [200]" in text
    assert "unsupported_currency" in text                 # reflection excerpt


def test_carry_over_can_be_disabled(world, monkeypatch):
    monkeypatch.setenv("MO_SCHED_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("MO_SCHED_CARRY_OVER", "0")
    runner = _runner(world, [{"verdict": "fail"}, {"verdict": "success"}])
    scheduler.dispatch_epic("A", world["root"], world["home"], "epic-runner", db=world["db"], runner_cmd=runner)
    scheduler.dispatch_epic("A", world["root"], world["home"], "epic-runner", db=world["db"], runner_cmd=runner)
    assert all(k.endswith("kickoffs/A.md") for k in _kickoffs_seen(world))


def test_required_verifier_failing_blocks_done(world, monkeypatch):
    monkeypatch.setenv("MO_SCHED_REQUIRED_VERIFIERS", "live-smoke,scope")
    runner = _runner(world, [{"verdict": "success", "verifiers": {
        "live-smoke": {"status": "REFUTED", "reason": "cmd rc=1"}, "scope": {"pass": True}}}])
    scheduler.dispatch_epic("A", world["root"], world["home"], "epic-runner", db=world["db"], runner_cmd=runner)
    assert _row(world["db"], "A")["status"] == "escalated"
    assert "required verifier live-smoke: cmd rc=1" in _attempts(world["db"], "A")[0][3]


def test_required_verifier_missing_counts_as_failing(world, monkeypatch):
    monkeypatch.setenv("MO_SCHED_REQUIRED_VERIFIERS", "live-smoke")
    runner = _runner(world, [{"verdict": "success"}])
    scheduler.dispatch_epic("A", world["root"], world["home"], "epic-runner", db=world["db"], runner_cmd=runner)
    assert _row(world["db"], "A")["status"] == "escalated"


def test_required_verifiers_passing_marks_done(world, monkeypatch):
    monkeypatch.setenv("MO_SCHED_REQUIRED_VERIFIERS", "live-smoke")
    runner = _runner(world, [{"verdict": "success", "verifiers": {"live-smoke": {"status": "PROVEN"}}}])
    scheduler.dispatch_epic("A", world["root"], world["home"], "epic-runner", db=world["db"], runner_cmd=runner)
    assert _row(world["db"], "A")["status"] == "done"


def test_pre_hook_defer_consumes_nothing_and_stops_the_pool(world, monkeypatch):
    monkeypatch.setenv("MO_SCHED_PRE_DISPATCH_HOOK", _hook(world, "pre.sh", 75, "BE down"))
    runner = _runner(world, [{"verdict": "success"}] * 2)
    stats: dict = {}
    scheduler.run_pool(world["root"], world["home"], "epic-runner", db=world["db"],
                       max_parallel=1, runner_cmd=runner, stats=stats)
    assert stats["deferred"] is True
    assert _row(world["db"], "A")["status"] == "not started"
    assert _row(world["db"], "A")["attempts"] == 0
    assert _kickoffs_seen(world) == []                   # the runner never ran


def test_main_exits_four_on_deferral(world, monkeypatch):
    monkeypatch.setenv("MO_SCHED_PRE_DISPATCH_HOOK", _hook(world, "pre.sh", 75))
    rc = scheduler.main(["--once"], db=world["db"], root=world["root"], home=world["home"],
                        runner_cmd=_runner(world, [{"verdict": "success"}]))
    assert rc == 4


def test_pre_hook_failure_is_a_failed_attempt(world, monkeypatch):
    monkeypatch.setenv("MO_SCHED_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("MO_SCHED_PRE_DISPATCH_HOOK", _hook(world, "pre.sh", 1, "rebase conflict"))
    scheduler.dispatch_epic("A", world["root"], world["home"], "epic-runner", db=world["db"],
                            runner_cmd=_runner(world, []))
    assert _row(world["db"], "A")["status"] == "not started"
    assert "rebase conflict" in _attempts(world["db"], "A")[0][3]


def test_post_hook_hold_parks_the_epic_until_retry(world, monkeypatch):
    monkeypatch.setenv("MO_SCHED_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("MO_SCHED_POST_VERDICT_HOOK", _hook(world, "post.sh", 10, "destructive migration held"))
    runner = _runner(world, [{"verdict": "success"}])
    scheduler.dispatch_epic("A", world["root"], world["home"], "epic-runner", db=world["db"], runner_cmd=runner)
    row = _row(world["db"], "A")
    assert (row["status"], row["held_reason"]) == ("blocked", "destructive migration held")
    assert "A" not in scheduler.pick_ready(db=world["db"])  # never auto-retried
    assert epics.main(["retry", "A"], db=world["db"]) == 0
    assert _row(world["db"], "A")["status"] == "not started"


def test_post_hook_sees_the_verdict_and_can_fail_a_success(world, monkeypatch):
    monkeypatch.setenv("MO_SCHED_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("MO_SCHED_POST_VERDICT_HOOK", _hook(world, "post.sh", 3, "push to main failed"))
    scheduler.dispatch_epic("A", world["root"], world["home"], "epic-runner", db=world["db"],
                            runner_cmd=_runner(world, [{"verdict": "success"}]))
    env = (world["tmp"] / "post.sh.env").read_text()
    assert "MO_VERDICT=success" in env and "MO_OUTCOME=done" in env and "MO_EPIC_ATTEMPT=1" in env
    assert _row(world["db"], "A")["status"] == "not started"
    assert "push to main failed" in _attempts(world["db"], "A")[0][3]


def test_per_epic_recipe_overrides_the_global_one(world, monkeypatch):
    monkeypatch.setenv("MO_SCHED_PRE_DISPATCH_HOOK", _hook(world, "pre.sh", 0))
    assert epics.main(["set", "A", "--recipe", "my-rsi", "--max-attempts", "4"], db=world["db"]) == 0
    scheduler.dispatch_epic("A", world["root"], world["home"], "epic-runner", db=world["db"],
                            runner_cmd=_runner(world, [{"verdict": "fail"}]))
    assert "MO_EPIC_RECIPE=my-rsi" in (world["tmp"] / "pre.sh.env").read_text()
    assert _row(world["db"], "A")["status"] == "not started"   # max_attempts 4 from the epic


def test_retry_refuses_active_epics(world):
    assert epics.main(["retry", "A"], db=world["db"]) == 1


def test_roadmap_settings_are_ingested(world, tmp_path):
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text("## Shop listing sync (id: shop-s1)\n- recipe: libwit-shop-rsi\n- max attempts: 3\n"
                       "\n## Checkout (id: shop-s8)\n- depends on: shop-s1\n", encoding="utf-8")
    assert epics.main(["ingest", str(roadmap)], db=world["db"]) == 0
    s1 = _row(world["db"], "shop-s1")
    assert (s1["recipe"], s1["max_attempts"]) == ("libwit-shop-rsi", 3)
    assert _row(world["db"], "shop-s8")["recipe"] is None
