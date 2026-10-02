"""Unit tests for ``mini_ork.remote.node_agent`` (epic 05).

The Review bar (kickoff lines 139-153) demands one test per Acceptance
bullet that exercises the production path (``TestClient(create_app(...))``,
not only the bare module). Every Docker invocation flows through a
fake ``_run`` and every detached proc flows through a fake ``_spawn``,
so the suite has no daemon requirement and runs in the default lane.

The daemon-gated live test (``test_real_container_session``) checks
colima and ``/Volumes/docker-ssd`` bind-mount visibility; on hosts
without those it skips with a WHY in the docstring, not a bare
``# noqa``.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
from fastapi.testclient import TestClient  # noqa: E402

from mini_ork.remote.node_agent.app import create_app  # noqa: E402
from mini_ork.cli.node_agent import (  # noqa: E402
    _parse,
    main as cli_main,
    uvicorn_argv,
    validate_bind,
)


TOKEN_ENV = "MO_TEST_NODE_TOKEN"
TOKEN_VALUE = "supersecret-token-xyz"
AUTH = f"Bearer {TOKEN_VALUE}"


# ---------------------------------------------------------------------------
# fake Docker runner + fake proc spawn — the single test seam pattern from
# sandbox_reaper.py:46-59. Every Docker / git invocation flows through
# ``fake_run``; every detached child flows through ``fake_spawn``.
# ---------------------------------------------------------------------------


class _FakeCompleted:
    returncode = 0
    stdout = ""
    stderr = ""


def _fake_docker_run(argv, *, records=None):
    """Mimic ``docker run -d …`` by returning a fresh container id."""
    r = _FakeCompleted()
    if "ps" in argv and "--filter" in argv:
        r.stdout = ""  # no leaked containers
        return r
    if "info" in argv:
        r.stdout = "fake"
        return r
    if "rm" in argv and "-f" in argv:
        return r
    # ``docker run -d --name X ...``
    if argv[:2] == ["docker", "run"]:
        name_idx = argv.index("--name") + 1
        cid_name = argv[name_idx]
        if records is not None:
            records.setdefault("containers", {})[cid_name] = "running"
        r.stdout = cid_name
        return r
    r.stdout = ""
    return r


def _fake_spawn(argv, *, stdin="", env=None, cwd=None):
    """Drive a real subprocess with /bin/echo so the watcher thread sees
    real bytes land on the .out / .err files. Tests can inject argv with
    ``/bin/sh -c "echo hi; sleep 0.2; echo bye"`` to validate offset
    reconnect."""
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace",
        env=env or {}, cwd=cwd,
        start_new_session=True,
    )
    if stdin:
        try:
            proc.stdin.write(stdin)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
    return proc


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    d = tmp_path / "node-agent-state"
    d.mkdir()
    return d


@pytest.fixture
def client(state_dir):
    app = create_app(
        state_dir=state_dir,
        token_env=TOKEN_ENV,
        runtime="host",  # host runtime so we don't need a daemon
        _run_fn=lambda argv: _fake_docker_run(argv),
        _spawn_fn=_fake_spawn,
    )
    return TestClient(app)


def _create_session(client, run_id=None, image="alpine:latest"):
    payload = {"run_id": run_id or f"run-{uuid.uuid4().hex[:6]}", "image": image}
    r = client.post("/v1/sessions", json=payload, headers={"Authorization": AUTH})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. Missing or wrong token → 401 on every non-health route.
# ---------------------------------------------------------------------------


def test_health_does_not_require_token(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body and "engine_shas" in body


def test_missing_token_rejected_on_non_health_routes(client):
    s = _create_session(client)
    sid = s["sid"]
    for method, path, kwargs in [
        ("post", "/v1/sessions", {"json": {"run_id": "x", "image": "y"}}),
        ("delete", f"/v1/sessions/{sid}", {}),
        ("get", "/v1/sessions/x/procs/1", {}),
    ]:
        # no Authorization header
        fn = getattr(client, method)
        r = fn(path, **kwargs)
        assert r.status_code == 401, f"{method} {path} -> {r.status_code}"


def test_wrong_token_rejected(client):
    r = client.post(
        "/v1/sessions",
        json={"run_id": "x", "image": "y"},
        headers={"Authorization": "Bearer not-the-token"},
    )
    assert r.status_code == 401


def test_empty_token_env_closes_routes(state_dir, monkeypatch):
    """A token env that exists but is empty MUST refuse every non-health request."""
    monkeypatch.setenv(TOKEN_ENV, "")
    app = create_app(state_dir=state_dir, token_env=TOKEN_ENV, runtime="host",
                     _run_fn=lambda a: _FakeCompleted(),
                     _spawn_fn=_fake_spawn)
    with TestClient(app) as c:
        r = c.post("/v1/sessions", json={"run_id": "x", "image": "y"},
                   headers={"Authorization": "Bearer anything"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# 2. Detached-proc lifecycle: stream from offset 0, reconnect with the last
#    offset (no dup, no loss), final rc.
# ---------------------------------------------------------------------------


def _start_proc(client, run_id, *, argv, timeout_s=None, env_keys=None):
    payload = {"argv": argv, "env_keys": env_keys or [], "cwd": None,
               "stdin": "", "timeout_s": timeout_s}
    r = client.post(f"/v1/sessions/{run_id}/procs", json=payload,
                    headers={"Authorization": AUTH})
    assert r.status_code == 200, r.text
    return r.json()["pid"]


def test_proc_lifecycle_stream_then_reconnect_no_dup_or_loss(client):
    sess = _create_session(client)
    run_id = sess["run_id"]
    # host runtime — argv runs DIRECTLY on the test host. ``sh -c`` with
    # a tiny gap so the streaming generator can yield mid-run.
    argv = ["sh", "-c", "echo line-one; sleep 0.2; echo line-two"]
    pid = _start_proc(client, run_id, argv=argv)

    # First read from offset 0 — must include both lines.
    r1 = client.get(f"/v1/sessions/{run_id}/procs/{pid}/stream?out=0&err=0",
                    headers={"Authorization": AUTH})
    assert r1.status_code == 200
    body1 = r1.text
    assert "line-one" in body1
    # The stream endpoint emits the final exit record; the offset it
    # reports is the cumulative position.
    last_out = 0
    final_rc = None
    for line in body1.splitlines():
        rec = json.loads(line)
        if rec.get("stream") == "out":
            last_out = rec["offset"]
        if rec.get("stream") == "exit":
            final_rc = rec["rc"]
    assert final_rc == 0
    assert "line-two" in body1  # full lifecycle returned

    # Reconnect from the last offset — must be empty (no dup) and
    # terminate with another exit record.
    r2 = client.get(f"/v1/sessions/{run_id}/procs/{pid}/stream?out={last_out}&err=0",
                    headers={"Authorization": AUTH})
    body2 = r2.text
    assert "line-one" not in body2
    assert "line-two" not in body2


# ---------------------------------------------------------------------------
# 3. Timeout → rc=124 and the process group is killed. Kill → state killed.
# ---------------------------------------------------------------------------


def test_proc_timeout_records_rc_124_and_kills_group(client):
    sess = _create_session(client)
    run_id = sess["run_id"]
    # Long-running sleep; timeout=0.2 should fire and the registry records
    # rc=124 with state ``killed`` (per A.1 contract).
    argv = ["sh", "-c", "sleep 30"]
    pid = _start_proc(client, run_id, argv=argv, timeout_s=0.2)

    # Give the watcher a moment to fire the timeout.
    deadline = time.time() + 5
    final_state = None
    final_rc = None
    while time.time() < deadline:
        r = client.get(f"/v1/sessions/{run_id}/procs/{pid}",
                       headers={"Authorization": AUTH})
        body = r.json()
        if body["state"] in ("killed", "timeout", "exited"):
            final_state = body["state"]
            final_rc = body["rc"]
            break
        time.sleep(0.05)
    assert final_rc == 124, f"expected rc=124, got {final_rc!r} (state={final_state})"


def test_proc_explicit_kill_sets_state_killed(client):
    sess = _create_session(client)
    run_id = sess["run_id"]
    argv = ["sh", "-c", "sleep 30"]
    pid = _start_proc(client, run_id, argv=argv, timeout_s=60)
    r = client.post(f"/v1/sessions/{run_id}/procs/{pid}/kill",
                    headers={"Authorization": AUTH})
    assert r.status_code == 200
    # Eventually state == killed and rc reflects the SIGKILL exit.
    deadline = time.time() + 5
    while time.time() < deadline:
        r = client.get(f"/v1/sessions/{run_id}/procs/{pid}",
                       headers={"Authorization": AUTH})
        body = r.json()
        if body["state"] in ("killed", "exited"):
            assert body["state"] == "killed"
            return
        time.sleep(0.05)
    raise AssertionError(f"proc did not transition to killed: {body}")


# ---------------------------------------------------------------------------
# 4. After the server object restarts, the proc registry is rebuilt from disk.
# ---------------------------------------------------------------------------


def test_proc_registry_rebuilt_after_restart(state_dir):
    """Create a session + proc on app#1, throw the app away, recreate
    app#2 against the same state_dir, and confirm the state file is
    reloaded from ``.procs/<pid>.json``."""
    def make():
        return create_app(state_dir=state_dir, token_env=TOKEN_ENV,
                          runtime="host", retain_hours=24,
                          _run_fn=lambda a: _FakeCompleted(),
                          _spawn_fn=_fake_spawn)
    app1 = make()
    with TestClient(app1) as c1:
        sess = _create_session(c1)
        run_id = sess["run_id"]
        # Use a real quick proc; the registry will write <pid>.json on disk.
        argv = ["sh", "-c", "echo survived-restart"]
        pid = _start_proc(c1, run_id, argv=argv)
        deadline = time.time() + 5
        while time.time() < deadline:
            r = c1.get(f"/v1/sessions/{run_id}/procs/{pid}",
                       headers={"Authorization": AUTH})
            if r.json()["state"] in ("exited", "killed", "timeout"):
                break
            time.sleep(0.05)

    # Drop the first app, recreate a second against the SAME state dir.
    app2 = make()
    with TestClient(app2) as c2:
        r = c2.get(f"/v1/sessions/{run_id}/procs/{pid}",
                   headers={"Authorization": AUTH})
        assert r.status_code == 200, r.text
        body = r.json()
        # The proc entry was loaded from disk; state should reflect the
        # terminal state captured before app#1 exited.
        assert body["state"] in ("exited", "killed", "timeout", "orphaned")
        assert body["pid"] == pid


# ---------------------------------------------------------------------------
# 5. Tar extraction rejects ../escape and absolute members.
# ---------------------------------------------------------------------------


def _tar_with_member(name: str, data: bytes = b"x") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_tar_rejects_absolute_path(client):
    sess = _create_session(client)
    run_id = sess["run_id"]
    body = _tar_with_member("/etc/passwd_evil")
    r = client.put(f"/v1/sessions/{run_id}/files?root=run",
                   headers={"Authorization": AUTH}, content=body)
    assert r.status_code == 400, r.text


def test_tar_rejects_dotdot_escape(client):
    sess = _create_session(client)
    run_id = sess["run_id"]
    body = _tar_with_member("../escape.txt")
    r = client.put(f"/v1/sessions/{run_id}/files?root=run",
                   headers={"Authorization": AUTH}, content=body)
    assert r.status_code == 400, r.text


def test_tar_accepts_safe_member_and_manifest_round_trip(client):
    sess = _create_session(client)
    run_id = sess["run_id"]
    body = _tar_with_member("hello.txt", b"hi")
    r = client.put(f"/v1/sessions/{run_id}/files?root=run",
                   headers={"Authorization": AUTH}, content=body)
    assert r.status_code == 200, r.text
    m = client.post(f"/v1/sessions/{run_id}/files/manifest?root=run",
                    headers={"Authorization": AUTH})
    assert m.status_code == 200
    manifest = m.json()["manifest"]
    assert "hello.txt" in manifest
    # sha256("hi") is well known
    import hashlib
    assert manifest["hello.txt"] == hashlib.sha256(b"hi").hexdigest()


# ---------------------------------------------------------------------------
# 6. Bind of 0.0.0.0 without TLS refuses to start.
# ---------------------------------------------------------------------------


def test_bind_0_0_0_0_without_tls_refuses():
    rc = cli_main(["--bind", "0.0.0.0"], _exec=True)
    assert rc == 2


def test_bind_loopback_allowed_without_tls():
    # should not raise
    validate_bind("127.0.0.1", None, None)
    validate_bind("100.64.0.1", None, None)  # tailnet
    # And the launcher as a whole is happy with a loopback bind. _exec=False:
    # with _exec=True main() os.execvp()s uvicorn, REPLACING the pytest process
    # with a server that never exits (it hung the gate and orphaned a node-agent).
    rc = cli_main(["--bind", "127.0.0.1", "--port", "7091"], _exec=False)
    assert rc == 0


def test_bind_non_loopback_with_tls_succeeds():
    args = _parse(["--bind", "0.0.0.0", "--tls-cert", "/tmp/c.pem",
                   "--tls-key", "/tmp/k.pem"])
    argv = uvicorn_argv(args)
    assert "--ssl-certfile" in argv
    assert "--ssl-keyfile" in argv


# ---------------------------------------------------------------------------
# 7. Subcommand registration — the exact-set guard.
# ---------------------------------------------------------------------------


def test_node_agent_subcommand_in_registry():
    from mini_ork.cli.main import _NATIVE_MODULE_SUBS, SUBCOMMAND_REGISTRY
    assert "node-agent" in _NATIVE_MODULE_SUBS
    assert _NATIVE_MODULE_SUBS["node-agent"] == "mini_ork.cli.node_agent"
    assert "node-agent" in SUBCOMMAND_REGISTRY


def test_node_agent_cli_help_runs():
    """The CLI parses --help without raising (argparse handles exit)."""
    rc = cli_main(["--help"], _exec=True)
    # argparse parses --help via SystemExit → main catches it and returns 0
    assert rc == 0


# ---------------------------------------------------------------------------
# 8. Daemon-gated live test (skipped cleanly when docker / bind-visibility
#    is absent, with the WHY in this docstring, not a bare skip).
# ---------------------------------------------------------------------------


def test_real_container_session():
    """End-to-end against a real ``docker run`` session.

    Skipped when the local docker CLI is missing, the daemon is not
    reachable, or /Volumes/docker-ssd is not bind-visible (colima
    bind-mounts only that path and /Volumes/ssd-2 — see MEMORY
    feedback_colima_bind_mount_var_folders). The skip message names
    the gap so the reviewer sees WHY, not just ``# skipped``.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not on PATH; daemon-gated live test cannot run")
    info = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                          capture_output=True, text=True, check=False)
    if info.returncode != 0:
        pytest.skip(f"docker daemon unreachable: {info.stderr.strip() or info.stdout.strip()}")
    if not os.path.isdir("/Volumes/docker-ssd"):
        pytest.skip("/Volumes/docker-ssd not bind-visible; colima cannot mount here")
    # State dir on a bind-visible volume (colima shares /Volumes/docker-ssd,
    # not /tmp), so the session's /workspace mounts are REAL host dirs and the
    # write-through below proves the bind mounts, not just exec + streaming.
    repo_root = Path(__file__).resolve().parents[2]
    state_dir = Path(tempfile.mkdtemp(prefix=".mo-bindprobe-", dir=str(repo_root)))
    app = create_app(state_dir=state_dir, token_env=TOKEN_ENV, runtime="docker",
                     engine_root=state_dir, _spawn_fn=None)
    os.environ[TOKEN_ENV] = TOKEN_VALUE
    run_id = None
    try:
        with TestClient(app) as c:
            s = _create_session(c)
            run_id = s["run_id"]
            # env + cwd go through a real `docker exec`: the value must reach the
            # in-container process, the relative write must land in the cwd.
            payload = {"argv": ["sh", "-c",
                                'echo hi; pwd; echo "probe=$MO_PROBE"; '
                                'echo from-container > proof.txt; echo bye'],
                       "env": {"MO_PROBE": "probe-value-42"},
                       "cwd": "/workspace/target"}
            r = c.post(f"/v1/sessions/{run_id}/procs", json=payload,
                       headers={"Authorization": AUTH})
            assert r.status_code == 200, r.text
            pid = r.json()["pid"]
            stream = c.get(f"/v1/sessions/{run_id}/procs/{pid}/stream?out=0&err=0",
                           headers={"Authorization": AUTH})
            assert "hi" in stream.text and "bye" in stream.text
            assert "/workspace/target" in stream.text
            assert "probe=probe-value-42" in stream.text
            proof = state_dir / "runs" / run_id / "target" / "proof.txt"
            assert proof.read_text().strip() == "from-container"
            record = json.loads((state_dir / "runs" / run_id / ".procs" / f"{pid}.json").read_text())
            assert "MO_PROBE" in record["env_keys"]
            assert "probe-value-42" not in json.dumps(record), "an env VALUE reached disk"
            r = c.delete(f"/v1/sessions/{run_id}", headers={"Authorization": AUTH})
            assert r.status_code == 200, r.text
        left = subprocess.run(["docker", "ps", "-aq", "--filter", "label=mo.sandbox=1",
                               "--filter", f"label=mo.run_id={run_id}"],
                              capture_output=True, text=True).stdout.strip()
        assert not left, f"DELETE /v1/sessions left container(s) behind: {left}"
    finally:
        if run_id:  # never leak a container, even when an assertion above failed
            ids = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=mo.run_id={run_id}"],
                                 capture_output=True, text=True).stdout.split()
            if ids:
                subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)
        shutil.rmtree(state_dir, ignore_errors=True)



