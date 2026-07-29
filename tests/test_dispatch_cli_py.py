"""End-to-end test for the Python dispatch CLI entrypoint (mini_ork.dispatch
__main__) — driven entirely offline through a fixture codex CLI. Proves the
full chain: stdin prompt → dispatch_model → codex_transport (native Python
replacement for cl_codex.sh since bash-removal WS6) → codex sidecar
usage/cost → write out-file → persist llm_calls → faithful exit code.
"""

from __future__ import annotations

import io
import os
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

# Fake codex CLI: emits a JSONL event stream like `codex exec --json` (a
# turn.completed with usage + an agent_message echoing the prompt after `--`),
# writes no --output-last-message (so the transport exercises its
# reconstruction path), and exits MO_TEST_RC (default 0) for rc propagation.
FAKE_CODEX = r"""#!/usr/bin/env bash
prompt=""
seen_dd=0
for a in "$@"; do
  if [ "$seen_dd" = "1" ]; then prompt="$a"; fi
  if [ "$a" = "--" ]; then seen_dd=1; fi
done
printf '%s\n' '{"type":"thread.started","thread_id":"thr-cli"}'
printf '%s\n' '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":40,"cached_input_tokens":0}}'
printf '%s\n' "{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"ECHO:$prompt\"}}"
exit "${MO_TEST_RC:-0}"
"""


def _fixture_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    (root / "config" / "providers.yaml").write_text(
        "providers:\n  codex:\n    kind: codex-native\n    family: openai\n",
        encoding="utf-8",
    )

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "codex"
    fake.write_text(FAKE_CODEX)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")

    # The transport's framework-tree cwd guard refuses the repo root the test
    # suite runs from — point the dispatch at a plain target dir instead.
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(target))
    monkeypatch.delenv("MINI_ORK_TARGET_REPO", raising=False)
    monkeypatch.delenv("MO_ALLOW_FRAMEWORK_CWD", raising=False)
    monkeypatch.delenv("MO_OAI_BASE_URL", raising=False)
    monkeypatch.delenv("MO_OAI_ENV_KEY", raising=False)
    monkeypatch.delenv("MO_OAI_MODEL", raising=False)
    # Deterministic cost rates (env overrides win over pricing.yaml/defaults).
    monkeypatch.setenv("MO_CODEX_USD_PER_MTOK_IN", "1.0")
    monkeypatch.setenv("MO_CODEX_USD_PER_MTOK_CACHED", "0.5")
    monkeypatch.setenv("MO_CODEX_USD_PER_MTOK_OUT", "2.0")
    return root


def _db(tmp_path):
    p = tmp_path / "state.db"
    con = sqlite3.connect(p)
    con.executescript(LLM_CALLS_SCHEMA)
    con.commit()
    con.close()
    return p


def test_cli_dispatch_writes_text_persists_and_exits_ok(tmp_path, monkeypatch):
    root = _fixture_root(tmp_path, monkeypatch)
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
    # cost from the sidecar: (100*1.0 + 0*0.5 + 40*2.0)/1e6
    assert row[5] == pytest.approx(0.00018)


def test_cli_propagates_nonzero_exit_code(tmp_path, monkeypatch):
    root = _fixture_root(tmp_path, monkeypatch)
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.setenv("MO_TEST_RC", "5")  # codex CLI itself fails
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("q"))

    rc = main(["codex"])  # stdout path (no --out)
    # cl_codex.sh contract: a failed `codex exec` maps to wrapper rc 4.
    assert rc == 4


def test_cli_unknown_lane_exits_two(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))  # no wrappers here
    monkeypatch.setattr("sys.stdin", io.StringIO("q"))
    assert main(["not-a-lane"]) == 2
