"""`mini-ork recover` verify-only retry + carry-patch restore + hint gate.

This file pins the kickoff contract for ``recover --strategy verify``,
``recover --carry-patch``, the planner-as-reusable shortcut, and the
retry-hint gate. Each test stands up an isolated ``tmp_path`` home so the
ambient ``MINI_ORK_*`` family cannot leak into the planner (lens §4
"environment leakage is the sharpest hazard"). Test fixtures mirror the
``_setup`` pattern at ``tests/unit/test_recover_cli_dispatch.py:23``:
seed ``node_checkpoints`` with success rows, inject ``execute_fn`` so the
executor is never actually dispatched.

Recipe workflows are typed: the kickoff's verify strategy depends on a
``verifier``-typed node (kickoff §3) and the planner-as-reusable shortcut
depends on a ``planner``-typed node (kickoff §2). Tests declare the
right types up front.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from mini_ork.recovery import planner as rp
from mini_ork.recovery.restore import (
    plan_restore,
    restore_carry_patch,
)
from test_recover_lease_wiring import SCHEMA_SQL, _seed_success


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _set_run_dir(monkeypatch, run_dir: str) -> None:
    """Pin MINI_ORK_RUN_DIR so the planner finds the right run directory
    in tests where ``<home>/runs/<id>`` is NOT the layout."""
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch, tmp_path):
    """Clear the ambient MINI_ORK_* family so the planner cannot leak
    into a parent run dir. Mirrors the env-leak guard from
    ``tests/unit/test_recover_cli_dispatch.py:42-43``."""
    for key in (
        "MINI_ORK_RUN_DIR",
        "MINI_ORK_RECIPE",
        "MINI_ORK_WORKFLOW",
        "MINI_ORK_TASK_CLASS",
        "MINI_ORK_RUN_ID",
        "MINI_ORK_LEASE_TOKEN",
        "MINI_ORK_RECOVERY_REQUEST",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))


def _write_workflow(path: Path, *, nodes: list[dict], edges: list[dict],
                    recovery: dict | None = None) -> None:
    """Write a workflow.yaml with the given nodes/edges/recovery key.

    The recovery top-level key carries the optional ``carry_patch``
    override (kickoff §4). When omitted, the restore defaults to
    ``salvage.patch``.
    """
    body = {
        "version": "0.1.0",
        "task_class": "framework_edit",
        "nodes": nodes,
        "edges": edges,
    }
    if recovery is not None:
        body["recovery"] = recovery
    import yaml

    path.write_text(yaml.safe_dump(body, sort_keys=False))


def _init_git_repo(path: Path) -> Path:
    """Init a git repo at ``path`` with a single empty baseline commit.

    Required for ``git apply --check`` to work — the restore module shells
    out to git and a missing repo aborts with "fatal: not a git
    repository".
    """
    path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@t"
    env.setdefault("PATH", "/usr/bin:/usr/local/bin")
    for argv in (("init", "-q"),
                 ("config", "user.email", "t@t"),
                 ("config", "user.name", "t"),
                 ("config", "commit.gpgsign", "false"),
                 ("config", "tag.gpgsign", "false")):
        subprocess.run(["git", "-C", str(path), *argv],
                       env=env, capture_output=True, check=True)
    (path / "baseline.txt").write_text("baseline\n")
    subprocess.run(["git", "-C", str(path), "add", "baseline.txt"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "baseline"],
                   env=env, capture_output=True, check=True)
    return path


def _setup_db(tmp_path: Path) -> tuple[str, str, str, Path]:
    """Build a fresh sqlite state.db + run_dir. Caller seeds checkpoints."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_SQL)
    con.close()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # Mirror ``test_recover_cli_dispatch.py:31`` so the planner finds the
    # recipe via the run profile (we never set MINI_ORK_RECIPE so the
    # run-profile path is exercised by every fixture).
    (run_dir / "run_profile.json").write_text(
        json.dumps({"recipe": "framework-edit",
                    "roots": {"exec_cwd": None, "target": None}})
    )
    return str(db), str(run_dir), "framework-edit", run_dir


# ─────────────────────────────────────────────────────────────────────────────
# 1. Overlay recipe resolution (kickoff §1)
# ─────────────────────────────────────────────────────────────────────────────


