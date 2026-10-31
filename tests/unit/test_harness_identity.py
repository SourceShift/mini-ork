"""Harness identity (mini_ork.dispatch.harness_identity) and its ledger stamp.

Hermetic: a temporary providers registry (MINI_ORK_PROVIDERS), fake CLIs on a
temporary PATH, a temporary MINI_ORK_HOME, and a fresh migrated state.db.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from mini_ork.dispatch import harness_identity
from mini_ork.stores import migrate as mig
from mini_ork.version import __version__

REPO = Path(__file__).resolve().parents[2]

REGISTRY = """
providers:
  native:
    kind: anthropic-native
  compat:
    kind: anthropic-compat
    base_url: https://gateway.invalid
    api_key_env: COMPAT_KEY
  cx:
    kind: codex-native
  http:
    kind: openai-compat
    base_url: https://api.invalid/v1
    api_key_env: HTTP_KEY
  script_lane:
    kind: executable
    script: {script}
"""


def _cli(path: Path, version: str, counter: Path) -> None:
    path.write_text(f"#!/bin/sh\necho x >> '{counter}'\necho '{version}'\n")
    path.chmod(0o755)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "calls"
    _cli(bin_dir / "claude", "9.9.9 (Claude Code)", counter)
    script = tmp_path / "lane.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    registry = tmp_path / "providers.yaml"
    registry.write_text(REGISTRY.format(script=script))
    home = tmp_path / "home"
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(registry))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setattr(harness_identity, "_MEMO", {})
    return {"PATH": str(bin_dir), "MINI_ORK_HOME": str(home), "bin": bin_dir,
            "counter": counter, "script": script}


def _calls(env) -> int:
    return len(env["counter"].read_text().splitlines()) if env["counter"].exists() else 0


def test_claude_lanes_report_the_cli_and_its_version(env):
    expected = {"harness": "claude-code", "binary": "claude", "version": "9.9.9 (Claude Code)"}
    native = harness_identity.identity("native", environment=env)
    compat = harness_identity.identity("compat", environment=env)
    assert native == {**expected, "kind": "anthropic-native"}
    assert compat == {**expected, "kind": "anthropic-compat"}


def test_the_version_is_asked_once_per_release_of_the_binary(env, monkeypatch):
    harness_identity.identity("native", environment=env)
    harness_identity.identity("native", environment=env)
    assert _calls(env) == 1
    monkeypatch.setattr(harness_identity, "_MEMO", {})  # a new process
    harness_identity.identity("native", environment=env)
    assert _calls(env) == 1, "the on-disk cache should answer a new process"
    cache = json.loads((Path(env["MINI_ORK_HOME"]) / "harness-versions.json").read_text())
    assert [v["version"] for v in cache.values()] == ["9.9.9 (Claude Code)"]

    _cli(env["bin"] / "claude", "10.0.0 (Claude Code)", env["counter"])  # an upgrade
    assert harness_identity.identity("native", environment=env)["version"] == "10.0.0 (Claude Code)"
    assert _calls(env) == 2


def test_without_a_home_nothing_is_written_to_the_cwd(env, tmp_path, monkeypatch):
    work = tmp_path / "target-repo"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    no_home = {"PATH": env["PATH"]}
    assert harness_identity.identity("native", environment=no_home)["version"] == "9.9.9 (Claude Code)"
    assert list(work.iterdir()) == []


def test_a_missing_binary_reports_no_version(env):
    assert harness_identity.identity("cx", environment=env) == {
        "harness": "codex", "kind": "codex-native", "binary": "codex", "version": ""}


def test_an_in_process_transport_reports_the_mini_ork_version(env):
    assert harness_identity.identity("http", environment=env) == {
        "harness": "mini-ork/openai-compat", "kind": "openai-compat", "binary": "",
        "version": f"mini-ork {__version__}"}


def test_an_executable_lane_reports_its_script_digest(env):
    found = harness_identity.identity("script_lane", environment=env)
    assert found["harness"] == "executable"
    assert found["binary"] == str(env["script"])
    assert found["version"].startswith("sha256:") and len(found["version"]) == len("sha256:") + 12


def test_an_unknown_lane_has_no_identity(env):
    assert harness_identity.identity("nope", environment=env) is None


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "state.db"
    rc, _, err = mig.init_db(db=str(path), root=str(REPO))
    assert rc == 0, err
    return str(path)


def _ledger(db_path):
    con = sqlite3.connect(db_path)
    try:
        md = con.execute("SELECT metadata_json FROM llm_calls ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        audit = con.execute("SELECT payload FROM audit_log ORDER BY audit_id DESC LIMIT 1").fetchone()[0]
    finally:
        con.close()
    return json.loads(md), json.loads(audit)["event"]


def test_every_ledger_row_and_its_audit_event_name_the_harness(env, db, monkeypatch):
    from mini_ork.dispatch.llm_dispatch import write_llm_calls_row

    monkeypatch.setenv("PATH", env["PATH"] + os.pathsep + os.environ.get("PATH", ""))
    write_llm_calls_row(db, "anthropic", "native", "default", "mini-ork:reviewer", "reviewer",
                        "success", 900, "0.01", "", 10, 5, "{}")
    metadata, event = _ledger(db)
    assert metadata["harness"]["version"] == "9.9.9 (Claude Code)"
    assert event["harness"] == metadata["harness"]


def test_a_harness_the_caller_already_named_is_kept(env, db):
    from mini_ork.dispatch.llm_dispatch import write_llm_calls_row

    named = {"harness": "claude-code", "kind": "anthropic-native", "binary": "claude", "version": "1.0"}
    write_llm_calls_row(db, "anthropic", "native", "default", "f", "a", "success", 1, "0", "",
                        1, 1, json.dumps({"harness": named}))
    metadata, _ = _ledger(db)
    assert metadata["harness"] == named
