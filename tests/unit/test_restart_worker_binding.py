"""Hermetic branch coverage for the goal-loop worker-restart binding.

kickoffs/book-goal-loop/binding/restart_worker.py SIGTERMs the incumbent
book-generation worker (a Redis-SETNX singleton) and starts a replacement from
the fixed worktree so the RUNNING worker executes the goal-loop's fix. Its real
effects — pgrep, os.kill(SIGTERM), subprocess.Popen, the readiness HTTP probe —
are monkeypatched here so every decision branch is proven with zero process or
network effect. The live worker is never touched by this suite.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_BINDING = (
    Path(__file__).resolve().parents[2]
    / "kickoffs"
    / "book-goal-loop"
    / "binding"
    / "restart_worker.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("restart_worker_binding", _BINDING)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load()


def test_missing_worktree_is_usage_error(mod, monkeypatch):
    monkeypatch.delenv("MO_GOAL_TARGET_CWD", raising=False)
    assert mod.main([]) == 2


def test_worktree_not_a_dir_is_usage_error(mod, monkeypatch, tmp_path):
    monkeypatch.setenv("MO_GOAL_TARGET_CWD", str(tmp_path / "does-not-exist"))
    assert mod.main([]) == 2


def test_dry_prints_plan_and_touches_nothing(mod, monkeypatch, tmp_path, capsys):
    """DRY resolves the incumbent + prints the plan but never SIGTERMs/spawns."""
    monkeypatch.setenv("MO_GOAL_TARGET_CWD", str(tmp_path))
    monkeypatch.setenv("MO_GOAL_WORKER_RESTART_DRY", "1")
    monkeypatch.setattr(mod, "_worker_pids", lambda role: [4242])
    touched = {"stop": False, "start": False}
    monkeypatch.setattr(
        mod, "_stop_incumbent", lambda *a, **k: touched.__setitem__("stop", True) or (True, "x")
    )
    monkeypatch.setattr(
        mod, "_start_replacement", lambda *a, **k: touched.__setitem__("start", True) or (True, "x")
    )
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert touched == {"stop": False, "start": False}  # neither seam fired
    assert "DRY" in out and "4242" in out


def test_stop_failure_never_starts(mod, monkeypatch, tmp_path):
    """A still-draining incumbent (graceful-only, no SIGKILL) is never replaced."""
    monkeypatch.setenv("MO_GOAL_TARGET_CWD", str(tmp_path))
    monkeypatch.delenv("MO_GOAL_WORKER_RESTART_DRY", raising=False)
    monkeypatch.setattr(mod, "_worker_pids", lambda role: [7])
    monkeypatch.setattr(
        mod, "_stop_incumbent", lambda *a, **k: (False, "still alive after 120s; refusing SIGKILL")
    )
    started = {"v": False}
    monkeypatch.setattr(
        mod, "_start_replacement", lambda *a, **k: started.__setitem__("v", True) or (True, "x")
    )
    assert mod.main([]) == 1
    assert started["v"] is False  # never spawns a 2nd worker onto a held singleton lock


def test_start_failure_never_awaits(mod, monkeypatch, tmp_path):
    monkeypatch.setenv("MO_GOAL_TARGET_CWD", str(tmp_path))
    monkeypatch.delenv("MO_GOAL_WORKER_RESTART_DRY", raising=False)
    monkeypatch.setattr(mod, "_worker_pids", lambda role: [])
    monkeypatch.setattr(mod, "_stop_incumbent", lambda *a, **k: (True, "nothing to stop"))
    monkeypatch.setattr(
        mod, "_start_replacement", lambda *a, **k: (False, "tsx missing under worktree node_modules")
    )
    awaited = {"v": False}
    monkeypatch.setattr(
        mod, "_await_ready", lambda *a, **k: awaited.__setitem__("v", True) or (True, "x")
    )
    assert mod.main([]) == 1
    assert awaited["v"] is False  # a failed spawn is never treated as ready


def test_ready_is_exit_0(mod, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MO_GOAL_TARGET_CWD", str(tmp_path))
    monkeypatch.delenv("MO_GOAL_WORKER_RESTART_DRY", raising=False)
    monkeypatch.setattr(mod, "_worker_pids", lambda role: [7])
    monkeypatch.setattr(mod, "_stop_incumbent", lambda *a, **k: (True, "incumbent drained"))
    monkeypatch.setattr(mod, "_start_replacement", lambda *a, **k: (True, "spawned replacement pid 999"))
    monkeypatch.setattr(mod, "_await_ready", lambda *a, **k: (True, "worker registered (log line observed)"))
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "registered" in out


def test_not_ready_is_exit_1(mod, monkeypatch, tmp_path):
    monkeypatch.setenv("MO_GOAL_TARGET_CWD", str(tmp_path))
    monkeypatch.delenv("MO_GOAL_WORKER_RESTART_DRY", raising=False)
    monkeypatch.setattr(mod, "_worker_pids", lambda role: [7])
    monkeypatch.setattr(mod, "_stop_incumbent", lambda *a, **k: (True, "drained"))
    monkeypatch.setattr(mod, "_start_replacement", lambda *a, **k: (True, "spawned"))
    monkeypatch.setattr(mod, "_await_ready", lambda *a, **k: (False, "no readiness signal within 180s"))
    assert mod.main([]) == 1


def test_await_ready_observes_registered_line(mod, tmp_path):
    """Primary readiness signal is the child-log line, not the cross-process health probe."""
    log = tmp_path / "worker.log"
    log.write_text("booting…\nHatchet book-generation worker registered\n")
    ok, why = mod._await_ready(str(log), "http://127.0.0.1:0/unused", 5)
    assert ok is True and "registered" in why


def test_await_ready_detects_startup_failure(mod, tmp_path):
    """A 'Failed to start' line short-circuits to a fail without waiting the full timeout."""
    log = tmp_path / "worker.log"
    log.write_text("Failed to start worker: boom\n")
    ok, _why = mod._await_ready(str(log), "http://127.0.0.1:0/unused", 5)
    assert ok is False


def test_await_ready_detects_watchdog_giving_up(mod, tmp_path):
    """The watchdog exhausting its restart budget ('giving up') is terminal too."""
    log = tmp_path / "worker.log"
    log.write_text("[worker-watchdog:book-generation] hit MAX_RESTARTS=5 — giving up\n")
    ok, _why = mod._await_ready(str(log), "http://127.0.0.1:0/unused", 5)
    assert ok is False


def test_stop_terminates_watchdog_before_worker(mod, monkeypatch):
    """The supervisor is SIGTERM'd BEFORE the leaf (else the watchdog respawns a
    primary-checkout worker onto the freed lock) — and only ever gracefully."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(mod.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    wd_state = {"n": 0}
    w_state = {"n": 0}

    def wd(_role):
        wd_state["n"] += 1
        return [100] if wd_state["n"] == 1 else []  # gone after the first look

    def w(_role):
        w_state["n"] += 1
        return [200] if w_state["n"] == 1 else []

    monkeypatch.setattr(mod, "_watchdog_pids", wd)
    monkeypatch.setattr(mod, "_worker_pids", w)
    ok, why = mod._stop_incumbent("book-generation", 5)
    assert ok is True and "drained" in why
    assert calls == [(100, mod.signal.SIGTERM), (200, mod.signal.SIGTERM)]  # watchdog first, no SIGKILL


