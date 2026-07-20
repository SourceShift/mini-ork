"""Parity gate: mini_ork.gates.artifact_contract vs lib/artifact_contract.sh.

Each test invokes the LIVE bash subprocess on the same inputs as the Python
port and asserts byte/JSON-identical output. No mocks, no hardcoded expected
outputs — expected is always derived from a control bash invocation that
shares the inputs.

>=7 cases (per the kickoff: >=6 required):
  (1) load default contract (no file)              — JSON byte-equal
  (2) load YAML contract (patch + request_changes) — JSON byte-equal
  (3) load JSON-with-.yaml-extension contract      — JSON byte-equal
  (4) validate correct-type .patch artifact       — 'pass' + JSON byte-equal
  (5) validate wrong-type .json artifact (patch)  — 'fail' + JSON byte-equal
  (6) validate nonexistent artifact path          — 'fail' + JSON byte-equal
  (7) DB-coupled: contract with sqlite3 verifier,
      temp DB seeded by db/init.sh, row-count diff — 'pass' + row diff

The bash-side unit test (``bash tests/unit/test_artifact_contract.sh``) is
run separately as a regression guard to prove the bash script was not
disturbed.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.gates import artifact_contract as ac

SH = REPO / "lib" / "artifact_contract.sh"
INIT_SH = REPO / "db" / "init.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — invoke LIVE bash via subprocess and format Python output to match
# ─────────────────────────────────────────────────────────────────────────────
def _bash_load(task_class: str, contracts_dir: Path) -> str:
    """Invoke ``artifact_contract_load <tc>`` via bash subprocess, return stdout.

    Compares ONLY stdout (the JSON payload) against the Python port — bash's
    default-fallback 'using default' stderr notice is excluded by parity design.
    """
    r = subprocess.run(
        ["bash", "-c", f'. "{SH}" && artifact_contract_load "$1"',
         "_", task_class],
        env={**os.environ, "MINI_ORK_ROOT": str(REPO),
             "MINI_ORK_HOME": str(contracts_dir.parent.parent)},
        capture_output=True, text=True,
    )
    return r.stdout


def _bash_validate(task_class: str, artifact_path: str, contracts_dir: Path,
                   mini_ork_root: str = str(REPO),
                   extra_env: dict | None = None) -> str:
    """Invoke ``artifact_contract_validate <tc> <artifact>`` via bash subprocess."""
    env = {**os.environ, "MINI_ORK_ROOT": mini_ork_root,
           "MINI_ORK_HOME": str(contracts_dir.parent.parent)}
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(
        ["bash", "-c",
         f'. "{SH}" && artifact_contract_validate "$1" "$2"',
         "_", task_class, artifact_path],
        env=env,
        capture_output=True, text=True,
    )
    return r.stdout


def _python_load_stdout(task_class: str, contracts_dir: Path) -> str:
    """Format ``load_contract(tc)`` to bash-equivalent stdout (``json + \\n``)."""
    contract = ac.load_contract(task_class, contracts_dir=str(contracts_dir))
    return json.dumps(contract) + "\n"


def _python_validate_stdout(task_class: str, artifact_path: str,
                            contracts_dir: Path,
                            mini_ork_root: str = str(REPO)) -> str:
    """Format ``validate(tc, artifact)`` to bash-equivalent stdout (2-line)."""
    contract = ac.load_contract(task_class, contracts_dir=str(contracts_dir))
    payload = ac.validate_artifact(contract, artifact_path,
                                    mini_ork_root=mini_ork_root)
    return payload["verdict"] + "\n" + json.dumps(payload) + "\n"


@pytest.fixture
def home(tmp_path):
    """Per-test MINI_ORK_HOME with empty config/artifact_contracts/."""
    cfg = tmp_path / "config" / "artifact_contracts"
    cfg.mkdir(parents=True)
    return tmp_path


# ─────────────────────────────────────────────────────────────────────────────
# (1) load default contract (no file present)
# ─────────────────────────────────────────────────────────────────────────────
def test_load_default_contract_parity(home):
    """Default permissive contract (no file) — bash and Python both emit the
    same JSON dict with the same key order and trailing newline."""
    contracts_dir = home / "config" / "artifact_contracts"
    bash_out = _bash_load("nonexistent-task-class", contracts_dir)
    py_out = _python_load_stdout("nonexistent-task-class", contracts_dir)
    assert bash_out == py_out
    parsed = json.loads(bash_out)
    assert parsed["_default"] is True
    assert parsed["expected_artifact"] == "data"
    assert parsed["failure_policy"] == "escalate"


# ─────────────────────────────────────────────────────────────────────────────
# (2) load YAML contract with patch + request_changes
# ─────────────────────────────────────────────────────────────────────────────
def test_load_yaml_contract_parity(home):
    """YAML contract with expected_artifact=patch — bash parses via PyYAML,
    Python port matches verbatim."""
    contracts_dir = home / "config" / "artifact_contracts"
    (contracts_dir / "test-patch.yaml").write_text(
        "task_type: test-patch\n"
        "expected_artifact: patch\n"
        "failure_policy: request_changes\n"
        "rollback_policy: auto\n"
        "success_verifiers: []\n",
        encoding="utf-8",
    )
    bash_out = _bash_load("test-patch", contracts_dir)
    py_out = _python_load_stdout("test-patch", contracts_dir)
    assert bash_out == py_out
    parsed = json.loads(bash_out)
    assert parsed["expected_artifact"] == "patch"
    assert parsed["failure_policy"] == "request_changes"
    assert parsed["rollback_policy"] == "auto"


# ─────────────────────────────────────────────────────────────────────────────
# (3) load JSON-with-.yaml-extension contract (JSON-fallback path on yaml-less envs)
# ─────────────────────────────────────────────────────────────────────────────
def test_load_json_extension_yaml_parity(home):
    """A .yaml file containing valid JSON. PyYAML (when installed) parses JSON
    as a subset of YAML and produces the same dict; on yaml-less envs the JSON
    fallback fires — both paths emit the same JSON to stdout."""
    contracts_dir = home / "config" / "artifact_contracts"
    (contracts_dir / "json-yaml.yaml").write_text(
        '{"task_type": "json-yaml", "expected_artifact": "data", '
        '"failure_policy": "rollback", "rollback_policy": "auto", '
        '"success_verifiers": []}\n',
        encoding="utf-8",
    )
    bash_out = _bash_load("json-yaml", contracts_dir)
    py_out = _python_load_stdout("json-yaml", contracts_dir)
    assert bash_out == py_out
    parsed = json.loads(bash_out)
    assert parsed["failure_policy"] == "rollback"
    assert parsed["task_type"] == "json-yaml"


# ─────────────────────────────────────────────────────────────────────────────
# (4) validate correct-type .patch artifact → 'pass'
# ─────────────────────────────────────────────────────────────────────────────
def test_validate_pass_patch_artifact_parity(home):
    """A valid .patch artifact against a patch-typed contract → 'pass' line
    plus identical JSON dict on line 2."""
    contracts_dir = home / "config" / "artifact_contracts"
    (contracts_dir / "test-patch.yaml").write_text(
        "task_type: test-patch\n"
        "expected_artifact: patch\n"
        "failure_policy: request_changes\n"
        "rollback_policy: auto\n"
        "success_verifiers: []\n",
        encoding="utf-8",
    )
    artifact = home / "diff.patch"
    artifact.write_text(
        "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-old\n+new\n",
        encoding="utf-8",
    )
    bash_out = _bash_validate("test-patch", str(artifact), contracts_dir)
    py_out = _python_validate_stdout("test-patch", str(artifact), contracts_dir)
    assert bash_out == py_out
    lines = bash_out.splitlines()
    assert lines[0] == "pass"
    payload = json.loads(lines[1])
    assert payload["verdict"] == "pass"
    assert payload["artifact_path"] == str(artifact)


# ─────────────────────────────────────────────────────────────────────────────
# (5) validate wrong-type .json artifact when patch expected → 'fail'
# ─────────────────────────────────────────────────────────────────────────────
def test_validate_fail_wrong_ext_parity(home):
    """A .json artifact against a patch-typed contract → 'fail' line plus
    identical reasons JSON."""
    contracts_dir = home / "config" / "artifact_contracts"
    (contracts_dir / "test-patch.yaml").write_text(
        "task_type: test-patch\n"
        "expected_artifact: patch\n"
        "failure_policy: request_changes\n"
        "rollback_policy: auto\n"
        "success_verifiers: []\n",
        encoding="utf-8",
    )
    artifact = home / "data.json"
    artifact.write_text('{"k":"v"}', encoding="utf-8")
    bash_out = _bash_validate("test-patch", str(artifact), contracts_dir)
    py_out = _python_validate_stdout("test-patch", str(artifact), contracts_dir)
    assert bash_out == py_out
    lines = bash_out.splitlines()
    assert lines[0] == "fail"
    payload = json.loads(lines[1])
    assert payload["verdict"] == "fail"
    assert any("expected artifact type 'patch'" in r for r in payload["reasons"])
    assert payload["failure_policy"] == "request_changes"
    assert payload["rollback_policy"] == "auto"


# ─────────────────────────────────────────────────────────────────────────────
# (6) validate nonexistent artifact path → 'fail'
# ─────────────────────────────────────────────────────────────────────────────
def test_validate_fail_missing_artifact_parity(home):
    """Artifact path that does not exist → 'fail' line with 'artifact not
    found' reason, identical to bash output."""
    contracts_dir = home / "config" / "artifact_contracts"
    (contracts_dir / "test-patch.yaml").write_text(
        "task_type: test-patch\n"
        "expected_artifact: patch\n"
        "failure_policy: request_changes\n"
        "rollback_policy: auto\n"
        "success_verifiers: []\n",
        encoding="utf-8",
    )
    missing = str(home / "no-such-artifact.patch")
    bash_out = _bash_validate("test-patch", missing, contracts_dir)
    py_out = _python_validate_stdout("test-patch", missing, contracts_dir)
    assert bash_out == py_out
    lines = bash_out.splitlines()
    assert lines[0] == "fail"
    payload = json.loads(lines[1])
    assert payload["verdict"] == "fail"
    assert any("artifact not found" in r for r in payload["reasons"])


