"""A remote check carries only what its caller added — never the control
plane's PATH/HOME, never a secret.

Verifier callers build ``{**os.environ, <their keys>}``. Forwarded whole, the
macOS PATH replaced the node's (the code-fix test verifier found no pytest on a
sandbox that had it, Daytona e2e run-1791912327-41843), and every secret the
run lifecycle exports into the process env reached the node with every check,
against D6 (only lane-scoped secrets cross)."""
from __future__ import annotations

from mini_ork.runtime import contract
from mini_ork.runtime.path_map import PathMap
from mini_ork.runtime.run_roots import RunRoots


def test_delta_keeps_only_what_the_caller_added(monkeypatch):
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin")
    monkeypatch.setenv("HOME", "/Users/someone")
    ambient = {"PATH": "/opt/homebrew/bin:/usr/bin", "HOME": "/Users/someone",
               "GLM_API_KEY": "glm-secret-0123456789"}
    caller = {**ambient,
              "MINI_ORK_PLAN_PATH": "/run/plan.json",
              "ARTIFACT_PATH": "/run/out.md",
              "PYTHONPATH": "/engine:/old",
              "DAYTONA_API_KEY": "dtn_should_never_cross"}
    assert contract._check_env_delta(caller, ambient) == {
        "MINI_ORK_PLAN_PATH": "/run/plan.json",
        "ARTIFACT_PATH": "/run/out.md",
        "PYTHONPATH": "/engine:/old",
    }


def test_remote_check_env_has_no_host_path_and_no_secret(monkeypatch, tmp_path):
    """Through ``run_check``'s remote branch, as the verifier node calls it."""
    target, run_dir, home, engine = (tmp_path / n for n in ("target", "run", "home", "engine"))
    for d in (target, run_dir, home, engine):
        d.mkdir()
    path_map = PathMap.from_roots(RunRoots(target=str(target), run_dir=str(run_dir),
                                           home=str(home), engine=str(engine)))
    sent: dict = {}

    class Remote:
        def exec(self, cmd, *, cwd, timeout, env=None):
            sent.update(cmd=cmd, cwd=cwd, env=dict(env or {}))
            return 0, "ok"

    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin")
    monkeypatch.setenv("HOME", "/Users/someone")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret-0123456789")
    monkeypatch.setattr(contract, "_resolve_remote_session", lambda run_id, env: Remote())
    monkeypatch.setattr("mini_ork.context.context_env",
                        lambda k, d="": "run-env" if k == "MINI_ORK_RUN_ID" else d)
    monkeypatch.setattr("mini_ork.dispatch.providers._run_path_map", lambda env: path_map)

    import os
    verifier_env = {**os.environ, "MINI_ORK_PLAN_PATH": str(run_dir / "plan.json"),
                    "PYTHONPATH": str(engine)}
    rc, out = contract.run_check(["python3", str(engine / "verify.py")], cwd=str(target),
                                 env=verifier_env, evidence_path="")
    assert (rc, out) == (0, "ok")
    env = sent["env"]
    assert "PATH" not in env and "HOME" not in env       # the node keeps its own
    assert "DEEPSEEK_API_KEY" not in env
    assert env["MINI_ORK_PLAN_PATH"] == "/workspace/run/plan.json"
    assert env["PYTHONPATH"] == "/opt/mini-ork"
    assert sent["cwd"] == "/workspace/target"
