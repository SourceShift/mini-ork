"""Modal node provider against a fake ``modal`` SDK (no network, no token).

Live verification needs a Modal token (``scripts/smoke_sandbox_node.py
--provider modal``); these pin the adapter's own decisions. The shared boot
sequence is covered by ``test_daytona_provider.py``."""
from __future__ import annotations

import subprocess
import sys
import types

import pytest

from mini_ork.remote.providers import base
from mini_ork.remote.providers import modal_sandbox as md
from mini_ork.remote.providers import sandbox_node as sn


class NotFoundError(Exception):
    pass


class FakeCloud:
    def __init__(self):
        self.boxes: dict[str, object] = {}
        self.by_name: dict[tuple[str, str], str] = {}
        self.created: list[dict] = []
        self.node_up: set[str] = set()
        self.fail_on: dict[str, int] = {}
        self.clients: list[tuple[str, str]] = []
        self.apps: list[tuple[str, dict]] = []

    def module(self):
        cloud = self

        class Proc:
            def __init__(self, rc, out=""):
                self.returncode = rc
                self.stdout = types.SimpleNamespace(read=lambda: out)
                self.stderr = types.SimpleNamespace(read=lambda: "")

            def wait(self):
                return self.returncode

        class Box:
            def __init__(self, oid, name, tags):
                self.object_id, self.name, self.tags = oid, name, tags
                self.files: dict[str, bytes] = {}
                self.execs: list[str] = []
                self.done = False
                box = self

                class FS:
                    def write_bytes(self, data, path):
                        box.files[path] = bytes(data)

                    def read_bytes(self, path):
                        if path not in box.files:
                            raise NotFoundError(path)
                        return box.files[path]

                self.filesystem = FS()

            def poll(self):
                return 0 if self.done else None

            def exec(self, *args, timeout=None):
                cmd = args[-1]
                self.execs.append(cmd)
                if "$HOME" in cmd:
                    return Proc(0, "/root")
                if cmd.startswith("nohup"):
                    cloud.node_up.add(self.object_id)
                for needle, rc in cloud.fail_on.items():
                    if needle in cmd:
                        return Proc(rc, f"{needle} failed")
                return Proc(0)

            def create_connect_token(self, port=8080):
                return types.SimpleNamespace(url=f"https://{port}-{self.object_id}.modal.test",
                                             token=f"ct-{self.object_id}")

            def get_tags(self):
                return dict(self.tags)

            def terminate(self, wait=False):
                if self.object_id not in cloud.boxes:
                    raise NotFoundError(self.object_id)
                self.done = True
                cloud.boxes.pop(self.object_id)

        class Sandbox:
            @staticmethod
            def create(*cmd, **kw):
                cloud.created.append({"cmd": cmd, **kw})
                oid = f"sb-{len(cloud.created)}"
                cloud.boxes[oid] = Box(oid, kw["name"], kw["tags"])
                cloud.by_name[(kw["app"].name, kw["name"])] = oid
                return cloud.boxes[oid]

            @staticmethod
            def from_name(app, name, client=None):
                oid = cloud.by_name.get((app, name))
                if oid not in cloud.boxes:
                    raise NotFoundError(name)
                return cloud.boxes[oid]

            @staticmethod
            def from_id(oid, client=None):
                if oid not in cloud.boxes:
                    raise NotFoundError(oid)
                return cloud.boxes[oid]

            @staticmethod
            def list(app_id=None, client=None, tags=None):
                return [b for b in cloud.boxes.values()
                        if all(b.tags.get(k) == v for k, v in (tags or {}).items())]

        class Image:
            def __init__(self, desc):
                self.desc = desc

            @staticmethod
            def from_registry(tag):
                return Image(f"registry:{tag}")

            @staticmethod
            def debian_slim():
                return Image("debian_slim")

            def apt_install(self, *pkgs):
                return Image(f"{self.desc}+apt:{','.join(pkgs)}")

        class App:
            @staticmethod
            def lookup(name, **kw):
                cloud.apps.append((name, kw))
                return types.SimpleNamespace(name=name, app_id=f"ap-{name}")

        class Client:
            @staticmethod
            def from_credentials(token_id, token_secret):
                cloud.clients.append((token_id, token_secret))
                return object()

        return types.SimpleNamespace(Sandbox=Sandbox, Image=Image, App=App, Client=Client,
                                     NotFoundError=NotFoundError)


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    c = FakeCloud()
    monkeypatch.setitem(sys.modules, "modal", c.module())
    monkeypatch.setattr(sn, "http_ok", lambda url, headers, timeout=10.0: any(
        f"-{oid}." in url for oid in c.node_up))
    monkeypatch.setattr(sn, "engine_archive", lambda root: ("c" * 40, b"TARGZ"))
    monkeypatch.setattr(sn.time, "sleep", lambda s: None)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_SECRETS", str(tmp_path / "secrets.local.sh"))
    return c


