"""E2B node provider against a fake ``e2b`` SDK (no network, no key).

The live counterpart is ``scripts/smoke_sandbox_node.py --provider e2b`` (7/7
on 2026-10-13); these pin the adapter's own decisions. The boot sequence itself
is shared with Daytona and covered by ``test_daytona_provider.py``."""
from __future__ import annotations

import subprocess
import sys
import types

import pytest

from mini_ork.remote.providers import base
from mini_ork.remote.providers import e2b as e2
from mini_ork.remote.providers import sandbox_node as sn


class SandboxException(Exception):
    pass


class NotFoundException(SandboxException):
    pass


class CommandExitException(SandboxException):
    def __init__(self, exit_code, stdout="", stderr=""):
        super().__init__(f"exit {exit_code}")
        self.exit_code, self.stdout, self.stderr = exit_code, stdout, stderr


class FakeCloud:
    def __init__(self):
        self.boxes: dict[str, object] = {}
        self.created: list[dict] = []
        self.node_up: set[str] = set()
        self.fail_on: dict[str, int] = {}
        self.create_raises: Exception | None = None

    def module(self):
        cloud = self

        class FakeBox:
            def __init__(self, sid, metadata):
                self.sandbox_id = sid
                self.traffic_access_token = f"traffic-{sid}"
                self.metadata = metadata
                self.files_store: dict[str, bytes] = {}
                self.commands_seen: list[tuple[str, bool]] = []
                self.running = True
                box = self

                class Files:
                    def write(self, path, data):
                        box.files_store[path] = data if isinstance(data, bytes) else data.encode()

                    def read(self, path, format="text"):
                        if path not in box.files_store:
                            raise NotFoundException(path)
                        return bytearray(box.files_store[path])

                class Commands:
                    def run(self, cmd, background=None, timeout=60):
                        box.commands_seen.append((cmd, bool(background)))
                        if background:
                            cloud.node_up.add(box.sandbox_id)
                            return types.SimpleNamespace(pid=1)
                        if "$HOME" in cmd:
                            return types.SimpleNamespace(exit_code=0, stdout="/home/user", stderr="")
                        for needle, rc in cloud.fail_on.items():
                            if needle in cmd:
                                raise CommandExitException(rc, "", f"{needle} failed")
                        return types.SimpleNamespace(exit_code=0, stdout="", stderr="")

                self.files = Files()
                self.commands = Commands()

            def is_running(self):
                return self.running

            def get_host(self, port):
                return f"{port}-{self.sandbox_id}.e2b.test"

            def get_info(self):
                return types.SimpleNamespace(metadata=self.metadata)

            def kill(self):
                return cloud.boxes.pop(self.sandbox_id, None) is not None

        class Sandbox:
            @classmethod
            def create(cls, **kw):
                if cloud.create_raises:
                    exc, cloud.create_raises = cloud.create_raises, None
                    raise exc
                cloud.created.append(kw)
                sid = f"sb{len(cloud.created)}"
                cloud.boxes[sid] = FakeBox(sid, dict(kw.get("metadata") or {}))
                return cloud.boxes[sid]

            @classmethod
            def connect(cls, sandbox_id, **kw):
                if sandbox_id not in cloud.boxes:
                    raise NotFoundException(sandbox_id)
                return cloud.boxes[sandbox_id]

            @classmethod
            def list(cls, query=None, **kw):
                want = dict(getattr(query, "metadata", None) or {})
                hits = [types.SimpleNamespace(sandbox_id=b.sandbox_id, metadata=b.metadata)
                        for b in cloud.boxes.values()
                        if all(b.metadata.get(k) == v for k, v in want.items())]
                return types.SimpleNamespace(next_items=lambda: hits, has_next=False)

        return types.SimpleNamespace(
            Sandbox=Sandbox, SandboxQuery=lambda metadata=None: types.SimpleNamespace(metadata=metadata),
            NotFoundException=NotFoundException, CommandExitException=CommandExitException)


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    c = FakeCloud()
    monkeypatch.setitem(sys.modules, "e2b", c.module())
    monkeypatch.setattr(sn, "http_ok", lambda url, headers, timeout=10.0: any(
        f"-{sid}." in url for sid in c.node_up))
    monkeypatch.setattr(sn, "engine_archive", lambda root: ("b" * 40, b"TARGZ"))
    monkeypatch.setattr(sn.time, "sleep", lambda s: None)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_SECRETS", str(tmp_path / "secrets.local.sh"))
    return c