def test_stop_nothing_running_is_noop(mod, monkeypatch):
    monkeypatch.setattr(mod, "_watchdog_pids", lambda role: [])
    monkeypatch.setattr(mod, "_worker_pids", lambda role: [])
    killed = {"v": False}
    monkeypatch.setattr(mod.os, "kill", lambda *a: killed.__setitem__("v", True))
    ok, why = mod._stop_incumbent("book-generation", 5)
    assert ok is True and killed["v"] is False and "nothing to stop" in why


def _fake_worktree(root):
    (root / "scripts").mkdir()
    wd = root / "scripts" / "dev-worker-watchdog.sh"
    wd.write_text("#!/bin/bash\n")
    (root / "node_modules" / ".bin").mkdir(parents=True)
    (root / "node_modules" / ".bin" / "tsx").write_text("#!/bin/sh\n")
    (root / "server").mkdir()
    (root / "server" / ".env").write_text(
        'QUEUE_PREFIX=dev_\n'
        '# a comment\n'
        'export REDIS_HOST=100.74.239.22\n'
        'REDIS_PORT="6380"\n'
        "REDIS_PASSWORD='pw with = sign'\n"
        "IGNORED=nope\n"
    )
    return wd


def test_start_launches_worktree_watchdog(mod, monkeypatch, tmp_path):
    """The replacement is the WATCHDOG (supervisor), run from the worktree, with
    the log target + dispatch namespace pinned from the worktree's server/.env."""
    wd = _fake_worktree(tmp_path)
    monkeypatch.setattr(mod, "_mini_ork_home", lambda w: (None, "unset"))  # isolate: focus on watchdog+namespace
    seen: dict = {}

    class FakePopen:
        def __init__(self, argv, cwd, stdout, stderr, stdin, start_new_session, env):
            seen.update(argv=argv, cwd=cwd, detach=start_new_session, env=env)
            self.pid = 4321

    monkeypatch.setattr(mod.subprocess, "Popen", FakePopen)
    log = str(tmp_path / "w.log")
    ok, why = mod._start_replacement(str(tmp_path), "book-generation", log)
    assert ok is True and "4321" in why
    assert seen["argv"] == ["bash", str(wd), "book-generation"]
    assert seen["cwd"] == str(tmp_path)
    assert seen["detach"] is True
    assert seen["env"]["WORKER_LOG"] == log
    assert seen["env"]["QUEUE_PREFIX"] == "dev_"
    assert seen["env"]["REDIS_HOST"] == "100.74.239.22"
    assert seen["env"]["REDIS_PORT"] == "6380"
    assert seen["env"]["REDIS_PASSWORD"] == "pw with = sign"  # partition on FIRST '='


