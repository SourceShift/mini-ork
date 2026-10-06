"""remote-nodes-15 — the end-to-end proof: a `code-fix` run whose agent and
checks execute on a separate "node host", with the fix landing locally.

The node host is a container built FROM the agent image, running
``mini-ork node-agent --runtime host`` over TLS with **no volume mounts**: it
shares no filesystem with this machine, gets the engine via ``PUT
/v1/engines`` and the target tree via the sync protocol. The control plane
is the real CLI — ``mini-ork run code-fix <kickoff> --placement remote --env
e2e`` — with every lane pointed at ``tests/fixtures/bin/mo-fake-agent`` (no
LLM spend), preceded by ``mini-ork nodes doctor``.

Daemon-gated: needs Docker and the ``mini-ork/agent-node`` image
(``bash docker/agent-node/build.sh``). The engine shipped to the node is the
git HEAD of this checkout, so the fixtures must be committed.

Live tier (operator-run, costs money): ``MO_REMOTE_LIVE=1 MO_E2E_LANE=<lane>``
swaps the fake agent for a real ``providers.yaml`` lane; its secret must be in
the local store.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
BASE_IMAGE = os.environ.get("MO_E2E_BASE_IMAGE", "mini-ork/agent-node:latest")
KICKOFF = REPO / "tests" / "fixtures" / "remote_e2e_kickoff.md"
FIXTURE_REPO = REPO / "tests" / "fixtures" / "remote_e2e_repo"
FAKE_AGENT = REPO / "tests" / "fixtures" / "bin" / "mo-fake-agent"
TOKEN = "tok-e2e-" + uuid.uuid4().hex[:12]
FIX = "return sum(range(1, n + 1))"


def _docker(*args: str, check: bool = True, timeout: float = 120, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check,
                          timeout=timeout, **kw)


def _skip_reason() -> str:
    if shutil.which("docker") is None:
        return "docker CLI not on PATH"
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return "docker daemon unreachable"
    if subprocess.run(["docker", "image", "inspect", BASE_IMAGE], capture_output=True).returncode != 0:
        return f"{BASE_IMAGE} not built — run: bash docker/agent-node/build.sh"
    if subprocess.run(["git", "-C", str(REPO), "ls-files", "--error-unmatch", str(FAKE_AGENT)],
                      capture_output=True).returncode != 0:
        return "the fixtures are not committed (the node gets the engine as git HEAD)"
    return ""


pytestmark = pytest.mark.skipif(bool(_skip_reason()), reason=_skip_reason() or "ok")


def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return int(sk.getsockname()[1])


_NODE_HOST_DOCKERFILE = f"""\
FROM {BASE_IMAGE}
USER root
RUN pip install --no-cache-dir --break-system-packages fastapi 'uvicorn>=0.29' pytest
COPY src /srv/node-agent
RUN mkdir -p /srv/mini-ork /srv/tls && chown -R agent:agent /srv/mini-ork
USER agent
ENV PYTHONPATH=/srv/node-agent
"""


@pytest.fixture(scope="module")
def node_host(tmp_path_factory):
    """The node host: built from the agent image + this HEAD's node-agent,
    started with no mounts, TLS on, and torn down after the module."""
    work = tmp_path_factory.mktemp("node-host")
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()
    tag = f"mo-e2e-node-host:{head[:12]}"
    if _docker("image", "inspect", tag, check=False).returncode != 0:
        ctx = work / "ctx"
        (ctx / "src").mkdir(parents=True)
        archive = subprocess.run(["git", "-C", str(REPO), "archive", "HEAD"], capture_output=True,
                                 check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(ctx / "src")], input=archive, check=True)
        (ctx / "Dockerfile").write_text(_NODE_HOST_DOCKERFILE)
        built = _docker("build", "-t", tag, str(ctx), check=False, timeout=1200)
        assert built.returncode == 0, built.stderr[-3000:]
    tls = work / "tls"
    tls.mkdir()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
                    "-keyout", str(tls / "key.pem"), "-out", str(tls / "cert.pem")],
                   capture_output=True, check=True)
    (tls / "key.pem").chmod(0o644)          # test-only key, readable by the container user
    port = _free_port()
    name = f"mo-e2e-node-{uuid.uuid4().hex[:8]}"
    _docker("create", "--name", name, "-p", f"127.0.0.1:{port}:7091", "-e", f"MO_NODE_TOKEN={TOKEN}",
            tag, "python3", "-m", "mini_ork.cli.node_agent", "--bind", "0.0.0.0", "--port", "7091",
            "--runtime", "host", "--state-dir", "/srv/mini-ork",
            "--tls-cert", "/srv/tls/cert.pem", "--tls-key", "/srv/tls/key.pem")
    try:
        _docker("cp", f"{tls}/.", f"{name}:/srv/tls")
        _docker("start", name)
        url = f"https://127.0.0.1:{port}"
        ctx = ssl.create_default_context(cafile=str(tls / "cert.pem"))
        deadline = time.time() + 90
        while True:
            try:
                with urllib.request.urlopen(f"{url}/v1/health", context=ctx, timeout=5):
                    break
            except (urllib.error.URLError, OSError):
                if time.time() > deadline:
                    pytest.fail("node host never became healthy:\n"
                                + _docker("logs", name, check=False).stderr[-3000:])
                time.sleep(1)
        yield SimpleNamespace(url=url, cert=str(tls / "cert.pem"), name=name, ssl=ctx)
    finally:
        _docker("rm", "-f", name, check=False)


def _client(tmp_path: Path, node, *, lane: str = "e2e_fake") -> SimpleNamespace:
    """A control-plane home + target checkout for one run against ``node``."""
    import yaml

    from mini_ork.stores.migrate import init_db

    home = tmp_path / "home"
    (home / "config" / "environments").mkdir(parents=True)
    (home / "config" / "nodes.yaml").write_text(
        f"nodes:\n  e2e-node:\n    url: {node.url}\n    token_env: MO_E2E_NODE_TOKEN\n"
        "    max_sessions: 4\n")
    secrets = f"[{os.environ['MO_E2E_SECRET']}]" if os.environ.get("MO_E2E_SECRET") else "[]"
    (home / "config" / "environments" / "e2e.yaml").write_text(
        f"node: e2e-node\nimage: {BASE_IMAGE}\nnetwork: full\nsecrets: {secrets}\n")
    policy = yaml.safe_load((REPO / "config" / "agents.yaml").read_text())
    policy["lanes"] = {role: lane for role in policy.get("lanes") or {}}
    (home / "config" / "agents.yaml").write_text(yaml.safe_dump(policy))
    db = home / "state.db"
    rc, _out, err = init_db(str(db), str(REPO))
    assert rc == 0, err
    target = tmp_path / "target"
    shutil.copytree(FIXTURE_REPO, target)
    for args in (["init", "-q"], ["config", "user.email", "e2e@t"], ["config", "user.name", "e2e"],
                 ["add", "-A"], ["commit", "-q", "-m", "fixture"]):
        subprocess.run(["git", *args], cwd=target, check=True, capture_output=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=target, capture_output=True, text=True,
                          check=True).stdout.strip()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MINI_ORK_", "MO_"))}
    env.update({"MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": str(home), "MINI_ORK_DB": str(db),
                "MO_TARGET_CWD": str(target), "MINI_ORK_NONINTERACTIVE": "1",
                "MO_STATIC_RECIPE_PLAN": "1", "SSL_CERT_FILE": node.cert,
                "MO_E2E_NODE_TOKEN": TOKEN, "MO_REMOTE_ALLOW_DIRTY_ENGINE": "1",
                "PYTHONPATH": str(REPO)})
    # The `MO_*` filter above drops the verification-stack knobs, which are now
    # DEFAULT ON in the remote container. Pin them OFF: these tests exercise
    # remote placement, not verification policy.
    env["MO_LEVEL_VECTOR"] = "0"
    env["MO_SUITE_ADEQUACY"] = "0"
    if lane == "e2e_fake":
        providers = tmp_path / "providers.yaml"
        providers.write_text(f"providers:\n  e2e_fake:\n    kind: executable\n    script: {FAKE_AGENT}\n")
        env["MINI_ORK_PROVIDERS"] = str(providers)
        env["MINI_ORK_SECRETS"] = str(tmp_path / "no-secrets.sh")
    return SimpleNamespace(home=home, db=db, target=target, base=base, env=env)


def _run_cmd(c) -> list[str]:
    return [sys.executable, str(REPO / "bin" / "mini-ork"), "run", "code-fix", str(KICKOFF),
            "--placement", "remote", "--env", "e2e"]


def _newest_run(db: Path) -> tuple[str, str] | None:
    try:
        con = sqlite3.connect(db, timeout=10)
        try:
            return con.execute("SELECT id, status FROM task_runs ORDER BY created_at DESC LIMIT 1").fetchone()
        finally:
            con.close()
    except sqlite3.OperationalError:   # the run holds the db mid-migration / mid-write
        return None


def _events(db: Path, run_id: str) -> list[tuple[str, dict]]:
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT event_type, payload_json FROM run_events WHERE run_id=? ORDER BY rowid",
                           (run_id,)).fetchall()
    finally:
        con.close()
    return [(etype, json.loads(payload or "{}")) for etype, payload in rows]


def _audit(node) -> list[dict]:
    out = _docker("exec", node.name, "cat", "/srv/mini-ork/audit.jsonl", check=False).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _leaks(entries: list[dict], forbidden: list[str]) -> list[str]:
    """Every audit value that names a path on THIS machine (it should have
    been translated to a /workspace or /opt/mini-ork path before sending)."""
    found = []
    for entry in entries:
        for value in [*(entry.get("argv") or []), entry.get("cwd") or ""]:
            if any(str(value).startswith(p) or f" {p}" in str(value) for p in forbidden if p):
                found.append(str(value))
    return found


def _forbidden(tmp_path: Path) -> list[str]:
    return [os.path.expanduser("~"), "/Users", "/Volumes", "/private", str(tmp_path)]


def _session_exists(node, run_id: str) -> bool:
    req = urllib.request.Request(f"{node.url}/v1/sessions/{run_id}",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, context=node.ssl, timeout=10):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


# --------------------------------------------------------------------------- plumbing tier


def test_code_fix_runs_on_the_node_and_the_fix_lands_locally(node_host, tmp_path):
    c = _client(tmp_path, node_host)
    doctor = subprocess.run([sys.executable, "-m", "mini_ork.cli.nodes", "doctor", "--env", "e2e",
                             "--no-llm"], cwd=c.target, env=c.env, capture_output=True, text=True,
                            timeout=300)
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr

    run = subprocess.run(_run_cmd(c), cwd=c.target, env=c.env, capture_output=True, text=True,
                         timeout=1200)
    assert run.returncode == 0, (run.stdout + run.stderr)[-4000:]
    run_id, status = _newest_run(c.db)
    assert status == "published", status

    # The fix — and only the fix — is in the local checkout.
    changed = subprocess.run(["git", "diff", "--name-only", c.base], cwd=c.target, capture_output=True,
                             text=True, check=True).stdout.split()
    assert changed == ["calc_pkg/calc.py"], changed
    assert FIX in (c.target / "calc_pkg" / "calc.py").read_text()

    events = _events(c.db, run_id)
    kinds = [e for e, _ in events]
    first_node = kinds.index("node_start")
    setup = {(p.get("step"), p.get("status")) for e, p in events[:first_node] if e == "remote.setup.step"}
    for step in ("health", "engine", "session_up", "initial_sync"):
        assert (step, "ok") in setup, setup
    impl = next(p for e, p in events if e == "node_start" and p.get("node_type") == "implementer")
    assert impl["placement"] == "remote" and impl["node_host"] == "e2e-node"
    assert "remote.sync.up" in kinds and "remote.sync.down" in kinds
    # Every dispatch/check is its own process; only the run-start sync ships
    # the whole tree — the rest reuse the persisted sync base.
    full = [p for e, p in events if e == "remote.sync.up" and p.get("mode") != "incremental"]
    assert len(full) == 1, full
    checks = [p for e, p in events if e == "remote.check"]
    assert any(p["name"] == "test.py" and p["rc"] == 0 for p in checks), checks
    assert "remote.run.summary" in kinds

    audit = [e for e in _audit(node_host) if e.get("session") == run_id]
    assert any("mo-fake-agent" in " ".join(e["argv"]) for e in audit), audit   # the agent ran there
    assert _leaks(audit, _forbidden(tmp_path)) == []
    assert not _session_exists(node_host, run_id)


def test_kill_run_leaves_no_live_proc_and_no_session(node_host, tmp_path, monkeypatch):
    from mini_ork.web.control import kill_run
    from mini_ork.web.db import StateDB

    c = _client(tmp_path, node_host)
    c.env["MO_FAKE_AGENT_SLEEP"] = "600"
    proc = subprocess.Popen(_run_cmd(c), cwd=c.target, env=c.env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        run_id, deadline = None, time.time() + 600
        while time.time() < deadline:
            row = _newest_run(c.db) if c.db.exists() else None
            run_id = row[0] if row else None
            if run_id and any(e.get("session") == run_id and "mo-fake-agent" in " ".join(e["argv"])
                              for e in _audit(node_host)):
                break
            time.sleep(2)
        else:
            pytest.fail("the sleeping agent never started on the node")
        for key in ("MO_E2E_NODE_TOKEN", "SSL_CERT_FILE", "MINI_ORK_HOME", "MINI_ORK_DB"):
            monkeypatch.setenv(key, c.env[key])
        result = kill_run(c.home, StateDB(c.db), run_id)
        assert result.get("ok") is not False, result
        proc.wait(timeout=120)
        deadline = time.time() + 60
        while _session_exists(node_host, run_id) and time.time() < deadline:
            time.sleep(2)
        assert not _session_exists(node_host, run_id)
        # The image has no procps: scan /proc (the [t] keeps grep's own shell out).
        live = _docker("exec", node_host.name, "sh", "-c",
                       "grep -l 'mo-fake-agen[t]' /proc/[0-9]*/cmdline 2>/dev/null", check=False)
        assert live.stdout.strip() == "", f"agent procs still alive on the node: {live.stdout}"
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, 9)


def test_audit_catches_a_host_path_that_skipped_translation(node_host, tmp_path, monkeypatch):
    """The leakage assertion is not vacuous: a spawn that carries a path on
    this machine (what a skipped PathMap translation would send) shows up in
    the node's audit log and trips ``_leaks``."""
    from mini_ork.runtime.backends.remote import RemoteWorkspace, _NodeRef

    c = _client(tmp_path, node_host)
    monkeypatch.setenv("MO_E2E_NODE_TOKEN", TOKEN)
    monkeypatch.setenv("SSL_CERT_FILE", node_host.cert)
    monkeypatch.setenv("MO_REMOTE_ALLOW_DIRTY_ENGINE", "1")
    run_id = f"leak-{uuid.uuid4().hex[:8]}"
    ws = RemoteWorkspace(node=_NodeRef(name="e2e-node", url=node_host.url, token=TOKEN, max_sessions=8),
                         run_id=run_id,
                         image=BASE_IMAGE, drive_root=str(tmp_path), engine_root=str(REPO),
                         token_env="MO_E2E_NODE_TOKEN", target_root=str(c.target),
                         run_dir=str(tmp_path))
    ws.up()
    try:
        leaked = str(c.target / "calc_pkg" / "calc.py")
        ws.spawn(["cat", leaked], stdin="", timeout=30.0, env={"PATH": "/usr/bin:/bin"},
                 cwd="/workspace/target")
    finally:
        ws.down()
    audit = [e for e in _audit(node_host) if e.get("session") == run_id]
    assert _leaks(audit, _forbidden(tmp_path)) == [leaked]


