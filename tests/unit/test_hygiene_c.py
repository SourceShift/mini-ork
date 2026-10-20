"""RBF-40 / RB-41: guard skips submodules, post-run has its own identity.

Hermetic regression tests for the two mechanisms fixed in kickoff
``rbf-hygiene-c``:

* RB-40 — ``.githooks/post-commit`` treated every path that is not
  ``is_file()`` as deleted, so the ``ui`` gitlink (index mode 160000, a
  directory) was "restored" on every 5 s poll with no effect (48 log rows).
  The heal routine must skip gitlinks and directories, must still restore a
  genuinely deleted regular file, and must not repeat a heal that changed
  nothing in the same process.
* RB-41 — ``_post_run_learning`` ran with the last dispatched node's id in
  ``MO_NODE_ID``; it must publish the sentinel ``post-run`` before any side
  channel, and per-node isolation must never provision a sandbox for it.

No network, no credentials, no live DB: throwaway git repos under
``tmp_path`` and side channels patched at the seams.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GUARD_PATH = REPO / ".githooks" / "post-commit"


# ─────────────────────────────────────────────────────────────────────────────
# RB-40 — file-reversion guard
# ─────────────────────────────────────────────────────────────────────────────
def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        },
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    r = _git(repo, "init", "-b", "main")
    assert r.returncode == 0, f"fixture: git init failed: {r.stderr}"
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "hygiene-test")
    return repo


def _commit(repo: Path, msg: str) -> str:
    r = _git(repo, "-c", "commit.gpgsign=false", "commit", "--no-verify",
             "--allow-empty", "-m", msg)
    assert r.returncode == 0, f"fixture: git commit failed: {r.stderr}"
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _load_guard():
    loader = SourceFileLoader("hygiene_c_post_commit", str(GUARD_PATH))
    spec = importlib.util.spec_from_file_location(
        "hygiene_c_post_commit", GUARD_PATH, loader=loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_watchdog(monkeypatch, repo: Path, sha: str, files: list,
                  log: Path, wrap=None):
    """Drive ``_watchdog`` in-process, recording every ``_git`` invocation.

    ``wrap`` may intercept a git call (return True to substitute a fake rc-0
    result) — used to simulate a heal that succeeds but changes nothing.
    """
    mod = _load_guard()
    orig = mod._git
    calls: list = []

    def wrapped(repo_root, *args):
        calls.append(tuple(args))
        if wrap is not None and wrap(tuple(args)):
            return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")
        return orig(repo_root, *args)

    mod._git = wrapped
    try:
        branch = _git(repo, "symbolic-ref", "--quiet", "--short",
                      "HEAD").stdout.strip()
        mod._watchdog(
            repo_root=str(repo), sha=sha, branch=branch, files=list(files),
            watch_s=0.3, poll_s=0.1, log_path=str(log),
        )
    finally:
        mod._git = orig
    return calls, mod


def _heal_attempts(calls, path: str) -> list:
    return [
        c for c in calls
        if c and c[0] in ("checkout", "restore") and path in c
    ]


def test_guard_skips_gitlink_entry(tmp_path, monkeypatch):
    """RB-40: a mode-160000 gitlink (a working-tree directory) is never healed."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    repo = _init_repo(tmp_path)
    (repo / "keep.txt").write_text("base\n")
    _git(repo, "add", "keep.txt")
    base = _commit(repo, "base")

    (repo / "ui").mkdir()
    (repo / "ui" / "placeholder.txt").write_text("submodule tree\n")
    r = _git(repo, "update-index", "--add", "--cacheinfo",
             f"160000,{base},ui")
    assert r.returncode == 0, f"fixture: update-index failed: {r.stderr}"
    sha = _commit(repo, "add gitlink ui")
    assert _git(repo, "ls-files", "-s", "ui").stdout.startswith("160000")

    log = tmp_path / "guard.log"
    calls, _ = _run_watchdog(monkeypatch, repo, sha, ["ui"], log)

    assert not _heal_attempts(calls, "ui"), (
        "guard attempted to heal the gitlink entry 'ui'")
    assert not log.exists() or "\tui\t" not in log.read_text(), (
        "guard wrote a restore row for the gitlink entry 'ui'")


def test_guard_skips_directory_entry(tmp_path, monkeypatch):
    """RB-40: an untracked working-tree directory is never healed either."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    repo = _init_repo(tmp_path)
    (repo / "keep.txt").write_text("base\n")
    _git(repo, "add", "keep.txt")
    sha = _commit(repo, "base")
    (repo / "plaindir").mkdir()
    (repo / "plaindir" / "nested.txt").write_text("not a git path\n")

    log = tmp_path / "guard.log"
    calls, _ = _run_watchdog(monkeypatch, repo, sha, ["plaindir"], log)

    assert not _heal_attempts(calls, "plaindir"), (
        "guard attempted to heal the directory entry 'plaindir'")
    assert not log.exists() or "\tplaindir\t" not in log.read_text()


def test_guard_still_restores_deleted_regular_file(tmp_path, monkeypatch):
    """RB-40: the skip is narrow — a deleted regular file is still restored."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    repo = _init_repo(tmp_path)
    (repo / "keep.txt").write_text("v1\n")
    _git(repo, "add", "keep.txt")
    _commit(repo, "base")
    (repo / "keep.txt").write_text("v2\n")
    _git(repo, "add", "keep.txt")
    sha = _commit(repo, "v2")
    (repo / "keep.txt").unlink()

    log = tmp_path / "guard.log"
    _run_watchdog(monkeypatch, repo, sha, ["keep.txt"], log)

    assert (repo / "keep.txt").is_file()
    assert (repo / "keep.txt").read_text() == "v2\n"
    text = log.read_text() if log.exists() else ""
    assert "restored-deleted-from-" in text and "\tkeep.txt\t" in text