def test_start_refuses_without_watchdog_script(mod, tmp_path):
    """A dir that is NOT a sanctioned researcher worktree (no watchdog) is refused
    cleanly — never a bare/unsupervised worker."""
    (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
    (tmp_path / "node_modules" / ".bin" / "tsx").write_text("x")
    ok, why = mod._start_replacement(str(tmp_path), "book-generation", str(tmp_path / "w.log"))
    assert ok is False and "watchdog missing" in why


def test_env_overlay_pins_only_namespace_keys(mod, monkeypatch, tmp_path):
    _fake_worktree(tmp_path)
    monkeypatch.delenv("QUEUE_PREFIX", raising=False)
    env = mod._env_overlay(str(tmp_path), "/run/w.log")
    assert env["WORKER_LOG"] == "/run/w.log"
    assert env["LOKI_ENABLED"] == "false"
    assert env["QUEUE_PREFIX"] == "dev_"
    assert env["REDIS_PASSWORD"] == "pw with = sign"
    assert env.get("IGNORED") != "nope"  # non-namespace keys are not lifted from .env


def test_env_overlay_extra_wins_last(mod, tmp_path):
    """`extra` (the runner-aligned model pin) is applied last and overrides."""
    _fake_worktree(tmp_path)
    env = mod._env_overlay(str(tmp_path), "/run/w.log", {"CHAPTER_PRIMARY_MODEL": "glm-5.3"})
    assert env["CHAPTER_PRIMARY_MODEL"] == "glm-5.3"
    assert env["QUEUE_PREFIX"] == "dev_"  # namespace overlay still applied


def test_env_overlay_scrubs_run_scoped_identity(mod, monkeypatch, tmp_path):
    """A stale MINI_ORK_RUN_DIR/RUN_ID inherited from a goal-loop parent must NOT
    reach the long-lived worker: it spawns many `mini-ork run`s, each of which must
    mint a fresh run id + canonical run dir. Inheriting a stale pin is what split the
    failing W9_scaffold_sections run — execute wrote to a leaked /tmp dir while the
    caller read runs/<id>/verified-artifact.json (ENOENT)."""
    _fake_worktree(tmp_path)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", "/tmp/mo-restart-colocated-STALE")
    monkeypatch.setenv("MINI_ORK_RUN_ID", "goalloop-parent-run")
    env = mod._env_overlay(str(tmp_path), "/run/w.log")
    assert "MINI_ORK_RUN_DIR" not in env
    assert "MINI_ORK_RUN_ID" not in env
    assert env["WORKER_LOG"] == "/run/w.log"  # scrub is surgical, log pin still lands


def test_env_overlay_scrubs_the_rest_of_the_goal_loop_run_identity(mod, monkeypatch, tmp_path):
    """The RUN_DIR/RUN_ID pair was only the first two of the goal-loop's run-scoped
    vars. The rest leak just as silently: MINI_ORK_WORKFLOW/MINI_ORK_PLAN_PATH would
    point a per-chapter child at the goal-loop's own plan, and MINI_ORK_TEST_CMD /
    MINI_ORK_TYPECHECK_CMD are pinned to `echo` upstream — inheriting those would
    neuter every gate the worker's children run."""
    _fake_worktree(tmp_path)
    leaked = {
        "MINI_ORK_RECIPE": "goal-loop",
        "MINI_ORK_RECIPE_ROOT": "/Volumes/docker-ssd/ps/mini-ork",
        "MINI_ORK_WORKFLOW": "/Volumes/docker-ssd/ps/mini-ork/recipes/goal-loop/workflow.yaml",
        "MINI_ORK_TASK_CLASS": "goal_loop",
        "MINI_ORK_PLAN_PATH": "/goalloop/plan.json",
        "MINI_ORK_PROFILE_PATH": "/goalloop/run_profile.json",
        "MINI_ORK_PROFILE_GATE": "0",
        "MINI_ORK_NODE_INPUT_DIR": "/goalloop/inputs/goal_apply",
        "MINI_ORK_NODE_INPUT_MANIFEST": "/goalloop/inputs.json",
        "MINI_ORK_TEST_CMD": "echo",
        "MINI_ORK_TYPECHECK_CMD": "echo",
        # The loop's own dispatch pin (read straight through as `--model`) and the
        # node that spawned this binding — both are per-run identity, not the
        # worker's.
        "MO_DISPATCH_CHAIN": "transform",
        "MO_NODE_ID": "goal_apply",
    }
    for key, value in leaked.items():
        monkeypatch.setenv(key, value)
    env = mod._env_overlay(str(tmp_path), "/run/w.log")
    assert [k for k in leaked if k in env] == []


def test_env_overlay_keeps_the_engine_and_db_pins(mod, monkeypatch, tmp_path):
    """Two leaks are deliberate and must NOT be scrubbed: ENGINE_ROOT/ROOT so the
    worker's children run THIS loop's (fixed) engine instead of the researcher
    home's older vendored copy, and DB so the goal-loop's cost circuit keeps seeing
    the spend it caused. A future reader must not 'finish the job' and blind the
    budget rail."""
    _fake_worktree(tmp_path)
    kept = {
        "MINI_ORK_ENGINE_ROOT": "/engine",
        "MINI_ORK_ROOT": "/engine",
        "MINI_ORK_DB": "/engine/.mini-ork/state.db",
    }
    for key, value in kept.items():
        monkeypatch.setenv(key, value)
    env = mod._env_overlay(str(tmp_path), "/run/w.log")
    assert {k: env[k] for k in kept} == kept


def test_env_overlay_drops_the_ambient_home_pair_when_a_home_is_pinned(mod, monkeypatch, tmp_path):
    """The live W9 failure: the goal-loop's MINI_ORK_PROJECT_HOME outranks the
    caller's MINI_ORK_HOME_DIR in the launcher, so the child wrote runs/<id>/
    verified-artifact.json into the GOAL-LOOP's .mini-ork while the caller read the
    worktree's and reported "verifier did not run" for a run that verified cleanly.
    Both ambient values must go, and PROJECT_HOME must come back pinned to the one
    home the caller also reads."""
    _fake_worktree(tmp_path)
    monkeypatch.setenv("MINI_ORK_PROJECT_HOME", "/Volumes/docker-ssd/ps/mini-ork/.mini-ork")
    monkeypatch.setenv("MINI_ORK_HOME", "/Volumes/docker-ssd/ps/mini-ork/.mini-ork")
    home = str(tmp_path / ".mini-ork")
    env = mod._env_overlay(
        str(tmp_path),
        "/run/w.log",
        {"MINI_ORK_HOME_DIR": home, "MINI_ORK_PROJECT_HOME": home, "MINI_ORK_HOME": home},
    )
    # Every home name the launcher consults must name the ONE resolved home; a
    # single surviving foreign value is enough to split runs/<id>/ from the reader.
    assert env["MINI_ORK_HOME_DIR"] == home
    assert env["MINI_ORK_PROJECT_HOME"] == home
    assert env["MINI_ORK_HOME"] == home


def test_env_overlay_keeps_the_ambient_home_when_nothing_replaces_it(mod, monkeypatch, tmp_path):
    """Drop the pair only alongside a replacement. With no home resolved there is
    nothing to pin, and popping would strand the launcher on its cwd/.mini-ork
    fallback — a worse answer than the inherited one."""
    _fake_worktree(tmp_path)
    monkeypatch.setenv("MINI_ORK_PROJECT_HOME", "/inherited/.mini-ork")
    env = mod._env_overlay(str(tmp_path), "/run/w.log")
    assert env["MINI_ORK_PROJECT_HOME"] == "/inherited/.mini-ork"


def test_start_pins_project_home_alongside_home_dir(mod, monkeypatch, tmp_path):
    """PROJECT_HOME is the variable the launcher actually consults first, so the
    start path must pin it — pinning only HOME_DIR leaves the bug live."""
    seen: dict[str, str] = {}

    def _capture(worktree, log_path, extra=None):
        seen.update(extra or {})
        return {}

    monkeypatch.setattr(mod, "_env_overlay", _capture)
    monkeypatch.setattr(mod, "_runner_model", lambda wt: (None, "no runner"))
    monkeypatch.setattr(mod, "_mini_ork_home", lambda wt: ("/pinned/.mini-ork", "pinned"))
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 1})())
    _fake_worktree(tmp_path)
    ok, _ = mod._start_replacement(str(tmp_path), "book-generation", str(tmp_path / "w.log"))
    assert ok is True
    assert seen["MINI_ORK_HOME_DIR"] == "/pinned/.mini-ork"
    assert seen["MINI_ORK_PROJECT_HOME"] == "/pinned/.mini-ork"


