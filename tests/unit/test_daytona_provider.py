"""Daytona node provider against a fake ``daytona`` SDK (no network, no key).

The live counterpart is ``scripts/smoke_daytona_node.py``, run against a real
Daytona organization; these tests pin the provider's own decisions: params,
idempotency on the run's sandbox name, cleanup on a failed boot, error mapping,
the TTL sweep, and that no secret is written anywhere."""
from __future__ import annotations

import json
import subprocess
import sys
import types
from dataclasses import dataclass, field

import pytest

from mini_ork.remote.providers import base
from mini_ork.remote.providers import daytona as dt


# --- fake SDK ---------------------------------------------------------------------


class DaytonaError(Exception):
    pass


class DaytonaConflictError(DaytonaError):
    pass


class DaytonaNotFoundError(DaytonaError):
    pass


@dataclass
class Params:
    kind: str
    kw: dict


@dataclass
class ExecResp:
    exit_code: int = 0
    result: str = ""


@dataclass
class FakeSandbox:
    id: str
    name: str
    labels: dict
    cloud: "FakeCloud"
    state: str = "started"
    files: dict = field(default_factory=dict)
    execs: list = field(default_factory=list)
    sessions: list = field(default_factory=list)
    session_cmds: list = field(default_factory=list)
    deleted: bool = False

    def __post_init__(self):
        sb = self

        class FS:
            def create_folder(self, path, mode):
                sb.files.setdefault(path + "/", b"")

            def upload_file(self, data, path):
                sb.files[path] = data

            def download_file(self, path):
                if path not in sb.files:
                    raise DaytonaNotFoundError(path)
                return sb.files[path]

        class Proc:
            def exec(self, command, timeout=None):
                sb.execs.append(command)
                return sb.cloud.exec_result(command)

            def create_session(self, sid):
                sb.sessions.append(sid)

            def execute_session_command(self, sid, req):
                sb.session_cmds.append((sid, req.command, req.run_async))
                sb.cloud.node_up.add(sb.id)

        self.fs = FS()
        self.process = Proc()

    def get_user_home_dir(self):
        return "/home/daytona"

    def get_preview_link(self, port):
        return types.SimpleNamespace(url=f"https://{port}-{self.id}.proxy.test/",
                                     token=f"preview-{self.id}")

    def delete(self, wait=False):
        if self.cloud.delete_raises:
            raise self.cloud.delete_raises
        self.deleted = True
        self.cloud.sandboxes.pop(self.id, None)


class FakeCloud:
    def __init__(self):
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.created: list[Params] = []
        self.node_up: set[str] = set()
        self.exec_fail: dict[str, ExecResp] = {}
        self.create_raises: Exception | None = None
        self.delete_raises: Exception | None = None
        self.configs: list[dict] = []

    def exec_result(self, command):
        for needle, resp in self.exec_fail.items():
            if needle in command:
                return resp
        return ExecResp()

    def module(self) -> types.SimpleNamespace:
        cloud = self

        def config(**kw):
            cloud.configs.append(kw)
            return kw

        class Daytona:
            def __init__(self, cfg):
                self.cfg = cfg

            def create(self, params, timeout=60):
                if cloud.create_raises:
                    exc, cloud.create_raises = cloud.create_raises, None
                    raise exc
                name = params.kw["name"]
                if any(s.name == name for s in cloud.sandboxes.values()):
                    raise DaytonaConflictError(f"Sandbox with name {name} already exists")
                sid = f"sb{len(cloud.created)}"
                cloud.created.append(params)
                sb = FakeSandbox(id=sid, name=name, labels=dict(params.kw["labels"]), cloud=cloud)
                cloud.sandboxes[sid] = sb
                return sb

            def get(self, id_or_name):
                for sb in cloud.sandboxes.values():
                    if id_or_name in (sb.id, sb.name):
                        return sb
                raise DaytonaNotFoundError(id_or_name)

            def list(self, query):
                want = query["labels"]
                return iter([s for s in cloud.sandboxes.values()
                             if all(s.labels.get(k) == v for k, v in want.items())])

        return types.SimpleNamespace(
            DaytonaError=DaytonaError, DaytonaConflictError=DaytonaConflictError,
            DaytonaNotFoundError=DaytonaNotFoundError, Daytona=Daytona, DaytonaConfig=config,
            CreateSandboxFromSnapshotParams=lambda **kw: Params("snapshot", kw),
            CreateSandboxFromImageParams=lambda **kw: Params("image", kw),
            Resources=lambda **kw: kw, ListSandboxesQuery=lambda **kw: kw,
            SessionExecuteRequest=lambda **kw: types.SimpleNamespace(**kw))


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    c = FakeCloud()
    monkeypatch.setitem(sys.modules, "daytona", c.module())
    # node-agent health: up once a session command started it
    monkeypatch.setattr(dt, "_http_ok", lambda url, headers, timeout=10.0: any(
        f"-{sid}." in url for sid in c.node_up))
    monkeypatch.setattr(dt, "_engine_archive", lambda root: ("a" * 40, b"TARGZ"))
    monkeypatch.setattr(dt.time, "sleep", lambda s: None)
    # never the real secret store
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_SECRETS", str(tmp_path / "secrets.local.sh"))
    return c


