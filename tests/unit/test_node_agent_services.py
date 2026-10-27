"""Unit tests for the node-agent service runtime (``/v1/services``).

Every Docker invocation flows through one fake runner, so the suite needs no
daemon. The tests assert on the argv the fabric composes (labels, ports,
restart policy, healthcheck, env) as well as the HTTP contract, because the
argv *is* the product here: the control plane can only trust what the node
actually ran.

The last test is the security property the module exists to keep: env values
cross into the container but never land in ``services.json``.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi.testclient")
from fastapi.testclient import TestClient  # noqa: E402

from mini_ork.remote.node_agent.app import create_app  # noqa: E402


TOKEN_ENV = "MO_TEST_SERVICE_TOKEN"
TOKEN_VALUE = "svc-token-abc"
AUTH = {"Authorization": f"Bearer {TOKEN_VALUE}"}


class _FakeCompleted:
    returncode = 0
    stdout = ""
    stderr = ""


class _Docker:
    """Minimal docker CLI double with a state table keyed by container name."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.states: dict[str, str] = {}
        self.exit_codes: dict[str, int] = {}
        self.finished: dict[str, str] = {}
        self.networks: set[str] = set()
        self.network_creates: list[list[str]] = []
        self.network_removes: list[list[str]] = []
        self.network_connects: list[list[str]] = []
        self.fail_network: str | None = None
        #: Non-null makes every ``docker network connect`` fail with this
        #: daemon message — the extra-network analogue of ``fail_network``.
        self.fail_network_connect: str | None = None
        #: ``"in_use"`` reproduces Docker refusing a network with containers
        #: attached; ``"daemon"`` a daemon-level refusal. Both are exit 1, so
        #: the manager has to read the reason rather than the code.
        self.fail_network_rm: str | None = None
        self.fail_on: str | None = None
        self.fail_stop: str | None = None
        self.fail_start: str | None = None
        self.removed: list[str] = []
        #: (argv, stdin) pairs the exec route handed to the runner. Kept apart
        #: from ``calls`` so a test can assert a credential travelled on stdin.
        self.inputs: list[tuple[list[str], str | None]] = []
        self.exec_exit = 0
        self.exec_stdout = ""
        self.exec_stderr = ""
        self.exec_timeout = False
        #: ``docker stats --format '{{json .}}'`` rows keyed by container name.
        #: A name that is absent prints nothing, which is how Docker reports a
        #: container that is not running.
        self.stats: dict[str, dict] = {}
        self.stats_returncode = 0
        self.stats_stderr = ""

    def run_input(self, argv, **kwargs):
        argv = [str(a) for a in argv]
        self.inputs.append((argv, kwargs.get("stdin")))
        if self.exec_timeout:
            import subprocess

            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))
        r = _FakeCompleted()
        r.returncode = self.exec_exit
        r.stdout = self.exec_stdout
        r.stderr = self.exec_stderr
        return r

    def __call__(self, argv, **_kwargs):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        r = _FakeCompleted()

        if argv[:2] == ["docker", "run"]:
            name = argv[argv.index("--name") + 1]
            if self.fail_on and self.fail_on in name:
                r.returncode = 125
                r.stderr = "docker: image not found"
                return r
            self.states[name] = "running"
            self.exit_codes.pop(name, None)
            self.finished.pop(name, None)
            r.stdout = f"cid-{name}"
            return r

        if argv[:3] == ["docker", "network", "inspect"]:
            if argv[-1] not in self.networks:
                r.returncode = 1
                r.stderr = "Error: No such network"
            return r

        if argv[:3] == ["docker", "network", "create"]:
            if self.fail_network:
                r.returncode = 1
                r.stderr = f"Error response from daemon: {self.fail_network}"
                return r
            self.networks.add(argv[-1])
            self.network_creates.append(argv)
            r.stdout = argv[-1]
            return r

        if argv[:3] == ["docker", "network", "connect"]:
            if self.fail_network_connect:
                r.returncode = 1
                r.stderr = (
                    f"Error response from daemon: {self.fail_network_connect}"
                )
                return r
            self.network_connects.append(argv)
            return r

        if argv[:3] == ["docker", "network", "rm"]:
            name = argv[-1]
            if self.fail_network_rm == "vanished":
                # Inspect saw it; a concurrent delete removed it first.
                self.networks.discard(name)
                r.returncode = 1
                r.stderr = f"Error response from daemon: network {name} not found"
                return r
            if self.fail_network_rm == "in_use":
                r.returncode = 1
                r.stderr = (
                    f"Error response from daemon: network {name} has active endpoints"
                )
                return r
            if self.fail_network_rm == "daemon":
                r.returncode = 1
                r.stderr = "Error response from daemon: operation is not permitted"
                return r
            if name not in self.networks:
                r.returncode = 1
                r.stderr = f"Error response from daemon: network {name} not found"
                return r
            self.networks.discard(name)
            self.network_removes.append(argv)
            return r

        if argv[:2] == ["docker", "inspect"]:
            name = argv[-1]
            if name not in self.states:
                r.returncode = 1
                r.stderr = "Error: No such object"
                return r
            state = self.states[name]
            if any("{{.State.ExitCode}}" in a for a in argv):
                # Docker's own zero value for "never finished" — the manager
                # must translate it to null rather than pass it through.
                finished = self.finished.get(name, "0001-01-01T00:00:00Z")
                exit_code = self.exit_codes.get(name, 0)
                r.stdout = f"{state}|{exit_code}|{finished}"
            else:
                r.stdout = state
            return r

        if argv[:2] == ["docker", "logs"]:
            r.stdout = "listening on :8080\n"
            return r

        if argv[:2] == ["docker", "stats"]:
            names = argv[argv.index("{{json .}}") + 1:]
            lines = [json.dumps(self.stats[n]) for n in names if n in self.stats]
            r.stdout = "".join(f"{line}\n" for line in lines)
            r.returncode = self.stats_returncode
            r.stderr = self.stats_stderr
            return r

        if argv[:2] == ["docker", "rm"]:
            self.removed.append(argv[-1])
            self.states.pop(argv[-1], None)
            self.exit_codes.pop(argv[-1], None)
            self.finished.pop(argv[-1], None)
            return r

        if argv[:2] == ["docker", "restart"]:
            self.states[argv[-1]] = "running"
            self.finished.pop(argv[-1], None)
            return r

        if argv[:2] == ["docker", "stop"]:
            name = argv[-1]
            if self.fail_stop and self.fail_stop in name:
                r.returncode = 1
                r.stderr = "Error response from daemon: cannot stop container"
                return r
            self.states[name] = "exited"
            return r

        if argv[:2] == ["docker", "start"]:
            name = argv[-1]
            if self.fail_start and self.fail_start in name:
                r.returncode = 1
                r.stderr = "Error response from daemon: cannot start container"
                return r
            self.states[name] = "running"
            return r

        return r

    def run_argv(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["docker", "run"]]


@pytest.fixture()
def docker(monkeypatch) -> _Docker:
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    return _Docker()


@pytest.fixture()
def app(tmp_path: Path, docker: _Docker):
    return create_app(
        state_dir=tmp_path, token_env=TOKEN_ENV, _run_fn=docker, _run_input_fn=docker.run_input
    )


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