class _FakeReadyz:
    """A context-manager stand-in for urlopen's response."""

    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        import json

        return json.dumps(self._payload).encode()


def _write_runner_env(root, url="http://100.74.239.22:7910", token="x" * 40):
    (root / "server").mkdir(exist_ok=True)
    (root / "server" / ".env").write_text(
        f"CHAPTER_MICROVM_RUNNER_URL={url}\nCHAPTER_MICROVM_RUNNER_TOKEN={token}\n"
    )


def test_runner_model_aligns_from_readyz(mod, monkeypatch, tmp_path):
    """A ready runner's advertised model is what we pin the worker to."""
    _write_runner_env(tmp_path)
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        return _FakeReadyz({"status": "ready", "model": "glm-5.3"})

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    model, why = mod._runner_model(str(tmp_path))
    assert model == "glm-5.3" and "glm-5.3" in why
    assert seen["url"] == "http://100.74.239.22:7910/readyz"  # trailing slash stripped, /readyz joined
    assert seen["auth"] == "Bearer " + "x" * 40


def test_runner_model_absent_keys_never_touches_network(mod, monkeypatch, tmp_path):
    """No runner config in the worktree .env -> no probe, model left as-is."""
    _fake_worktree(tmp_path)  # writes server/.env WITHOUT runner keys

    def boom(*a, **k):
        raise AssertionError("must not probe the network without runner config")

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    model, why = mod._runner_model(str(tmp_path))
    assert model is None and "absent" in why