ENV = {"MODAL_TOKEN_ID": "ak-test", "MODAL_TOKEN_SECRET": "as-test"}


def _profile(**kw):
    defaults = dict(image=None, resources=None, network="full", allow_domains=[], env={},
                    setup="", setup_timeout_s=900.0, provider_options={})
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


def _create(run_id="run-1", profile=None, env=ENV):
    return md.ModalProvider().create(run_id=run_id, profile=profile, env=env, engine_root="/e")


def test_create_boots_behind_a_connect_token_cookie(cloud):
    node = _create()
    box = cloud.boxes[node.handle]
    assert node.provider == "modal" and node.setup_applied is True
    assert node.url == f"https://7091-{box.object_id}.modal.test"
    # the cookie, not Authorization: the node-agent bearer owns that header
    assert node.headers == {"Cookie": f"_modal_connect_token=ct-{box.object_id}"}
    kw = cloud.created[0]
    assert kw["cmd"] == ("sleep", "infinity") and kw["name"] == "mo-run-1"
    assert kw["tags"]["mo.provider"] == "modal" and kw["tags"]["mo.run"] == "run-1"
    assert kw["timeout"] == 6 * 3600 and kw["app"].name == "mini-ork-nodes"
    assert kw["image"].desc == "debian_slim+apt:git,curl"
    assert cloud.clients == [("ak-test", "as-test")] * len(cloud.clients)
    assert box.files["/root/.mini-ork-node/node-token"].decode() == node.token
    assert any(c.startswith("nohup") and "--runtime host" in c for c in box.execs)


def test_profile_maps_image_resources_env_and_app(cloud):
    _create(profile=_profile(image="python:3.12", resources={"cpus": 2, "memory": "4g"},
                             env={"A": "1"}, provider_options={"environment": "dev"}),
            env={**ENV, "MO_MODAL_APP": "my-nodes"})
    kw = cloud.created[0]
    assert kw["image"].desc == "registry:python:3.12"
    assert kw["cpu"] == 2.0 and kw["memory"] == 4096 and kw["env"] == {"A": "1"}
    assert kw["app"].name == "my-nodes"
    assert ("my-nodes", {"client": cloud.apps[-1][1]["client"], "create_if_missing": True,
                         "environment_name": "dev"}) in cloud.apps


@pytest.mark.parametrize("profile, match", [
    (dict(network="allowlist", allow_domains=["pypi.org"]), "allowlist"),
    (dict(provider_options={"timeout_s": 100000}), "24 h"),
    (dict(provider_options={"app": "x"}), "unknown provider_options"),
])
def test_unsupported_settings_fail_closed_before_any_api_call(cloud, profile, match):
    with pytest.raises(base.ProviderUnavailableError, match=match):
        _create(profile=_profile(**profile))
    assert cloud.created == []


def test_failed_boot_terminates_the_sandbox(cloud):
    cloud.fail_on["boot.sh"] = 3
    with pytest.raises(base.ProviderUnavailableError, match="exited 3"):
        _create()
    assert cloud.boxes == {}


def test_create_reuses_this_runs_booted_sandbox_by_name(cloud):
    first = _create()
    again = _create()
    assert again.handle == first.handle and len(cloud.created) == 1


def test_attach_release_and_reap(cloud):
    node = _create()
    prov = md.ModalProvider()
    assert prov.attach(node.handle, env=ENV).url == node.url
    old = _create(run_id="old")
    cloud.boxes[old.handle].tags["mo.created"] = "1000"
    assert prov.reap(max_age_s=3600, dry_run=False, env=ENV) == [old.handle]
    prov.release(node.handle, env=ENV)
    assert cloud.boxes == {}
    prov.release(node.handle, env=ENV)                  # already gone: fine


def test_missing_tokens_fail_closed(cloud):
    with pytest.raises(base.ProviderUnavailableError, match="MODAL_TOKEN_ID"):
        _create(env={})
    assert cloud.created == []


def test_missing_sdk_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "modal", None)
    with pytest.raises(base.ProviderUnavailableError, match=r"mini-ork\[modal\]"):
        _create()


def test_selectable_and_sdk_imported_lazily():
    assert "modal" in base.known_node_providers()
    code = ("import sys, mini_ork.remote.providers.modal_sandbox\n"
            "print('modal' in sys.modules)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