def _spec(**overrides) -> dict:
    payload = {
        "schema_version": "mini-ork.deployment/v1",
        "service_id": "web",
        "deployment_id": "deploy-42",
        "image": "ghcr.io/acme/web:1",
        "command": ["uvicorn", "app:app", "--port", "8080"],
        "replicas": 2,
        "ports": [{"container": 8080}],
        "resources": {"cpus": 0.5, "memory": "512m"},
        "restart_policy": "unless-stopped",
        "healthcheck": {"cmd": "curl -fsS localhost:8080/health", "interval": "10s"},
        "env": {"DATABASE_URL": "postgres://secret@db/app"},
        "env_keys": ["DATABASE_URL", "LOG_LEVEL"],
        "labels": {"team": "core"},
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_returns_one_container_per_replica(client, docker):
    r = client.post("/v1/services", json=_spec(), headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["service_id"] == "web"
    assert body["replicas"] == 2
    assert [rep["state"] for rep in body["replicas_state"]] == ["running", "running"]
    assert len(docker.run_argv()) == 2


def test_create_argv_carries_the_contract(client, docker):
    r = client.post("/v1/services", json=_spec(), headers=AUTH)
    assert r.status_code == 200, r.text
    argv = docker.run_argv()[0]

    assert "--label" in argv and "mo.service=web" in argv
    assert "mo.deployment=deploy-42" in argv
    assert "mo.sandbox=1" in argv
    assert "team=core" in argv
    assert argv[argv.index("--restart") + 1] == "unless-stopped"
    assert "8080" in argv
    assert argv[argv.index("--cpus") + 1] == "0.5"
    assert argv[argv.index("--memory") + 1] == "512m"
    assert argv[argv.index("--health-cmd") + 1] == "curl -fsS localhost:8080/health"
    assert "--health-interval" in argv
    # env value crosses into the container
    assert "DATABASE_URL=postgres://secret@db/app" in argv
    # command follows the image
    image_at = argv.index("ghcr.io/acme/web:1")
    assert argv[image_at + 1:] == ["uvicorn", "app:app", "--port", "8080"]


def test_port_mappings_cover_bind_and_protocol():
    """The Docker ``-p`` grammar, pinned without a daemon."""
    from mini_ork.remote.node_agent.services import _port_mapping

    assert _port_mapping({"container": 8080}) == "8080"
    assert _port_mapping({"container": 5432, "host": 20001}) == "20001:5432"
    assert _port_mapping({"container": 53, "host": 20002, "protocol": "udp"}) == "20002:53/udp"
    assert (
        _port_mapping({"container": 5432, "host": 20003, "bind": "10.0.0.5"}) == "10.0.0.5:20003:5432"
    )
    assert (
        _port_mapping(
            {"container": 53, "host": 20004, "bind": "::1", "protocol": "udp"}
        )
        == "[::1]:20004:53/udp"
    )


def test_create_publishes_a_bound_udp_port(client, docker):
    r = client.post(
        "/v1/services",
        json=_spec(replicas=1, ports=[{"container": 5432, "host": 20001, "bind": "10.0.0.5"}]),
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    argv = docker.run_argv()[0]
    assert argv[argv.index("-p") + 1] == "10.0.0.5:20001:5432"

    client.post(
        "/v1/services",
        json=_spec(
            service_id="dns", replicas=1, ports=[{"container": 53, "host": 20002, "protocol": "udp"}]
        ),
        headers=AUTH,
    )
    argv = docker.run_argv()[1]
    assert argv[argv.index("-p") + 1] == "20002:53/udp"


def test_the_spec_keeps_defaults_out_of_state(client, tmp_path):
    r = client.post(
        "/v1/services",
        json=_spec(
            ports=[
                {"container": 8080, "host": 20001, "protocol": "tcp"},
                {"container": 9000, "host": 20002, "protocol": "udp", "bind": "127.0.0.1"},
            ]
        ),
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    data = json.loads((tmp_path / "services.json").read_text(encoding="utf-8"))
    assert data["services"][0]["ports"] == [
        {"container": 8080, "host": 20001},
        {"container": 9000, "host": 20002, "protocol": "udp", "bind": "127.0.0.1"},
    ]


@pytest.mark.parametrize(
    ("port", "reason"),
    [
        ({"container": 8080, "protocol": "sctp"}, "protocol"),
        ({"container": 8080, "bind": "node.example.com"}, "IP literal"),
        ({"container": 8080, "bind": "300.1.1.1"}, "IP literal"),
        ("8080", "objects"),
    ],
)
def test_bad_port_shapes_are_400s_before_docker(client, docker, port, reason):
    r = client.post("/v1/services", json=_spec(ports=[port]), headers=AUTH)
    assert r.status_code == 400, r.text
    assert reason in r.text
    assert docker.run_argv() == []


def test_create_persists_keys_but_never_values(client, tmp_path):
    r = client.post("/v1/services", json=_spec(), headers=AUTH)
    assert r.status_code == 200, r.text
    raw = (tmp_path / "services.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["services"][0]["env_keys"] == ["DATABASE_URL", "LOG_LEVEL"]
    assert "postgres://secret" not in raw
    assert "secret@db" not in raw


def test_create_rejects_bad_service_id(client):
    r = client.post("/v1/services", json=_spec(service_id="Bad ID!"), headers=AUTH)
    assert r.status_code == 400
    assert "service_id" in r.json()["detail"]


def test_create_rejects_unknown_restart_policy(client):
    r = client.post("/v1/services", json=_spec(restart_policy="sometimes"), headers=AUTH)
    assert r.status_code == 400


def test_create_rejects_unknown_schema_version(client):
    r = client.post("/v1/services", json=_spec(schema_version="v2"), headers=AUTH)
    assert r.status_code == 400


def test_create_rolls_back_partial_replica_set(client, docker, tmp_path):
    docker.fail_on = "mo-svc-web-1"
    r = client.post("/v1/services", json=_spec(), headers=AUTH)
    assert r.status_code == 500
    # replica 0 was removed on the failure path …
    assert "cid-mo-svc-web-0" in docker.removed
    # … and nothing was persisted, so a retry starts clean.
    assert not (tmp_path / "services.json").exists()

    r = client.get("/v1/services", headers=AUTH)
    assert r.json() == {"services": []}


def test_create_replaces_by_default(client, docker):
    assert client.post("/v1/services", json=_spec(), headers=AUTH).status_code == 200
    assert (
        client.post("/v1/services", json=_spec(image="ghcr.io/acme/web:2"), headers=AUTH)
        .status_code
        == 200
    )
    assert len(docker.run_argv()) == 4  # 2 + 2
    assert client.get("/v1/services", headers=AUTH).json()["services"][0]["image"].endswith(":2")


# ---------------------------------------------------------------------------
# read / restart / logs / delete
# ---------------------------------------------------------------------------


def test_list_and_get(client):
    client.post("/v1/services", json=_spec(replicas=1), headers=AUTH)
    listed = client.get("/v1/services", headers=AUTH).json()["services"]
    assert [s["service_id"] for s in listed] == ["web"]

    got = client.get("/v1/services/web", headers=AUTH)
    assert got.status_code == 200
    assert got.json()["image"] == "ghcr.io/acme/web:1"


def test_get_unknown_is_404(client):
    assert client.get("/v1/services/nope", headers=AUTH).status_code == 404


def test_restart_hits_every_replica(client, docker):
    client.post("/v1/services", json=_spec(), headers=AUTH)
    r = client.post("/v1/services/web/restart", headers=AUTH)
    assert r.status_code == 200
    restarted = [c[-1] for c in docker.calls if c[:2] == ["docker", "restart"]]
    assert sorted(restarted) == ["mo-svc-web-0", "mo-svc-web-1"]


def test_restart_unknown_is_404(client):
    assert client.post("/v1/services/web/restart", headers=AUTH).status_code == 404


def test_logs_merge_replicas(client):
    client.post("/v1/services", json=_spec(), headers=AUTH)
    r = client.get("/v1/services/web/logs", params={"tail": 10}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["service_id"] == "web"
    assert "--- replica 0 ---" in body["logs"]
    assert "--- replica 1 ---" in body["logs"]
    assert "listening on :8080" in body["logs"]


def test_logs_unknown_is_404(client):
    assert client.get("/v1/services/web/logs", headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------
# live log tail (`/logs/stream`)
# ---------------------------------------------------------------------------


class _FakeLogProc:
    """One ``docker logs --follow`` child: buffered lines, then EOF.

    ``live=True`` models a running container: the child prints its lines and
    then blocks, the way ``docker logs -f`` waits for more output, until
    :meth:`kill` unblocks it.
    """

    def __init__(self, lines: list[str], *, exit_code: int = 0, live: bool = False) -> None:
        self.stdout = self
        self._lines = lines
        self._live = live
        self._unblock = threading.Event()
        self.exit_code = exit_code
        self.killed = False
        self.closed = False
        self.returncode: int | None = None

    def __iter__(self):
        for line in self._lines:
            yield line
        if self._live:
            self._unblock.wait(timeout=5)

    def close(self) -> None:
        self.closed = True

    def wait(self) -> int:
        if self.returncode is None:
            self.returncode = -9 if self.killed else self.exit_code
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._unblock.set()


class _FakePopen:
    """Records the argv of every tail child and hands back canned lines."""

    def __init__(
        self, scripts: dict[str, list[str]] | None = None, *, live: bool = False
    ) -> None:
        self.calls: list[list[str]] = []
        self.procs: list[_FakeLogProc] = []
        self.scripts = scripts or {}
        self.live = live

    def __call__(self, argv):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        proc = _FakeLogProc(
            self.scripts.get(argv[-1], ["2026-10-26T10:00:00.000000001Z listening\n"]),
            live=self.live,
        )
        self.procs.append(proc)
        return proc


def _popen_client(tmp_path: Path, docker: _Docker, popen: _FakePopen) -> TestClient:
    return TestClient(
        create_app(
            state_dir=tmp_path,
            token_env=TOKEN_ENV,
            _run_fn=docker,
            _run_input_fn=docker.run_input,
            _popen_fn=popen,
        )
    )


def _ndjson(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_log_stream_frames_both_replicas(monkeypatch, tmp_path, docker):
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    popen = _FakePopen(
        {
            "mo-svc-web-0": [
                "2026-10-26T10:00:00.100000000Z ready\n",
                "2026-10-26T10:00:01.000000000Z GET /health 200\n",
            ],
            "mo-svc-web-1": ["2026-10-26T10:00:00.200000000Z ready\n"],
        }
    )
    client = _popen_client(tmp_path, docker, popen)
    client.post("/v1/services", json=_spec(), headers=AUTH)

    r = client.get("/v1/services/web/logs/stream", params={"tail": 5}, headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert r.headers["cache-control"] == "no-store"

    frames = _ndjson(r)
    bodies = [f for f in frames if not f.get("eof")]
    eofs = [f for f in frames if f.get("eof")]
    assert {(f["replica"], f["line"]) for f in bodies} == {
        (0, "ready"),
        (0, "GET /health 200"),
        (1, "ready"),
    }
    assert all(f["ts"].startswith("2026-10-26T10:00:0") for f in bodies)
    assert sorted((f["replica"], f["exit_code"]) for f in eofs) == [(0, 0), (1, 0)]

    # The tail is history then follow, timestamped by Docker's clock.
    assert [c[-1] for c in popen.calls] == ["mo-svc-web-0", "mo-svc-web-1"]
    assert all("--follow" in c and "--timestamps" in c for c in popen.calls)
    assert all(c[c.index("--tail") + 1] == "5" for c in popen.calls)


def test_log_stream_one_replica(monkeypatch, tmp_path, docker):
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    popen = _FakePopen({"mo-svc-web-1": ["2026-10-26T10:00:00.000000000Z only-one\n"]})
    client = _popen_client(tmp_path, docker, popen)
    client.post("/v1/services", json=_spec(), headers=AUTH)

    r = client.get(
        "/v1/services/web/logs/stream", params={"replica": 1}, headers=AUTH
    )
    assert r.status_code == 200
    assert [c[-1] for c in popen.calls] == ["mo-svc-web-1"]
    bodies = [f for f in _ndjson(r) if not f.get("eof")]
    assert [(f["replica"], f["line"]) for f in bodies] == [(1, "only-one")]


def test_log_stream_replica_out_of_range_is_400(monkeypatch, tmp_path, docker):
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    popen = _FakePopen()
    client = _popen_client(tmp_path, docker, popen)
    client.post("/v1/services", json=_spec(), headers=AUTH)

    r = client.get("/v1/services/web/logs/stream", params={"replica": 9}, headers=AUTH)
    assert r.status_code == 400
    assert popen.calls == []


def test_log_stream_unknown_service_is_404(monkeypatch, tmp_path, docker):
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    popen = _FakePopen()
    client = _popen_client(tmp_path, docker, popen)
    r = client.get("/v1/services/web/logs/stream", headers=AUTH)
    assert r.status_code == 404
    assert popen.calls == []


def test_log_stream_requires_bearer(monkeypatch, tmp_path, docker):
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    client = _popen_client(tmp_path, docker, _FakePopen())
    assert client.get("/v1/services/web/logs/stream").status_code == 401


def test_log_line_without_docker_timestamp_is_kept():
    from mini_ork.remote.node_agent.services import _parse_log_line

    assert _parse_log_line(3, "plain text from a container\n") == {
        "replica": 3,
        "ts": None,
        "line": "plain text from a container",
    }
    assert _parse_log_line(0, "2026-10-26T10:00:00Z x\n") == {
        "replica": 0,
        "ts": "2026-10-26T10:00:00Z",
        "line": "x",
    }
    assert _parse_log_line(0, "   \n") is None


def test_follow_logs_close_kills_the_children(monkeypatch, tmp_path, docker):
    from mini_ork.remote.node_agent.services import ServiceManager

    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    popen = _FakePopen(
        {
            "mo-svc-web-0": ["2026-10-26T10:00:00.000000000Z a\n"],
            "mo-svc-web-1": ["2026-10-26T10:00:00.000000000Z b\n"],
        },
        live=True,
    )
    manager = ServiceManager(
        tmp_path, _run_fn=docker, _run_input_fn=docker.run_input, _popen_fn=popen
    )
    manager.create(_spec())

    tail = manager.follow_logs("web")
    frame = None
    deadline = time.time() + 2
    while frame is None and time.time() < deadline:
        frame = tail.poll()
        time.sleep(0.01)
    assert frame is not None and frame["line"] in {"a", "b"}

    tail.close()  # the reader went away, as when a browser tab closes
    assert all(proc.killed for proc in popen.procs)


def test_log_stream_tail_is_clamped(monkeypatch, tmp_path, docker):
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    popen = _FakePopen()
    client = _popen_client(tmp_path, docker, popen)
    client.post("/v1/services", json=_spec(), headers=AUTH)

    r = client.get(
        "/v1/services/web/logs/stream", params={"tail": 999999}, headers=AUTH
    )
    assert r.status_code == 200
    assert all(c[c.index("--tail") + 1] == "5000" for c in popen.calls)


def test_log_stream_spawn_failure_is_502(monkeypatch, tmp_path, docker):
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)

    class _BrokenPopen:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, argv):
            self.calls += 1
            raise OSError("docker: command not found")

    popen = _BrokenPopen()
    client = _popen_client(tmp_path, docker, popen)
    client.post("/v1/services", json=_spec(), headers=AUTH)

    r = client.get("/v1/services/web/logs/stream", headers=AUTH)
    assert r.status_code == 502
    # The first replica could not start, so no second child was attempted.
    assert popen.calls == 1


def test_delete_removes_containers_and_state(client, docker, tmp_path):
    client.post("/v1/services", json=_spec(), headers=AUTH)
    r = client.delete("/v1/services/web", headers=AUTH)
    assert r.status_code == 200
    assert {"cid-mo-svc-web-0", "cid-mo-svc-web-1"} <= set(docker.removed)
    assert client.get("/v1/services/web", headers=AUTH).status_code == 404
    assert json.loads((tmp_path / "services.json").read_text())["services"] == []


def test_delete_unknown_is_404(client):
    assert client.delete("/v1/services/web", headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------
# stats — one resource sample per running replica
#
# Docker prints humanized strings in two bases (``kB`` decimal for I/O,
# ``MiB`` binary for memory) and keeps I/O cumulative. The node's job is to
# report exactly that, unmodified, so the control plane can derive rates from
# consecutive samples; the tests pin the parse and the omitted-replica rule.
# ---------------------------------------------------------------------------


def _docker_stats_row(name: str, **overrides) -> dict:
    row = {
        "BlockIO": "1.2MB / 3.4MB",
        "CPUPerc": "0.15%",
        "Container": "abc123",
        "ID": "abc123",
        "MemPerc": "0.004%",
        "MemUsage": "64.5MiB / 7.653GiB",
        "Name": name,
        "NetIO": "1.2kB / 3.4MB",
        "PIDs": "3",
    }
    row.update(overrides)
    return row


def test_stats_reports_one_sample_per_running_replica(client, docker):
    client.post("/v1/services", json=_spec(), headers=AUTH)
    docker.stats = {
        "mo-svc-web-0": _docker_stats_row("mo-svc-web-0"),
        "mo-svc-web-1": _docker_stats_row("mo-svc-web-1", CPUPerc="2.5%"),
    }

    r = client.get("/v1/services/web/stats", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["service_id"] == "web"
    assert body["replicas_total"] == 2
    assert body["sampled_at"].endswith("Z")
    assert [rep["replica"] for rep in body["replicas"]] == [0, 1]
    first = body["replicas"][0]
    assert first["name"] == "mo-svc-web-0"
    assert first["cpu_pct"] == 0.15
    assert first["mem_pct"] == 0.004
    assert first["mem_used_bytes"] == round(64.5 * 1024**2)
    assert first["mem_limit_bytes"] == round(7.653 * 1024**3)
    # Decimal for the I/O pairs — 1.2kB is 1200 bytes, not 1229.
    assert first["net_rx_bytes"] == 1200
    assert first["net_tx_bytes"] == 3_400_000
    assert first["block_read_bytes"] == 1_200_000
    assert first["block_write_bytes"] == 3_400_000
    assert first["pids"] == 3
    assert body["replicas"][1]["cpu_pct"] == 2.5

    argv = next(c for c in docker.calls if c[:2] == ["docker", "stats"])
    assert argv[2] == "--no-stream"
    assert argv[-2:] == ["mo-svc-web-0", "mo-svc-web-1"]


def test_stats_omits_a_stopped_replica_but_says_how_many_were_asked_for(client, docker):
    client.post("/v1/services", json=_spec(), headers=AUTH)
    docker.stats = {"mo-svc-web-1": _docker_stats_row("mo-svc-web-1")}

    r = client.get("/v1/services/web/stats", headers=AUTH)
    body = r.json()

    assert [rep["replica"] for rep in body["replicas"]] == [1]
    assert body["replicas_total"] == 2


def test_stats_are_cumulative_as_docker_reports_them(client, docker):
    """Two samples of the same container keep counting up; the node adds nothing."""
    client.post("/v1/services", json=_spec(), headers=AUTH)
    docker.stats = {"mo-svc-web-0": _docker_stats_row("mo-svc-web-0", NetIO="5MB / 1MB")}
    first = client.get("/v1/services/web/stats", headers=AUTH).json()["replicas"][0]
    docker.stats = {"mo-svc-web-0": _docker_stats_row("mo-svc-web-0", NetIO="9MB / 1MB")}
    second = client.get("/v1/services/web/stats", headers=AUTH).json()["replicas"][0]

    assert (first["net_rx_bytes"], second["net_rx_bytes"]) == (5_000_000, 9_000_000)


def test_stats_parse_docker_units():
    from mini_ork.remote.node_agent.services import _parse_size, _split_size_pair

    assert _parse_size("0B") == 0
    assert _parse_size("512") == 512  # unitless is bytes
    assert _parse_size("1.5kB") == 1500
    assert _parse_size("2MiB") == 2 * 1024**2
    assert _parse_size("1GiB") == 1024**3
    assert _parse_size("3 TB") == 3 * 1000**4  # Docker never prints this, but a space is not a lie
    assert _parse_size("lots") is None
    assert _parse_size("12QB") is None
    assert _parse_size(None) is None
    assert _parse_size(12) is None
    assert _split_size_pair("1.2kB / 3.4MB") == (1200, 3_400_000)
    assert _split_size_pair("1kB") == (None, None)
    assert _split_size_pair(None) == (None, None)


def test_stats_degrade_unreadable_fields_to_none_not_zero(client, docker):
    client.post("/v1/services", json=_spec(), headers=AUTH)
    docker.stats = {
        "mo-svc-web-0": _docker_stats_row(
            "mo-svc-web-0", CPUPerc="--", MemPerc="", MemUsage="?", PIDs="3 (approx)"
        )
    }
    sample = client.get("/v1/services/web/stats", headers=AUTH).json()["replicas"][0]

    assert sample["cpu_pct"] is None
    assert sample["mem_pct"] is None
    assert sample["mem_used_bytes"] is None
    assert sample["mem_limit_bytes"] is None
    assert sample["pids"] is None


def test_stats_keeps_the_lines_it_got_when_docker_exits_nonzero(client, docker):
    """Docker exits non-zero when *any* named container is not running."""
    client.post("/v1/services", json=_spec(), headers=AUTH)
    docker.stats = {"mo-svc-web-0": _docker_stats_row("mo-svc-web-0")}
    docker.stats_returncode = 1
    docker.stats_stderr = "Error: No such container: mo-svc-web-1"

    r = client.get("/v1/services/web/stats", headers=AUTH)
    assert r.status_code == 200
    assert [rep["replica"] for rep in r.json()["replicas"]] == [0]


def test_stats_failure_with_no_sample_is_500(client, docker):
    client.post("/v1/services", json=_spec(), headers=AUTH)
    docker.stats_returncode = 1
    docker.stats_stderr = "Cannot connect to the Docker daemon"

    r = client.get("/v1/services/web/stats", headers=AUTH)
    assert r.status_code == 500
    assert "Cannot connect to the Docker daemon" in r.json()["detail"]


def test_stats_unknown_is_404(client):
    assert client.get("/v1/services/web/stats", headers=AUTH).status_code == 404


def test_stats_requires_bearer(client):
    assert client.get("/v1/services/web/stats").status_code == 401


# ---------------------------------------------------------------------------
# exec — maintenance the control plane owns, with the secret on stdin
#
# ``ps`` on the node shows every ``docker exec`` argument, so a value that must
# not be readable by anyone with a shell belongs on stdin. These tests pin the
# two halves of that contract: the value reaches the command, and neither argv
# nor the persisted state ever carries it.
# ---------------------------------------------------------------------------


_EXEC_BODY = {"argv": ["sh", "-c", 'IFS= read -r new; echo "$new"'], "stdin": "s3cr3t\n"}


def test_exec_runs_in_replica_zero_and_returns_its_result(client, docker):
    assert client.post("/v1/services", json=_spec(), headers=AUTH).status_code == 200
    docker.exec_stdout = "ALTER ROLE"

    r = client.post("/v1/services/web/exec", json=_EXEC_BODY, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "service_id": "web",
        "replica": 0,
        "exit_code": 0,
        "stdout": "ALTER ROLE",
        "stderr": "",
    }

    argv, stdin = docker.inputs[0]
    assert argv[:4] == ["docker", "exec", "-i", "mo-svc-web-0"]
    assert argv[4:] == _EXEC_BODY["argv"]
    assert stdin == "s3cr3t\n"
    # The whole point of stdin: the value is not an argument anyone can read.
    assert "s3cr3t" not in " ".join(argv)


def test_exec_leaves_no_trace_of_argv_or_stdin_in_state(client, tmp_path):
    assert client.post("/v1/services", json=_spec(), headers=AUTH).status_code == 200
    assert client.post("/v1/services/web/exec", json=_EXEC_BODY, headers=AUTH).status_code == 200

    persisted = (tmp_path / "services.json").read_text()
    assert "s3cr3t" not in persisted
    assert "IFS= read" not in persisted


def test_exec_refuses_a_replica_that_is_not_running(client, docker):
    assert client.post("/v1/services", json=_spec(), headers=AUTH).status_code == 200
    docker.states["mo-svc-web-0"] = "exited"

    r = client.post("/v1/services/web/exec", json={"argv": ["true"]}, headers=AUTH)
    assert r.status_code == 400
    assert "exec needs a running container" in r.text
    # Refused before docker: an exec is maintenance, not a way to wake a service.
    assert docker.inputs == []


def test_exec_of_an_unknown_service_is_404(client, docker):
    r = client.post("/v1/services/web/exec", json={"argv": ["true"]}, headers=AUTH)
    assert r.status_code == 404
    assert docker.inputs == []


@pytest.mark.parametrize(
    "body, reason",
    [
        ({}, "argv must be"),
        ({"argv": []}, "argv must be"),
        ({"argv": "psql"}, "argv must be"),
        ({"argv": [""]}, "non-empty strings"),
        ({"argv": ["ok", 3]}, "non-empty strings"),
        ({"argv": ["x" * 4097]}, "exceeds 4096 bytes"),
        ({"argv": ["x"] * 65}, "at most 64 entries"),
        ({"argv": ["x"], "stdin": "y" * ((1 << 20) + 1)}, "stdin exceeds"),
        ({"argv": ["x"], "stdin": 5}, "stdin must be a string"),
        ({"argv": ["x"], "timeout": 0}, "between 1 and 300"),
        ({"argv": ["x"], "timeout": 301}, "between 1 and 300"),
        ({"argv": ["x"], "timeout": True}, "integer number of seconds"),
        ({"argv": ["x"], "timeout": "30"}, "integer number of seconds"),
    ],
    ids=[
        "no-argv",
        "empty-argv",
        "argv-not-a-list",
        "empty-entry",
        "non-string-entry",
        "entry-too-long",
        "too-many-entries",
        "stdin-too-large",
        "stdin-not-a-string",
        "timeout-zero",
        "timeout-too-large",
        "timeout-bool",
        "timeout-string",
    ],
)
def test_a_bad_exec_shape_is_a_400_before_docker(client, docker, body, reason):
    assert client.post("/v1/services", json=_spec(), headers=AUTH).status_code == 200

    r = client.post("/v1/services/web/exec", json=body, headers=AUTH)
    assert r.status_code == 400, r.text
    assert reason in r.text
    assert docker.inputs == []


def test_exec_reports_a_nonzero_exit_as_data(client, docker):
    assert client.post("/v1/services", json=_spec(), headers=AUTH).status_code == 200
    docker.exec_exit = 2
    docker.exec_stderr = "psql: error: role does not exist"

    r = client.post("/v1/services/web/exec", json={"argv": ["psql"]}, headers=AUTH)
    # The command ran and failed. That is a result, not a transport error: the
    # caller needs the exit code and the tail of stderr to decide what next.
    assert r.status_code == 200
    assert r.json()["exit_code"] == 2
    assert r.json()["stderr"] == "psql: error: role does not exist"


def test_exec_output_is_tail_bounded(client, docker):
    assert client.post("/v1/services", json=_spec(), headers=AUTH).status_code == 200
    docker.exec_stdout = "x" * ((1 << 20) + 5)

    r = client.post("/v1/services/web/exec", json={"argv": ["cat"]}, headers=AUTH)
    assert r.status_code == 200
    stdout = r.json()["stdout"]
    assert len(stdout) == (1 << 20) + 1
    assert stdout.startswith("…")


def test_a_command_that_outlives_its_budget_is_504(client, docker):
    assert client.post("/v1/services", json=_spec(), headers=AUTH).status_code == 200
    docker.exec_timeout = True

    r = client.post(
        "/v1/services/web/exec", json={"argv": ["sleep", "999"], "timeout": 5}, headers=AUTH
    )
    assert r.status_code == 504
    assert "did not finish within 5s" in r.text


def test_exec_requires_bearer(client):
    assert client.post("/v1/services/web/exec", json={"argv": ["true"]}).status_code == 401


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def test_services_require_bearer(client):
    assert client.get("/v1/services").status_code == 401
    assert client.post("/v1/services", json=_spec()).status_code == 401


# ---------------------------------------------------------------------------
# volumes — the node owns the host side, the caller only names one
# ---------------------------------------------------------------------------


def test_create_mounts_each_volume_under_the_node_root(client, docker, tmp_path):
    r = client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/var/lib/app"}]),
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    argv = docker.run_argv()[0]
    host = tmp_path / "volumes" / "web" / "data"
    assert argv[argv.index("-v") + 1] == f"{host}:/var/lib/app:rw"
    assert host.is_dir()
    # The container user must not need a second permission to write its own
    # volume just because another tenant shares the node.
    assert host.stat().st_mode & 0o077 == 0


def test_volume_root_env_is_the_secure_tier(tmp_path, monkeypatch, docker):
    elsewhere = tmp_path / "backed-up-disk"
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    monkeypatch.setenv("MO_NODE_VOLUME_ROOT", str(elsewhere))
    app = create_app(state_dir=tmp_path / "state", token_env=TOKEN_ENV, _run_fn=docker)
    r = TestClient(app).post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}]),
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    assert (elsewhere / "web" / "data").is_dir()
    assert not (tmp_path / "state" / "volumes").exists()


def test_a_relative_volume_root_refuses_to_start(tmp_path, monkeypatch, docker):
    from mini_ork.remote.node_agent.services import ServiceError

    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    monkeypatch.setenv("MO_NODE_VOLUME_ROOT", "volumes")
    with pytest.raises(ServiceError, match="MO_NODE_VOLUME_ROOT.*absolute"):
        create_app(state_dir=tmp_path, token_env=TOKEN_ENV, _run_fn=docker)


@pytest.mark.parametrize(
    "volume, reason",
    [
        ({"name": "../etc", "mount_path": "/etc"}, "volume name"),
        ({"name": "Data", "mount_path": "/data"}, "volume name"),
        ({"name": "data", "mount_path": "data"}, "absolute"),
        ({"name": "data", "mount_path": "/data:rw"}, "absolute"),
        ({"name": "data", "mount_path": "/a/../b"}, "absolute"),
        ({"name": "data"}, "absolute"),
    ],
)
def test_bad_volume_shapes_are_400s_before_docker(client, docker, volume, reason):
    r = client.post("/v1/services", json=_spec(volumes=[volume]), headers=AUTH)
    assert r.status_code == 400, r.text
    assert reason in r.json()["detail"]
    assert docker.run_argv() == []


def test_two_volumes_cannot_share_a_name_or_a_mount_path(client, docker):
    same_name = client.post(
        "/v1/services",
        json=_spec(
            volumes=[
                {"name": "data", "mount_path": "/data"},
                {"name": "data", "mount_path": "/other"},
            ]
        ),
        headers=AUTH,
    )
    assert same_name.status_code == 400
    assert "duplicate volume name" in same_name.json()["detail"]

    same_path = client.post(
        "/v1/services",
        json=_spec(
            volumes=[
                {"name": "one", "mount_path": "/data"},
                {"name": "two", "mount_path": "/data"},
            ]
        ),
        headers=AUTH,
    )
    assert same_path.status_code == 400
    assert "same path" in same_path.json()["detail"]
    assert docker.run_argv() == []


def test_delete_keeps_the_volume_data(client, tmp_path):
    client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}]),
        headers=AUTH,
    )
    marker = tmp_path / "volumes" / "web" / "data" / "leave-me.txt"
    marker.write_text("tenant data", encoding="utf-8")

    assert client.delete("/v1/services/web", headers=AUTH).status_code == 200
    assert marker.read_text(encoding="utf-8") == "tenant data"


def test_status_reports_volumes_but_never_the_host_path(client):
    client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/var/lib/app"}]),
        headers=AUTH,
    )
    body = client.get("/v1/services/web", headers=AUTH).json()
    assert body["volumes"] == [{"name": "data", "mount_path": "/var/lib/app"}]
    assert "volumes/" not in json.dumps(body)


def test_a_running_replica_reports_no_completion(client):
    client.post("/v1/services", json=_spec(replicas=1), headers=AUTH)
    replica = client.get("/v1/services/web", headers=AUTH).json()["replicas_state"][0]
    assert replica["state"] == "running"
    assert replica["exit_code"] is None
    # Docker's "never finished" zero value must not leak as a 0001 date.
    assert replica["finished_at"] is None


def test_a_one_shot_replica_reports_its_exit_code_and_finish_time(client, docker):
    """A cron run's exit code is its verdict; a state-only view cannot tell
    a successful job from a crashed one."""
    client.post(
        "/v1/services",
        json=_spec(replicas=1, restart_policy="no", ports=[]),
        headers=AUTH,
    )
    docker.states["mo-svc-web-0"] = "exited"
    docker.exit_codes["mo-svc-web-0"] = 3
    docker.finished["mo-svc-web-0"] = "2026-10-25T09:00:00.123456789Z"

    replica = client.get("/v1/services/web", headers=AUTH).json()["replicas_state"][0]
    assert replica["state"] == "exited"
    assert replica["exit_code"] == 3
    assert replica["finished_at"] == "2026-10-25T09:00:00.123456789Z"



def test_the_published_schema_rejects_the_same_shapes():
    """The contract and the node agree — a control plane fails at build time.

    The node validates too (it cannot trust its caller), but a tenant should
    learn that `/data:rw` is not a mount path from ``mini-ork validate``, not
    from a 400 halfway through a deploy.
    """
    jsonschema = pytest.importorskip("jsonschema")
    import json as _json

    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "deployment.schema.json"
    schema = _json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    ok = _spec(volumes=[{"name": "data", "mount_path": "/var/lib/app"}])
    assert list(validator.iter_errors(ok)) == []
    ok_port = _spec(ports=[{"container": 5432, "host": 20001, "protocol": "udp", "bind": "10.0.0.5"}])
    assert list(validator.iter_errors(ok_port)) == []

    for bad_port in (
        [{"container": 5432, "protocol": "sctp"}],
        [{"container": 5432, "protocol": "tcp", "bind": ""}],
    ):
        assert list(validator.iter_errors(_spec(ports=bad_port))), bad_port

    for bad in (
        [{"name": "data", "mount_path": "data"}],
        [{"name": "data", "mount_path": "/data:rw"}],
        [{"name": "data", "mount_path": "/a/../b"}],
        [{"name": "../etc", "mount_path": "/etc"}],
        [{"name": "data", "mount_path": "/data", "host": "/etc"}],
    ):
        assert list(validator.iter_errors(_spec(volumes=bad))), bad


# ---------------------------------------------------------------------------
# volume snapshots — the node copies, the control plane decides when
# ---------------------------------------------------------------------------

SNAPSHOT = "/v1/services/web/volumes/data/snapshots"


def _create_with_volume(client) -> Path:
    r = client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}]),
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    return Path(client.app.state.state_dir) / "volumes" / "web" / "data"