def test_runner_model_probe_failure_is_best_effort(mod, monkeypatch, tmp_path):
    """An unreachable/mis-responding runner degrades to None, never raises."""
    _write_runner_env(tmp_path)
    monkeypatch.setattr(
        mod.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("conn refused"))
    )
    model, why = mod._runner_model(str(tmp_path))
    assert model is None and "probe failed" in why


def test_runner_model_not_ready_is_none(mod, monkeypatch, tmp_path):
    _write_runner_env(tmp_path)
    monkeypatch.setattr(
        mod.urllib.request, "urlopen", lambda *a, **k: _FakeReadyz({"status": "degraded", "model": "glm-5.3"})
    )
    model, why = mod._runner_model(str(tmp_path))
    assert model is None and "not ready" in why


def test_start_pins_runner_model_into_worker_env(mod, monkeypatch, tmp_path):
    """The resolved runner model reaches the spawned worker's process env."""
    wd = _fake_worktree(tmp_path)
    monkeypatch.setattr(mod, "_runner_model", lambda w: ("glm-5.3", "pinned … glm-5.3"))
    monkeypatch.setattr(mod, "_mini_ork_home", lambda w: (None, "unset"))  # isolate: focus on model pin
    seen: dict = {}

    class FakePopen:
        def __init__(self, argv, cwd, stdout, stderr, stdin, start_new_session, env):
            seen.update(argv=argv, env=env)
            self.pid = 4321

    monkeypatch.setattr(mod.subprocess, "Popen", FakePopen)
    ok, why = mod._start_replacement(str(tmp_path), "book-generation", str(tmp_path / "w.log"))
    assert ok is True and "glm-5.3" in why
    assert seen["argv"] == ["bash", str(wd), "book-generation"]
    assert seen["env"]["CHAPTER_PRIMARY_MODEL"] == "glm-5.3"  # aligned to the live runner
    assert "MINI_ORK_HOME_DIR" not in seen["env"]  # home pin independent of model pin


def test_start_without_runner_leaves_model_unset(mod, monkeypatch, tmp_path):
    """No runner alignment -> we don't inject a CHAPTER_PRIMARY_MODEL (worker's own env wins)."""
    _fake_worktree(tmp_path)
    monkeypatch.setattr(mod, "_runner_model", lambda w: (None, "left CHAPTER_PRIMARY_MODEL as-is"))
    monkeypatch.setattr(mod, "_mini_ork_home", lambda w: (None, "unset"))  # isolate: neither pin fires
    seen: dict = {}

    class FakePopen:
        def __init__(self, argv, cwd, stdout, stderr, stdin, start_new_session, env):
            seen.update(argv=argv, env=env)
            self.pid = 4321

    monkeypatch.setattr(mod.subprocess, "Popen", FakePopen)
    ok, _why = mod._start_replacement(str(tmp_path), "book-generation", str(tmp_path / "w.log"))
    assert ok is True
    assert "CHAPTER_PRIMARY_MODEL" not in seen["env"]  # untouched, not force-defaulted
    assert "MINI_ORK_HOME_DIR" not in seen["env"]  # untouched, not force-defaulted


def _make_vendored(home_root):
    """Create an executable `<home_root>/.mini-ork/bin/mini-ork` and return its home."""
    binp = home_root / ".mini-ork" / "bin" / "mini-ork"
    binp.parent.mkdir(parents=True, exist_ok=True)
    binp.write_text("#!/bin/sh\n")
    binp.chmod(0o755)
    return home_root / ".mini-ork"


