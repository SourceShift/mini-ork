"""The vault's write side — ``mini_ork.dispatch.vault_write``.

The read side is ``tests/unit/test_vault_py.py``: the backend chain, the
precedence order, ``vault which``. This file is the other half, and it exists
because the write side has one rule the read side does not — **it writes
exactly one place, and refuses everything else.**

What is pinned:

* the file tier is the only thing written, at 0600, through
  ``secrets.write_secret_exports`` rather than a second atomic writer here
* a declared ``sops`` / ``openbao`` / ``command`` backend is refused with the
  command an operator should run — never bypassed to drop a plaintext file
  behind the encrypted one
* an environment-only chain is refused too: there is no store to write
* every refusal names the key, the backend and the store, and carries **no
  value** — asserted with a sentinel, not trusted
* ``assert_writable`` / ``store_path`` answer the same question as
  ``set_secret`` without writing, so the add flow can refuse before it prompts
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.dispatch import vault_write  # noqa: E402
from mini_ork.dispatch.secrets import SecretStoreError, read_secret_exports  # noqa: E402
from mini_ork.dispatch.vault import (  # noqa: E402
    CommandBackend,
    EnvBackend,
    FileBackend,
    OpenBaoBackend,
    SopsBackend,
    VaultConfig,
)

SENTINEL = "sk-live-SENTINEL-do-not-leak-9f3a"

#: A chain that writes the file: the simple tier, no vault.yaml needed.
FILE_CHAIN = VaultConfig([EnvBackend(), FileBackend()])


def _home(tmp_path: Path) -> dict[str, str]:
    return {"MINI_ORK_HOME": str(tmp_path / "proj" / ".mini-ork")}


def _store(home_env: dict[str, str]) -> Path:
    return Path(home_env["MINI_ORK_HOME"]) / "config" / "secrets.local.sh"


# ── the file tier: the one thing that is written ───────────────────────────


def test_set_secret_writes_an_owner_only_file_and_reads_back(tmp_path):
    env = _home(tmp_path)
    written = vault_write.set_secret("LINEAR_API_KEY", SENTINEL, env=env, config=FILE_CHAIN)
    assert written == _store(env)
    assert read_secret_exports(written) == {"LINEAR_API_KEY": SENTINEL}
    mode = stat.S_IMODE(written.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_the_store_directory_is_created_owner_only(tmp_path):
    env = _home(tmp_path)
    vault_write.set_secret("K", "v", env=env, config=FILE_CHAIN)
    directory = _store(env).parent
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_set_secret_merges_with_what_is_already_there(tmp_path):
    env = _home(tmp_path)
    vault_write.set_secret("FIRST", "1", env=env, config=FILE_CHAIN)
    vault_write.set_secret("SECOND", "2", env=env, config=FILE_CHAIN)
    assert read_secret_exports(_store(env)) == {"FIRST": "1", "SECOND": "2"}


def test_a_second_write_replaces_the_same_key(tmp_path):
    env = _home(tmp_path)
    vault_write.set_secret("K", "old", env=env, config=FILE_CHAIN)
    vault_write.set_secret("K", "new", env=env, config=FILE_CHAIN)
    assert read_secret_exports(_store(env)) == {"K": "new"}


@pytest.mark.parametrize("value", ["a b", "quote'and\"double", "tab\there", "semi;colon"])
def test_a_value_that_needs_quoting_round_trips(tmp_path, value):
    env = _home(tmp_path)
    written = vault_write.set_secret("K", value, env=env, config=FILE_CHAIN)
    assert read_secret_exports(written) == {"K": value}


def test_no_temp_file_is_left_behind(tmp_path):
    env = _home(tmp_path)
    vault_write.set_secret("K", "v", env=env, config=FILE_CHAIN)
    left = sorted(p.name for p in _store(env).parent.iterdir())
    assert left == ["secrets.local.sh"]


def test_the_file_tier_delegates_to_the_one_writer(tmp_path, monkeypatch):
    """``vault_write`` adds dispatch and refusals — not a second atomic writer.

    Pinned by replacing ``secrets.write_secret_exports`` at the name
    ``vault_write`` imported it under: if this module ever grew its own
    ``mkstemp``/``os.replace`` and stopped calling through, the stub would not
    be reached and this test would fail.
    """
    env = _home(tmp_path)
    calls: list[tuple[dict[str, str], Path]] = []

    def spy(updates, path=None):
        calls.append((dict(updates), Path(path)))
        return Path(path)

    monkeypatch.setattr(vault_write, "write_secret_exports", spy)
    returned = vault_write.set_secret("LINEAR_API_KEY", SENTINEL, env=env, config=FILE_CHAIN)
    assert calls == [({"LINEAR_API_KEY": SENTINEL}, _store(env))]
    assert returned == _store(env)
    assert not _store(env).exists()  # the real writer never ran


# ── the file tier's own rules still refuse ─────────────────────────────────


def test_a_multiline_value_is_refused(tmp_path):
    env = _home(tmp_path)
    with pytest.raises(SecretStoreError):
        vault_write.set_secret("K", "line1\nline2", env=env, config=FILE_CHAIN)
    assert not _store(env).exists()


def test_a_name_that_is_not_a_shell_identifier_is_refused(tmp_path):
    env = _home(tmp_path)
    with pytest.raises(SecretStoreError):
        vault_write.set_secret("not-a-key", "v", env=env, config=FILE_CHAIN)
    assert not _store(env).exists()


# ── the refusals: every other tier ─────────────────────────────────────────


def test_sops_is_refused_with_the_command_to_run_instead(tmp_path):
    backend = SopsBackend(tmp_path / "secrets.enc.yaml")
    with pytest.raises(vault_write.SopsWriteRefused) as caught:
        vault_write.set_secret("LINEAR_API_KEY", SENTINEL,
                               env=_home(tmp_path), config=VaultConfig([backend]))
    message = str(caught.value)
    assert "sops" in message
    assert "LINEAR_API_KEY" in message
    assert str(backend._path) in message
    assert caught.value.argv[:3] == ("sops", "set", str(backend._path))


def test_openbao_is_refused_with_the_bao_put_command(tmp_path):
    backend = OpenBaoBackend("http://127.0.0.1:8200", mount="secret",
                             paths=["mini-ork/collector"])
    with pytest.raises(vault_write.OpenBaoWriteRefused) as caught:
        vault_write.set_secret("LINEAR_API_KEY", SENTINEL,
                               env=_home(tmp_path), config=VaultConfig([backend]))
    argv = caught.value.argv
    assert argv[:3] == ("bao", "kv", "put")
    assert "-mount=secret" in argv
    assert "mini-ork/collector" in argv
    assert any(part.startswith("LINEAR_API_KEY=") for part in argv)


def test_a_command_backend_is_refused_naming_the_tool(tmp_path):
    """A ``command`` backend hands back no argv: its configured argv *reads*.

    Offering it under "run this instead" would send the operator to run a
    lookup as if it were the write, so the refusal names the tool and stops.
    """
    backend = CommandBackend(["pass", "show", "mini-ork/{key}"])
    with pytest.raises(vault_write.CommandWriteRefused) as caught:
        vault_write.set_secret("K", SENTINEL, env=_home(tmp_path),
                               config=VaultConfig([backend]))
    assert "pass" in str(caught.value)
    assert caught.value.argv == ()
    assert "run:" not in str(caught.value)


def test_an_env_only_chain_is_refused_because_there_is_no_store(tmp_path):
    with pytest.raises(vault_write.EnvWriteRefused) as caught:
        vault_write.set_secret("K", SENTINEL, env=_home(tmp_path),
                               config=VaultConfig([EnvBackend()]))
    assert caught.value.argv[0] == "export"
    assert "no store" in str(caught.value)


def test_an_unknown_backend_is_named_with_the_known_set(tmp_path):
    class TeleportBackend:
        name = "teleport"

    with pytest.raises(vault_write.UnknownBackendWriteRefused) as caught:
        vault_write.set_secret("K", SENTINEL, env=_home(tmp_path),
                               config=VaultConfig([TeleportBackend()]))
    assert "teleport" in str(caught.value)
    for known in vault_write.KNOWN_BACKENDS:
        assert known in str(caught.value)


def test_every_refusal_is_a_secret_store_error():
    """A caller that already handles an unusable vault handles a refusal too."""
    assert issubclass(vault_write.VaultWriteRefused, SecretStoreError)


@pytest.mark.parametrize("backend", [
    SopsBackend("/tmp/x.enc.yaml"),
    OpenBaoBackend("http://127.0.0.1:8200", paths=["mini-ork/collector"]),
    CommandBackend(["pass", "show", "{key}"]),
    EnvBackend(),
])
def test_no_refusal_ever_carries_the_value(tmp_path, backend):
    """A refusal names the key, the backend and the store — and nothing else.

    The value the caller passed never reaches the message or the argv, in any
    backend; a placeholder stands where it would go.
    """
    with pytest.raises(vault_write.VaultWriteRefused) as caught:
        vault_write.set_secret("LINEAR_API_KEY", SENTINEL, env=_home(tmp_path),
                               config=VaultConfig([backend]))
    assert SENTINEL not in str(caught.value)
    assert all(SENTINEL not in part for part in caught.value.argv)


@pytest.mark.parametrize("backend", [
    SopsBackend("/tmp/x.enc.yaml"),
    OpenBaoBackend("http://127.0.0.1:8200", paths=["mini-ork/collector"]),
    EnvBackend(),
])
def test_a_refusal_that_offers_a_command_uses_a_placeholder(tmp_path, backend):
    """The command an operator is handed carries ``<value>``, never a value."""
    with pytest.raises(vault_write.VaultWriteRefused) as caught:
        vault_write.set_secret("LINEAR_API_KEY", SENTINEL, env=_home(tmp_path),
                               config=VaultConfig([backend]))
    assert vault_write.VALUE_PLACEHOLDER in str(caught.value)


# ── the chain decides, and a secure tier is never bypassed ─────────────────


def test_env_is_skipped_not_refused_when_a_file_follows(tmp_path):
    """``env, file`` is the default chain; skipping the ambient tier is what
    keeps the simple tier from refusing its own default."""
    env = _home(tmp_path)
    written = vault_write.set_secret("K", "v", env=env, config=FILE_CHAIN)
    assert written == _store(env)
    assert read_secret_exports(written) == {"K": "v"}


def test_a_secure_tier_in_front_of_the_file_refuses_rather_than_bypasses(tmp_path):
    """``sops, file`` must not drop a plaintext file the reader never consults."""
    env = _home(tmp_path)
    chain = VaultConfig([SopsBackend(tmp_path / "s.enc.yaml"), FileBackend()])
    with pytest.raises(vault_write.SopsWriteRefused):
        vault_write.set_secret("K", SENTINEL, env=env, config=chain)
    assert not _store(env).exists()


# ── the read-only twins: answer without writing ────────────────────────────


def test_assert_writable_returns_none_for_the_file_tier_and_writes_nothing(tmp_path):
    env = _home(tmp_path)
    assert vault_write.assert_writable("K", env=env, config=FILE_CHAIN) is None
    assert not _store(env).exists()


def test_assert_writable_raises_the_same_refusal_without_writing(tmp_path):
    env = _home(tmp_path)
    chain = VaultConfig([SopsBackend(tmp_path / "s.enc.yaml")])
    with pytest.raises(vault_write.SopsWriteRefused):
        vault_write.assert_writable("K", env=env, config=chain)
    assert not _store(env).exists()


def test_store_path_names_where_a_write_would_go(tmp_path):
    env = _home(tmp_path)
    assert vault_write.store_path("K", env=env, config=FILE_CHAIN) == _store(env)


def test_store_path_raises_the_refusal_for_a_secure_tier(tmp_path):
    env = _home(tmp_path)
    chain = VaultConfig([SopsBackend(tmp_path / "s.enc.yaml")])
    with pytest.raises(vault_write.SopsWriteRefused):
        vault_write.store_path("K", env=env, config=chain)


# ── the chain read from vault.yaml is the one that decides ─────────────────


def test_the_default_chain_with_no_vault_yaml_is_env_then_file(tmp_path):
    """No ``config/vault.yaml`` at all: still writable, to the default store."""
    env = _home(tmp_path)
    written = vault_write.set_secret("K", "v", env=env)
    assert written == _store(env)


def test_a_vault_yaml_that_puts_sops_first_is_the_chain_that_decides(tmp_path):
    """The chain comes from the file, never from a guess about the platform."""
    home = Path(env_home := _home(tmp_path)["MINI_ORK_HOME"])
    config_dir = home / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "vault.yaml").write_text(
        "backends:\n  - type: env\n  - type: sops\n    file: secrets.enc.yaml\n",
        encoding="utf-8",
    )
    with pytest.raises(vault_write.SopsWriteRefused):
        vault_write.set_secret("K", SENTINEL, env=_home(tmp_path))
    assert env_home  # the home the refusal was resolved against


def test_a_vault_yaml_with_a_file_entry_writes_where_the_reader_reads(tmp_path):
    """An explicit ``file:`` is resolved exactly as ``FileBackend`` reads it.

    The writer must not choose a different path than the reader: the store the
    vault consults is the only path worth writing.
    """
    env = _home(tmp_path)
    home = Path(env["MINI_ORK_HOME"])
    config_dir = home / "config"
    config_dir.mkdir(parents=True)
    explicit = config_dir / "custom.store"
    (config_dir / "vault.yaml").write_text(
        f"backends:\n  - type: file\n    file: {explicit}\n", encoding="utf-8"
    )
    written = vault_write.set_secret("K", "v", env=env)
    assert written == explicit
    assert read_secret_exports(explicit) == {"K": "v"}