def _volume_dir(tmp_path: Path) -> Path:
    return tmp_path / "volumes" / "web" / "data"


def test_snapshot_writes_an_archive_and_its_metadata(client, tmp_path):
    client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}]),
        headers=AUTH,
    )
    volume = _volume_dir(tmp_path)
    (volume / "state.json").write_text('{"counter": 1}', encoding="utf-8")
    (volume / "nested").mkdir()
    (volume / "nested" / "blob.bin").write_bytes(b"\x00\x01" * 1024)

    r = client.post(SNAPSHOT, headers=AUTH)
    assert r.status_code == 200, r.text
    meta = r.json()
    assert len(meta["snapshot_id"]) == 32
    assert meta["volume"] == "data"
    assert meta["sha256"] and len(meta["sha256"]) == 64

    archive = tmp_path / "volume-snapshots" / "web" / "data" / f"{meta['snapshot_id']}.tar.gz"
    assert archive.is_file()
    assert archive.stat().st_size == meta["bytes"]

    # Metadata and archive agree: the size the control plane is about to store
    # is the size of the file that exists, not of a directory estimate.
    import hashlib

    assert hashlib.sha256(archive.read_bytes()).hexdigest() == meta["sha256"]


def test_snapshot_refuses_a_volume_that_never_existed(client):
    r = client.post(SNAPSHOT, headers=AUTH)
    assert r.status_code == 404
    assert "no data on this node" in r.json()["detail"]