# ---------------------------------------------------------------------------
# 9. Review fixes: env/cwd reach the child, restart rediscovery, DELETE by run.
# ---------------------------------------------------------------------------


def test_docker_exec_argv_forwards_env_keys_and_cwd_without_values(tmp_path):
    from mini_ork.remote.node_agent.procs import ProcRegistry, ProcSpec
    seen = {}

    def spawn(argv, *, stdin, env, cwd):
        seen.update(argv=argv, env=env, cwd=cwd)
        return _fake_spawn(["true"], stdin=stdin, env=env, cwd=None)

    reg = ProcRegistry(tmp_path, "r1", runtime="docker", session_cid="cid123", _spawn_fn=spawn)
    reg.spawn(ProcSpec(argv=["claude", "--print"], env_keys=[], cwd="/workspace/target",
                       stdin="", timeout_s=5, env={"ANTHROPIC_AUTH_TOKEN": "sk-secret"}))
    argv = seen["argv"]
    assert argv[:6] == ["docker", "exec", "-i", "-w", "/workspace/target", "-e"]
    assert "ANTHROPIC_AUTH_TOKEN" in argv and "sk-secret" not in " ".join(argv)
    assert seen["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-secret"   # read by `-e KEY`
    assert seen["cwd"] is None                                   # host side: no container path


def test_restart_rediscovers_by_os_pid_not_sequence_number(tmp_path):
    from mini_ork.remote.node_agent.procs import ProcRegistry
    procs = tmp_path / "runs" / "r2" / ".procs"
    procs.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    for pid, os_pid in ((1, os.getpid()), (2, dead.pid)):
        (procs / f"{pid}.json").write_text(json.dumps(
            {"pid": pid, "state": "running", "argv": ["x"], "env_keys": [], "os_pid": os_pid}))
        (procs / f"{pid}.out").write_text(""); (procs / f"{pid}.err").write_text("")
    reg = ProcRegistry(tmp_path, "r2", runtime="host")
    assert reg._procs[1].state == "running"    # its OS pid (this test process) is alive
    assert reg._procs[2].state == "orphaned"   # its OS pid exited


def test_delete_session_accepts_the_run_id(client):
    s = _create_session(client)
    r = client.delete(f"/v1/sessions/{s['run_id']}", headers={"Authorization": AUTH})
    assert r.status_code == 200, r.text


def test_large_output_at_exit_is_not_truncated(tmp_path):
    """A child that prints far more than one pipe read and exits at once must
    land every byte on disk. The watcher used to do ONE 4 KiB read after exit
    and drop the rest — an agent CLI's final JSON result is exactly that shape."""
    from mini_ork.remote.node_agent.procs import ProcRegistry, ProcSpec

    reg = ProcRegistry(tmp_path, "big", runtime="host")
    size = 300_000
    ps = reg.spawn(ProcSpec(argv=[sys.executable, "-c", f"import sys; sys.stdout.write('x' * {size})"],
                            env_keys=[], cwd=None, stdin="", timeout_s=30,
                            env={"PATH": os.environ.get("PATH", "")}))
    deadline = time.time() + 30
    while reg._procs[ps.pid].state == "running" and time.time() < deadline:
        time.sleep(0.05)
    out = tmp_path / "runs" / "big" / ".procs" / f"{ps.pid}.out"
    assert reg._procs[ps.pid].state == "exited"
    assert out.stat().st_size == size


# ---------------------------------------------------------------------------
# remote-nodes-15 §3 (host-runtime engine gap) + §4 (audit.jsonl)
# ---------------------------------------------------------------------------


def test_host_runtime_translates_opt_mini_ork_onto_staged_engine(tmp_path):
    """Host-runtime argv containing ``/opt/mini-ork[/...]`` must rewrite onto
    the most-recently-staged engine checkout under ``<state>/engines/<sha>``.

    Without this the fake agent (and every transport) the node-agent
    spawns is ``/opt/mini-ork/tests/fixtures/bin/mo-fake-agent`` —
    rc=127. The docker runtime already gets this right (its container
    mounts the engine); only the host runtime needs the rewrite.
    """
    from mini_ork.remote.node_agent.procs import ProcRegistry

    engines = tmp_path / "engines"
    sha = "deadbeef"
    checkout = engines / sha
    checkout.mkdir(parents=True)
    (checkout / "tests" / "fixtures" / "bin").mkdir(parents=True)
    (checkout / "tests" / "fixtures" / "bin" / "mo-fake-agent").write_text("#!/bin/sh\nexit 0\n")
    (checkout / "tests" / "fixtures" / "bin" / "mo-fake-agent").chmod(0o755)
    # Touch another checkout AFTER to confirm "most recently staged wins".
    other = engines / "f00dface"
    other.mkdir()
    (other / "tests" / "fixtures" / "bin").mkdir(parents=True)
    (other / "tests" / "fixtures" / "bin" / "mo-fake-agent").write_text("#!/bin/sh\nexit 99\n")
    # Newer mtime on `other` to flip the "most recent" choice.
    import os as _os
    future = _os.stat(other).st_mtime + 60
    _os.utime(other, (future, future))

    reg = ProcRegistry(tmp_path, "r3", runtime="host",
                       engines_dir=engines, image="mini-ork/agent-node:latest")
    argv = ["/opt/mini-ork/tests/fixtures/bin/mo-fake-agent", "--print"]
    out = reg._host_path(argv[0])
    assert out == str(other / "tests" / "fixtures" / "bin" / "mo-fake-agent"), out

    # And the full argv translates element-wise (the actual call site).
    from mini_ork.remote.node_agent.procs import ProcSpec
    spec = ProcSpec(
        argv=argv, env_keys=[], cwd=None, stdin="", timeout_s=5,
        env={"PATH": os.environ.get("PATH", "")},
    )
    translated = reg._build_host_argv(spec)
    assert translated[0] == str(other / "tests" / "fixtures" / "bin" / "mo-fake-agent")


def test_host_runtime_opt_mini_ork_passthrough_when_no_engine_staged(tmp_path):
    """No engine staged yet → pass the path through unchanged so the shell
    surfaces a real ``no such file`` (rc 127) instead of a silent rewrite
    onto a missing tree."""
    from mini_ork.remote.node_agent.procs import ProcRegistry

    reg = ProcRegistry(tmp_path, "r4", runtime="host", engines_dir=tmp_path / "engines")
    assert reg._host_path("/opt/mini-ork/tests/fixtures/bin/mo-fake-agent") == (
        "/opt/mini-ork/tests/fixtures/bin/mo-fake-agent"
    )


def test_docker_runtime_does_not_translate_opt_mini_ork(tmp_path):
    """The docker runtime mounts the engine into the container — argv
    inside the container is already the sandbox path. The host-side
    ``_build_host_argv`` keeps the original sandbox argv because
    ``docker exec -i <cid> setsid <argv>`` hands the path to the cid
    verbatim, where the container's own mount at ``/opt/mini-ork`` is
    what resolves it. Verifying via ``_build_host_argv`` (the actual
    integration point) rather than ``_host_path`` directly — the
    mapping helper is pure; the runtime gate is in the caller."""
    from mini_ork.remote.node_agent.procs import ProcRegistry, ProcSpec

    engines = tmp_path / "engines"
    (engines / "abc").mkdir(parents=True)
    reg = ProcRegistry(tmp_path, "r5", runtime="docker", session_cid="cidxyz",
                       engines_dir=engines)
    spec = ProcSpec(
        argv=["/opt/mini-ork/tests/fixtures/bin/mo-fake-agent", "--print"],
        env_keys=[], cwd="/workspace/target", stdin="", timeout_s=5,
        env={"PATH": os.environ.get("PATH", "")},
    )
    argv = reg._build_host_argv(spec)
    assert "/opt/mini-ork/tests/fixtures/bin/mo-fake-agent" in argv, argv


def test_audit_log_appends_one_jsonl_per_proc_with_keys_only(tmp_path):
    """Kickoff §4: every spawn appends one JSON line to ``<state>/audit.jsonl``.
    Env KEYS land on disk; env VALUES never do (D6 + the kickoff's redaction
    rule). The argv is the host-side argv the child actually ran, so the
    leakage assertion in ``tests/integration/test_remote_nodes_e2e.py`` can
    grep it for forbidden prefixes without racing the watcher's redactor."""
    from mini_ork.remote.node_agent.procs import ProcRegistry, ProcSpec

    reg = ProcRegistry(tmp_path, "r6", runtime="host",
                       engines_dir=tmp_path / "engines", image="img:1")
    spec = ProcSpec(
        argv=["sh", "-c", "echo hi"],
        env_keys=["MO_PROBE"],
        cwd=None,
        stdin="",
        timeout_s=5,
        env={"MO_PROBE": "probe-secret-value", "PATH": os.environ.get("PATH", "")},
    )
    reg.spawn(spec)
    # Drain so the watcher thread has time to write + the audit append
    # has landed.
    deadline = time.time() + 10
    while reg._procs[1].state == "running" and time.time() < deadline:
        time.sleep(0.05)
    audit = tmp_path / "audit.jsonl"
    assert audit.is_file(), f"audit log not written: {audit}"
    lines = [json.loads(l) for l in audit.read_text().splitlines() if l]
    assert len(lines) == 1
    entry = lines[0]
    assert entry["session"] == "r6"
    assert entry["image"] == "img:1"
    assert entry["argv"] == ["sh", "-c", "echo hi"]
    assert "MO_PROBE" in entry["env_keys"]
    # D6 + kickoff §4: VALUES are never written, including by a different
    # thread or a different json field. ``json.dumps`` round-trips the
    # whole dict — a single false positive is a one-line fix that is hard
    # to ship silently.
    raw = audit.read_text()
    assert "probe-secret-value" not in raw
    assert "probe-secret" not in raw
    # cwd is None for host runtime with no cwd (we did not pass one).
    assert entry["cwd"] is None


def test_audit_log_records_spawn_failures_too(tmp_path):
    """A spawn that never made it past ``Popen`` still gets an audit line.
    The e2e leak check needs to see ALL attempts — including the ones that
    127'd because a host-path leaked."""
    from mini_ork.remote.node_agent.procs import ProcRegistry, ProcSpec

    reg = ProcRegistry(tmp_path, "r7", runtime="host",
                       engines_dir=tmp_path / "engines", image="img:1")
    spec = ProcSpec(
        argv=["/nonexistent/argv/element/that/does/not/exist/anywhere"],
        env_keys=[], cwd=None, stdin="", timeout_s=5,
        env={"PATH": os.environ.get("PATH", "")},
    )
    reg.spawn(spec)
    audit = tmp_path / "audit.jsonl"
    assert audit.is_file(), "spawn-failed proc did not append to audit.jsonl"
    entry = json.loads(audit.read_text().strip().splitlines()[-1])
    assert entry["state"] == "spawn_failed"
    assert "/nonexistent" in entry["argv"][0]


# ---------------------------------------------------------------------------
# remote-nodes-15: defects the simulated-remote E2E found on a real node host
# ---------------------------------------------------------------------------


def _bundle_of_tiny_repo(base: Path) -> tuple[bytes, str]:
    repo = base / "engine-src"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "mini_ork").mkdir()
    (repo / "mini_ork" / "__init__.py").write_text("ENGINE = 'from-bundle'\n")
    (repo / "AGENTS.md").write_text("a\n")
    os.symlink("AGENTS.md", repo / "CLAUDE.md")                 # an in-tree symlink, as in mini-ork
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "e"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                         check=True).stdout.strip()
    subprocess.run(["git", "bundle", "create", str(base / "e.bundle"), "HEAD"], cwd=repo,
                   check=True, capture_output=True)
    return (base / "e.bundle").read_bytes(), sha