def test_has_vendored_binary_requires_executable(mod, tmp_path):
    home = tmp_path / ".mini-ork"
    (home / "bin").mkdir(parents=True)
    binp = home / "bin" / "mini-ork"
    binp.write_text("#!/bin/sh\n")
    assert mod._has_vendored_binary(str(home)) is False  # present but not +x
    binp.chmod(0o755)
    assert mod._has_vendored_binary(str(home)) is True
    assert mod._has_vendored_binary("") is False  # empty home never matches


def test_mini_ork_home_prefers_operator_override(mod, monkeypatch, tmp_path):
    """An operator-set MINI_ORK_HOME_DIR is never clobbered (it is already inherited)."""
    monkeypatch.setenv("MINI_ORK_HOME_DIR", "/opt/custom/.mini-ork")
    home, why = mod._mini_ork_home(str(tmp_path))
    assert home is None and "operator override" in why


def test_mini_ork_home_uses_worktree_vendored_when_present(mod, monkeypatch, tmp_path):
    """If the worktree has its own (genuine, non-co-located) vendored copy, no pin is
    needed and the self-heal never clobbers it (default resolves)."""
    monkeypatch.delenv("MINI_ORK_HOME_DIR", raising=False)
    # Pin primary at a non-existent path so the self-heal deterministically skips
    # (no dependency on the real researcher checkout) and we isolate the
    # 'worktree carries its own' branch.
    monkeypatch.setenv("MO_RESEARCHER_DIR", str(tmp_path / "no-primary"))
    _make_vendored(tmp_path)  # <worktree>/.mini-ork/bin/mini-ork executable, no engine pointer
    home, why = mod._mini_ork_home(str(tmp_path))
    assert home is None and "worktree carries its own" in why


def test_mini_ork_home_falls_back_to_primary(mod, monkeypatch, tmp_path):
    """Worktree lacks .mini-ork -> pin to the primary checkout's proven runtime."""
    monkeypatch.delenv("MINI_ORK_HOME_DIR", raising=False)
    wt = tmp_path / "wt"
    wt.mkdir()  # no .mini-ork here
    primary = tmp_path / "primary"
    primary.mkdir()
    _make_vendored(primary)
    monkeypatch.setenv("MO_RESEARCHER_DIR", str(primary))
    home, why = mod._mini_ork_home(str(wt))
    assert home == str(primary / ".mini-ork")
    assert "primary vendored runtime" in why


def test_mini_ork_home_unset_when_nothing_found(mod, monkeypatch, tmp_path):
    """Neither worktree nor primary has a vendored binary -> leave it unset (honest miss)."""
    monkeypatch.delenv("MINI_ORK_HOME_DIR", raising=False)
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setenv("MO_RESEARCHER_DIR", str(tmp_path / "no-primary"))
    home, why = mod._mini_ork_home(str(wt))
    assert home is None and "unset" in why


def test_start_pins_mini_ork_home_into_worker_env(mod, monkeypatch, tmp_path):
    """The resolved vendored-runtime home reaches the spawned worker's process env."""
    wd = _fake_worktree(tmp_path)
    monkeypatch.setattr(mod, "_runner_model", lambda w: (None, "left CHAPTER_PRIMARY_MODEL as-is"))
    monkeypatch.setattr(mod, "_mini_ork_home", lambda w: ("/primary/.mini-ork", "pinned MINI_ORK_HOME_DIR to primary"))
    seen: dict = {}

    class FakePopen:
        def __init__(self, argv, cwd, stdout, stderr, stdin, start_new_session, env):
            seen.update(argv=argv, env=env)
            self.pid = 4321

    monkeypatch.setattr(mod.subprocess, "Popen", FakePopen)
    ok, why = mod._start_replacement(str(tmp_path), "book-generation", str(tmp_path / "w.log"))
    assert ok is True and "MINI_ORK_HOME_DIR" in why
    assert seen["argv"] == ["bash", str(wd), "book-generation"]
    assert seen["env"]["MINI_ORK_HOME_DIR"] == "/primary/.mini-ork"  # DAG shell-out nodes can now resolve mini-ork
    assert "CHAPTER_PRIMARY_MODEL" not in seen["env"]  # model pin independent of home pin


# ── self-heal: co-located `.mini-ork` build (capability 1) ──────────────────────
_OVERLAY_REL = "../../server/resources/miniork-overlay-recipes"


