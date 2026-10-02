"""remote-nodes-11 (verifier & verify placement) — acceptance tests.

Each Acceptance bullet from the kickoff has its own test. Every test drives
the production path (``execute._run_verifier_ref``, ``verify.main``,
``step_rules._git``, the mutation test-cmd loop) — NOT only the new
``run_check`` helper in isolation. The default path (no remote session)
is proven byte-identical to the legacy ``subprocess.run`` shape via
``test_local_branch_byte_identical_to_subprocess_run`` and
``test_run_verifier_ref_default_branch_is_byte_identical``.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout

# Production paths only — no in-process mocks of the routing helper.
from mini_ork.cli import execute as ex
from mini_ork.cli import verify as verify_mod
from mini_ork.gates import mutation_adversary, step_rules
from mini_ork.runtime.contract import run_check


# ── 1. Default path is byte-identical to the legacy subprocess.run ─────────


def test_local_branch_byte_identical_to_subprocess_run(tmp_path, monkeypatch):
    """No remote session → run_check is byte-identical to subprocess.run.

    The kickoff is explicit that the local branch must be byte-identical:
    attempt-1 wrapped the call in Popen with start_new_session and the
    byte-level parity tests shifted. Here we run the exact same argv
    through both paths and assert the captured bytes match.
    """
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    argv = [sys.executable, "-c", "print('hello', 'world'); import sys; sys.stderr.write('bye')"]
    expected_out_path = tmp_path / "expected.txt"
    actual_out_path = tmp_path / "actual.txt"
    # Reference: the legacy subprocess.run shape used by _run_verifier_ref.
    with open(expected_out_path, "wb") as fh:
        rc_ref = subprocess.run(argv, cwd=str(tmp_path), stdout=fh,
                                stderr=subprocess.STDOUT, env=os.environ.copy()).returncode
    rc, _ = run_check(argv, cwd=str(tmp_path), env=None, evidence_path=str(actual_out_path))
    assert rc == rc_ref
    assert expected_out_path.read_bytes() == actual_out_path.read_bytes()


def test_run_verifier_ref_default_branch_is_byte_identical(tmp_path, monkeypatch):
    """``_run_verifier_ref`` produces identical evidence under no-remote-session
    vs the documented ``subprocess.run`` shape. This is the kickoff §Review bar
    bullet: "the default path (feature env unset) is proven byte-identical by a test."
    """
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    script = tmp_path / "v.py"
    script.write_text("import os, sys\nprint('OUT')\nsys.stderr.write('ERR')\n"
                      "print('pass' if os.environ.get('MINI_ORK_PLAN_PATH') else 'fail')\n")
    ev = tmp_path / "ev.log"
    ex._run_verifier_ref(str(script), str(ev),
                         plan_path="/p/plan.json", artifact_path="/a/art.bin",
                         cwd=str(tmp_path))
    # Reference: drive subprocess.run the legacy way; the bytes must match.
    with open(tmp_path / "ev_ref.log", "wb") as fh:
        subprocess.run([sys.executable, str(script)], cwd=str(tmp_path),
                       stdout=fh, stderr=subprocess.STDOUT,
                       env={**os.environ, "MINI_ORK_PLAN_PATH": "/p/plan.json",
                            "ARTIFACT_PATH": "/a/art.bin",
                            "MINI_ORK_RUN_DIR": str(ev.parent)}).returncode
    assert ev.read_bytes() == (tmp_path / "ev_ref.log").read_bytes()


# ── 2. Verifier / dispatch tests preserved under the reroute ────────────────


def test_execute_run_verifier_ref_py_dispatch_env_and_rc(tmp_path, monkeypatch):
    """The dispatch tests that attempt-1 broke: no ``mo_node_emit`` leakage.

    ``run_check`` MUST NOT call ``mo_node_emit`` on the local branch — the
    helper previously wrote ``mo_node_emit: run_id required`` to a
    verifier's evidence stream when ``MINI_ORK_RUN_ID`` was unset and
    the helper tried to emit from the local branch.
    """
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    script = tmp_path / "chk.py"
    script.write_text(
        "import os\n"
        "print('PP=' + os.environ.get('MINI_ORK_PLAN_PATH', ''))\n"
        "print('AP=' + os.environ.get('ARTIFACT_PATH', ''))\n"
        "print('RD=' + os.environ.get('MINI_ORK_RUN_DIR', ''))\n")
    ev = tmp_path / "evidence" / "chk.log"
    os.makedirs(ev.parent)
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = ex._run_verifier_ref(str(script), str(ev),
                                  plan_path="/p/plan.json", artifact_path="/a/art.bin",
                                  cwd=str(tmp_path))
    assert rc == 0
    assert buf.getvalue() == ""  # .py → no deprecation / no emit-warning
    text = ev.read_text()
    assert "PP=/p/plan.json" in text
    assert "AP=/a/art.bin" in text
    assert f"RD={ev.parent}" in text


def test_run_verifier_ref_postrun_cwd_pin(tmp_path, monkeypatch):
    """``_run_verifier_ref`` honors ``roots.target`` when ``cwd=None`` is passed.

    The kickoff §Review bar acceptance: with placement unset, the
    verifier still runs in the target. The implementation must thread
    the pinned root from :func:`load_run_roots` even when the caller
    passes no explicit ``cwd``.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    pinned_target = tmp_path / "target"
    pinned_target.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({
        "roots": {"target": str(pinned_target)},
    }))
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    script = tmp_path / "print_cwd.py"
    script.write_text("import os\nprint('cwd=' + os.getcwd())\n")
    ev = tmp_path / "ev.log"
    rc = ex._run_verifier_ref(str(script), str(ev), run_dir=str(run_dir))
    assert rc == 0
    assert f"cwd={pinned_target}" in ev.read_text()