def test_engine_bundle_stages_without_a_surrounding_repo(tmp_path, monkeypatch):
    """A node's cwd is not a git repo. `git bundle verify` used to fail there
    ("need a repository") and `git archive` read the cwd's repo, not the bundle."""
    from mini_ork.remote.node_agent.engines import EngineManager

    bundle, sha = _bundle_of_tiny_repo(tmp_path)
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    em = EngineManager(tmp_path / "state", "0.1.0")
    em.stage_bundle(sha, bundle)
    tree = tmp_path / "state" / "engines" / sha
    assert (tree / "mini_ork" / "__init__.py").read_text() == "ENGINE = 'from-bundle'\n"
    assert os.readlink(tree / "CLAUDE.md") == "AGENTS.md"
    assert not list((tmp_path / "state").glob(".engine-stage-*"))      # scratch repo cleaned up


def test_engine_extraction_without_the_tar_data_filter(tmp_path, monkeypatch):
    """The agent image's Python 3.11.2 has no extractall(filter=...); the
    fallback keeps the same rules: in-tree links pass, escapes are refused."""
    from mini_ork.remote.node_agent import engines

    real = tarfile.TarFile.extractall

    def no_filter(self, path=".", members=None, *, numeric_owner=False, **kw):
        if "filter" in kw:
            raise TypeError("extractall() got an unexpected keyword argument 'filter'")
        return real(self, path, members, numeric_owner=numeric_owner)

    monkeypatch.setattr(tarfile.TarFile, "extractall", no_filter)

    def archive(members):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for name, link in members:
                info = tarfile.TarInfo(name)
                if link:
                    info.type, info.linkname = tarfile.SYMTYPE, link
                    tf.addfile(info)
                else:
                    info.size = 1
                    tf.addfile(info, io.BytesIO(b"x"))
        buf.seek(0)
        return tarfile.open(fileobj=buf)

    ok = tmp_path / "ok"
    engines._safe_extract(archive([("a.md", ""), ("b.md", "a.md")]), ok)
    assert os.readlink(ok / "b.md") == "a.md"
    for bad in ([("../evil", "")], [("x", "/etc/passwd")], [("d/x", "../../out")]):
        with pytest.raises(RuntimeError, match="refusing"):
            engines._safe_extract(archive(bad), tmp_path / uuid.uuid4().hex)