ENV = {"E2B_API_KEY": "e2b_test_key"}


def _profile(**kw):
    defaults = dict(image=None, resources=None, network="full", allow_domains=[], env={},
                    setup="", setup_timeout_s=900.0, provider_options={})
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


def _create(run_id="run-1", profile=None, env=ENV):
    return e2.E2BProvider().create(run_id=run_id, profile=profile, env=env, engine_root="/e")


def test_create_boots_behind_the_traffic_token(cloud):
    node = _create()
    box = cloud.boxes[node.handle]
    assert node.provider == "e2b" and node.setup_applied is True
    assert node.url == f"https://7091-{box.sandbox_id}.e2b.test"
    assert node.headers == {e2.TRAFFIC_TOKEN_HEADER: f"traffic-{box.sandbox_id}"}
    kw = cloud.created[0]
    assert kw["network"] == {"allow_public_traffic": False}
    assert kw["api_key"] == "e2b_test_key"
    assert kw["metadata"]["mo.provider"] == "e2b" and kw["metadata"]["mo.run"] == "run-1"
    assert kw["timeout"] == 3600 and "template" not in kw
    assert box.files_store["/home/user/.mini-ork-node/node-token"].decode() == node.token
    assert any(bg and "--runtime host" in cmd for cmd, bg in box.commands_seen)


def test_options_map_template_timeout_and_env(cloud):
    _create(profile=_profile(env={"PYTHONUNBUFFERED": "1"},
                             provider_options={"template": "mo-agent", "timeout_s": 86400}))
    kw = cloud.created[0]
    assert kw["template"] == "mo-agent" and kw["timeout"] == 86400
    assert kw["envs"] == {"PYTHONUNBUFFERED": "1"}


def test_default_timeout_respects_a_shorter_ttl(cloud):
    _create(env={**ENV, "MO_SANDBOX_MAX_AGE": "900"})
    assert cloud.created[0]["timeout"] == 900


@pytest.mark.parametrize("profile, match", [
    (dict(network="allowlist", allow_domains=["pypi.org"]), "allowlist"),
    (dict(resources={"cpus": 2}), "template"),
    (dict(provider_options={"tempalte": "x"}), "unknown provider_options"),
])
def test_unsupported_profile_fields_fail_closed_before_any_api_call(cloud, profile, match):
    with pytest.raises(base.ProviderUnavailableError, match=match):
        _create(profile=_profile(**profile))
    assert cloud.created == []


def test_a_failing_command_reports_its_exit_code_and_output(cloud):
    cloud.fail_on["boot.sh"] = 3
    with pytest.raises(base.ProviderUnavailableError, match="(?s)exited 3.*boot.sh failed"):
        _create()
    assert cloud.boxes == {}                      # the half-booted sandbox was killed


def test_create_reuses_this_runs_booted_sandbox(cloud):
    first = _create()
    again = _create()                             # no run-dir record: found by mo.run
    assert again.handle == first.handle and len(cloud.created) == 1


def test_attach_and_release(cloud):
    node = _create()
    assert e2.E2BProvider().attach(node.handle, env=ENV) == node
    e2.E2BProvider().release(node.handle, env=ENV)
    assert cloud.boxes == {}
    assert e2.E2BProvider().attach(node.handle, env=ENV) is None
    e2.E2BProvider().release(node.handle, env=ENV)             # already gone: fine


def test_reap_sweeps_only_old_mini_ork_sandboxes(cloud):
    old = _create(run_id="old")
    young = _create(run_id="young")
    cloud.boxes[old.handle].metadata["mo.created"] = "1000"
    prov = e2.E2BProvider()
    assert prov.reap(max_age_s=3600, dry_run=True, env=ENV) == [old.handle]
    assert prov.reap(max_age_s=3600, dry_run=False, env=ENV) == [old.handle]
    assert set(cloud.boxes) == {young.handle}


def test_missing_key_fails_closed(cloud):
    with pytest.raises(base.ProviderUnavailableError, match="E2B_API_KEY"):
        _create(env={})
    assert cloud.created == []


def test_missing_sdk_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "e2b", None)
    with pytest.raises(base.ProviderUnavailableError, match=r"mini-ork\[e2b\]"):
        _create()


def test_selectable_by_name_and_sdk_imported_lazily():
    assert "e2b" in base.known_node_providers()
    code = ("import sys, mini_ork.remote.providers.e2b\n"
            "print('e2b' in sys.modules)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