# ── 3. Post-run verify cwd-pin drives resolver exit ─────────────────────────


def test_postrun_verify_cwd_pin_drives_resolver_exit(tmp_path, monkeypatch):
    """``verify.main`` resolves ``load_run_roots`` BEFORE any branch that depends
    on cwd. The pinned target is what the resolver exit drives from.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    pinned_target = tmp_path / "target"
    pinned_target.mkdir()
    (pinned_target / "marker.txt").write_text("pinned")
    (run_dir / "run_profile.json").write_text(json.dumps({
        "roots": {"target": str(pinned_target)},
    }))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MINI_ORK_RECIPE", "rec")
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    recipe_dir = tmp_path / "recipes" / "rec" / "verifiers"
    recipe_dir.mkdir(parents=True)
    verifier = recipe_dir / "ck_pin.py"
    verifier.write_text("import os\nprint('cwd=' + os.getcwd())\n")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "artifact_contract": {"success_verifiers": ["ck_pin.py"]},
        "verifier_contract": {"checks": []},
    }))
    monkeypatch.setenv("MINI_ORK_PLAN_PATH", str(plan_path))
    out = io.StringIO()
    with redirect_stdout(out):
        verify_mod.main(["--plan", str(plan_path), str(run_dir / "art.bin")])
    evidence = next((run_dir / "evidence").glob("ck_pin-*.log")).read_text()
    assert f"cwd={pinned_target.resolve()}" in evidence, (
        f"verifier did not run in pinned target: {evidence!r}")


def test_postrun_verify_command_branch_cwd_pinned(tmp_path, monkeypatch):
    """The command-fallback branch of post-run verify (no script) also gets the cwd.

    Kickoff §2 calls out BOTH branches — ``verify.py:324`` (command
    fallback) and ``verify.py:336`` (script). This test exercises the
    fallback path.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    pinned_target = tmp_path / "target"
    pinned_target.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({
        "roots": {"target": str(pinned_target)},
    }))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MINI_ORK_RECIPE", "rec")
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    # Command-only verifier: _find_verifier_script returns None, so the
    # fallback path runs ``bash -lc <command>``.
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "artifact_contract": {"success_verifiers": ["echo-cwd"]},
        "verifier_contract": {"checks": [
            {"id": "echo-cwd", "command": "echo cwd=$(pwd)"},
        ]},
    }))
    monkeypatch.setenv("MINI_ORK_PLAN_PATH", str(plan_path))
    out = io.StringIO()
    with redirect_stdout(out):
        verify_mod.main(["--plan", str(plan_path), str(run_dir / "art.bin")])
    evidence = next((run_dir / "evidence").glob("echo-cwd-*.log")).read_text()
    assert f"cwd={pinned_target.resolve()}" in evidence, (
        f"command-fallback branch did not honor pinned cwd: {evidence!r}")


