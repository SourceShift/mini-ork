"""Tests for the secret vault backend chain (``mini_ork.dispatch.vault``).

The vault exists so that ``${NAME}`` resolves the same way on a laptop with a
0600 file and on a team deployment behind OpenBao. These tests pin the two
properties that make that safe: precedence is the declared order and the
environment always wins, and every misconfiguration fails closed with an error
that names the key and the backend but never a value.
"""
import json
import subprocess
import urllib.error

import pytest
import yaml

from mini_ork.dispatch.vault import (
    BackendStatus,
    CommandBackend,
    EnvBackend,
    FileBackend,
    OpenBaoBackend,
    SopsBackend,
    VaultEnvironment,
    VaultError,
    load_vault_config,
    parse_vault_config,
    report_protection_gap,
    vault_config_path,
    vault_environment,
    vault_gap_marker_path,
)


# --------------------------------------------------------------------------- #
# Config parsing
# --------------------------------------------------------------------------- #

def test_no_config_gives_env_then_file():
    config = parse_vault_config(None)
    assert [backend.name for backend in config.backends] == ["env", "file"]


def test_missing_config_file_gives_default_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    assert not vault_config_path().exists()
    config = load_vault_config()
    assert [backend.name for backend in config.backends] == ["env", "file"]


def test_backends_absent_uses_default_types():
    config = parse_vault_config({"backends": None})
    assert [backend.name for backend in config.backends] == ["env", "file"]


def test_unknown_section_is_rejected():
    # A typo like `backend:` must not silently leave the default chain in place.
    with pytest.raises(VaultError, match="unknown vault section"):
        parse_vault_config({"backend": [{"type": "env"}]})


def test_unknown_backend_type_is_rejected():
    with pytest.raises(VaultError, match="unknown vault backend 'keepass'"):
        parse_vault_config({"backends": [{"type": "keepass"}]})


def test_backend_entry_may_be_a_bare_type_name():
    config = parse_vault_config({"backends": ["env", "file"]})
    assert [backend.name for backend in config.backends] == ["env", "file"]


def test_relative_file_paths_resolve_against_the_config_dir(tmp_path):
    config = parse_vault_config(
        {"backends": [{"type": "sops", "file": "secrets.enc.yaml"}]}, base=tmp_path
    )
    assert config.backends[0]._path == tmp_path / "secrets.enc.yaml"


def test_sops_without_a_file_is_rejected():
    with pytest.raises(VaultError, match="needs 'file:'"):
        parse_vault_config({"backends": [{"type": "sops"}]})


def test_command_without_argv_is_rejected():
    with pytest.raises(VaultError, match="needs 'argv:'"):
        parse_vault_config({"backends": [{"type": "command", "argv": []}]})


def test_bad_vault_yaml_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    path = vault_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("backends: [\n", encoding="utf-8")
    with pytest.raises(VaultError, match="cannot read vault config"):
        load_vault_config()


# --------------------------------------------------------------------------- #
# The chain as a Mapping
# --------------------------------------------------------------------------- #

def _write_store(tmp_path, body):
    path = tmp_path / "secrets.local.sh"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_environment_wins_over_the_file(tmp_path):
    store = _write_store(tmp_path, "export TOKEN=from-file\n")
    environment = VaultEnvironment(
        [EnvBackend({"TOKEN": "from-env"}), FileBackend(store)]
    )
    assert environment.resolve("TOKEN") == "from-env"


def test_file_serves_a_key_the_environment_does_not_have(tmp_path):
    store = _write_store(tmp_path, "export ONLY_FILE=value\n")
    environment = VaultEnvironment([EnvBackend({}), FileBackend(store)])
    assert environment.resolve("ONLY_FILE") == "value"


def test_missing_key_resolves_to_none_and_raises_keyerror(tmp_path):
    environment = VaultEnvironment([EnvBackend({})])
    assert environment.resolve("ABSENT") is None
    assert "ABSENT" not in environment
    with pytest.raises(KeyError):
        environment["ABSENT"]


def test_environment_is_a_well_formed_mapping(tmp_path):
    store = _write_store(tmp_path, "export FROM_FILE=f\n")
    environment = VaultEnvironment(
        [EnvBackend({"FROM_ENV": "e"}), FileBackend(store)]
    )
    assert dict(environment) == {"FROM_ENV": "e", "FROM_FILE": "f"}
    assert len(environment) == 2
    assert "FROM_FILE" in environment


def test_a_lookup_is_cached_across_calls(tmp_path):
    calls = []

    class Counting:
        name = "counting"

        def get(self, key):
            calls.append(key)
            return "v"

        def describe(self):
            return "counting"

        def status(self):
            return BackendStatus("counting", True, "ready")

    environment = VaultEnvironment([Counting()])
    environment.resolve("K")
    environment.resolve("K")
    assert calls == ["K"]