def test_an_empty_volume_is_a_real_snapshot(client, tmp_path):
    client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}]),
        headers=AUTH,
    )
    r = client.post(SNAPSHOT, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["bytes"] > 0  # a valid empty tar.gz


def test_a_quiesced_snapshot_stops_the_owner_and_starts_it_again(client, docker):
    client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}], replicas=1),
        headers=AUTH,
    )
    r = client.post(SNAPSHOT, headers=AUTH, json={"quiesce": True})
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["quiesced"] is True
    assert meta["quiesce_stopped"] == 1
    assert "quiesce_restart_failed" not in meta

    verbs = [c[1] for c in docker.calls if c[:2] in (["docker", "stop"], ["docker", "start"])]
    assert verbs == ["stop", "start"]
    name = "mo-svc-web-0"
    assert docker.states[name] == "running"
    # The stop carries a grace period: a database needs long enough to flush.
    stop = next(c for c in docker.calls if c[:2] == ["docker", "stop"])
    assert stop[2] == "--time" and stop[3] == "10"


def test_a_crash_consistent_snapshot_does_not_stop_the_service(client, docker):
    client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}], replicas=1),
        headers=AUTH,
    )
    r = client.post(SNAPSHOT, headers=AUTH, json={"quiesce": False})
    assert r.status_code == 200, r.text
    assert r.json()["quiesced"] is False
    assert not [c for c in docker.calls if c[:2] == ["docker", "stop"]]


