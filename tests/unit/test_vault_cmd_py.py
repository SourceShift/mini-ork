"""``mini-ork vault`` must answer "is it wired" and "where does NAME live"
without ever printing a value — a command that prints secrets ends up in shell
history and on screen shares.
"""
import json

from mini_ork.cli import vault_cmd


def _store(tmp_path, body):
    path = tmp_path / "config" / "secrets.local.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_status_reports_the_default_chain(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    assert vault_cmd.main(["status"], root="") == 0
    out = capsys.readouterr().out
    assert "environment" in out
    assert "file (0600)" in out


def test_which_names_the_backend_and_never_the_value(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    _store(tmp_path, "export ARXIV_API_TOKEN=hunter2\n")
    assert vault_cmd.main(["which", "ARXIV_API_TOKEN"], root="") == 0
    out = capsys.readouterr().out
    assert "ARXIV_API_TOKEN" in out
    assert "file (0600)" in out
    assert "hunter2" not in out


def test_which_prefers_the_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    _store(tmp_path, "export TOKEN=from-file\n")
    monkeypatch.setenv("TOKEN", "from-env")
    assert vault_cmd.main(["which", "TOKEN"], root="") == 0
    assert "environment" in capsys.readouterr().out


def test_which_missing_key_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    assert vault_cmd.main(["which", "ABSENT"], root="") == 1
    assert "not found in any backend" in capsys.readouterr().out


def test_status_json_lists_backends(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    assert vault_cmd.main(["status", "--json"], root="") == 0
    payload = json.loads(capsys.readouterr().out)
    assert [entry["backend"] for entry in payload["backends"]] == ["environment", "file (0600)"]
    assert all(entry["ready"] for entry in payload["backends"])


def test_a_broken_config_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    _store(tmp_path, "export A=1\n")
    (tmp_path / "config" / "vault.yaml").write_text(
        "backends:\n  - type: carrier-pigeon\n", encoding="utf-8"
    )
    assert vault_cmd.main(["status"], root="") == 2
    assert "carrier-pigeon" in capsys.readouterr().err


def test_unknown_action_is_a_usage_error(capsys):
    assert vault_cmd.main(["frobnicate"], root="") == 2
    assert "unknown vault action" in capsys.readouterr().err


def test_help_prints_usage(capsys):
    assert vault_cmd.main([], root="") == 0
    assert "vault status" in capsys.readouterr().out


def test_which_requires_a_name(capsys):
    assert vault_cmd.main(["which"], root="") == 2
    assert "NAME is required" in capsys.readouterr().err