def _make_primary_home(root):
    """A realistic primary `.mini-ork`: an executable bin/mini-ork, a `.venv` +
    `config` + `.git` at top-level (to prove .git is excluded and the rest are
    borrowed), a `state.db` file, and a `recipes/` dir carrying BOTH kinds of
    entry — a base recipe (real dir) and relative overlay symlinks into
    ../../server/resources/miniork-overlay-recipes (the exact shape the guard
    early-returns on when home == worktree). Returns the home path."""
    home = root / ".mini-ork"
    (home / "bin").mkdir(parents=True)
    binp = home / "bin" / "mini-ork"
    binp.write_text("#!/bin/sh\n")
    binp.chmod(0o755)
    (home / ".venv" / "bin").mkdir(parents=True)
    (home / "config").mkdir()
    (home / ".git").mkdir()  # must be EXCLUDED from the farm
    (home / "state.db").write_text("")  # a borrowed live-state file
    recipes = home / "recipes"
    recipes.mkdir()
    (recipes / "code-fix").mkdir()  # a base recipe: real dir -> absolute symlink
    (recipes / "verified-artifact").symlink_to(f"{_OVERLAY_REL}/verified-artifact")  # overlay -> verbatim
    (recipes / "__researcher-overlay").symlink_to(_OVERLAY_REL)  # dir-level overlay -> verbatim
    return home


def test_is_colocated_home_reads_engine_pointer(mod, tmp_path):
    """The co-located signature is the engine pointer FILE naming the primary home —
    what tells our farmed home apart from a genuine vendored one."""
    home = tmp_path / ".mini-ork"
    home.mkdir()
    primary = "/Volumes/x/researcher/.mini-ork"
    assert mod._is_colocated_home(str(home), primary) is False  # no engine pointer yet
    (home / "engine").write_text(primary + "\n")
    assert mod._is_colocated_home(str(home), primary) is True
    (home / "engine").write_text("/some/other/home\n")
    assert mod._is_colocated_home(str(home), primary) is False  # points elsewhere


def test_ensure_colocated_home_builds_full_structure(mod, tmp_path):
    """Fresh worktree + a real primary home -> a co-located `.mini-ork` whose home ==
    worktree, engine points at primary, base recipes are absolute symlinks and
    overlay recipes are copied VERBATIM (so they re-resolve inside the worktree)."""
    primary = _make_primary_home(tmp_path / "primary")
    wt = tmp_path / "wt"
    wt.mkdir()
    home, why = mod._ensure_colocated_home(str(wt), str(primary))
    assert home == str(wt / ".mini-ork") and "built co-located" in why

    farm = wt / ".mini-ork"
    # top-level: borrowed by symlink, EXCEPT recipes/engine/.git
    assert (farm / "bin").is_symlink() and (farm / "bin").resolve() == (primary / "bin").resolve()
    assert (farm / ".venv").is_symlink()
    assert (farm / "config").is_symlink()
    assert (farm / "state.db").is_symlink()
    assert not (farm / ".git").exists()  # .git is never farmed
    # engine: a real pointer FILE naming the primary home (venv stays on real path)
    assert not (farm / "engine").is_symlink()
    assert (farm / "engine").read_text().strip() == str(primary)
    # recipes: a REAL dir, not a symlink
    assert (farm / "recipes").is_dir() and not (farm / "recipes").is_symlink()
    # base recipe -> absolute symlink into the primary
    cf = farm / "recipes" / "code-fix"
    assert cf.is_symlink() and cf.resolve() == (primary / "recipes" / "code-fix").resolve()
    # overlay recipe -> link string copied VERBATIM (relative, co-locates in worktree)
    import os as _os

    va = farm / "recipes" / "verified-artifact"
    assert va.is_symlink()
    assert _os.readlink(str(va)) == f"{_OVERLAY_REL}/verified-artifact"  # byte-for-byte
    ov = farm / "recipes" / "__researcher-overlay"
    assert _os.readlink(str(ov)) == _OVERLAY_REL
    # the co-located home carries a resolvable binary (the DAG's X_OK check clears)
    assert mod._has_vendored_binary(str(farm)) is True


def test_ensure_colocated_home_overlay_relink_targets_worktree(mod, tmp_path):
    """The whole point: an overlay link inside the farmed home resolves to THIS
    worktree's server/resources tree — the invariant that makes the overlay guard a
    no-op (home == worktree) instead of an EEXIST against a foreign home."""
    primary = _make_primary_home(tmp_path / "primary")
    wt = tmp_path / "wt"
    # the worktree's real overlay target the relative link must land on
    (wt / "server" / "resources" / "miniork-overlay-recipes" / "verified-artifact").mkdir(parents=True)
    home, _why = mod._ensure_colocated_home(str(wt), str(primary))
    va = Path(home) / "recipes" / "verified-artifact"
    # realpath of the farmed overlay link == the worktree's own overlay dir
    assert va.resolve() == (wt / "server" / "resources" / "miniork-overlay-recipes" / "verified-artifact").resolve()