def test_spawned_agent_gets_eof_on_stdin_and_the_prompt_is_not_persisted(tmp_path):
    """An agent CLI reads its prompt to EOF; the pipe used to stay open, so
    every remote agent blocked forever. The prompt is not kept on disk."""
    from mini_ork.remote.node_agent.procs import ProcRegistry, ProcSpec

    reg = ProcRegistry(tmp_path, "eof", runtime="host")
    ps = reg.spawn(ProcSpec(argv=["cat"], env_keys=[], cwd=None, stdin="the prompt\n", timeout_s=20,
                            env={"PATH": os.environ.get("PATH", "")}))
    deadline = time.time() + 20
    while reg._procs[ps.pid].state == "running" and time.time() < deadline:
        time.sleep(0.05)
    procs = tmp_path / "runs" / "eof" / ".procs"
    assert reg._procs[ps.pid].state == "exited"
    assert (procs / f"{ps.pid}.out").read_text() == "the prompt\n"
    record = json.loads((procs / f"{ps.pid}.json").read_text())
    assert "stdin" not in record and record["stdin_bytes"] == len("the prompt\n")


def test_session_engine_is_the_control_planes_sha(tmp_path):
    """D8: a session mounts (docker) / maps (host) engines/<sha> for the sha the
    control plane sent — not whichever engine was staged last."""
    from mini_ork.remote.node_agent.procs import ProcRegistry
    from mini_ork.remote.node_agent.sessions import SessionManager

    engines_dir = tmp_path / "engines"
    (engines_dir / "aaa").mkdir(parents=True)
    time.sleep(0.01)
    (engines_dir / "bbb").mkdir()                                  # staged later
    reg = ProcRegistry(tmp_path, "r", runtime="host", engines_dir=engines_dir, engine_sha="aaa")
    assert reg._host_path("/opt/mini-ork/x.py") == str(engines_dir / "aaa" / "x.py")
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "cid", "")

    SessionManager(tmp_path, runtime="docker", _run_fn=run).create(
        run_id="r2", image="img", engine_sha="aaa")
    docker_run = " ".join(next(c for c in calls if c[:2] == ["docker", "run"]))
    assert f"{engines_dir / 'aaa'}:/opt/mini-ork:ro" in docker_run


