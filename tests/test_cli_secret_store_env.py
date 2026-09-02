"""main() must load the secret store into os.environ (bash-source parity).

The retired bash dispatcher `source`d secrets.local.sh, so store overrides
reached every child process including verifiers. Native dispatch scoped the
store to provider transports only, which stranded non-credential overrides
(e.g. MINI_ORK_TYPECHECK_CMD) outside the verifier subprocess env.
"""
import os

from mini_ork.cli.main import _load_secret_store_env


def _write_store(home, lines):
    config = home / "config"
    config.mkdir(parents=True)
    store = config / "secrets.local.sh"
    store.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(store, 0o600)
    return store


def test_store_exports_reach_os_environ(tmp_path, monkeypatch):
    _write_store(tmp_path, ['export MINI_ORK_TYPECHECK_CMD="pnpm run type-check:full"'])
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_SECRETS", raising=False)
    monkeypatch.delenv("MINI_ORK_TYPECHECK_CMD", raising=False)
    _load_secret_store_env()
    assert os.environ["MINI_ORK_TYPECHECK_CMD"] == "pnpm run type-check:full"


def test_real_environment_wins_over_store(tmp_path, monkeypatch):
    _write_store(tmp_path, ["export MINI_ORK_TYPECHECK_CMD=from-store"])
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_SECRETS", raising=False)
    monkeypatch.setenv("MINI_ORK_TYPECHECK_CMD", "from-env")
    _load_secret_store_env()
    assert os.environ["MINI_ORK_TYPECHECK_CMD"] == "from-env"


def test_unreadable_store_warns_and_continues(tmp_path, monkeypatch, capsys):
    store = _write_store(tmp_path, ["export MO_PROBE_VAR=bar"])
    os.chmod(store, 0o644)  # group/world-readable -> SecretStoreError
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_SECRETS", raising=False)
    monkeypatch.delenv("MO_PROBE_VAR", raising=False)
    _load_secret_store_env()
    assert "MO_PROBE_VAR" not in os.environ
    assert "secret store not loaded" in capsys.readouterr().err
