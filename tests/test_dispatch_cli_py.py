"""End-to-end test for the Python dispatch CLI entrypoint (mini_ork.dispatch
__main__) — driven entirely offline through a fixture codex wrapper. Proves the
full chain: stdin prompt → dispatch_model → wrapper-over-stdin → codex sidecar
usage/cost → write out-file → persist llm_calls → faithful exit code.
"""

from __future__ import annotations

import io
import sqlite3
import stat

import pytest

from mini_ork.dispatch.__main__ import main

LLM_CALLS_SCHEMA = """
CREATE TABLE llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL, model_id TEXT NOT NULL, tier TEXT NOT NULL,
  feature_name TEXT NOT NULL, actor TEXT, run_id INTEGER,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('success','failed')),
  error_message TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
  cached_input_tokens INTEGER DEFAULT 0
);
"""

# Fake cl_codex.sh: reads the prompt from stdin (the wrapper-stdin contract),
# writes the usage/cost sidecars cl_codex.sh would, and emits cleaned text on
# stdout. Exits MO_TEST_RC (default 0) so we can test rc propagation.
FIXTURE_WRAPPER = r"""#!/usr/bin/env bash
prompt="$(cat)"
[ -n "${MO_USAGE_FILE:-}" ] && printf '%s\t%s\n' 100 40 > "$MO_USAGE_FILE"
[ -n "${MO_COST_FILE:-}" ]  && printf '0.001234\n' > "$MO_COST_FILE"
printf 'ECHO:%s\n' "$prompt"
exit "${MO_TEST_RC:-0}"
"""


def _fixture_root(tmp_path):
    root = tmp_path / "root"
    prov = root / "lib" / "providers"
    prov.mkdir(parents=True)
    wrapper = prov / "cl_codex.sh"
    wrapper.write_text(FIXTURE_WRAPPER)
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return root


def _db(tmp_path):
    p = tmp_path / "state.db"
    con = sqlite3.connect(p)
    con.executescript(LLM_CALLS_SCHEMA)
    con.commit()
    con.close()
    return p


def test_cli_dispatch_writes_text_persists_and_exits_ok(tmp_path, monkeypatch):
    root = _fixture_root(tmp_path)
    db = _db(tmp_path)
    out = tmp_path / "out.txt"
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-cli-1")
    monkeypatch.setattr("sys.stdin", io.StringIO("hello world"))

    rc = main(["codex", "--out", str(out), "--feature", "mini-ork:implementer"])

    assert rc == 0
    assert out.read_text() == "ECHO:hello world\n"  # cleaned text written
    row = sqlite3.connect(db).execute(
        "SELECT provider, model_id, status, input_tokens, output_tokens, cost_usd "
        "FROM llm_calls ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "openai"  # provider_for_model('codex')
    assert row[1] == "codex"
    assert row[2] == "success"
    assert row[3] == 100 and row[4] == 40  # usage from the sidecar
    assert row[5] == pytest.approx(0.001234)  # cost from the sidecar


def test_cli_propagates_nonzero_exit_code(tmp_path, monkeypatch):
    root = _fixture_root(tmp_path)
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.setenv("MO_TEST_RC", "5")  # wrapper exits 5
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("q"))

    rc = main(["codex"])  # stdout path (no --out)
    assert rc == 5  # the provider's exit code is propagated to the CLI exit


def test_cli_unknown_lane_exits_two(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))  # no wrappers here
    monkeypatch.setattr("sys.stdin", io.StringIO("q"))
    assert main(["not-a-lane"]) == 2