def test_node_agent_flags_reach_the_app_factory(tmp_path, monkeypatch):
    """`--runtime/--token-env/--retain-hours` were parsed, printed and dropped:
    the uvicorn factory takes no arguments, so they travel in the env."""
    from mini_ork.cli import node_agent as cli

    for key in ("MO_NODE_AGENT_RUNTIME", "MO_NODE_AGENT_TOKEN_ENV", "MO_NODE_AGENT_RETAIN_HOURS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MO_NODE_AGENT_STATE_DIR", str(tmp_path / "s"))
    assert cli.main(["--runtime", "host", "--token-env", "MY_NODE_TOKEN", "--retain-hours", "2"],
                    _exec=False) == 0
    assert (os.environ["MO_NODE_AGENT_RUNTIME"], os.environ["MO_NODE_AGENT_TOKEN_ENV"]) == \
        ("host", "MY_NODE_TOKEN")
    monkeypatch.setenv("MY_NODE_TOKEN", "t-123")
    with TestClient(create_app(state_dir=tmp_path / "s")) as c:
        assert c.get("/v1/sessions/none", headers={"Authorization": "Bearer t-123"}).status_code == 404
        created = c.post("/v1/sessions", json={"run_id": "r", "image": "i"},
                         headers={"Authorization": "Bearer t-123"})
        assert created.status_code == 200 and created.json()["cid"].startswith("host:")