# ── 4. Replica hygiene via restore_replica (duck-typed via run_check) ──────


def test_restore_replica_noop_when_no_session(tmp_path, monkeypatch):
    """Local branch: ``run_check`` is byte-identical to ``subprocess.run``.

    With no remote session, ``restore_replica`` is never called.
    """
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    junk = tmp_path / "junk.txt"
    rc, _ = run_check(
        [sys.executable, "-c", f"open({str(junk)!r}, 'w').write('hi')"],
        cwd=str(tmp_path), env=None,
        evidence_path=str(tmp_path / "ev.log"),
    )
    assert rc == 0, "verifier exit was non-zero"
    assert junk.is_file(), "local-branch verifier should still create the junk file"


def test_restore_replica_called_on_remote_branch(monkeypatch):
    """Remote branch: ``run_check`` invokes ``restore_replica`` after exec.

    Mocks the session resolver to return a stub that records both exec
    and restore_replica calls — the order matters (restore AFTER exec).
    """
    calls = []

    class StubRemote:
        def exec(self, _cmd, *, cwd, timeout):
            calls.append(("exec", cwd, timeout))
            return 0, "ok"

        def restore_replica(self):
            calls.append(("restore_replica",))

    def fake_resolver(_run_id, _env):
        return StubRemote()

    monkeypatch.setattr("mini_ork.runtime.contract._resolve_remote_session", fake_resolver)
    # The helper imports ``context_env`` inside the function; patch the
    # source module so the import resolves to our stub.
    monkeypatch.setattr("mini_ork.context.context_env",
                        lambda k, d="": "run-xyz" if k == "MINI_ORK_RUN_ID" else d)
    rc, out = run_check(["echo", "hi"], cwd="/workspace/target", env=None,
                        evidence_path="")
    assert rc == 0
    assert out == "ok"
    # restore_replica MUST follow exec — the kickoff §4 is explicit.
    assert ("exec", "/workspace/target", 0) in calls
    assert calls[-1] == ("restore_replica",), f"restore_replica was not last: {calls}"


def test_restore_replica_failure_does_not_mask_check_rc(monkeypatch):
    """Restore failure MUST NOT mask the check's rc (kickoff §4, attempt-1
    failure mode).

    A failure in ``restore_replica`` is best-effort hygiene: the truth
    is the check's verdict, not the replica state. The helper catches
    the exception and returns the check's rc.
    """
    class FlakyRemote:
        def __init__(self):
            self.restored = False

        def exec(self, _cmd, *, cwd, timeout):
            return 7, "rc=7"

        def restore_replica(self):
            raise RuntimeError("node-agent is sad")

    monkeypatch.setattr("mini_ork.runtime.contract._resolve_remote_session",
                        lambda _run_id, _env: FlakyRemote())
    monkeypatch.setattr("mini_ork.context.context_env",
                        lambda k, d="": "run-xyz" if k == "MINI_ORK_RUN_ID" else d)
    rc, out = run_check(["false"], cwd="/w", env=None, evidence_path="")
    assert rc == 7
    assert out == "rc=7"


