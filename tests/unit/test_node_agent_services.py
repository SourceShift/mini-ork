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
        self.networks: set[str] = set()
        self.network_creates: list[list[str]] = []
        self.fail_network: str | None = None
        self.fail_on: str | None = None
        self.removed: list[str] = []

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

        if argv[:2] == ["docker", "inspect"]:
            name = argv[-1]
            if name in self.states:
                r.stdout = self.states[name]
            else:
                r.returncode = 1
                r.stderr = "Error: No such object"
            return r

        if argv[:2] == ["docker", "logs"]:
            r.stdout = "listening on :8080\n"
            return r

        if argv[:2] == ["docker", "rm"]:
            self.removed.append(argv[-1])
            self.states.pop(argv[-1], None)
            return r

        if argv[:2] == ["docker", "restart"]:
            self.states[argv[-1]] = "running"
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
    return create_app(state_dir=tmp_path, token_env=TOKEN_ENV, _run_fn=docker)


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
        ("post", "/v1/services/web/volumes/data/restore"),
    ):
        r = getattr(client, method)(path, **({"json": {"snapshot_id": "0" * 32}} if method == "post" else {}))
        assert r.status_code in (401, 403), (method, path, r.status_code)


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