def test_describe_names_backends_not_values(tmp_path):
    store = _write_store(tmp_path, "export SECRET=hunter2\n")
    environment = VaultEnvironment([EnvBackend({}), FileBackend(store)])
    assert "hunter2" not in environment.describe()
    assert str(store) in environment.describe()


def test_status_reports_each_backend(tmp_path):
    store = _write_store(tmp_path, "export A=1\n")
    environment = VaultEnvironment([EnvBackend({}), FileBackend(store)])
    statuses = environment.status()
    assert [status.backend for status in statuses] == ["env", "file"]
    assert all(status.ready for status in statuses)
    assert statuses[1].source == str(store)


def test_missing_store_file_is_not_an_error(tmp_path):
    # The unconfigured state: nothing installed, nothing exported. It must read
    # as "empty", not as "broken".
    environment = VaultEnvironment([FileBackend(tmp_path / "absent.sh")])
    assert environment.status()[0].ready is True
    assert "no file yet" in environment.status()[0].detail


def test_file_backend_never_serves_a_symlink(tmp_path):
    real = _write_store(tmp_path, "export A=1\n")
    link = tmp_path / "link.sh"
    link.symlink_to(real)
    with pytest.raises(VaultError, match="symlink"):
        FileBackend(link).get("A")


# --------------------------------------------------------------------------- #
# sops
# --------------------------------------------------------------------------- #

def test_sops_decrypts_once_and_caches(tmp_path, monkeypatch):
    encrypted = tmp_path / "secrets.enc.yaml"
    encrypted.write_text("ENC[age]\n", encoding="utf-8")
    runs = []

    def fake_run(cmd, **kwargs):
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "TOKEN: decrypted\n", "")

    monkeypatch.setattr("mini_ork.dispatch.vault.shutil.which", lambda name: "/usr/bin/sops")
    monkeypatch.setattr("mini_ork.dispatch.vault.subprocess.run", fake_run)

    backend = SopsBackend(encrypted)
    assert backend.get("TOKEN") == "decrypted"
    assert backend.get("TOKEN") == "decrypted"
    assert len(runs) == 1


def test_sops_missing_binary_is_a_vault_error(tmp_path, monkeypatch):
    encrypted = tmp_path / "secrets.enc.yaml"
    encrypted.write_text("ENC\n", encoding="utf-8")
    monkeypatch.setattr("mini_ork.dispatch.vault.shutil.which", lambda name: None)
    with pytest.raises(VaultError, match="not on PATH"):
        SopsBackend(encrypted).get("TOKEN")


def test_sops_failure_relays_stderr_but_no_value(tmp_path, monkeypatch):
    encrypted = tmp_path / "secrets.enc.yaml"
    encrypted.write_text("ENC\n", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "age: no identity matched\n")

    monkeypatch.setattr("mini_ork.dispatch.vault.shutil.which", lambda name: "/usr/bin/sops")
    monkeypatch.setattr("mini_ork.dispatch.vault.subprocess.run", fake_run)
    with pytest.raises(VaultError, match="no identity matched"):
        SopsBackend(encrypted).get("TOKEN")


# --------------------------------------------------------------------------- #
# command
# --------------------------------------------------------------------------- #

def test_command_backend_substitutes_the_key(monkeypatch):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "s3cret\n", "")

    monkeypatch.setattr("mini_ork.dispatch.vault.shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr("mini_ork.dispatch.vault.subprocess.run", fake_run)

    backend = CommandBackend(["pass", "show", "mini-ork/{key}"])
    assert backend.get("ARXIV_API_TOKEN") == "s3cret"
    assert seen[0] == ["/bin/pass", "show", "mini-ork/ARXIV_API_TOKEN"]


def test_command_backend_never_uses_a_shell(monkeypatch):
    # A key with a space must stay one argv element rather than become two.
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "v\n", "")

    monkeypatch.setattr("mini_ork.dispatch.vault.shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr("mini_ork.dispatch.vault.subprocess.run", fake_run)

    backend = CommandBackend(["get", "{key}"])
    backend.get("A B; rm -rf /")
    assert seen[0] == ["/bin/get", "A B; rm -rf /"]


def test_command_nonzero_exit_is_not_an_absent_key(monkeypatch):
    # `pass show missing` and a network timeout both exit non-zero; treating
    # that as "unset" would silently drop a credential.
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "boom\n")

    monkeypatch.setattr("mini_ork.dispatch.vault.shutil.which", lambda name: "/bin/pass")
    monkeypatch.setattr("mini_ork.dispatch.vault.subprocess.run", fake_run)
    with pytest.raises(VaultError, match="exited 1"):
        CommandBackend(["pass", "show", "{key}"]).get("TOKEN")