def test_quiesce_refuses_when_a_replica_will_not_stop(client, docker, tmp_path):
    client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}], replicas=1),
        headers=AUTH,
    )
    docker.fail_stop = "mo-svc-web"
    r = client.post(SNAPSHOT, headers=AUTH, json={"quiesce": True})
    assert r.status_code == 400
    assert "could not stop" in r.json()["detail"]
    # Fail closed: no partial archive was written for a copy that is not the
    # consistent one the caller asked for.
    assert not list((tmp_path / "volume-snapshots" / "web" / "data").glob("*.tar.gz"))


def test_a_snapshot_whose_service_did_not_restart_says_so(client, docker):
    client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}], replicas=1),
        headers=AUTH,
    )
    docker.fail_start = "mo-svc-web"
    r = client.post(SNAPSHOT, headers=AUTH, json={"quiesce": True})
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "did not restart" in detail
    assert "mo-svc-web-0" in detail
    # The bytes are real: the sidecar exists, so the backup is listed even
    # though the request errored — the operator can act on both facts.
    listed = client.get(SNAPSHOT, headers=AUTH).json()["snapshots"]
    assert listed and listed[0]["quiesce_restart_failed"] == ["mo-svc-web-0"]


def test_quiesce_of_a_deleted_service_is_refused(client, docker):
    client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}], replicas=1),
        headers=AUTH,
    )
    assert client.delete("/v1/services/web", headers=AUTH).status_code == 200
    r = client.post(SNAPSHOT, headers=AUTH, json={"quiesce": True})
    assert r.status_code == 400
    assert "not defined on this node" in r.json()["detail"]
    assert not [c for c in docker.calls if c[:2] == ["docker", "stop"]]