def test_overlay_recipe_under_home_is_found_by_status(tmp_path, monkeypatch, capsys):
    """A recipe that lives only under ``<home>/recipes/`` (none in the
    engine) must be resolved by ``--status``; the kickoff's pre-state
    bug #1 returns ``workflow.yaml not found`` for such recipes today.
    """
    db, run_dir, _, _ = _setup_db(tmp_path)
    _set_run_dir(monkeypatch, run_dir)
    home = tmp_path / "home"
    home.mkdir()
    overlay_recipe = home / "recipes" / "acq-wave5-rsi"
    overlay_recipe.mkdir(parents=True)
    (overlay_recipe / "task_class.yaml").write_text(
        "name: framework_edit\ndescription: overlay\n"
    )
    wf = overlay_recipe / "workflow.yaml"
    _write_workflow(
        wf,
        nodes=[
            {"name": "planner", "type": "planner", "model_lane": "glm_lens"},
            {"name": "live_smoke", "type": "verifier", "model_lane": "verifier"},
        ],
        edges=[{"from": "planner", "to": "live_smoke", "edge_type": "depends_on"}],
    )

    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    # No MINI_ORK_RECIPE — recipe must come from run_profile.json.
    (Path(run_dir) / "run_profile.json").write_text(
        json.dumps({"recipe": "acq-wave5-rsi"})
    )
    rc = rp.main(["run-overlay-1", "--status", "--workflow", str(wf), "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "recipe:     acq-wave5-rsi" in out, out
    assert "task_class: framework_edit" in out, out


# ─────────────────────────────────────────────────────────────────────────────
# 2. Planner-as-reusable shortcut (kickoff §2)
# ─────────────────────────────────────────────────────────────────────────────


def test_status_shows_planner_reusable_when_plan_json_present(
    tmp_path, monkeypatch, capsys
):
    """The planner is the skipped-planner case (kickoff §2): no row,
    but ``plan.json`` exists. With 5 upstream success rows, status must
    list ``planner + 5`` in reuse, NOT in rerun; entry must be the
    first verifier (not the planner).
    """
    db, run_dir, recipe, _ = _setup_db(tmp_path)
    _set_run_dir(monkeypatch, run_dir)
    _set_run_dir(monkeypatch, run_dir)
    run_id = "run-planner-reuse-1"
    tc = "framework_edit"
    # All upstream nodes are seeded success — the planner gets a row
    # missing by design. plan.json exists and parses.
    for nid in ("lens_a", "lens_b", "lens_c", "synth", "implementer"):
        _seed_success(db, run_id, nid, recipe, tc, run_dir)
    Path(run_dir, "plan.json").write_text(json.dumps({
        "objective": "x", "decomposition": [], "dependencies": [],
        "artifact_contract": {"outputs": [], "success_verifiers": []},
        "verifier_contract": {"checks": []},
    }))

    wf = tmp_path / "wf.yaml"
    _write_workflow(
        wf,
        nodes=[
            {"name": "planner", "type": "planner", "model_lane": "glm_lens"},
            {"name": "lens_a", "type": "researcher", "model_lane": "minimax_lens"},
            {"name": "lens_b", "type": "researcher", "model_lane": "minimax_lens"},
            {"name": "lens_c", "type": "researcher", "model_lane": "minimax_lens"},
            {"name": "synth", "type": "researcher", "model_lane": "minimax_lens"},
            {"name": "implementer", "type": "implementer", "model_lane": "minimax_lens"},
            {"name": "static_check", "type": "verifier", "model_lane": "verifier"},
            {"name": "test_check", "type": "verifier", "model_lane": "verifier"},
        ],
        edges=[
            {"from": "planner", "to": "lens_a", "edge_type": "depends_on"},
            {"from": "planner", "to": "lens_b", "edge_type": "depends_on"},
            {"from": "planner", "to": "lens_c", "edge_type": "depends_on"},
            {"from": "lens_a", "to": "synth", "edge_type": "depends_on"},
            {"from": "lens_b", "to": "synth", "edge_type": "depends_on"},
            {"from": "lens_c", "to": "synth", "edge_type": "depends_on"},
            {"from": "synth", "to": "implementer", "edge_type": "depends_on"},
            {"from": "implementer", "to": "static_check", "edge_type": "verifies"},
            {"from": "implementer", "to": "test_check", "edge_type": "verifies"},
        ],
    )

    rc = rp.main(
        [run_id, "--status", "--workflow", str(wf), "--db", db],
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    # Reuse must include the planner + the 5 upstream success nodes.
    assert "[reuse]  planner" in out, out
    for nid in ("lens_a", "lens_b", "lens_c", "synth", "implementer"):
        assert f"[reuse]  {nid}" in out, out
    # No reused node may appear in the rerun list.
    rerun_section = out.split("rerun (")[1].split(")")[0] if "rerun (" in out else ""
    for nid in ("planner", "lens_a", "lens_b", "lens_c", "synth", "implementer"):
        assert nid not in rerun_section, f"{nid} leaked into rerun: {rerun_section!r}"
    # Entry is the first verifier (static_check, declared before test_check).
    assert "entry: static_check" in out, out


# ─────────────────────────────────────────────────────────────────────────────
# 3. ``--strategy verify`` refusal (kickoff §3)
# ─────────────────────────────────────────────────────────────────────────────


def test_verify_refuses_when_upstream_llm_node_lacks_checkpoint(
    tmp_path, monkeypatch, capsys
):
    """A non-reusable upstream LLM node must refuse ``verify`` with
    ``"<node> has no reusable checkpoint — use --strategy resume"``.

    The executor must NEVER be called (kickoff test 4's contract).
    """
    db, run_dir, recipe, _ = _setup_db(tmp_path)
    _set_run_dir(monkeypatch, run_dir)
    run_id = "run-verify-refuse-1"
    tc = "framework_edit"
    # planner + 4 upstream success; the verifier has NO row → first
    # verifier (static_check) becomes the entry; its upstream is fully
    # reusable. Add a SECOND verifier with no row that depends on a
    # non-reusable node to force the refusal.
    for nid in ("lens_a", "lens_b", "synth", "implementer",
              "first_verifier", "second_verifier"):
        _seed_success(db, run_id, nid, recipe, tc, run_dir)
    Path(run_dir, "plan.json").write_text(json.dumps({"objective": "x"}))

    wf = tmp_path / "wf.yaml"
    _write_workflow(
        wf,
        nodes=[
            {"name": "planner", "type": "planner", "model_lane": "glm_lens"},
            {"name": "lens_a", "type": "researcher", "model_lane": "minimax_lens"},
            {"name": "synth", "type": "researcher", "model_lane": "minimax_lens"},
            {"name": "implementer", "type": "implementer", "model_lane": "minimax_lens"},
            {"name": "first_verifier", "type": "verifier", "model_lane": "verifier"},
            {"name": "rogue_researcher", "type": "researcher",
             "model_lane": "minimax_lens"},
            {"name": "second_verifier", "type": "verifier",
             "model_lane": "verifier"},
        ],
        edges=[
            {"from": "planner", "to": "lens_a", "edge_type": "depends_on"},
            {"from": "lens_a", "to": "synth", "edge_type": "depends_on"},
            {"from": "synth", "to": "implementer", "edge_type": "depends_on"},
            {"from": "implementer", "to": "first_verifier",
             "edge_type": "verifies"},
            # rogue_researcher (no checkpoint, LLM) sits upstream of
            # the second verifier; verify will refuse.
            {"from": "rogue_researcher", "to": "second_verifier",
             "edge_type": "supplies_context_to"},
            {"from": "first_verifier", "to": "second_verifier",
             "edge_type": "depends_on"},
        ],
    )

    # Force the test-mode entry to the SECOND verifier so the rogue
    # upstream is in scope; otherwise the planner picks first_verifier
    # and rogue_researcher is OUTSIDE the upstream set.
    rc = rp.main(
        [
            run_id, "--strategy", "verify",
            "--from-node", "second_verifier",
            "--workflow", str(wf), "--db", db,
        ],
    )
    out_err = capsys.readouterr()
    assert rc == 1, (rc, out_err)
    assert "rogue_researcher has no reusable checkpoint" in out_err.err
    assert "use --strategy resume" in out_err.err


# ─────────────────────────────────────────────────────────────────────────────
# 4. Restore (kickoff §4)
# ─────────────────────────────────────────────────────────────────────────────


def _add_file_patch(path: str, body: str) -> str:
    """Hand-crafted unified-diff patch that creates ``path`` with ``body``.

    ``git diff`` does not include untracked files, so a test that wants
    a "create new.txt" patch must either ``git add`` first or synthesize
    the patch text. This helper does the latter so the test stays
    independent of the host git version.
    """
    # Standard dev/null /dev/null diff: a/ vs b/ on both sides.
    return (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode 100644\n"
        f"index 0000000..0000000\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{body.count(chr(10)) or 1} @@\n"
        + "".join(f"+{line}\n" for line in body.splitlines())
    )


def test_restore_apply_then_already_applied(tmp_path):
    """First restore applies the patch; second call returns
    ``already_applied`` without touching the tree."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = _init_git_repo(tmp_path / "target_repo")
    patch = Path(run_dir) / "salvage.patch"
    patch.write_text(_add_file_patch("new.txt", "new\n"))
    (Path(run_dir) / "rolled-back.json").write_text(json.dumps({"paths": []}))

    status1, _ = restore_carry_patch(str(run_dir), target=str(target))
    assert status1 == "applied", status1
    # The patch should have created new.txt.
    assert (target / "new.txt").exists()
    assert (target / "new.txt").read_text() == "new\n"

    status2, msg2 = restore_carry_patch(str(run_dir), target=str(target))
    assert status2 == "already_applied", (status2, msg2)


def test_restore_conflict_refuses_and_executor_never_called(
    tmp_path, monkeypatch, capsys
):
    """A conflicting tree must report ``conflict`` and the dispatch path
    must NOT invoke the executor (kickoff §4 test 4)."""
    db, run_dir, recipe, _ = _setup_db(tmp_path)
    _set_run_dir(monkeypatch, run_dir)
    run_id = "run-restore-conflict-1"
    tc = "framework_edit"
    # Seed the planner-as-reusable precondition + an implementer
    # checkpoint so the planner triggers the restore hook.
    _seed_success(db, run_id, "implementer", recipe, tc, run_dir)
    _seed_success(db, run_id, "first_verifier", recipe, tc, run_dir)
    _seed_success(db, run_id, "second_verifier", recipe, tc, run_dir)
    Path(run_dir, "plan.json").write_text(json.dumps({"objective": "x"}))

    target = _init_git_repo(tmp_path / "target_repo")
    # The patch wants to add new.txt with body "fresh content\n", but
    # the target already has new.txt with conflicting content → conflict.
    (target / "new.txt").write_text("unrelated edit\n")
    (Path(run_dir) / "salvage.patch").write_text(
        _add_file_patch("new.txt", "fresh content\n")
    )
    (Path(run_dir) / "rolled-back.json").write_text(json.dumps({"paths": []}))
    # Make run_profile.json point at the target as the worktree.
    (Path(run_dir) / "implementer-summary.json").write_text(json.dumps({
        "status": "implemented", "worktree_path": str(target),
        "files_changed": ["new.txt"],
    }))

    wf = tmp_path / "wf.yaml"
    _write_workflow(
        wf,
        nodes=[
            {"name": "planner", "type": "planner", "model_lane": "glm_lens"},
            {"name": "implementer", "type": "implementer",
             "model_lane": "minimax_lens"},
            {"name": "first_verifier", "type": "verifier",
             "model_lane": "verifier"},
            {"name": "second_verifier", "type": "verifier",
             "model_lane": "verifier"},
        ],
        edges=[
            {"from": "planner", "to": "implementer", "edge_type": "depends_on"},
            {"from": "implementer", "to": "first_verifier",
             "edge_type": "verifies"},
            {"from": "first_verifier", "to": "second_verifier",
             "edge_type": "depends_on"},
        ],
    )

    calls: list = []
    closed: list = []
    if rp._lease is not None:
        real_close = rp._lease.close_recovery

        def _spy_close(db_path, request_id, **kw):
            closed.append((request_id, kw.get("status")))
            return real_close(db_path, request_id, **kw)

        monkeypatch.setattr(rp._lease, "close_recovery", _spy_close)
    rc = rp.cli_main(
        [run_id, "--strategy", "verify", "--workflow", str(wf), "--db", db],
        execute_fn=calls.append,
    )
    out_err = capsys.readouterr()
    assert rc == 1, (rc, out_err)
    assert "refusing to dispatch: carry-patch conflict" in out_err.err
    # The executor must NEVER have been called.
    assert calls == [], f"executor was invoked despite conflict: {calls!r}"
    # The recovery request is closed as failed, not left as dispatched.
    if rp._lease is not None and rp._lease.lease_tables_present(db):
        assert [s for _, s in closed] == ["failed"], closed


def test_carry_patch_override_workflow_and_flag(tmp_path):
    """``--carry-patch`` and ``workflow.recovery.carry_patch`` override
    the default ``salvage.patch`` name (kickoff §4)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = _init_git_repo(tmp_path / "target_repo")
    # Point the run profile at the target so ``_target_from_run_dir``
    # can resolve it (kickoff §4 second point in the precedence list).
    (run_dir / "run_profile.json").write_text(json.dumps({
        "recipe": "framework-edit",
        "roots": {"exec_cwd": None, "target": str(target)},
    }))
    # Synthesize a clean add-file patch; the file does NOT exist in the
    # target yet so apply succeeds.
    patch = Path(run_dir) / "cycle-delta.patch"
    patch.write_text(_add_file_patch("cycle.txt", "cycle\n"))
    (Path(run_dir) / "rolled-back.json").write_text(json.dumps({"paths": []}))

    # 1. ``--carry-patch cycle-delta.patch`` override.
    plan = plan_restore(str(run_dir), cli_carry_patch="cycle-delta.patch",
                        workflow_path=None)
    assert plan["would_apply"] == "applied", plan

    # 2. ``workflow.recovery.carry_patch: cycle-delta.patch`` override
    # via the workflow YAML (no flag).
    wf = tmp_path / "wf.yaml"
    _write_workflow(wf, nodes=[{"name": "x", "type": "verifier",
                                "model_lane": "verifier"}],
                    edges=[],
                    recovery={"carry_patch": "cycle-delta.patch"})
    # cycle.txt is now present from the first invocation — reset to
    # baseline so the second invocation's plan ALSO says "applied".
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@t"
    subprocess.run(["git", "-C", str(target), "reset", "--hard", "HEAD"],
                   env=env, capture_output=True, check=True)
    plan2 = plan_restore(str(run_dir), cli_carry_patch=None,
                         workflow_path=str(wf))
    assert plan2["would_apply"] == "applied", plan2


# ─────────────────────────────────────────────────────────────────────────────
# 5. Retry-hint gate (kickoff §5)
# ─────────────────────────────────────────────────────────────────────────────


def _hint_workflow(path: Path) -> None:
    _write_workflow(
        path,
        nodes=[
            {"name": "planner", "type": "planner", "model_lane": "glm_lens"},
            {"name": "implementer", "type": "implementer",
             "model_lane": "minimax_lens"},
            {"name": "verifier_node", "type": "verifier",
             "model_lane": "verifier"},
        ],
        edges=[
            {"from": "planner", "to": "implementer", "edge_type": "depends_on"},
            {"from": "implementer", "to": "verifier_node",
             "edge_type": "verifies"},
        ],
    )


def test_hint_retryable_false_refuses_unless_force(tmp_path, monkeypatch, capsys):
    db, run_dir, recipe, _ = _setup_db(tmp_path)
    _set_run_dir(monkeypatch, run_dir)
    run_id = "run-hint-noretry-1"
    Path(run_dir, "retry-hint.json").write_text(json.dumps({
        "retryable": False, "strategy": "resume", "from_node": None,
        "needs_change": {"kind": "stale_code", "summary": "needs refresh",
                          "detail": "fetch latest"},
        "command": "mini-ork recover run-hint-noretry-1 --force",
    }))

    wf = tmp_path / "wf.yaml"
    _hint_workflow(wf)

    # Without --force → rc=1 + refusal message.
    rc = rp.main([run_id, "--strategy", "resume", "--workflow", str(wf), "--db", db])
    out_err = capsys.readouterr()
    assert rc == 1, out_err
    assert "retry-hint refuses this run" in out_err.err
    assert "needs refresh" in out_err.err

    # With --force → no refusal (the plan will compute; we don't care
    # about the rc beyond "not the refusal rc").
    monkeypatch.setenv("MINI_ORK_RECIPE", recipe)
    rc_force = rp.main(
        [run_id, "--strategy", "resume", "--workflow", str(wf), "--db", db,
         "--force"],
    )
    assert rc_force != 1 or "retry-hint" not in capsys.readouterr().err


def test_hint_needs_change_refuses_unless_ack_change(
    tmp_path, monkeypatch, capsys
):
    db, run_dir, _, _ = _setup_db(tmp_path)
    _set_run_dir(monkeypatch, run_dir)
    run_id = "run-hint-needs-change-1"
    Path(run_dir, "retry-hint.json").write_text(json.dumps({
        "retryable": True, "strategy": "resume", "from_node": None,
        "needs_change": {"kind": "config_drift", "summary": "fix CONFIG",
                          "detail": "edit mini_ork/config.py"},
        "command": "mini-ork recover run-hint-needs-change-1 --ack-change",
    }))

    wf = tmp_path / "wf.yaml"
    _hint_workflow(wf)

    rc = rp.main([run_id, "--strategy", "resume", "--workflow", str(wf), "--db", db])
    out_err = capsys.readouterr()
    assert rc == 1, out_err
    assert "Needs a change before retrying" in out_err.err
    assert "fix CONFIG" in out_err.err
    assert "--ack-change" in out_err.err


# ─────────────────────────────────────────────────────────────────────────────
# 6. plan_restore dry-run
# ─────────────────────────────────────────────────────────────────────────────


def test_plan_restore_no_target(tmp_path):
    """No implementer-summary.json + no run_profile.json roots →
    ``would_apply=no_target`` and never raises."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    plan = plan_restore(str(run_dir), workflow_path=None)
    assert plan["would_apply"] == "no_target", plan
    assert plan["target"] is None


# ─────────────────────────────────────────────────────────────────────────────
# 7. recover-verify revision 2 — exact fixes (kickoff §3)
# ─────────────────────────────────────────────────────────────────────────────


def _verify_workflow(path: Path) -> None:
    """Workflow matching the kickoff's read-only proof: planner + 5
    upstream reusable + an implementer-success row, two verifier-typed
    nodes downstream of the implementer, plus a smoke verifier with a
    failed checkpoint (kickoff test 2)."""
    _write_workflow(
        path,
        nodes=[
            {"name": "planner", "type": "planner", "model_lane": "glm_lens"},
            {"name": "lens_a", "type": "researcher", "model_lane": "minimax_lens"},
            {"name": "lens_b", "type": "researcher", "model_lane": "minimax_lens"},
            {"name": "synth", "type": "researcher", "model_lane": "minimax_lens"},
            {"name": "implementer", "type": "implementer",
             "model_lane": "minimax_lens"},
            {"name": "gate", "type": "verifier", "model_lane": "verifier"},
            {"name": "smoke", "type": "verifier", "model_lane": "verifier"},
        ],
        edges=[
            {"from": "planner", "to": "lens_a", "edge_type": "depends_on"},
            {"from": "planner", "to": "lens_b", "edge_type": "depends_on"},
            {"from": "lens_a", "to": "synth", "edge_type": "depends_on"},
            {"from": "lens_b", "to": "synth", "edge_type": "depends_on"},
            {"from": "synth", "to": "implementer", "edge_type": "depends_on"},
            {"from": "implementer", "to": "gate", "edge_type": "verifies"},
            {"from": "gate", "to": "smoke", "edge_type": "depends_on"},
        ],
    )


def test_verify_dispatches_with_failed_first_verifier(
    tmp_path, monkeypatch, capsys
):
    """Kickoff test 1: upstream fully reusable + FIRST verifier failed
    (no success row). ``--strategy verify`` MUST dispatch: ``execute_fn``
    is called once, the entry is that verifier, and the verifier is NOT
    in the reuse set.

    Pre-fix behaviour: ``_ancestors`` includes the entry itself, so the
    refusal loop tests the entry for reusability — every verify call
    refuses on the scenario verify exists for.
    """
    db, run_dir, recipe, _ = _setup_db(tmp_path)
    _set_run_dir(monkeypatch, run_dir)
    run_id = "run-verify-dispatch-r2"
    tc = "framework_edit"
    # Seed 5 upstream success rows; planner stays no_row (reused via
    # plan.json shortcut); do NOT seed gate — it's the entry.
    for nid in ("lens_a", "lens_b", "synth", "implementer"):
        _seed_success(db, run_id, nid, recipe, tc, run_dir)
    Path(run_dir, "plan.json").write_text(json.dumps({
        "objective": "x", "decomposition": [], "dependencies": [],
        "artifact_contract": {"outputs": [], "success_verifiers": []},
        "verifier_contract": {"checks": []},
    }))

    wf = tmp_path / "wf.yaml"
    _verify_workflow(wf)

    calls: list = []
    captured: dict = {}
    def _fake_executor(a):
        calls.append(list(a))
        # Snapshot the recovery env at the time execute_fn is called.
        for key in ("MINI_ORK_RECOVERY_FROM", "MINI_ORK_RECOVERY_CLOSURE",
                    "MINI_ORK_RECOVERY_STRATEGY", "MINI_ORK_RUN_ID"):
            captured[key] = os.environ.get(key)
        return 0

    rc = rp.cli_main(
        [run_id, "--strategy", "verify", "--workflow", str(wf), "--db", db],
        execute_fn=_fake_executor,
    )
    out_err = capsys.readouterr()
    assert rc == 0, (rc, out_err)
    # ``execute_fn`` called exactly once (no upstream rerun).
    assert len(calls) == 1, calls
    # The entry is the first verifier in topo order (gate, declared
    # before smoke in the workflow).
    assert captured.get("MINI_ORK_RECOVERY_FROM") == "gate", captured
    # The failed entry verifier and its dependents are rerun (the closure is
    # space-joined, planner.py ``_emit_recovery_env``) ...
    closure_env = set((captured.get("MINI_ORK_RECOVERY_CLOSURE") or "").split())
    assert {"gate", "smoke"} <= closure_env, closure_env
    # ... and the entry verifier is never listed as reused.
    reused, rerun = _status_sets(out_err.out)
    assert "gate" not in reused and "gate" in rerun, out_err.out


def test_no_node_in_both_reuse_and_rerun_with_from_node(
    tmp_path, monkeypatch, capsys
):
    """Kickoff test 2: ``--from-node gate`` where gate HAS a success row
    (so it is reusable on its own) → gate and smoke are rerun and neither is
    listed under reuse. Without ``reuse -= closure`` gate shows in both.
    """
    db, run_dir, recipe, _ = _setup_db(tmp_path)
    _set_run_dir(monkeypatch, run_dir)
    run_id = "run-no-dup-r2"
    tc = "framework_edit"
    for nid in ("lens_a", "lens_b", "synth", "implementer", "gate"):
        _seed_success(db, run_id, nid, recipe, tc, run_dir)
    Path(run_dir, "plan.json").write_text(json.dumps({"objective": "x"}))

    wf = tmp_path / "wf.yaml"
    _verify_workflow(wf)

    for strategy in ("verify", "resume"):
        rc = rp.main(
            [run_id, "--status", "--strategy", strategy,
             "--from-node", "gate",
             "--workflow", str(wf), "--db", db],
        )
        out = capsys.readouterr().out
        assert rc == 0, out
        reused, rerun = _status_sets(out)
        assert {"gate", "smoke"} <= rerun, out
        assert reused.isdisjoint(rerun), f"reuse ∩ rerun: {reused & rerun}\n{out}"
        assert {"lens_a", "lens_b", "synth", "implementer"} <= reused, out



def _status_sets(out: str) -> tuple[set[str], set[str]]:
    """``(reused, rerun)`` node ids from a ``format_status`` printout."""
    import re as _re

    reused = set(_re.findall(r"(?m)^\s+\[reuse\]\s+(\S+)", out))
    rerun = set(_re.findall(r"(?m)^\s+\[(?:first| +)\]\s+(\S+)", out))
    return reused, rerun


def test_status_with_needs_change_hint_exits_zero_with_plan(
    tmp_path, monkeypatch, capsys
):
    """Kickoff test 3: ``--status`` with a ``needs_change`` hint exits
    0 AND prints the hint block, the restore plan, and the full
    reuse/rerun plan.

    Pre-fix behaviour: ``--status`` short-circuited the refuses but
    the refusal guards fired BEFORE the status printout, so the hint
    block never reached stdout and rc was 1.
    """
    db, run_dir, _, _ = _setup_db(tmp_path)
    _set_run_dir(monkeypatch, run_dir)
    run_id = "run-status-needs-change-r2"
    Path(run_dir, "retry-hint.json").write_text(json.dumps({
        "retryable": True, "strategy": "resume", "from_node": None,
        "needs_change": {"kind": "config_drift", "summary": "fix CONFIG",
                          "detail": "edit mini_ork/config.py"},
        "command": f"mini-ork recover {run_id} --ack-change",
    }))

    wf = tmp_path / "wf.yaml"
    _verify_workflow(wf)

    rc = rp.main(
        [run_id, "--status", "--workflow", str(wf), "--db", db],
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "retry-hint:" in out, out
    assert "needs_change:" in out, out
    assert "reuse (" in out, out
    assert "rerun (" in out, out


def test_retry_hint_load_or_compute_module_used_when_present(
    tmp_path, monkeypatch, capsys
):
    """Kickoff test 4: ``mini_ork.recovery.retry_hint.load_or_compute``
    is the durable seam — when the module is importable (monkeypatched
    into sys.modules), the planner calls it instead of reading
    ``<run_dir>/retry-hint.json``.

    The injected fake returns a dict with NO file on disk; the gate
    uses it.
    """
    db, run_dir, _, _ = _setup_db(tmp_path)
    _set_run_dir(monkeypatch, run_dir)
    run_id = "run-retry-hint-module-r2"

    expected_hint = {
        "retryable": True, "strategy": "verify", "from_node": "gate",
        "needs_change": None,
        "command": f"mini-ork recover {run_id} --strategy verify",
    }
    seen_args: list = []

    def _fake_load_or_compute(home, rid):
        seen_args.append((str(home), rid))
        return expected_hint

    # Inject BEFORE the planner reads it. The shim imports lazily, so
    # the test only needs the module to exist in sys.modules at call
    # time (which it does, right here).
    import types
    fake_module = types.ModuleType("mini_ork.recovery.retry_hint")
    fake_module.load_or_compute = _fake_load_or_compute  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mini_ork.recovery.retry_hint", fake_module)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))

    # No retry-hint.json file — the gate must NOT fall back to a file.
    assert not Path(run_dir, "retry-hint.json").exists()

    wf = tmp_path / "wf.yaml"
    _verify_workflow(wf)

    rc = rp.main(
        [run_id, "--status", "--workflow", str(wf), "--db", db],
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    # Fake's load_or_compute was called exactly once with home + run_id.
    assert seen_args == [(str(tmp_path), run_id)], seen_args
    # Hint block is in the status printout (proves the module's value
    # was used, not a file's).
    assert "retry-hint:" in out, out
    assert "strategy:     'verify'" in out, out


def test_lease_held_blocks_restore_carry_patch(
    tmp_path, monkeypatch, capsys
):
    """Kickoff test 5: when the lease is held by another worker (the
    acquire_lease call returns None), ``restore_carry_patch`` MUST
    NEVER be called and the worktree stays untouched.

    Pre-fix behaviour: the restore block ran BEFORE the lease, so the
    second worker could clobber the tree while a concurrent recovery
    was still in flight.
    """
    db, run_dir, recipe, _ = _setup_db(tmp_path)
    _set_run_dir(monkeypatch, run_dir)
    run_id = "run-lease-block-r2"
    tc = "framework_edit"
    # Set up the preconditions for ``_needs_restore``:
    #   - reuse contains an implementer-typed node
    #   - rolled-back.json OR salvage.patch present in run_dir
    for nid in ("lens_a", "lens_b", "synth", "implementer"):
        _seed_success(db, run_id, nid, recipe, tc, run_dir)
    Path(run_dir, "plan.json").write_text(json.dumps({"objective": "x"}))
    target = _init_git_repo(tmp_path / "target_repo")
    (Path(run_dir) / "salvage.patch").write_text(
        _add_file_patch("from_restore.txt", "should never be applied\n")
    )
    (Path(run_dir) / "rolled-back.json").write_text(json.dumps({"paths": []}))
    (Path(run_dir) / "implementer-summary.json").write_text(json.dumps({
        "status": "implemented", "worktree_path": str(target),
        "files_changed": ["from_restore.txt"],
    }))

    wf = tmp_path / "wf.yaml"
    _verify_workflow(wf)

    # Spy on restore_carry_patch — must never be called.
    calls: list = []
    real_restore = rp.restore_carry_patch
    def _spy_restore(*args, **kwargs):
        calls.append((args, kwargs))
        return real_restore(*args, **kwargs)
    monkeypatch.setattr(rp, "restore_carry_patch", _spy_restore)

    # Force acquire_lease to return None (lease held).
    # Patch on the planner's imported ``_lease`` module reference.
    lease_mod = rp._lease
    assert lease_mod is not None, "lease module must be importable for this test"
    def _blocked_acquire(*a, **kw):  # noqa: ARG001
        return None
    monkeypatch.setattr(lease_mod, "acquire_lease", _blocked_acquire)

    rc = rp.main(
        [run_id, "--strategy", "resume", "--workflow", str(wf), "--db", db],
    )
    out_err = capsys.readouterr()
    assert rc == 0, (rc, out_err)
    # The safe descriptive message for "lease held by another worker"
    # appears in stdout.
    assert "already being recovered" in out_err.out, out_err.out
    # restore_carry_patch was NEVER invoked.
    assert calls == [], f"restore_carry_patch was called despite lease block: {calls!r}"
    # The worktree is unchanged — from_restore.txt must NOT exist.
    assert not (target / "from_restore.txt").exists(), (
        f"worktree mutated despite lease block; target contents: {list(target.iterdir())}"
    )

def test_a_recipe_carry_patch_alone_counts_as_rolled_back(tmp_path):
    """A recipe whose rollback saves the work only as its own carry patch
    (no rolled-back.json, no salvage.patch) still gets the work restored."""
    from mini_ork.recovery.restore import plan_restore, restore_carry_patch

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = _init_git_repo(tmp_path / "target_repo")
    (run_dir / "cycle-delta.patch").write_text(_add_file_patch("carried.txt", "carried\n"))
    (run_dir / "implementer-summary.json").write_text(json.dumps({
        "status": "implemented", "worktree_path": str(target),
        "files_changed": ["carried.txt"],
    }))

    plan = plan_restore(str(run_dir), cli_carry_patch="cycle-delta.patch")
    assert plan["would_apply"] == "applied", plan
    status, _msg = restore_carry_patch(str(run_dir), cli_carry_patch="cycle-delta.patch")
    assert status == "applied"
    assert (target / "carried.txt").read_text() == "carried\n"
    # Without the flag (and no workflow key) there is nothing to restore.
    assert plan_restore(str(run_dir))["would_apply"] == "no_patch"
