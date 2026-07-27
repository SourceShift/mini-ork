"""Standalone unit tests for ``mini_ork.gates.artifact_contract``.

Replaces the bash-parity gate (against ``lib/artifact_contract.sh``) as
part of the bash→Python migration: the Python port is now the sole
implementation, so its coverage no longer invokes the LIVE bash subprocess
— it asserts the port's behaviour directly. The expected values below are
the semantic contract the bash side used to pin (default-contract shape,
YAML/JSON parsing, validate verdicts + reasons, DB-coupled verifier side
effects, error paths), now asserted on the port's output.

Cases:
  (1) load default contract (no file)              — default shape
  (2) load YAML contract (patch + request_changes) — parsed fields
  (3) load JSON-with-.yaml-extension contract      — JSON-fallback path
  (4) validate correct-type .patch artifact        — 'pass'
  (5) validate wrong-type .json artifact (patch)   — 'fail' + reasons
  (6) validate nonexistent artifact path           — 'fail' + reasons
  (7) DB-coupled: contract with sqlite3 verifier,
      temp DB seeded by init_db, row-count diff   — 'pass' + row written
  (8) missing-args error path (both entrypoints)   — port raises TypeError
  (9) WS4: gate-registry function verifiers dispatch NATIVELY (no
      `source lib/gate_registry.sh`): gate_list → 'pass'; gate_evaluate
      missing context_json → 'fail' with rc=1.

This file subsumes tests/unit/test_artifact_contract.sh (retired): every
one of that fixture's 9 assertions is covered here. Case (8) was ported
from the .sh error-path assertions #8/#9 (``artifact_contract_load`` /
``artifact_contract_validate`` with no args exit non-zero; the port's
required-positionals TypeError is the analog of bash's ${1:?}).

Scope note for (8): bash's ${1:?} (colon variant) also errors on an
EMPTY-string arg, whereas the port returns the default contract for
load_contract(""). That divergence is out of scope — the .sh only asserts
the no-args case (lines 99/108), so no-args is the faithful port;
empty-string parity is deliberately NOT asserted here (it would not hold).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.gates import artifact_contract as ac  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402


def _python_validate_stdout(task_class: str, artifact_path: str,
                            contracts_dir: Path,
                            mini_ork_root: str = str(REPO)) -> str:
    """Format ``validate(tc, artifact)`` to the canonical 2-line stdout
    (verdict line + JSON payload line)."""
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
def test_load_default_contract(home):
    """Default permissive contract (no file) — the port emits the default
    dict shape."""
    contracts_dir = home / "config" / "artifact_contracts"
    contract = ac.load_contract("nonexistent-task-class",
                                contracts_dir=str(contracts_dir))
    assert contract["_default"] is True
    assert contract["expected_artifact"] == "data"
    assert contract["failure_policy"] == "escalate"


# ─────────────────────────────────────────────────────────────────────────────
# (2) load YAML contract with patch + request_changes
# ─────────────────────────────────────────────────────────────────────────────
def test_load_yaml_contract(home):
    """YAML contract with expected_artifact=patch — parsed fields."""
    contracts_dir = home / "config" / "artifact_contracts"
    (contracts_dir / "test-patch.yaml").write_text(
        "task_type: test-patch\n"
        "expected_artifact: patch\n"
        "failure_policy: request_changes\n"
        "rollback_policy: auto\n"
        "success_verifiers: []\n",
        encoding="utf-8",
    )
    contract = ac.load_contract("test-patch", contracts_dir=str(contracts_dir))
    assert contract["expected_artifact"] == "patch"
    assert contract["failure_policy"] == "request_changes"
    assert contract["rollback_policy"] == "auto"


# ─────────────────────────────────────────────────────────────────────────────
# (3) load JSON-with-.yaml-extension contract (JSON-fallback path on yaml-less envs)
# ─────────────────────────────────────────────────────────────────────────────
def test_load_json_extension_yaml(home):
    """A .yaml file containing valid JSON. PyYAML (when installed) parses
    JSON as a subset of YAML and produces the same dict; on yaml-less envs
    the JSON fallback fires — both paths emit the same dict."""
    contracts_dir = home / "config" / "artifact_contracts"
    (contracts_dir / "json-yaml.yaml").write_text(
        '{"task_type": "json-yaml", "expected_artifact": "data", '
        '"failure_policy": "rollback", "rollback_policy": "auto", '
        '"success_verifiers": []}\n',
        encoding="utf-8",
    )
    contract = ac.load_contract("json-yaml", contracts_dir=str(contracts_dir))
    assert contract["failure_policy"] == "rollback"
    assert contract["task_type"] == "json-yaml"


# ─────────────────────────────────────────────────────────────────────────────
# (4) validate correct-type .patch artifact → 'pass'
# ─────────────────────────────────────────────────────────────────────────────
def test_validate_pass_patch_artifact(home):
    """A valid .patch artifact against a patch-typed contract → 'pass'
    verdict."""
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
    py_out = _python_validate_stdout("test-patch", str(artifact), contracts_dir)
    lines = py_out.splitlines()
    assert lines[0] == "pass"
    payload = json.loads(lines[1])
    assert payload["verdict"] == "pass"
    assert payload["artifact_path"] == str(artifact)


# ─────────────────────────────────────────────────────────────────────────────
# (5) validate wrong-type .json artifact when patch expected → 'fail'
# ─────────────────────────────────────────────────────────────────────────────
def test_validate_fail_wrong_ext(home):
    """A .json artifact against a patch-typed contract → 'fail' verdict +
    reasons."""
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
    py_out = _python_validate_stdout("test-patch", str(artifact), contracts_dir)
    lines = py_out.splitlines()
    assert lines[0] == "fail"
    payload = json.loads(lines[1])
    assert payload["verdict"] == "fail"
    assert any("expected artifact type 'patch'" in r for r in payload["reasons"])
    assert payload["failure_policy"] == "request_changes"
    assert payload["rollback_policy"] == "auto"


# ─────────────────────────────────────────────────────────────────────────────
# (6) validate nonexistent artifact path → 'fail'
# ─────────────────────────────────────────────────────────────────────────────
def test_validate_fail_missing_artifact(home):
    """Artifact path that does not exist → 'fail' verdict with 'artifact
    not found' reason."""
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
    py_out = _python_validate_stdout("test-patch", missing, contracts_dir)
    lines = py_out.splitlines()
    assert lines[0] == "fail"
    payload = json.loads(lines[1])
    assert payload["verdict"] == "fail"
    assert any("artifact not found" in r for r in payload["reasons"])