def test_the_quiesce_flag_must_be_a_boolean(client, docker):
    client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}], replicas=1),
        headers=AUTH,
    )
    r = client.post(SNAPSHOT, headers=AUTH, json={"quiesce": "false"})
    assert r.status_code == 400
    assert "quiesce must be a boolean" in r.json()["detail"]
    assert not [c for c in docker.calls if c[:2] == ["docker", "stop"]]


def test_list_is_newest_first_and_skips_unanchored_archives(client, tmp_path):
    client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}]),
        headers=AUTH,
    )
    first = client.post(SNAPSHOT, headers=AUTH).json()["snapshot_id"]
    second = client.post(SNAPSHOT, headers=AUTH).json()["snapshot_id"]
    # A crash between archive and sidecar must not produce a listed snapshot
    # that cannot be trusted; the orphan is invisible.
    (tmp_path / "volume-snapshots" / "web" / "data" / f"{'f' * 32}.tar.gz").write_bytes(b"x")

    r = client.get(SNAPSHOT, headers=AUTH)
    assert r.status_code == 200
    ids = [s["snapshot_id"] for s in r.json()["snapshots"]]
    assert ids[:2] == [second, first]
    assert "f" * 32 not in ids


def test_restore_round_trips_the_volume(client, tmp_path):
    client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}]),
        headers=AUTH,
    )
    volume = _volume_dir(tmp_path)
    (volume / "keep.txt").write_text("before", encoding="utf-8")
    snap = client.post(SNAPSHOT, headers=AUTH).json()["snapshot_id"]

    (volume / "keep.txt").write_text("after", encoding="utf-8")
    (volume / "dropped.txt").write_text("new", encoding="utf-8")

    # A deleted service still owns its data; stopping is a deployment action,
    # not a data-retention decision, so restore must work on its volumes.
    assert client.delete("/v1/services/web", headers=AUTH).status_code == 200

    r = client.post(
        "/v1/services/web/volumes/data/restore",
        json={"snapshot_id": snap},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    assert r.json()["restored"] is True
    assert (volume / "keep.txt").read_text(encoding="utf-8") == "before"
    assert not (volume / "dropped.txt").exists()


def test_restore_is_refused_while_a_container_holds_the_volume(client, tmp_path):
    client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}]),
        headers=AUTH,
    )
    volume = _volume_dir(tmp_path)
    (volume / "keep.txt").write_text("before", encoding="utf-8")
    snap = client.post(SNAPSHOT, headers=AUTH).json()["snapshot_id"]
    (volume / "keep.txt").write_text("live", encoding="utf-8")

    r = client.post(
        "/v1/services/web/volumes/data/restore",
        json={"snapshot_id": snap},
        headers=AUTH,
    )
    assert r.status_code == 409
    assert "hold the volume" in r.json()["detail"]
    assert (volume / "keep.txt").read_text(encoding="utf-8") == "live"


def test_restore_refuses_an_unknown_or_malformed_snapshot(client):
    client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}]),
        headers=AUTH,
    )
    unknown = client.post(
        "/v1/services/web/volumes/data/restore",
        json={"snapshot_id": "0" * 32},
        headers=AUTH,
    )
    assert unknown.status_code == 404

    # The id is a path component; anything that is not the minted shape is
    # refused before it can name a file.
    traversal = client.post(
        "/v1/services/web/volumes/data/restore",
        json={"snapshot_id": "../../services"},
        headers=AUTH,
    )
    assert traversal.status_code == 400
    assert "32 lowercase hex" in traversal.json()["detail"]


def test_delete_removes_the_snapshot_and_reports_a_second_delete(client, tmp_path):
    client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}]),
        headers=AUTH,
    )
    snap = client.post(SNAPSHOT, headers=AUTH).json()["snapshot_id"]
    assert client.delete(f"{SNAPSHOT}/{snap}", headers=AUTH).status_code == 200
    assert client.get(SNAPSHOT, headers=AUTH).json()["count"] == 0
    assert client.delete(f"{SNAPSHOT}/{snap}", headers=AUTH).status_code == 404


def test_snapshot_root_env_is_the_secure_tier(tmp_path, monkeypatch, docker):
    elsewhere = tmp_path / "backup-disk"
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    monkeypatch.setenv("MO_NODE_SNAPSHOT_ROOT", str(elsewhere))
    app = create_app(state_dir=tmp_path / "state", token_env=TOKEN_ENV, _run_fn=docker)
    c = TestClient(app)
    c.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}]),
        headers=AUTH,
    )
    r = c.post(SNAPSHOT, headers=AUTH)
    assert r.status_code == 200, r.text
    assert (elsewhere / "web" / "data" / f"{r.json()['snapshot_id']}.tar.gz").is_file()
    assert not (tmp_path / "state" / "volume-snapshots").exists()


def test_a_relative_snapshot_root_refuses_to_start(tmp_path, monkeypatch, docker):
    from mini_ork.remote.node_agent.services import ServiceError

    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    monkeypatch.setenv("MO_NODE_SNAPSHOT_ROOT", "backups")
    with pytest.raises(ServiceError, match="MO_NODE_SNAPSHOT_ROOT.*absolute"):
        create_app(state_dir=tmp_path, token_env=TOKEN_ENV, _run_fn=docker)


def test_snapshot_routes_require_bearer(client):
    for method, path in (
        ("post", SNAPSHOT),
        ("get", SNAPSHOT),
        ("delete", f"{SNAPSHOT}/{'0' * 32}"),
        ("get", f"{SNAPSHOT}/{'0' * 32}/blob"),
        ("put", f"{SNAPSHOT}/{'0' * 32}/blob"),
        ("post", "/v1/services/web/volumes/data/restore"),
    ):
        r = getattr(client, method)(path, **({"json": {"snapshot_id": "0" * 32}} if method == "post" else {}))
        assert r.status_code in (401, 403), (method, path, r.status_code)


# ---------------------------------------------------------------------------
# shipping a snapshot off-node — download, import, cross-node restore
# ---------------------------------------------------------------------------


