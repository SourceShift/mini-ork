"""Contract tests for the node-provider registry and the one-node-per-run helpers
(mini_ork/remote/providers/base.py). A fake provider stands in for the cloud;
nothing here imports a cloud SDK or touches the network."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import pytest

from mini_ork.remote.providers import base


class FakeProvider:
    """Counts calls; ``alive`` is the set of handles the 'cloud' still holds."""

    name = "fake"

    def __init__(self, *, handle_prefix: str = "sb-", create_delay: float = 0.0) -> None:
        self.created: list[str] = []
        self.released: list[str] = []
        self.alive: set[str] = set()
        self._prefix = handle_prefix
        self._delay = create_delay
        self._lock = threading.Lock()

    def _endpoint(self, handle: str) -> base.NodeEndpoint:
        return base.NodeEndpoint(name=f"fake-{handle}", url=f"https://{handle}.example",
                                 token="node-secret", headers={"x-preview-token": "p-secret"},
                                 handle=handle, provider=self.name)

    def create(self, *, run_id, profile, env, engine_root):
        time.sleep(self._delay)
        with self._lock:
            handle = f"{self._prefix}{len(self.created)}" if self._prefix else ""
            self.created.append(run_id)
            if handle:
                self.alive.add(handle)
        return self._endpoint(handle)

    def attach(self, handle, *, env):
        return self._endpoint(handle) if handle in self.alive else None

    def release(self, handle, *, env):
        self.released.append(handle)
        self.alive.discard(handle)

    def reap(self, *, max_age_s, dry_run, env):
        return sorted(self.alive)


@pytest.fixture
def fake(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setitem(base._PROVIDERS, "fake", lambda: provider)
    return provider


def _acquire(provider, run_dir, **kw):
    return base.acquire_run_node(provider, run_id="run-1", run_dir=run_dir, profile=None,
                                 env=kw.pop("env", {}), engine_root="/engine", **kw)


# --- registry -----------------------------------------------------------------


def test_builtin_providers_are_selectable_without_importing_them():
    assert {"static", "daytona"} <= set(base.known_node_providers())


def test_get_registered_provider(fake):
    assert base.get_node_provider("fake") is fake


def test_unknown_provider_is_a_loud_value_error():
    with pytest.raises(ValueError, match="unknown node provider 'nope'"):
        base.get_node_provider("nope")


def test_lazy_provider_whose_module_is_missing_is_provider_unavailable(monkeypatch):
    monkeypatch.setitem(base._LAZY_PROVIDERS, "ghost", "mini_ork.remote.providers._no_such_mod")
    with pytest.raises(base.ProviderUnavailableError, match="ghost"):
        base.get_node_provider("ghost")


def test_register_lazy_provider_imports_on_first_get(monkeypatch, tmp_path):
    pkg = tmp_path / "mo_plugin_prov.py"
    pkg.write_text(
        "from mini_ork.remote.providers.base import register_node_provider\n"
        "class P:\n    name = 'plug'\n"
        "register_node_provider('plug', P)\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(base._PROVIDERS, "plug", raising=False)
    base.register_lazy_node_provider("plug", "mo_plugin_prov")
    try:
        assert base.get_node_provider("plug").name == "plug"
        assert "plug" in base.known_node_providers()
    finally:
        base._LAZY_PROVIDERS.pop("plug", None)
        base._PROVIDERS.pop("plug", None)


@pytest.mark.parametrize("profile_provider, env, expected", [
    ("daytona", {"MO_NODE_PROVIDER": "static"}, "daytona"),   # profile wins
    ("", {"MO_NODE_PROVIDER": "daytona"}, "daytona"),          # then env
    ("", {}, "static"),                                        # default = today
    (None, {"MO_NODE_PROVIDER": "  "}, "static"),
])
def test_provider_name_precedence(profile_provider, env, expected):
    profile = type("P", (), {"provider": profile_provider})() if profile_provider is not None else None
    assert base.provider_name_for(profile, env) == expected


# --- endpoint secrecy -----------------------------------------------------------


def test_endpoint_repr_and_locator_never_carry_secrets():
    ep = base.NodeEndpoint(name="n", url="https://u", token="TOKEN-123",
                           headers={"x-daytona-preview-token": "PREVIEW-456"},
                           handle="sb-1", provider="daytona")
    text = repr(ep) + json.dumps(ep.locator())
    assert "TOKEN-123" not in text and "PREVIEW-456" not in text
    assert ep.locator() == {"provider": "daytona", "handle": "sb-1", "name": "n",
                            "url": "https://u", "token_env": "MO_NODE_TOKEN"}


# --- one node per run -----------------------------------------------------------


def test_acquire_creates_once_then_attaches(fake, tmp_path):
    first = _acquire(fake, tmp_path)
    second = _acquire(fake, tmp_path)
    assert fake.created == ["run-1"]
    assert first.handle == second.handle == "sb-0"
    record = json.loads((tmp_path / ".node-provider.json").read_text())
    assert record["provider"] == "fake" and record["handle"] == "sb-0"
    assert record["run_id"] == "run-1" and "replaced" not in record


def test_record_on_disk_holds_no_secret(fake, tmp_path):
    _acquire(fake, tmp_path)
    raw = (tmp_path / ".node-provider.json").read_text()
    assert "node-secret" not in raw and "p-secret" not in raw


def test_acquire_replaces_a_node_that_is_gone(fake, tmp_path):
    _acquire(fake, tmp_path)
    fake.alive.clear()                      # TTL expired on the cloud side
    node = _acquire(fake, tmp_path)
    assert fake.created == ["run-1", "run-1"]
    assert node.handle == "sb-1"
    record = json.loads((tmp_path / ".node-provider.json").read_text())
    assert record["handle"] == "sb-1" and record["replaced"] == "sb-0"


def test_static_node_with_no_handle_is_never_recorded(tmp_path):
    static = FakeProvider(handle_prefix="")
    _acquire(static, tmp_path)
    _acquire(static, tmp_path)
    assert static.created == ["run-1", "run-1"]   # re-selected, nothing to attach to
    assert not (tmp_path / ".node-provider.json").exists()


def test_concurrent_acquire_creates_exactly_one_node(tmp_path):
    slow = FakeProvider(create_delay=0.3)
    results: list[str] = []
    errors: list[BaseException] = []

    def worker():
        try:
            results.append(_acquire(slow, tmp_path).handle)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert slow.created == ["run-1"]
    assert results == ["sb-0"] * 4


def test_concurrent_acquire_across_processes_creates_one_node(tmp_path):
    # flock is the cross-PROCESS guarantee; prove it with real processes.
    script = (
        "import sys, time\n"
        "from mini_ork.remote.providers import base\n"
        "class P:\n"
        "    name='fake'\n"
        "    def create(self, **kw):\n"
        "        open(sys.argv[2], 'a').write('c\\n'); time.sleep(0.4)\n"
        "        return base.NodeEndpoint(name='n', url='u', handle='sb-x', provider='fake')\n"
        "    def attach(self, handle, *, env):\n"
        "        return base.NodeEndpoint(name='n', url='u', handle=handle, provider='fake')\n"
        "base.acquire_run_node(P(), run_id='r', run_dir=sys.argv[1], profile=None, env={}, engine_root='/e')\n"
    )
    counter = tmp_path / "creates.txt"
    procs = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(counter)])
             for _ in range(3)]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    assert counter.read_text().count("c") == 1


def test_lock_wait_times_out_loudly(fake, tmp_path):
    import fcntl
    import os

    fd = os.open(tmp_path / ".node-provider.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(base.ProviderUnavailableError, match="timed out"):
            _acquire(fake, tmp_path, env={"MO_NODE_PROVISION_WAIT_S": "0.3"})
    finally:
        os.close(fd)
    assert fake.created == []


def test_acquire_requires_run_dir_and_run_id(fake, tmp_path):
    with pytest.raises(ValueError, match="run dir"):
        _acquire(fake, "")
    with pytest.raises(ValueError, match="run_id"):
        base.acquire_run_node(fake, run_id="", run_dir=tmp_path, profile=None, env={},
                              engine_root="/e")


def test_attach_run_node_never_creates(fake, tmp_path):
    assert base.attach_run_node(tmp_path, {}) is None
    _acquire(fake, tmp_path)
    node = base.attach_run_node(tmp_path, {})
    assert node is not None and node.handle == "sb-0"
    assert fake.created == ["run-1"]


def test_release_run_node_is_idempotent(fake, tmp_path):
    _acquire(fake, tmp_path)
    assert base.release_run_node(tmp_path, {}) is True
    assert fake.released == ["sb-0"]
    assert not (tmp_path / ".node-provider.json").exists()
    assert base.release_run_node(tmp_path, {}) is False
    assert fake.released == ["sb-0"]


def test_release_that_raises_keeps_the_record_for_the_reaper(fake, tmp_path, monkeypatch):
    _acquire(fake, tmp_path)

    def boom(handle, *, env):
        raise RuntimeError("cloud down")

    monkeypatch.setattr(fake, "release", boom)
    assert base.release_run_node(tmp_path, {}) is False
    assert (tmp_path / ".node-provider.json").exists()


def test_reap_is_fail_open_per_provider(fake, monkeypatch):
    class Broken:
        name = "broken"

        def reap(self, **kw):
            raise RuntimeError("no creds")

    monkeypatch.setitem(base._PROVIDERS, "broken", Broken)
    fake.alive.update({"sb-9"})
    out = base.reap_node_providers(max_age_s=1, dry_run=True, env={},
                                   names=["fake", "broken", "nope"])
    assert out == {"fake": ["sb-9"], "broken": [], "nope": []}


# --- secrets ------------------------------------------------------------------


@pytest.fixture
def isolated_vault(monkeypatch, tmp_path):
    """Point the vault's file backend at an empty tmp home — never the real store."""
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_SECRETS", str(tmp_path / "config" / "secrets.local.sh"))
    monkeypatch.delenv("MO_TEST_PROVIDER_KEY", raising=False)
    return tmp_path