ENV = {"DAYTONA_API_KEY": "dtn_test_key"}


def _create(run_id="run-1", profile=None, env=ENV):
    return dt.DaytonaProvider().create(run_id=run_id, profile=profile, env=env, engine_root="/e")


def _profile(**kw):
    defaults = dict(image=None, resources=None, network="full", allow_domains=[], env={},
                    setup="", setup_timeout_s=900.0, provider_options={})
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


# --- create / boot ------------------------------------------------------------------


def test_create_boots_a_reachable_node(cloud):
    node = _create()
    sb = cloud.sandboxes[node.handle]
    assert node.provider == "daytona" and node.handle == sb.id
    assert node.url == f"https://7091-{sb.id}.proxy.test"
    assert node.headers[dt.PREVIEW_TOKEN_HEADER] == f"preview-{sb.id}"
    assert node.headers["X-Daytona-Skip-Preview-Warning"] == "true"
    # the bearer is minted per sandbox and lives in a sandbox file, not its env
    base_dir = "/home/daytona/.mini-ork-node"
    assert sb.files[f"{base_dir}/node-token"].decode() == node.token and len(node.token) >= 40
    assert node.token not in json.dumps(cloud.created[0].kw)
    # engine pre-staged at engines/<sha>, node-agent loopback-bound on host runtime
    assert sb.files[f"{base_dir}/engine.tar.gz"] == b"TARGZ"
    boot = sb.files[f"{base_dir}/boot.sh"].decode()
    assert f"{base_dir}/state/engines/{'a' * 40}" in boot
    (sid, cmd, run_async), = sb.session_cmds
    assert run_async and "--runtime host" in cmd and "--bind 127.0.0.1" in cmd
    assert "--port 7091" in cmd and "node-token" in cmd and node.token not in cmd


def test_create_params_name_labels_and_lifetime(cloud):
    _create(run_id="run-1791901915-72597")
    p = cloud.created[0]
    assert p.kind == "snapshot" and "snapshot" not in p.kw   # Daytona default snapshot
    assert p.kw["name"] == "mo-run-1791901915-72597"
    assert p.kw["labels"]["mo.sandbox"] == "1" and p.kw["labels"]["mo.provider"] == "daytona"
    assert p.kw["labels"]["mo.run"] == "run-1791901915-72597"
    assert p.kw["labels"]["mo.created"].isdigit()
    assert p.kw["ttl_minutes"] == 360 and p.kw["auto_stop_interval"] == 0
    assert p.kw["ephemeral"] is True
    assert p.kw["env_vars"] == {}


def test_ttl_follows_mo_sandbox_max_age(cloud):
    _create(env={**ENV, "MO_SANDBOX_MAX_AGE": "900"})
    assert cloud.created[0].kw["ttl_minutes"] == 15