def _archive_bytes(files: dict[str, bytes]) -> bytes:
    """A real tar.gz, so an imported snapshot can be restored, not just listed."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _sha(body: bytes) -> str:
    import hashlib

    return hashlib.sha256(body).hexdigest()


def test_export_blob_returns_the_archive_and_its_digest(client, tmp_path):
    client.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}]),
        headers=AUTH,
    )
    (tmp_path / "volumes" / "web" / "data" / "state.json").write_bytes(b'{"n":1}')
    meta = client.post(SNAPSHOT, headers=AUTH).json()

    r = client.get(f"{SNAPSHOT}/{meta['snapshot_id']}/blob", headers=AUTH)

    assert r.status_code == 200, r.text
    on_disk = (tmp_path / "volume-snapshots" / "web" / "data" / f"{meta['snapshot_id']}.tar.gz").read_bytes()
    assert r.content == on_disk
    # The headers repeat what the sidecar says, so a shipper can verify the
    # body it just received without a second call that a delete could race.
    assert r.headers["x-content-sha256"] == meta["sha256"]
    assert r.headers["x-content-bytes"] == str(meta["bytes"])
    assert r.headers["content-type"].startswith("application/gzip")
    assert f"{meta['snapshot_id']}.tar.gz" in r.headers["content-disposition"]


def test_export_blob_refuses_unknown_and_malformed_ids(client):
    assert client.get(f"{SNAPSHOT}/{'0' * 32}/blob", headers=AUTH).status_code == 404
    # An id that is not the minted shape is a bad request, not a lookup miss:
    # the id is a path component and never a caller-chosen path.
    r = client.get(f"{SNAPSHOT}/{'A' * 32}/blob", headers=AUTH)
    assert r.status_code == 400
    assert "32 lowercase hex" in r.json()["detail"]


def test_import_then_restore_round_trips_bytes(client, tmp_path):
    body = _archive_bytes({"imported.txt": b"from another node"})
    snap = "a" * 32

    r = client.put(
        f"{SNAPSHOT}/{snap}/blob",
        content=body,
        headers={**AUTH, "X-Content-Sha256": _sha(body)},
    )

    assert r.status_code == 200, r.text
    assert r.json()["imported"] is True
    assert r.json()["bytes"] == len(body)
    assert r.json()["sha256"] == _sha(body)
    assert client.get(SNAPSHOT, headers=AUTH).json()["count"] == 1

    restored = client.post(
        "/v1/services/web/volumes/data/restore",
        json={"snapshot_id": snap},
        headers=AUTH,
    )
    assert restored.status_code == 200, restored.text
    assert (tmp_path / "volumes" / "web" / "data" / "imported.txt").read_bytes() == b"from another node"


def test_a_repeat_import_of_the_same_bytes_is_a_no_op(client, tmp_path):
    body = _archive_bytes({"one.txt": b"1"})
    snap = "b" * 32
    headers = {**AUTH, "X-Content-Sha256": _sha(body)}

    first = client.put(f"{SNAPSHOT}/{snap}/blob", content=body, headers=headers)
    created_at = first.json()["created_at"]
    second = client.put(f"{SNAPSHOT}/{snap}/blob", content=body, headers=headers)

    assert second.status_code == 200, second.text
    assert second.json()["imported"] is False
    assert second.json()["created_at"] == created_at
    assert client.get(SNAPSHOT, headers=AUTH).json()["count"] == 1
    # No temporary files leaked from either attempt.
    assert not list((tmp_path / "volume-snapshots" / "web" / "data").glob(".*.import-*"))


def test_import_refuses_different_bytes_for_a_taken_id(client):
    snap = "c" * 32
    first = _archive_bytes({"one.txt": b"1"})
    client.put(
        f"{SNAPSHOT}/{snap}/blob", content=first, headers={**AUTH, "X-Content-Sha256": _sha(first)}
    )
    other = _archive_bytes({"two.txt": b"2"})

    r = client.put(
        f"{SNAPSHOT}/{snap}/blob", content=other, headers={**AUTH, "X-Content-Sha256": _sha(other)}
    )

    assert r.status_code == 409
    assert "different content" in r.json()["detail"]


def test_a_sha_mismatch_admits_nothing(client, tmp_path):
    body = _archive_bytes({"x.txt": b"x"})
    snap = "d" * 32

    r = client.put(
        f"{SNAPSHOT}/{snap}/blob",
        content=body,
        headers={**AUTH, "X-Content-Sha256": "e" * 64},
    )

    assert r.status_code == 400
    assert "does not match" in r.json()["detail"]
    assert client.get(SNAPSHOT, headers=AUTH).json()["count"] == 0
    out_dir = tmp_path / "volume-snapshots" / "web" / "data"
    assert not list(out_dir.glob(f"{snap}.*"))
    assert not list(out_dir.glob(".*.import-*"))


def test_import_requires_a_digest_header(client):
    body = _archive_bytes({"x.txt": b"x"})
    r = client.put(f"{SNAPSHOT}/{'f' * 32}/blob", content=body, headers=AUTH)
    assert r.status_code == 400
    assert "X-Content-Sha256" in r.json()["detail"]


def test_import_refuses_a_body_over_the_configured_cap(tmp_path, monkeypatch, docker):
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    monkeypatch.setenv("MO_NODE_SNAPSHOT_MAX_BYTES", "64")
    c = TestClient(create_app(state_dir=tmp_path, token_env=TOKEN_ENV, _run_fn=docker))
    body = _archive_bytes({"big.bin": b"x" * 4096})

    r = c.put(
        f"{SNAPSHOT}/{'a' * 32}/blob",
        content=body,
        headers={**AUTH, "X-Content-Sha256": _sha(body)},
    )

    assert r.status_code == 413
    assert c.get(SNAPSHOT, headers=AUTH).json()["count"] == 0


def test_a_malformed_snapshot_cap_refuses_to_start(tmp_path, monkeypatch, docker):
    from mini_ork.remote.node_agent.services import ServiceError

    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    monkeypatch.setenv("MO_NODE_SNAPSHOT_MAX_BYTES", "16GiB")
    with pytest.raises(ServiceError, match="MO_NODE_SNAPSHOT_MAX_BYTES.*positive integer"):
        create_app(state_dir=tmp_path, token_env=TOKEN_ENV, _run_fn=docker)


def test_a_snapshot_shipped_from_one_node_restores_on_another(tmp_path, monkeypatch, docker):
    """The disaster-recovery story: bytes leave a node and land on a new one."""
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)
    source_app = create_app(state_dir=tmp_path / "a", token_env=TOKEN_ENV, _run_fn=docker)
    a = TestClient(source_app)
    a.post(
        "/v1/services",
        json=_spec(volumes=[{"name": "data", "mount_path": "/data"}]),
        headers=AUTH,
    )
    (tmp_path / "a" / "volumes" / "web" / "data" / "app.db").write_bytes(b"tenant rows")
    meta = a.post(SNAPSHOT, headers=AUTH).json()
    shipped = a.get(f"{SNAPSHOT}/{meta['snapshot_id']}/blob", headers=AUTH)
    assert shipped.status_code == 200

    target_app = create_app(state_dir=tmp_path / "b", token_env=TOKEN_ENV, _run_fn=docker)
    b = TestClient(target_app)
    imported = b.put(
        f"{SNAPSHOT}/{meta['snapshot_id']}/blob",
        content=shipped.content,
        headers={**AUTH, "X-Content-Sha256": shipped.headers["x-content-sha256"]},
    )
    assert imported.status_code == 200, imported.text

    restored = b.post(
        "/v1/services/web/volumes/data/restore",
        json={"snapshot_id": meta["snapshot_id"]},
        headers=AUTH,
    )
    assert restored.status_code == 200, restored.text
    assert (tmp_path / "b" / "volumes" / "web" / "data" / "app.db").read_bytes() == b"tenant rows"


# ---------------------------------------------------------------------------
# private networking
# ---------------------------------------------------------------------------

NETWORK = "attestia-env-1f2a3b4c"
ALIASES = ["db", "db-1f2a3b4c.internal"]


def test_create_joins_the_environment_network_under_its_aliases(client, docker):
    r = client.post(
        "/v1/services",
        json=_spec(network=NETWORK, network_aliases=ALIASES),
        headers=AUTH,
    )
    assert r.status_code == 200, r.text

    assert docker.network_creates == [
        ["docker", "network", "create", "--driver", "bridge", NETWORK]
    ]
    argv = docker.run_argv()[0]
    assert argv[argv.index("--network") + 1] == NETWORK
    aliases = [argv[i + 1] for i, a in enumerate(argv) if a == "--network-alias"]
    assert aliases == ALIASES


def test_a_second_service_reuses_the_network_instead_of_recreating_it(client, docker):
    first = client.post(
        "/v1/services",
        json=_spec(service_id="web", network=NETWORK, network_aliases=["web"]),
        headers=AUTH,
    )
    second = client.post(
        "/v1/services",
        json=_spec(service_id="api", network=NETWORK, network_aliases=["api"]),
        headers=AUTH,
    )

    assert first.status_code == 200 and second.status_code == 200
    assert len(docker.network_creates) == 1
    assert docker.network_creates[0][-1] == NETWORK


def test_network_driver_is_the_nodes_choice(client, docker, monkeypatch):
    monkeypatch.setenv("MO_NODE_NETWORK_DRIVER", "overlay")

    r = client.post(
        "/v1/services",
        json=_spec(network=NETWORK, network_aliases=["db"]),
        headers=AUTH,
    )

    assert r.status_code == 200, r.text
    assert docker.network_creates[0][
        docker.network_creates[0].index("--driver") + 1
    ] == "overlay"


def test_an_unknown_network_driver_refuses_before_creating_anything(
    client, docker, monkeypatch
):
    monkeypatch.setenv("MO_NODE_NETWORK_DRIVER", "macvlan")

    r = client.post(
        "/v1/services",
        json=_spec(network=NETWORK, network_aliases=["db"]),
        headers=AUTH,
    )

    assert r.status_code == 500
    assert "MO_NODE_NETWORK_DRIVER" in r.json()["detail"]
    assert docker.network_creates == []
    assert docker.run_argv() == []


def test_a_network_the_daemon_refuses_is_a_loud_failure(client, docker):
    # The overlay driver on a node that never joined a swarm lands here: the
    # deploy must fail, not fall back to an isolated bridge.
    docker.fail_network = "operation is not permitted"

    r = client.post(
        "/v1/services",
        json=_spec(network=NETWORK, network_aliases=["db"]),
        headers=AUTH,
    )

    assert r.status_code == 500
    assert "network create failed" in r.json()["detail"]
    assert docker.run_argv() == []
    assert not (docker.networks)


def test_an_alias_without_a_network_is_refused(client):
    r = client.post(
        "/v1/services",
        json=_spec(network_aliases=["db"]),
        headers=AUTH,
    )
    assert r.status_code == 400
    assert "requires network" in r.json()["detail"]


@pytest.mark.parametrize("alias", ["Bad_Name", "-db", "db..internal", "db.", "a/b"])
def test_a_malformed_alias_is_refused(client, alias):
    r = client.post(
        "/v1/services",
        json=_spec(network=NETWORK, network_aliases=[alias]),
        headers=AUTH,
    )
    assert r.status_code == 400
    assert "network alias" in r.json()["detail"]


def test_duplicate_aliases_are_collapsed(client, docker):
    r = client.post(
        "/v1/services",
        json=_spec(network=NETWORK, network_aliases=["db", "db", "db.internal"]),
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    argv = docker.run_argv()[0]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--network-alias"] == [
        "db",
        "db.internal",
    ]


def test_aliases_survive_the_state_file(client, tmp_path):
    r = client.post(
        "/v1/services",
        json=_spec(network=NETWORK, network_aliases=ALIASES),
        headers=AUTH,
    )
    assert r.status_code == 200, r.text

    data = json.loads((tmp_path / "services.json").read_text(encoding="utf-8"))
    assert data["services"][0]["network"] == NETWORK
    assert data["services"][0]["network_aliases"] == ALIASES


PAIR = "attestia-pair-1f2a3b4c-9d8e7f60"
PAIR_ALIASES = ["db", "db-1f2a3b4c.internal"]


def test_extra_networks_are_created_then_attached_after_start(client, docker):
    r = client.post(
        "/v1/services",
        json=_spec(
            network=NETWORK,
            network_aliases=["db"],
            additional_networks=[{"name": PAIR, "aliases": PAIR_ALIASES}],
        ),
        headers=AUTH,
    )
    assert r.status_code == 200, r.text

    # Both networks exist before the first container, so a connect can never
    # race the create.
    assert [c[-1] for c in docker.network_creates] == [NETWORK, PAIR]
    assert docker.network_connects == [
        ["docker", "network", "connect", "--alias", PAIR_ALIASES[0], "--alias",
         PAIR_ALIASES[1], PAIR, "mo-svc-web-0"],
        ["docker", "network", "connect", "--alias", PAIR_ALIASES[0], "--alias",
         PAIR_ALIASES[1], PAIR, "mo-svc-web-1"],
    ]
    # ``docker run`` still carries exactly one --network: the primary.
    argv = docker.run_argv()[0]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--network"] == [NETWORK]


def test_extra_networks_survive_the_state_file(client, tmp_path):
    r = client.post(
        "/v1/services",
        json=_spec(
            network=NETWORK,
            additional_networks=[{"name": PAIR, "aliases": ["db"]}],
        ),
        headers=AUTH,
    )
    assert r.status_code == 200, r.text

    data = json.loads((tmp_path / "services.json").read_text(encoding="utf-8"))
    assert data["services"][0]["additional_networks"] == [
        {"name": PAIR, "aliases": ["db"]}
    ]


def test_a_failed_attach_removes_the_replicas_already_started(client, docker):
    # A replica on only some of its networks is either reachable by a peer the
    # policy excludes or unreachable by one it names; neither is a half-served
    # service, so the create fails closed.
    docker.fail_network_connect = "network not found"

    r = client.post(
        "/v1/services",
        json=_spec(
            service_id="db",
            network=NETWORK,
            additional_networks=[{"name": PAIR, "aliases": ["db"]}],
        ),
        headers=AUTH,
    )

    assert r.status_code == 500
    assert "docker network connect failed" in r.json()["detail"]
    assert PAIR in r.json()["detail"]
    # The first attach already failed, so only the first replica existed to
    # remove — and leaving it running would be the half-connected service.
    assert docker.removed == ["cid-mo-svc-db-0"]
    assert client.get("/v1/services/db", headers=AUTH).status_code == 404


def test_extra_networks_without_a_primary_network_are_refused(client):
    r = client.post(
        "/v1/services",
        json=_spec(additional_networks=[{"name": PAIR}]),
        headers=AUTH,
    )
    assert r.status_code == 400
    assert "requires network" in r.json()["detail"]


def test_the_primary_network_cannot_be_repeated_as_an_extra_one(client):
    r = client.post(
        "/v1/services",
        json=_spec(network=NETWORK, additional_networks=[{"name": NETWORK}]),
        headers=AUTH,
    )
    assert r.status_code == 400
    assert "repeats the primary network" in r.json()["detail"]


@pytest.mark.parametrize("name", ["Pair_Network", "-pair", "pair network", "p" * 64])
def test_a_malformed_extra_network_name_is_refused(client, name):
    r = client.post(
        "/v1/services",
        json=_spec(network=NETWORK, additional_networks=[{"name": name}]),
        headers=AUTH,
    )
    assert r.status_code == 400
    assert "additional network name" in r.json()["detail"]


def test_duplicate_extra_networks_are_refused(client):
    r = client.post(
        "/v1/services",
        json=_spec(
            network=NETWORK,
            additional_networks=[{"name": PAIR}, {"name": PAIR}],
        ),
        headers=AUTH,
    )
    assert r.status_code == 400
    assert "duplicate additional network" in r.json()["detail"]


def test_at_most_eight_extra_networks(client):
    r = client.post(
        "/v1/services",
        json=_spec(
            network=NETWORK,
            additional_networks=[{"name": f"pair-{i}"} for i in range(9)],
        ),
        headers=AUTH,
    )
    assert r.status_code == 400
    assert "at most 8 additional networks" in r.json()["detail"]


def test_a_malformed_alias_on_an_extra_network_is_refused(client):
    r = client.post(
        "/v1/services",
        json=_spec(
            network=NETWORK,
            additional_networks=[{"name": PAIR, "aliases": ["Bad_Name"]}],
        ),
        headers=AUTH,
    )
    assert r.status_code == 400
    assert "network alias" in r.json()["detail"]


def test_delete_network_removes_an_empty_network(client, docker):
    client.post(
        "/v1/services",
        json=_spec(network=NETWORK, network_aliases=["db"]),
        headers=AUTH,
    )
    # Docker only removes a network once nothing is attached, so the teardown
    # order is services first, network second.
    assert client.delete("/v1/services/web", headers=AUTH).status_code == 200
    assert NETWORK in docker.networks

    r = client.delete(f"/v1/networks/{NETWORK}", headers=AUTH)

    assert r.status_code == 200, r.text
    assert r.json() == {"name": NETWORK, "removed": True}
    assert docker.network_removes == [["docker", "network", "rm", NETWORK]]
    assert NETWORK not in docker.networks


def test_delete_network_is_idempotent_when_it_is_already_gone(client, docker):
    # No network on this node — a teardown retried after a partial success
    # must not fail, and must not run rm on something it did not inspect.
    r = client.delete(f"/v1/networks/{NETWORK}", headers=AUTH)

    assert r.status_code == 200, r.text
    assert r.json() == {"name": NETWORK, "removed": False}
    assert docker.network_removes == []
    assert ["docker", "network", "inspect", NETWORK] in docker.calls
    assert ["docker", "network", "rm", NETWORK] not in docker.calls


def test_a_network_that_vanishes_between_inspect_and_rm_is_already_removed(
    client, docker
):
    client.post(
        "/v1/services",
        json=_spec(network=NETWORK, network_aliases=["db"]),
        headers=AUTH,
    )
    docker.fail_network_rm = "vanished"

    r = client.delete(f"/v1/networks/{NETWORK}", headers=AUTH)

    # Two callers tearing down one environment race here. The loser asked for
    # the network to be gone and it is gone, so this is not a 502.
    assert r.status_code == 200, r.text
    assert r.json() == {"name": NETWORK, "removed": False}


def test_remove_network_refuses_a_name_with_a_trailing_newline(tmp_path, docker):
    # ``$`` matches before a final newline; a fullmatch is what keeps the
    # argv to the leaf shape the create path assumes.
    from mini_ork.remote.node_agent.services import ServiceError, ServiceManager

    manager = ServiceManager(state_dir=tmp_path, _run_fn=docker)
    with pytest.raises(ServiceError):
        manager.remove_network(f"{NETWORK}\n")
    assert docker.calls == []


def test_delete_network_with_containers_attached_is_409(client, docker):
    client.post(
        "/v1/services",
        json=_spec(network=NETWORK, network_aliases=["db"]),
        headers=AUTH,
    )
    docker.fail_network_rm = "in_use"

    r = client.delete(f"/v1/networks/{NETWORK}", headers=AUTH)

    assert r.status_code == 409
    assert "active endpoints" in r.json()["detail"]
    # The network stays: a 409 that removed it anyway would lie.
    assert NETWORK in docker.networks


def test_a_daemon_refusal_to_delete_a_network_is_502(client, docker):
    client.post(
        "/v1/services",
        json=_spec(network=NETWORK, network_aliases=["db"]),
        headers=AUTH,
    )
    docker.fail_network_rm = "daemon"

    r = client.delete(f"/v1/networks/{NETWORK}", headers=AUTH)

    assert r.status_code == 502
    assert "network rm failed" in r.json()["detail"]
    assert NETWORK in docker.networks


@pytest.mark.parametrize("name", ["Bad_Name", "-net", "a/b", "net.name", ""])
def test_delete_network_rejects_a_name_that_is_not_a_leaf(client, docker, name):
    r = client.delete(f"/v1/networks/{name}", headers=AUTH)

    # A name with a slash fails routing because {name} stops at the segment;
    # the rest must be refused before Docker sees them.
    assert r.status_code in (400, 404)
    assert docker.network_removes == []
    assert not any(c[:3] == ["docker", "network", "rm"] for c in docker.calls)


def test_delete_network_requires_a_bearer(client):
    r = client.delete(f"/v1/networks/{NETWORK}")
    assert r.status_code in (401, 403)