# ─────────────────────────────────────────────────────────────────────────────
# (7) DB-coupled — sqlite3 verifier writes to a temp DB seeded by init_db;
#     'pass' AND the row diff matches.
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path_factory):
    """Spin up a real mini-ork SQLite DB via init_db."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    rc, out, err = mig.init_db(db=dbp, root=str(REPO))
    if rc != 0:
        pytest.skip(f"init_db failed:\n{out}\n{err}")
    return dbp, home


def test_validate_db_coupled_verifier(temp_db, monkeypatch):
    """Contract with a sqlite3 verifier that writes a row to a temp DB; the
    port must emit 'pass' AND the verifier must write its marker row —
    proves verifier subprocess behaviour, not just JSON shape."""
    dbp, home = temp_db
    # Expose MINI_ORK_DB to the port's verifier subprocess.
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    contracts_dir = home / "config" / "artifact_contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)

    # Verifier creates the side table (idempotent) and INSERTs a marker row.
    # `; true` swallows the trailing artifact_path arg the verifier harness
    # always appends, so sqlite3 isn't called with extra positionals. (`:`
    # would also work but YAML would mis-parse the trailing `:` as a
    # key-value separator, turning the verifier into a dict and getting
    # skipped by the harness's `isinstance(verifier, str)` guard.)
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

    # Python validate — must emit 'pass' AND write a row.
    py_out = _python_validate_stdout(
        "db-coupled", str(artifact), contracts_dir, mini_ork_root=str(REPO),
    )
    assert py_out.splitlines()[0] == "pass", f"python stdout was: {py_out!r}"

    con = sqlite3.connect(dbp)
    try:
        after_py = con.execute(
            "SELECT COUNT(*) FROM _verifier_log WHERE marker='parity-marker'"
        ).fetchone()[0]
    finally:
        con.close()
    assert after_py == 1, f"verifier wrote {after_py} rows (expected 1)"


# ─────────────────────────────────────────────────────────────────────────────
# (8) missing-args error path — the port's required-positionals raise
#     TypeError (the analog of bash's ${1:?task_class required}). Ports
#     test_artifact_contract.sh assertions #8/#9.
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_args_error():
    """Both entrypoints reject a no-args call: the port raises TypeError
    (missing required positional)."""
    with pytest.raises(TypeError):
        ac.load_contract()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ac.validate()  # type: ignore[call-arg]


# ─────────────────────────────────────────────────────────────────────────────
# (9) WS4: gate-registry function verifiers dispatch NATIVELY (no
#     `source lib/gate_registry.sh`). `gate_list` ignores the positional →
#     rc 0 → 'pass'; `gate_evaluate` missing context_json → the native
#     TypeError maps to rc=1 → 'fail'.
# ─────────────────────────────────────────────────────────────────────────────
def test_validate_gate_list_verifier_native(temp_db, monkeypatch):
    """A contract whose success_verifiers = ['gate_list'] passes through the
    native dispatch (mini_ork.gates.gate_registry.gate_list) — the native
    path must not execute any bash."""
    dbp, home = temp_db
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    contracts_dir = home / "config" / "artifact_contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    (contracts_dir / "gate-list.yaml").write_text(
        "task_type: gate-list\n"
        "expected_artifact: data\n"
        "failure_policy: escalate\n"
        "rollback_policy: none\n"
        "success_verifiers:\n"
        "  - gate_list\n",
        encoding="utf-8",
    )
    artifact = home / "data.json"
    artifact.write_text('{"k":"v"}', encoding="utf-8")

    py_out = _python_validate_stdout(
        "gate-list", str(artifact), contracts_dir, mini_ork_root=str(REPO),
    )
    assert py_out.splitlines()[0] == "pass", f"python: {py_out!r}"


def test_validate_gate_evaluate_verifier_fails(temp_db, monkeypatch):
    """A contract whose success_verifiers = ['gate_evaluate'] fails: the
    native dispatch raises TypeError for the missing context_json (mapped
    to rc=1) → fail verdict with an rc=1 reason."""
    dbp, home = temp_db
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    contracts_dir = home / "config" / "artifact_contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    (contracts_dir / "gate-eval.yaml").write_text(
        "task_type: gate-eval\n"
        "expected_artifact: data\n"
        "failure_policy: escalate\n"
        "rollback_policy: none\n"
        "success_verifiers:\n"
        "  - gate_evaluate\n",
        encoding="utf-8",
    )
    artifact = home / "data.json"
    artifact.write_text('{"k":"v"}', encoding="utf-8")

    contract = ac.load_contract("gate-eval", contracts_dir=str(contracts_dir))
    py_payload = ac.validate_artifact(contract, str(artifact),
                                      mini_ork_root=str(REPO))

    assert py_payload["verdict"] == "fail"
    # The native dispatch maps the missing-context TypeError to rc=1.
    assert any(
        r.startswith("verifier 'gate_evaluate' failed (rc=1)")
        for r in py_payload["reasons"]
    ), f"py reasons: {py_payload['reasons']!r}"