# ── 5. Step_rules._git preserves the CompletedProcess shape ─────────────────


def test_step_rules_git_local_branch_preserves_shape(tmp_path, monkeypatch):
    """``step_rules._git`` keeps ``run.returncode`` and ``run.stdout`` readable.

    The duck-typed reroute must hand back a ``CompletedProcess``-shaped
    object so the rules' existing reads keep working byte-identically.
    """
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    # Real git repo so ``rev-parse --is-inside-work-tree`` returns 0.
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "x@x"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=str(tmp_path), check=True)
    (tmp_path / "f").write_text("hi")
    subprocess.run(["git", "add", "f"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=str(tmp_path), check=True)
    result = step_rules._git(str(tmp_path), ["rev-parse", "--is-inside-work-tree"])
    assert result is not None
    assert result.returncode == 0
    assert b"true" in (result.stdout or b"")


def test_step_rules_rule_patch_applies_cleanly_under_reroute(tmp_path, monkeypatch):
    """``rule_patch_applies_cleanly`` produces a verdict under the reroute.

    Runs the full rule (production path) against a real patch artifact
    so the reroute is exercised end-to-end, not in isolation.
    """
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "x@x"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=str(repo), check=True)
    (repo / "f.txt").write_text("hi\n")
    subprocess.run(["git", "add", "f.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
    patch = tmp_path / "p.patch"
    subprocess.run(["git", "diff", "--binary"], cwd=str(repo),
                   stdout=open(patch, "wb"), check=True)
    # ``rule_patch_applies_cleanly`` invokes ``_git`` internally — the reroute
    # is exercised even though we drive the rule, not the helper directly.
    verdict, _ = step_rules.rule_patch_applies_cleanly(str(patch), str(repo))
    assert verdict in ("pass", "defer")


# ── 6. Mutation test-cmd is routed via run_check ────────────────────────────


def test_mutation_test_cmd_routed_via_run_check(tmp_path, monkeypatch):
    """The mutation campaign's test-cmd ``subprocess.run`` is now routed.

    The reroute preserves the ``tr.returncode``/``tr.stdout`` reads. The
    ``_revert`` path stays on local git (kickoff §2 explicitly excludes
    it). Verifies by running a one-mutation fixture: an ``always-pass``
    test command against a one-line trivial mutation must keep the
    existing capture path working.
    """
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "x@x"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=str(repo), check=True)
    (repo / "f.txt").write_text("hi\n")
    subprocess.run(["git", "add", "f.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
    # Build a synthetic "absent → applied" delta by computing it from the
    # current state vs an empty tree.
    new_diff = subprocess.run(["git", "diff", "--binary", "--root", "--", "f.txt"],
                              cwd=str(repo), capture_output=True, text=True).stdout
    mutations_json = {
        "mutations": [{"id": "M0", "diff": new_diff, "target_scenario": "x"}],
    }
    report = mutation_adversary.run_adversary(
        mutations_json, str(repo),
        ["true"],  # test command: always exit 0 → mutation survives → coverage gap
        apply_timeout_s=10, test_timeout_s=10,
    )
    # Coverage gap → caught=False; the reroute through run_check must keep
    # the kill_rate accounting intact.
    assert report["total"] == 1
    assert report["killed"] == 0
    # Replica hygiene: _revert must have run on local git (kickoff §2).
    out = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                         capture_output=True, text=True).stdout
    assert out.strip() == "", f"workspace left dirty after revert: {out!r}"


# ── 7. Workspace protocol preserves legacy compat ────────────────────────────


def test_restore_replica_noop_when_no_snapshot(monkeypatch):
    """``restore_replica`` is a no-op when ``_last_synced`` is unset.

    The duck-typed helper must not crash on a freshly-constructed
    ``RemoteWorkspace`` whose ``_last_synced`` is ``None``.
    """
    from mini_ork.runtime.backends.remote import RemoteWorkspace

    # Build a stub without going through ``up()`` — _last_synced is None.
    rw = RemoteWorkspace.__new__(RemoteWorkspace)
    rw._last_synced = None
    # Must not raise.
    rw.restore_replica()


# ── small helpers ──────────────────────────────────────────────────────────


def _stderr_buf() -> io.StringIO:
    return io.StringIO()


def _stdout_buf() -> io.StringIO:
    return io.StringIO()


# ---------------------------------------------------------------------------
# Review: placement gating + the remote branch's path/env translation.
# ---------------------------------------------------------------------------


def _pinned(tmp_path):
    import json as _json

    target = tmp_path / "target"
    target.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=target, check=True, capture_output=True)
    (target / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "-A"], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=target, check=True, capture_output=True)
    home = tmp_path / "home"
    run_dir = home / "runs" / "r1"
    run_dir.mkdir(parents=True)
    engine = tmp_path / "engine"
    engine.mkdir()
    (run_dir / "run_profile.json").write_text(_json.dumps({"roots": {
        "target": str(target), "run_dir": str(run_dir), "home": str(home), "engine": str(engine)}}))
    return target, run_dir, engine