def test_guard_does_not_repeat_noop_heal(tmp_path, monkeypatch):
    """RB-40: a heal that returns rc 0 but changes nothing is not retried."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    repo = _init_repo(tmp_path)
    (repo / "flat.txt").write_text("payload\n")
    _git(repo, "add", "flat.txt")
    sha = _commit(repo, "flat")
    (repo / "flat.txt").unlink()

    attempts: list = []

    def simulate_noop_heal(args):
        if args and args[0] in ("checkout", "restore") and "flat.txt" in args:
            attempts.append(args)
            return True
        return False

    log = tmp_path / "guard.log"
    _run_watchdog(monkeypatch, repo, sha, ["flat.txt"], log,
                  wrap=simulate_noop_heal)

    assert not (repo / "flat.txt").exists(), (
        "fixture corruption: the intercepted heal must not restore the file")
    assert len(attempts) == 1, (
        f"a no-change heal was attempted {len(attempts)} time(s) in one "
        "process; expected exactly one")


# ─────────────────────────────────────────────────────────────────────────────
# RB-41 — post-run identity
# ─────────────────────────────────────────────────────────────────────────────
_POST_RUN_ENV = {
    "MO_NODE_ID": "rollback",
    "MO_IMPL_REWARD": "run_verified",
    "MO_GRADE_RUN_REWARD": "1",
    "MO_LEARNING_WRITEBACK": "1",
    "MO_LANE_ROUTER": "1",
    "MO_AUTO_APPLY": "1",
    "MO_APPLY_ENABLED": "1",
}


def _spy_post_run_internals(monkeypatch):
    import mini_ork.cli.execute as ex

    seen: list = []

    def spy(*_args, **_kwargs):
        seen.append(os.environ.get("MO_NODE_ID", "<unset>"))
        return None

    monkeypatch.setattr("mini_ork.trace_store.grade_run_reward", spy)
    monkeypatch.setattr("mini_ork.learning.writeback.stamp_impl_run_verified", spy)
    monkeypatch.setattr("mini_ork.lane_router.recompute_advantages", spy)
    monkeypatch.setattr("mini_ork.cli.apply.auto_sweep", spy)
    monkeypatch.setattr(ex, "learning_update_conductor_outcomes", spy)
    monkeypatch.setattr(ex, "write_grpo_advantages", spy)
    return seen, ex


def test_post_run_learning_runs_as_post_run(tmp_path, monkeypatch):
    """RB-41: ``_post_run_learning`` publishes ``MO_NODE_ID=post-run``."""
    for key, val in _POST_RUN_ENV.items():
        monkeypatch.setenv(key, val)
    seen, ex = _spy_post_run_internals(monkeypatch)

    # Scoped so the contextvar binding does not leak into later tests; the
    # os.environ half is restored by tests/conftest.py's autouse fixture.
    with ex.run_context_scope({}):
        ex._post_run_learning(
            db=str(tmp_path / "absent.sqlite"),
            run_dir=str(tmp_path),
            run_id="hygiene-run",
            task_class="spec",
            fail_count=0,
        )

    assert seen, "no post-run side channel observed"
    assert set(seen) == {"post-run"}, (
        f"post-run work ran under node id(s) {sorted(set(seen))!r}")


def _workspace_with_stub_provider(tmp_path, monkeypatch, provider_env):
    from mini_ork.runtime.backends import remote as remote_mod
    from mini_ork.remote.providers import run_nodes

    per_node: list = []
    run_level: list = []
    endpoint = types.SimpleNamespace(
        name="stub-node", url="http://127.0.0.1:1", token="", max_sessions=1,
        headers={}, provider="stub", handle="stub-h",
    )
    monkeypatch.setattr(
        "mini_ork.remote.providers.base.get_node_provider",
        lambda name: "stub-provider",
    )
    monkeypatch.setattr(
        run_nodes, "acquire_node",
        lambda provider, **kw: per_node.append(kw.get("node_id")) or endpoint,
    )
    monkeypatch.setattr(
        "mini_ork.remote.providers.base.acquire_run_node",
        lambda provider, **kw: run_level.append(kw.get("run_id")) or endpoint,
    )

    ws = remote_mod.RemoteWorkspace(
        node=remote_mod._NodeRef(
            name="placeholder", url="", token="", max_sessions=1, headers={}),
        run_id="hygiene-run",
        image="hygiene-image",
        drive_root=str(tmp_path),
        engine_root=str(tmp_path),
        run_dir=str(tmp_path),
        node_provider="stub",
        provider_env=provider_env,
    )
    return ws, per_node, run_level


def test_post_run_isolation_uses_run_level_placement(tmp_path, monkeypatch):
    """RB-41: per-node isolation never provisions a sandbox for ``post-run``."""
    ws, per_node, run_level = _workspace_with_stub_provider(
        tmp_path, monkeypatch,
        {"MO_NODE_ISOLATION": "1", "MO_NODE_ID": "post-run"},
    )

    assert ws.node_isolation() == ""
    assert ws.ensure_node(create=True) is not None
    assert per_node == [], (
        f"per-node isolation provisioned a sandbox for post-run: {per_node!r}")
    assert run_level, "post-run must land on the run-level placement"


def test_real_node_still_gets_its_own_sandbox(tmp_path, monkeypatch):
    """RB-41: the skip names ``post-run`` only — real nodes stay isolated."""
    ws, per_node, run_level = _workspace_with_stub_provider(
        tmp_path, monkeypatch,
        {"MO_NODE_ISOLATION": "1", "MO_NODE_ID": "implementer"},
    )

    assert ws.node_isolation() == "implementer"
    assert ws.ensure_node(create=True) is not None
    assert per_node == ["implementer"]
    assert run_level == []