def test_host_runtime_maps_sandbox_paths_inside_shell_scripts(tmp_path):
    """Checks arrive as ``sh -c "<script>"``: one argv string, many paths. The
    host runtime rewrites every sandbox path token, not just whole arguments."""
    from mini_ork.remote.node_agent.procs import ProcRegistry

    (tmp_path / "engines" / "abc").mkdir(parents=True)
    reg = ProcRegistry(tmp_path, "r", runtime="host", engines_dir=tmp_path / "engines",
                       engine_sha="abc")
    run = tmp_path / "runs" / "r"
    engine = tmp_path / "engines" / "abc"
    assert reg._host_text("python3 /opt/mini-ork/v/test.py --dir '/workspace/target'") == \
        f"python3 {engine}/v/test.py --dir '{run}/target'"
    assert reg._host_text("/opt/mini-ork:/x") == f"{engine}:/x"
    for untouched in ("/workspaces/x", "a/opt/mini-ork/b", "/opt/mini-orkish"):
        assert reg._host_text(untouched) == untouched


def test_exec_with_timeout_zero_means_no_limit(client):
    """run_check sends timeout 0 for "no limit"; the node used to read it as
    zero seconds and kill every remote check at once (rc 124)."""
    headers = {"Authorization": f"Bearer {TOKEN_VALUE}"}
    assert client.post("/v1/sessions", json={"run_id": "t0", "image": "i"}, headers=headers).status_code == 200
    r = client.post("/v1/sessions/t0/exec", headers=headers,
                    json={"argv": ["sh", "-c", "sleep 0.3; echo done"], "timeout_s": 0})
    assert r.status_code == 200 and r.json()["rc"] == 0 and "done" in r.json()["output"]