def test_command_empty_output_means_absent(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "\n", "")

    monkeypatch.setattr("mini_ork.dispatch.vault.shutil.which", lambda name: "/bin/pass")
    monkeypatch.setattr("mini_ork.dispatch.vault.subprocess.run", fake_run)
    assert CommandBackend(["pass", "show", "{key}"]).get("TOKEN") is None


def test_command_missing_executable_is_a_vault_error(monkeypatch):
    monkeypatch.setattr("mini_ork.dispatch.vault.shutil.which", lambda name: None)
    with pytest.raises(VaultError, match="not on PATH"):
        CommandBackend(["definitely-not-a-tool", "{key}"]).get("TOKEN")


def test_command_empty_argv_is_rejected():
    with pytest.raises(VaultError, match="argv"):
        CommandBackend([])


# --------------------------------------------------------------------------- #
# openbao
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(payloads, *, codes=None):
    """Return a urlopen stand-in serving payloads by path substring."""
    calls = []

    def fake(request, timeout=None):
        url = request.full_url
        calls.append((url, dict(request.headers)))
        for needle, code in (codes or {}).items():
            if needle in url:
                raise urllib.error.HTTPError(url, code, "err", {}, None)
        for needle, payload in payloads.items():
            if needle in url:
                return _FakeResponse(payload)
        raise urllib.error.HTTPError(url, 404, "missing", {}, None)

    fake.calls = calls
    return fake


def test_openbao_reads_kv_v2_and_merges_paths(monkeypatch):
    fake = _fake_urlopen({
        "collector": {"data": {"data": {"COLLECTOR_KEY": "one"}}},
        "reviewer": {"data": {"data": {"REVIEWER_KEY": "two"}}},
    })
    monkeypatch.setattr("mini_ork.dispatch.vault.urllib.request.urlopen", fake)

    backend = OpenBaoBackend(
        "http://bao:8200", paths=["collector", "reviewer"], token="t"
    )
    assert backend.get("COLLECTOR_KEY") == "one"
    assert backend.get("REVIEWER_KEY") == "two"
    assert fake.calls[0][0] == "http://bao:8200/v1/secret/data/collector"
    assert fake.calls[0][1].get("X-vault-token") == "t"


def test_openbao_404_on_a_path_is_not_an_error(monkeypatch):
    # An operator adds paths as they need them; a not-yet-created path is empty.
    fake = _fake_urlopen({"known": {"data": {"data": {"K": "v"}}}})
    monkeypatch.setattr("mini_ork.dispatch.vault.urllib.request.urlopen", fake)
    backend = OpenBaoBackend("http://bao:8200", paths=["missing", "known"], token="t")
    assert backend.get("K") == "v"


def test_openbao_403_names_the_token_env_and_no_value(monkeypatch):
    fake = _fake_urlopen({}, codes={"secret": 403})
    monkeypatch.setattr("mini_ork.dispatch.vault.urllib.request.urlopen", fake)
    backend = OpenBaoBackend(
        "http://bao:8200", paths=["collector"], token_env="NODE_TOKEN"
    )
    monkeypatch.setenv("NODE_TOKEN", "t")
    with pytest.raises(VaultError, match="NODE_TOKEN is not permitted"):
        backend.get("K")


def test_openbao_without_paths_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "mini_ork.dispatch.vault.urllib.request.urlopen",
        _fake_urlopen({}),
    )
    backend = OpenBaoBackend("http://bao:8200", token="t")
    with pytest.raises(VaultError, match="no 'paths' configured"):
        backend.get("K")


def test_openbao_without_a_token_is_rejected(monkeypatch):
    monkeypatch.delenv("OPENBAO_TOKEN", raising=False)
    backend = OpenBaoBackend("http://bao:8200", paths=["collector"])
    with pytest.raises(VaultError, match="no token"):
        backend.get("K")


def test_openbao_unreachable_is_a_vault_error(monkeypatch):
    def fake(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("mini_ork.dispatch.vault.urllib.request.urlopen", fake)
    backend = OpenBaoBackend("http://bao:8200", paths=["collector"], token="t")
    with pytest.raises(VaultError, match="cannot reach OpenBao"):
        backend.get("K")


def test_openbao_backend_needs_an_address():
    with pytest.raises(VaultError, match="'address' is required"):
        OpenBaoBackend("")


def test_openbao_status_reports_readiness(monkeypatch):
    monkeypatch.setattr(
        "mini_ork.dispatch.vault.urllib.request.urlopen",
        _fake_urlopen({"collector": {"data": {"data": {"K": "v"}}}}),
    )
    backend = OpenBaoBackend("http://bao:8200", paths=["collector"], token="t")
    status = backend.status()
    assert status.ready is True
    assert "1 keys" in status.detail


# --------------------------------------------------------------------------- #
# vault_environment end-to-end
# --------------------------------------------------------------------------- #

def test_vault_environment_reads_the_configured_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "vault.yaml").write_text(
        yaml.safe_dump({"backends": [{"type": "file", "file": "secrets.local.sh"}]}),
        encoding="utf-8",
    )
    store = config_dir / "secrets.local.sh"
    store.write_text("export FROM_FILE=v\n", encoding="utf-8")
    store.chmod(0o600)

    environment = vault_environment()
    assert [backend.name for backend in environment.backends] == ["file"]
    assert environment.resolve("FROM_FILE") == "v"