# ─────────────────────────────────────────────────────────────────────────────
# (7) DB-coupled parity — sqlite3 verifier writes to a temp DB seeded by
#     db/init.sh; both bash and Python must agree on 'pass' AND the row diff
#     matches.
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path_factory):
    """Spin up a real mini-ork SQLite DB via db/init.sh — kickoff requirement."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}")
    return dbp, home


def test_validate_db_coupled_verifier_parity(temp_db, monkeypatch):
    """Contract with a sqlite3 verifier that writes a row to a temp DB; both
    bash and Python must emit 'pass' AND the resulting row count diff must
    match — proves verifier subprocess parity, not just JSON shape parity."""
    dbp, home = temp_db
    # Expose MINI_ORK_DB to the Python port's verifier subprocess (mirrors the
    # bash subprocess env used above).
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    contracts_dir = home / "config" / "artifact_contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)

    # Verifier creates the side table (idempotent) and INSERTs a marker row.
    # `; true` swallows the trailing artifact_path arg the bash verifier harness
    # always appends, so sqlite3 isn't called with extra positionals. (`:` would
    # also work but YAML would mis-parse the trailing `:` as a key-value
    # separator, turning the verifier into a dict and getting skipped by the
    # bash's `isinstance(verifier, str)` guard.)
    verifier_cmd = (
        'sqlite3 "$MINI_ORK_DB" "CREATE TABLE IF NOT EXISTS _verifier_log '
        "(artifact_path TEXT, marker TEXT); "
        "INSERT INTO _verifier_log VALUES ('$1', 'parity-marker');\" "
        ">/dev/null 2>&1; true"
    )
    contract_yaml = (
        "task_type: db-coupled\n"
        "expected_artifact: patch\n"
        "failure_policy: escalate\n"
        "rollback_policy: none\n"
        "success_verifiers:\n"
        f"  - {verifier_cmd}\n"
    )
    (contracts_dir / "db-coupled.yaml").write_text(contract_yaml, encoding="utf-8")

    artifact = home / "diff.patch"
    artifact.write_text(
        "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n", encoding="utf-8"
    )

    # Sanity: side-table doesn't exist yet → row count = 0.
    con = sqlite3.connect(dbp)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS _verifier_log "
            "(artifact_path TEXT, marker TEXT)"
        )
        con.commit()
        before = con.execute(
            "SELECT COUNT(*) FROM _verifier_log WHERE marker='parity-marker'"
        ).fetchone()[0]
        assert before == 0
    finally:
        con.close()

    # Bash validate — must emit 'pass' AND write a row.
    bash_out = _bash_validate(
        "db-coupled", str(artifact), contracts_dir,
        mini_ork_root=str(REPO),
        extra_env={"MINI_ORK_DB": dbp},
    )
    assert bash_out.splitlines()[0] == "pass", f"bash stdout was: {bash_out!r}"

    con = sqlite3.connect(dbp)
    try:
        after_bash = con.execute(
            "SELECT COUNT(*) FROM _verifier_log WHERE marker='parity-marker'"
        ).fetchone()[0]
    finally:
        con.close()
    assert after_bash == 1, f"bash verifier wrote {after_bash} rows (expected 1)"

    # Python validate — must emit identical stdout AND write a row.
    py_out = _python_validate_stdout(
        "db-coupled", str(artifact), contracts_dir, mini_ork_root=str(REPO),
    )
    assert py_out == bash_out, (
        f"python stdout diverged from bash\n"
        f"bash: {bash_out!r}\npython: {py_out!r}"
    )

    con = sqlite3.connect(dbp)
    try:
        after_py = con.execute(
            "SELECT COUNT(*) FROM _verifier_log WHERE marker='parity-marker'"
        ).fetchone()[0]
    finally:
        con.close()
    assert after_py == 2, (
        f"python verifier wrote {after_py - after_bash} new rows "
        f"(expected 1, total = 2)"
    )