def test_profile_maps_onto_image_resources_network_and_env(cloud):
    prof = _profile(image="python:3.12", resources={"cpus": 2, "memory": "4g", "disk": "10g"},
                    network="allowlist", allow_domains=["api.anthropic.com", "pypi.org"],
                    env={"PYTHONUNBUFFERED": "1"})
    _create(profile=prof)
    p = cloud.created[0]
    assert p.kind == "image" and p.kw["image"] == "python:3.12"
    assert p.kw["resources"] == {"cpu": 2, "memory": 4, "disk": 10}
    assert p.kw["domain_allow_list"] == "api.anthropic.com,pypi.org"
    assert p.kw["env_vars"] == {"PYTHONUNBUFFERED": "1"}


def test_snapshot_option_wins_over_image(cloud):
    _create(profile=_profile(image="python:3.12", provider_options={"snapshot": "mo-agent:1"}))
    p = cloud.created[0]
    assert p.kind == "snapshot" and p.kw["snapshot"] == "mo-agent:1"


@pytest.mark.parametrize("raw, gib", [(4, 4), ("4g", 4), ("4096m", 4), ("1536m", 2), ("2GiB", 2)])
def test_memory_parsing(raw, gib):
    assert dt._memory_gib(raw) == gib


def test_unknown_provider_option_is_loud(cloud):
    with pytest.raises(base.ProviderUnavailableError, match="unknown provider_options"):
        _create(profile=_profile(provider_options={"snapshott": "x"}))
    assert cloud.created == []


def test_allowlist_without_domains_is_refused(cloud):
    with pytest.raises(base.ProviderUnavailableError, match="allow_domains"):
        _create(profile=_profile(network="allowlist", allow_domains=[]))


def test_profile_setup_runs_once_after_bootstrap(cloud):
    node = _create(profile=_profile(setup="npm i -g something"))
    sb = cloud.sandboxes[node.handle]
    assert sb.files["/home/daytona/.mini-ork-node/setup.sh"] == b"npm i -g something"
    assert [c for c in sb.execs if "setup.sh" in c] and "boot.sh" in sb.execs[0]


def test_sandbox_name_is_dns_safe_and_bounded():
    assert dt.sandbox_name("Run_ABC.1") == "mo-run-abc-1"
    assert len(dt.sandbox_name("x" * 200)) <= 63


# --- idempotency on the run's name ---------------------------------------------------


def test_name_conflict_reuses_a_healthy_sandbox(cloud):
    first = _create()
    again = _create()                          # e.g. a peer lost the run-dir record
    assert again.handle == first.handle and again.token == first.token
    assert len(cloud.created) == 1


def test_name_conflict_with_a_never_booted_sandbox_replaces_it(cloud):
    cloud.exec_fail["boot.sh"] = ExecResp(1, "boom")
    with pytest.raises(base.ProviderUnavailableError):
        _create(env={**ENV, "MO_DAYTONA_KEEP_FAILED": "1"})   # leave the half-booted one
    cloud.exec_fail.clear()
    stale_id = next(iter(cloud.sandboxes))
    node = _create()
    assert node.handle != stale_id and stale_id not in cloud.sandboxes
    assert len(cloud.sandboxes) == 1


# --- failure handling ---------------------------------------------------------------


def test_failed_bootstrap_deletes_the_sandbox_and_reports_output(cloud):
    cloud.exec_fail["boot.sh"] = ExecResp(3, "node-agent deps missing: fastapi")
    with pytest.raises(base.ProviderUnavailableError, match="deps missing: fastapi"):
        _create()
    assert cloud.sandboxes == {}


def test_node_agent_never_healthy_deletes_and_shows_log(cloud, monkeypatch):
    monkeypatch.setattr(dt, "_http_ok", lambda *a, **k: False)
    with pytest.raises(base.ProviderUnavailableError, match="not healthy"):
        _create(profile=_profile(provider_options={"ready_timeout_s": 0.01}))
    assert cloud.sandboxes == {}


def test_sdk_error_on_create_is_provider_unavailable(cloud):
    cloud.create_raises = DaytonaError("quota exceeded")
    with pytest.raises(base.ProviderUnavailableError, match="quota exceeded"):
        _create()


def test_missing_sdk_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "daytona", None)
    with pytest.raises(base.ProviderUnavailableError, match=r"mini-ork\[daytona\]"):
        _create()