def test_secret_from_env_wins(isolated_vault):
    assert base.resolve_provider_secret("MO_TEST_PROVIDER_KEY",
                                        {"MO_TEST_PROVIDER_KEY": "k1"}, provider="fake") == "k1"


def test_secret_from_the_vault_file_backend(isolated_vault):
    store = isolated_vault / "config" / "secrets.local.sh"
    store.parent.mkdir(parents=True)
    store.write_text("export MO_TEST_PROVIDER_KEY='from-file'\n", encoding="utf-8")
    store.chmod(0o600)
    env = {"MINI_ORK_HOME": str(isolated_vault), "MINI_ORK_SECRETS": str(store)}
    assert base.resolve_provider_secret("MO_TEST_PROVIDER_KEY", env, provider="fake") == "from-file"


def test_missing_secret_fails_closed_naming_the_key(isolated_vault):
    env = {"MINI_ORK_HOME": str(isolated_vault)}
    with pytest.raises(base.ProviderUnavailableError, match="MO_TEST_PROVIDER_KEY"):
        base.resolve_provider_secret("MO_TEST_PROVIDER_KEY", env, provider="fake")


# --- import discipline ------------------------------------------------------------


def test_importing_the_package_loads_no_provider_module():
    code = ("import sys, mini_ork.remote.providers as p\n"
            "bad = [m for m in sys.modules if m.startswith('mini_ork.remote.providers.') "
            "and m != 'mini_ork.remote.providers.base']\n"
            "bad += [m for m in sys.modules if m.split('.')[0] in ('daytona', 'daytona_api_client')]\n"
            "print(','.join(bad))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