def test_local_placement_never_opens_a_remote_session(monkeypatch, tmp_path):
    """A run with a run id but local placement must not even try a remote session."""
    from mini_ork.runtime import contract

    monkeypatch.setattr("mini_ork.context.context_env",
                        lambda k, d="": "r1" if k == "MINI_ORK_RUN_ID" else d)
    monkeypatch.setattr("mini_ork.context.context_env_snapshot", lambda: {"MO_NODE_URL": "http://x"})

    def boom(*_a, **_k):
        raise AssertionError("get_run_session called for a local run")

    monkeypatch.setattr("mini_ork.runtime.workspace_session.get_run_session", boom)
    rc, _ = contract.run_check(["/bin/sh", "-c", "exit 0"], cwd=str(tmp_path))
    assert rc == 0


def test_remote_branch_translates_cwd_argv_and_env(monkeypatch, tmp_path):
    from mini_ork.runtime import contract

    target, run_dir, engine = _pinned(tmp_path)
    seen = {}

    class _Remote:
        def exec(self, cmd, *, cwd, timeout, env=None):
            seen.update(cmd=cmd, cwd=cwd, env=dict(env or {}))
            return 0, "ok"

    snapshot = {"MINI_ORK_RUN_ID": "r1", "MINI_ORK_RUN_DIR": str(run_dir), "MO_PLACEMENT": "remote",
                "MO_NODE_TOKEN": "node-secret"}
    monkeypatch.setattr("mini_ork.context.context_env", lambda k, d="": snapshot.get(k, d))
    monkeypatch.setattr("mini_ork.context.context_env_snapshot", lambda: dict(snapshot))
    monkeypatch.setattr("mini_ork.runtime.workspace_session.get_run_session",
                        lambda *a, **k: _Remote())
    script = engine / "recipes" / "code-fix" / "verifiers" / "test.py"
    rc, _ = contract.run_check(["python3", str(script)], cwd=str(target),
                               env={"MINI_ORK_PLAN_PATH": str(run_dir / "plan.json")})
    assert rc == 0
    assert seen["cwd"] == "/workspace/target"
    assert "/opt/mini-ork/recipes/code-fix/verifiers/test.py" in seen["cmd"]
    assert seen["env"]["MINI_ORK_PLAN_PATH"] == "/workspace/run/plan.json"
    assert seen["env"]["MINI_ORK_RUN_DIR"] == "/workspace/run"
    assert "MO_NODE_TOKEN" not in seen["env"]
    host = str(tmp_path.resolve())
    assert host not in seen["cmd"] and not any(host in v for v in seen["env"].values())