def test_deleting_a_host_session_kills_its_procs(client, state_dir):
    """Host runtime: no container, so DELETE must kill the session's procs
    itself (kill_run left a sleeping agent running on the node)."""
    headers = {"Authorization": f"Bearer {TOKEN_VALUE}"}
    assert client.post("/v1/sessions", json={"run_id": "kd", "image": "i"}, headers=headers).status_code == 200
    spawned = client.post("/v1/sessions/kd/procs", headers=headers,
                          json={"argv": ["sleep", "300"], "env": {"PATH": os.environ.get("PATH", "")}})
    assert spawned.status_code == 200, spawned.text
    record = json.loads((Path(state_dir) / "runs" / "kd" / ".procs"
                         / f"{spawned.json()['pid']}.json").read_text())
    os_pid = record["os_pid"]
    os.kill(os_pid, 0)                                   # alive before the delete
    assert client.delete("/v1/sessions/kd", headers=headers).status_code == 200
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(os_pid, 0)
        except ProcessLookupError:
            break
        if subprocess.run(["ps", "-o", "stat=", "-p", str(os_pid)], capture_output=True,
                          text=True).stdout.strip().startswith("Z"):
            break                                        # killed, awaiting reap
        time.sleep(0.1)
    else:
        raise AssertionError(f"session proc {os_pid} survived DELETE")