# --------------------------------------------------------------------------- live tier


@pytest.mark.skipif(os.environ.get("MO_REMOTE_LIVE") != "1" or not os.environ.get("MO_E2E_LANE"),
                    reason="live tier: MO_REMOTE_LIVE=1 MO_E2E_LANE=<lane> (costs money)")
def test_live_lane_fixes_the_fixture_on_the_node(node_host, tmp_path):
    """The same run with a real cheap lane instead of the fake agent. The lane's
    secret must be in the local store and named by MO_E2E_SECRET (its
    api_key_env) so the profile permits it. Reports the run's cost."""
    c = _client(tmp_path, node_host, lane=os.environ["MO_E2E_LANE"])
    run = subprocess.run(_run_cmd(c), cwd=c.target, env=c.env, capture_output=True, text=True,
                         timeout=3600)
    assert run.returncode == 0, (run.stdout + run.stderr)[-4000:]
    run_id, status = _newest_run(c.db)
    assert status == "published"
    con = sqlite3.connect(c.db)
    try:
        cost = con.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE run_id=?",
                           (run_id,)).fetchone()[0]
    finally:
        con.close()
    print(f"\nlive tier: run {run_id} cost ${cost:.4f}")
    assert FIX.replace(" ", "") in (c.target / "calc_pkg" / "calc.py").read_text().replace(" ", "")