def test_ensure_colocated_home_idempotent_reuse(mod, tmp_path):
    """A second call reuses the existing farmed home (signature match) — no rebuild,
    no clobber."""
    primary = _make_primary_home(tmp_path / "primary")
    wt = tmp_path / "wt"
    wt.mkdir()
    home1, why1 = mod._ensure_colocated_home(str(wt), str(primary))
    assert "built co-located" in why1
    # drop a sentinel so we can prove the dir wasn't rebuilt
    sentinel = Path(home1) / "recipes" / ".sentinel"
    sentinel.write_text("keep")
    home2, why2 = mod._ensure_colocated_home(str(wt), str(primary))
    assert home2 == home1 and "reused" in why2
    assert sentinel.exists()  # untouched — not rebuilt


def test_ensure_colocated_home_never_clobbers_genuine_vendored(mod, tmp_path):
    """A worktree's OWN genuine vendored `.mini-ork` (no engine pointer) is left
    untouched — returns None so the caller falls back to it."""
    primary = _make_primary_home(tmp_path / "primary")
    wt = tmp_path / "wt"
    genuine = _make_vendored(wt)  # <wt>/.mini-ork/bin/mini-ork, NO engine pointer
    marker = genuine / "OWN"
    marker.write_text("x")
    home, why = mod._ensure_colocated_home(str(wt), str(primary))
    assert home is None and "non-co-located" in why
    assert marker.exists()  # genuine home untouched


def test_ensure_colocated_home_primary_missing_is_none(mod, tmp_path):
    """No primary home to borrow from -> honest None, nothing built."""
    wt = tmp_path / "wt"
    wt.mkdir()
    home, why = mod._ensure_colocated_home(str(wt), str(tmp_path / "absent" / ".mini-ork"))
    assert home is None and "primary vendored home missing" in why
    assert not (wt / ".mini-ork").exists()  # nothing left behind


def test_ensure_colocated_home_primary_lacks_recipes_is_none(mod, tmp_path):
    """A primary home without a recipes/ dir can't seed the farm -> None, no partial."""
    primary = tmp_path / "primary" / ".mini-ork"
    (primary / "bin").mkdir(parents=True)
    (primary / "bin" / "mini-ork").write_text("#!/bin/sh\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    home, why = mod._ensure_colocated_home(str(wt), str(primary))
    assert home is None and "recipes dir missing" in why
    assert not (wt / ".mini-ork").exists()


def test_mini_ork_home_self_heals_to_colocated(mod, monkeypatch, tmp_path):
    """End-to-end: an empty worktree + a real primary -> _mini_ork_home returns the
    CO-LOCATED home (home == worktree), the strictly-better pin that no-ops the
    overlay guard. This is the self-heal the loop now performs on its own."""
    monkeypatch.delenv("MINI_ORK_HOME_DIR", raising=False)
    primary_root = tmp_path / "primary"
    _make_primary_home(primary_root)
    monkeypatch.setenv("MO_RESEARCHER_DIR", str(primary_root))
    wt = tmp_path / "wt"
    wt.mkdir()
    home, why = mod._mini_ork_home(str(wt))
    assert home == str(wt / ".mini-ork")  # the worktree's OWN co-located home, not the primary
    assert "built co-located" in why


def test_start_pins_colocated_home_into_worker_env(mod, monkeypatch, tmp_path):
    """The self-healed co-located home flows into the spawned worker's env as
    MINI_ORK_HOME_DIR — home == worktree, so the first chapter clears BOTH the
    missing-binary AND the overlay-EEXIST guards without a human farming it."""
    # Side-effect only: plants the worktree (scripts/watchdog + node_modules/tsx
    # + server/.env) *at* tmp_path, which is the workdir passed to the spawner.
    _fake_worktree(tmp_path)
    primary_root = tmp_path / "primary"
    _make_primary_home(primary_root)
    monkeypatch.setenv("MO_RESEARCHER_DIR", str(primary_root))
    monkeypatch.delenv("MINI_ORK_HOME_DIR", raising=False)
    monkeypatch.setattr(mod, "_runner_model", lambda w: (None, "left CHAPTER_PRIMARY_MODEL as-is"))
    seen: dict = {}

    class FakePopen:
        def __init__(self, argv, cwd, stdout, stderr, stdin, start_new_session, env):
            seen.update(argv=argv, env=env)
            self.pid = 4321

    monkeypatch.setattr(mod.subprocess, "Popen", FakePopen)
    ok, why = mod._start_replacement(str(tmp_path), "book-generation", str(tmp_path / "w.log"))
    assert ok is True
    assert seen["env"]["MINI_ORK_HOME_DIR"] == str(tmp_path / ".mini-ork")  # co-located, == worktree
    assert "built co-located" in why