def test_vault_environment_defaults_without_a_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("EXPORTED_IN_TEST", "yes")
    environment = vault_environment()
    assert [backend.name for backend in environment.backends] == ["env", "file"]
    assert environment.resolve("EXPORTED_IN_TEST") == "yes"


def test_a_bad_config_raises_at_construction(tmp_path, monkeypatch):
    # A declared backend that cannot work means no lookup can succeed; the
    # caller should learn that at the boundary, not mid-node.
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "vault.yaml").write_text(
        yaml.safe_dump({"backends": [{"type": "carrier-pigeon"}]}), encoding="utf-8"
    )
    with pytest.raises(VaultError):
        vault_environment()


def test_vault_error_is_a_secret_store_error():
    # Every existing `except SecretStoreError` path must catch a vault failure.
    from mini_ork.dispatch.secrets import SecretStoreError

    assert issubclass(VaultError, SecretStoreError)


# --------------------------------------------------------------------------- #
# protection gap
# --------------------------------------------------------------------------- #

def test_only_the_encrypting_backends_count_as_protected(tmp_path):
    assert EnvBackend({}).protected is False
    assert FileBackend(tmp_path / "s.sh").protected is False
    assert SopsBackend(tmp_path / "e.yaml").protected is True
    assert CommandBackend(["true"]).protected is True
    assert OpenBaoBackend("http://bao:8200", paths=["p"], token="t").protected is True


def test_an_empty_chain_reports_no_gap():
    # Nothing configured yet is not a downgrade to warn about.
    assert VaultEnvironment([]).protection_gap() is None


def test_plaintext_only_chain_has_a_gap(tmp_path):
    store = _write_store(tmp_path, "export A=1\n")
    gap = VaultEnvironment([EnvBackend({}), FileBackend(store)]).protection_gap()
    assert gap is not None
    assert "sops" in gap and "openbao" in gap
    assert str(store) in gap  # names where values come from, not what they are


def test_any_protected_backend_closes_the_gap(tmp_path):
    store = _write_store(tmp_path, "export A=1\n")
    for protected in (
        SopsBackend(tmp_path / "e.yaml"),
        CommandBackend(["pass", "show", "{key}"]),
        OpenBaoBackend("http://bao:8200", paths=["p"], token="t"),
    ):
        chain = VaultEnvironment([EnvBackend({}), FileBackend(store), protected])
        assert chain.protection_gap() is None


def test_report_gap_prints_once_until_the_chain_changes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    store = _write_store(tmp_path, "export A=1\n")
    chain = VaultEnvironment([EnvBackend({}), FileBackend(store)])

    assert report_protection_gap(chain) is True
    assert "[vault] secrets resolve in the clear" in capsys.readouterr().err

    # Same chain, next run: quiet.
    assert report_protection_gap(chain) is False
    assert capsys.readouterr().err == ""

    # The chain changed, so the note is worth making again.
    changed = VaultEnvironment([EnvBackend({}), FileBackend(tmp_path / "other.sh")])
    assert report_protection_gap(changed) is True
    assert capsys.readouterr().err != ""


def test_a_protected_chain_is_quiet_and_records_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    chain = VaultEnvironment(
        [EnvBackend({}), OpenBaoBackend("http://bao:8200", paths=["p"], token="t")]
    )
    assert report_protection_gap(chain) is False
    assert capsys.readouterr().err == ""
    assert not vault_gap_marker_path().exists()


def test_the_marker_names_the_chain_and_carries_no_value(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    store = _write_store(tmp_path, "export SECRET=hunter2\n")
    report_protection_gap(VaultEnvironment([EnvBackend({}), FileBackend(store)]))

    text = vault_gap_marker_path().read_text(encoding="utf-8")
    assert "hunter2" not in text
    assert "file:" in text


def test_an_unwritable_marker_still_gives_the_note(tmp_path, monkeypatch, capsys):
    # A marker that cannot be read or written costs a repeated note, never the
    # note itself, and never a failed run.
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    marker = vault_gap_marker_path()
    marker.parent.mkdir(parents=True)
    marker.mkdir()  # a directory where the file should be

    store = _write_store(tmp_path, "export A=1\n")
    chain = VaultEnvironment([EnvBackend({}), FileBackend(store)])
    assert report_protection_gap(chain) is True
    assert "secrets resolve in the clear" in capsys.readouterr().err