def test_missing_key_fails_closed_before_any_api_call(cloud):
    with pytest.raises(base.ProviderUnavailableError, match="DAYTONA_API_KEY"):
        _create(env={})
    assert cloud.configs == [] and cloud.created == []


def test_api_url_and_target_reach_the_client(cloud):
    _create(env={**ENV, "DAYTONA_API_URL": "https://dt.example/api", "DAYTONA_TARGET": "eu"})
    assert cloud.configs[-1] == {"api_key": "dtn_test_key", "api_url": "https://dt.example/api",
                                 "target": "eu"}


# --- attach / release / reap ---------------------------------------------------------


def test_attach_rereads_token_through_the_sandbox(cloud):
    node = _create()
    again = dt.DaytonaProvider().attach(node.handle, env=ENV)
    assert again == node


def test_attach_restarts_a_dead_node_agent(cloud):
    node = _create()
    cloud.node_up.clear()                       # node-agent died
    again = dt.DaytonaProvider().attach(node.handle, env=ENV)
    assert again is not None and again.url == node.url
    assert len(cloud.sandboxes[node.handle].session_cmds) == 2


def test_attach_missing_or_stopped_is_none(cloud):
    assert dt.DaytonaProvider().attach("nope", env=ENV) is None
    node = _create()
    cloud.sandboxes[node.handle].state = "stopped"
    assert dt.DaytonaProvider().attach(node.handle, env=ENV) is None


def test_release_deletes_and_is_idempotent(cloud):
    node = _create()
    dt.DaytonaProvider().release(node.handle, env=ENV)
    assert cloud.sandboxes == {}
    dt.DaytonaProvider().release(node.handle, env=ENV)     # already gone: fine


def test_release_treats_delete_in_progress_as_done(cloud):
    node = _create()
    cloud.delete_raises = DaytonaConflictError("Sandbox state change in progress")
    dt.DaytonaProvider().release(node.handle, env=ENV)     # no raise


def test_reap_sweeps_only_old_mini_ork_sandboxes(cloud):
    old = _create(run_id="old")
    young = _create(run_id="young")
    cloud.sandboxes[old.handle].labels["mo.created"] = "1000"
    foreign = FakeSandbox(id="fx", name="someone-else", labels={"team": "x"}, cloud=cloud)
    cloud.sandboxes["fx"] = foreign
    prov = dt.DaytonaProvider()
    assert prov.reap(max_age_s=3600, dry_run=True, env=ENV) == [old.handle]
    assert old.handle in cloud.sandboxes                    # dry run removed nothing
    assert prov.reap(max_age_s=3600, dry_run=False, env=ENV) == [old.handle]
    assert set(cloud.sandboxes) == {young.handle, "fx"}


def test_reap_is_fail_open_without_credentials(cloud):
    assert dt.DaytonaProvider().reap(max_age_s=1, dry_run=True, env={}) == []


# --- through the contract -------------------------------------------------------------


def test_acquire_run_node_shares_one_sandbox_per_run(cloud, tmp_path):
    prov = base.get_node_provider("daytona")
    a = base.acquire_run_node(prov, run_id="r1", run_dir=tmp_path, profile=None, env=ENV,
                              engine_root="/e")
    b = base.acquire_run_node(prov, run_id="r1", run_dir=tmp_path, profile=None, env=ENV,
                              engine_root="/e")
    assert a == b and len(cloud.created) == 1
    record = (tmp_path / ".node-provider.json").read_text()
    assert a.token not in record and "preview-" not in record and "dtn_" not in record
    assert base.release_run_node(tmp_path, ENV) is True and cloud.sandboxes == {}


def test_importing_the_provider_does_not_import_the_sdk():
    code = ("import sys, mini_ork.remote.providers.daytona\n"
            "print('daytona' in sys.modules)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_endpoints_claim_the_profile_setup_was_applied(cloud):
    """Every Daytona endpoint is of a fully booted sandbox (bootstrap + profile
    setup done), so the remote workspace must not ask the host-runtime
    node-agent to prepare a docker image."""
    node = _create(profile=_profile(setup="pip install --user pytest"))
    assert node.setup_applied is True
    assert dt.DaytonaProvider().attach(node.handle, env=ENV).setup_applied is True
