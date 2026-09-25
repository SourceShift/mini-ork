"""A rehearsal sharing a run dir must not occupy the live run's own record.

Regression origin: `.mini-ork/runs/child-1790341005-57997`. A live code-fix
lifecycle (8 nodes, 3 failed, reviewer `needs_revision`) shared its run dir
with a DRY_RUN lifecycle of a *different* recipe (docs, 5 nodes) that had
inherited `MINI_ORK_RUN_ID`. Two first-writer-wins guards let the rehearsal
win: `execute.log` was written only `if not isfile`, and `verdict.json` — once
it carried `source == "execute@run-level"` from the rehearsal — made the live
emit return early. The run dir then recorded a rehearsal of the docs recipe as
if it were the live run's outcome.

`plan.json` was the same defect one stage earlier and is covered at the bottom
of this file: the rehearsal's `_DRY_RUN_PLACEHOLDER` landed on the live run's
own plan, so the implementer was handed `objective: '<dry-run: not generated>'`
with an empty decomposition.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from mini_ork.cli import execute
from mini_ork.cli import main as cli_main
from mini_ork.cli import plan

_REPO = Path(__file__).resolve().parents[2]


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_dry_run_verdict_lands_beside_not_on_verdict_json(tmp_path, capsys):
    execute._emit_run_verdict(str(tmp_path), 0, 5, dry_run=True)
    capsys.readouterr()

    assert (tmp_path / "verdict.dryrun.json").is_file()
    assert not (tmp_path / "verdict.json").exists()
    assert not (tmp_path / "run-verdict.json").exists()
    assert _read(tmp_path / "verdict.dryrun.json")["dispatched"] == 5


def test_live_verdict_survives_a_prior_rehearsal(tmp_path, capsys):
    """The exact child-1790341005-57997 corruption: rehearsal first, live second."""
    execute._emit_run_verdict(str(tmp_path), 0, 5, dry_run=True)
    execute._emit_run_verdict(str(tmp_path), 3, 8)
    capsys.readouterr()

    live = _read(tmp_path / "verdict.json")
    assert live["dispatched"] == 8
    assert live["failed_nodes"] == 3
    assert live["verdict"] == "fail"
    assert _read(tmp_path / "verdict.dryrun.json")["dispatched"] == 5


def test_live_verdict_is_idempotent(tmp_path, capsys):
    execute._emit_run_verdict(str(tmp_path), 0, 8)
    first = (tmp_path / "verdict.json").read_text(encoding="utf-8")
    execute._emit_run_verdict(str(tmp_path), 3, 99)
    capsys.readouterr()

    assert (tmp_path / "verdict.json").read_text(encoding="utf-8") == first


def test_live_verdict_still_defers_to_a_recipe_owned_verdict(tmp_path, capsys):
    (tmp_path / "verdict.json").write_text(
        json.dumps({"verdict": "pass", "detail": "recipe-owned"}), encoding="utf-8")
    execute._emit_run_verdict(str(tmp_path), 0, 8)
    capsys.readouterr()

    assert _read(tmp_path / "verdict.json")["detail"] == "recipe-owned"
    assert _read(tmp_path / "run-verdict.json")["source"] == "execute@run-level"


def test_execute_log_name_follows_the_dry_run_env(monkeypatch):
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "1")
    assert cli_main._execute_log_name() == "execute.dryrun.log"
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "0")
    assert cli_main._execute_log_name() == "execute.log"
    monkeypatch.delenv("MINI_ORK_DRY_RUN", raising=False)
    assert cli_main._execute_log_name() == "execute.log"


def test_rehearsal_and_live_logs_are_distinct_files(tmp_path, monkeypatch):
    """The two lifecycles that shared the child run dir must not share a log."""
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "1")
    rehearsal = os.path.join(str(tmp_path), cli_main._execute_log_name())
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "0")
    live = os.path.join(str(tmp_path), cli_main._execute_log_name())

    assert rehearsal != live
    assert rehearsal.endswith("execute.dryrun.log")
    assert live.endswith("execute.log")


# ── plan.json — the same defect one stage earlier ────────────────────────────

_RUN_ID = "run-shadow-1"
_VALID_PLAN = {
    "objective": "Ship the widget",
    "assumptions": ["a"],
    "decomposition": [{"id": "s1", "description": "do it",
                       "node_type": "implementer", "depends_on": []}],
    "dependencies": [], "risk_notes": [],
    "artifact_contract": {"outputs": ["x"], "success_verifiers": ["v"]},
    "verifier_contract": {"checks": [{"id": "c1", "description": "check it"}]},
}


def test_plan_file_name_follows_the_dry_run_flag():
    assert plan._plan_file_name(True) == "plan.dryrun.json"
    assert plan._plan_file_name(False) == "plan.json"


def _plan_env(home, db, dry_run, given=""):
    env = {
        "MINI_ORK_ROOT": str(_REPO),
        "MINI_ORK_HOME": home,
        "MINI_ORK_DB": db,
        "MINI_ORK_RUN_ID": _RUN_ID,
        "MINI_ORK_TASK_CLASS": "code_fix",
        "MINI_ORK_DRY_RUN": dry_run,
        "MO_INJECT_LEARNINGS": "0",
        "MINI_ORK_PROFILE_GATE": "0",
        "MINI_ORK_PROFILE_PATH": "",
        "MINI_ORK_NONINTERACTIVE": "1",
        "MO_AUTO_ANSWER_PROFILE": "0",
        "PATH": os.environ.get("PATH", ""),
    }
    if given:
        env["MO_GIVEN_PLAN"] = given
    return env


def _plan_home(tmp_path, name):
    home = tmp_path / name / ".mini-ork"
    home.mkdir(parents=True, exist_ok=True)
    db = str(home / "state.db")
    subprocess.run(["bash", str(_REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    return str(home), db


def _run_plan(tmp_path, name, dry_run, given="", capsys=None):
    """Run the plan CLI with NO --out, so the default run-dir path is chosen."""
    home, db = _plan_home(tmp_path, name)
    kickoff = tmp_path / f"{name}.md"
    kickoff.write_text("# Do the thing\n\n## Success\n- works\n")
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(_plan_env(home, db, dry_run, given))
    try:
        rc = plan.main([str(kickoff)], root=str(_REPO))
    finally:
        os.environ.clear()
        os.environ.update(saved)
    if capsys is not None:
        capsys.readouterr()
    assert rc == 0, f"plan.main returned {rc}"
    return Path(home) / "runs" / _RUN_ID


def test_rehearsal_plan_lands_beside_not_on_plan_json(tmp_path, capsys):
    run_dir = _run_plan(tmp_path, "solo", "1", capsys=capsys)

    assert (run_dir / "plan.dryrun.json").is_file()
    assert not (run_dir / "plan.json").exists()
    assert _read(run_dir / "plan.dryrun.json")["objective"] == "<dry-run: not generated>"


def test_live_plan_survives_a_prior_rehearsal(tmp_path, capsys):
    """The child-1790350588-96925 corruption: rehearsal first, live second."""
    given = tmp_path / "given.json"
    given.write_text(json.dumps(_VALID_PLAN), encoding="utf-8")

    _run_plan(tmp_path, "both", "1", capsys=capsys)
    run_dir = _run_plan(tmp_path, "both", "0", given=str(given), capsys=capsys)

    live = _read(run_dir / "plan.json")
    assert live["objective"] == "Ship the widget"
    assert live["decomposition"], "live plan must carry a real decomposition"
    assert _read(run_dir / "plan.dryrun.json")["objective"] == "<dry-run: not generated>"


def test_explicit_out_is_honoured_verbatim(tmp_path, capsys):
    """A caller that names the file keeps it — the rehearsal/live split is theirs."""
    home, db = _plan_home(tmp_path, "explicit")
    kickoff = tmp_path / "explicit.md"
    kickoff.write_text("# Do the thing\n", encoding="utf-8")
    out = tmp_path / "named-by-caller.json"

    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(_plan_env(home, db, "1"))
    try:
        rc = plan.main([str(kickoff), "--out", str(out)], root=str(_REPO))
    finally:
        os.environ.clear()
        os.environ.update(saved)
    capsys.readouterr()

    assert rc == 0
    assert out.is_file()
    assert not (tmp_path / "explicit" / ".mini-ork" / "runs" / _RUN_ID /
                "plan.dryrun.json").exists()


# ── the engine a module child imports ────────────────────────────────
#
# Same family as above, one layer down. Naming the engine in ``PYTHONPATH`` is
# not enough for a ``python -m`` child: the interpreter puts the WORKING
# DIRECTORY at ``sys.path[0]``, ahead of ``PYTHONPATH``. The goal-loop sets a
# child's cwd to the repo under repair, so any such repo that contains a
# ``mini_ork/`` tree shadowed the engine — the fix landed on disk and the
# running child still executed the old code.


def _decoy_tree(tmp_path):
    """A cwd that would win the import race if nothing stops it."""
    root = tmp_path / "decoy"
    (root / "mini_ork" / "cli").mkdir(parents=True)
    (root / "mini_ork" / "__init__.py").write_text("DECOY = True\n", encoding="utf-8")
    (root / "mini_ork" / "cli" / "__init__.py").write_text("", encoding="utf-8")
    (root / "mini_ork" / "cli" / "plan.py").write_text(
        "print('DECOY-PLAN-LOADED')\nraise SystemExit(0)\n", encoding="utf-8")
    return root


def test_engine_module_child_ignores_a_decoy_package_in_cwd(tmp_path):
    """``python -m mini_ork.cli.plan`` from a repo that shadows the engine must
    still import the engine the env names — otherwise a fix on disk is inert."""
    decoy = _decoy_tree(tmp_path)
    env = cli_main._module_env(str(_REPO))

    def spawn(child_env):
        return subprocess.run(
            [sys.executable, "-m", "mini_ork.cli.plan"],
            cwd=str(decoy), capture_output=True, text=True, env=child_env,
        )

    unguarded = dict(env)
    unguarded.pop("PYTHONSAFEPATH", None)
    assert "DECOY-PLAN-LOADED" in spawn(unguarded).stdout, (
        "decoy did not win, so this test cannot detect the shadowing it guards")

    assert "DECOY-PLAN-LOADED" not in spawn(env).stdout, (
        "the engine env let the cwd's mini_ork tree shadow the engine")


def test_module_env_pins_the_engine_everywhere_it_is_built():
    """One policy, every ``-m mini_ork.*`` child: whatever calls it gets it."""
    assert cli_main._module_env(str(_REPO))["PYTHONSAFEPATH"] == "1"
    plan_block = (Path(cli_main.__file__).read_text(encoding="utf-8")
                  .split("# ── plan ──", 1)[1].split("# ── execute ──", 1)[0])
    assert "_module_env(root)" in plan_block, (
        "the plan child is spawned with a hand-built env, so it can drift from "
        "the engine-pinning policy every other module child gets")
