"""Certificate seal (mini_ork.certify.seal) and `mini-ork certify verify`.

Hermetic: a fresh migrated state.db and a temporary MINI_ORK_HOME per test.
"""
from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.certify import seal as cert_seal
from mini_ork.certify.certificate import build_certificate
from mini_ork.cli import certify as certify_cli
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
pytest.importorskip("cryptography")


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "state.db"
    rc, _, err = mig.init_db(db=str(path), root=str(REPO))
    assert rc == 0, err
    return str(path)


def _cert() -> dict:
    return build_certificate(
        verdict="PROVEN", reason="fix verified", repo_path="/repo", remote=None,
        base_sha="a" * 40, head_sha="b" * 40,
        patch_text="+++ b/app.py\n+fixed = True\n", issue_text="app crashes on empty input",
        oracle_method="probe+delta+invariants", mr_n=3, proven_bar=0.667, model="m",
        image="img", poc_plus=None, mr_pass_rate=1.0, invariants=[], cost_usd=0.03,
        llm_calls=4, duration_s=12.0,
    )


def test_seal_records_the_issuance_and_verifies_against_the_pinned_key(tmp_path, db):
    cert = _cert()
    seal = cert_seal.seal_certificate(cert, home=tmp_path / "home", db=db)

    assert seal["signature"]["alg"] == "ed25519"
    assert seal["audit"]["audit_id"] == 1
    pub = (tmp_path / "home" / "keys" / "certify-ed25519.pub").read_text().strip()
    assert pub == seal["signature"]["public_key"]
    private = tmp_path / "home" / "keys" / "certify-ed25519.pem"
    assert oct(private.stat().st_mode & 0o777) == "0o600"

    result = cert_seal.verify_certificate(cert, seal, pubkey_b64=pub, db=db)
    assert result["verdict"] == "VERIFIED", result
    assert result["checks"] == {"digest": True, "seal_matches_certificate": True,
                                "signature": True, "audit_chain": True, "issuance_row": True}


def test_without_a_pinned_key_the_verdict_is_unpinned(tmp_path, db):
    cert = _cert()
    seal = cert_seal.seal_certificate(cert, home=tmp_path / "home", db=db)
    assert cert_seal.verify_certificate(cert, seal)["verdict"] == "UNPINNED"


@pytest.mark.parametrize("tamper", ["verdict", "signature", "foreign_key", "seal_digest"])
def test_tampering_fails(tmp_path, db, tamper):
    cert = _cert()
    seal = cert_seal.seal_certificate(cert, home=tmp_path / "home", db=db)
    pub = seal["signature"]["public_key"]
    if tamper == "verdict":
        cert = {**cert, "verdict": "REFUTED"}
    elif tamper == "signature":
        seal = {**seal, "signature": {**seal["signature"], "value": seal["signature"]["value"][::-1]}}
    elif tamper == "foreign_key":
        other = cert_seal.seal_certificate(_cert(), home=tmp_path / "other-home", db=None)
        pub = other["signature"]["public_key"]
    elif tamper == "seal_digest":
        seal = {**seal, "digest": "0" * 64}

    result = cert_seal.verify_certificate(cert, seal, pubkey_b64=pub, db=db)
    assert result["verdict"] == "FAILED"
    assert result["failures"]


def test_a_removed_issuance_row_fails_verification(tmp_path, db):
    cert = _cert()
    seal = cert_seal.seal_certificate(cert, home=tmp_path / "home", db=db)
    con = sqlite3.connect(db)
    con.execute("DROP TRIGGER IF EXISTS trg_al_no_delete")
    con.execute("DELETE FROM audit_log WHERE audit_id=?", (seal["audit"]["audit_id"],))
    con.commit()
    con.close()

    result = cert_seal.verify_certificate(cert, seal, pubkey_b64=seal["signature"]["public_key"], db=db)
    assert result["verdict"] == "FAILED"
    assert result["checks"]["issuance_row"] is False


def test_without_cryptography_the_seal_is_unsigned(tmp_path, db, monkeypatch):
    monkeypatch.setattr(cert_seal, "_crypto", lambda: None)
    cert = _cert()
    seal = cert_seal.seal_certificate(cert, home=tmp_path / "home", db=db)
    assert seal["signature"] is None
    assert "cryptography" in seal["unsigned_reason"]
    assert cert_seal.verify_certificate(cert, seal, db=db)["verdict"] == "UNSIGNED"


def test_certify_verify_cli_round_trip(tmp_path, db, monkeypatch):
    home = tmp_path / "home"
    cert = _cert()
    seal = cert_seal.seal_certificate(cert, home=home, db=db)
    cert_path = tmp_path / "c.json"
    cert_path.write_text(json.dumps(cert))
    (tmp_path / "c.seal.json").write_text(json.dumps(seal))
    pub_file = home / "keys" / "certify-ed25519.pub"

    out, err = io.StringIO(), io.StringIO()
    rc = certify_cli.main(["verify", str(cert_path), "--pubkey", str(pub_file), "--db", db],
                          stdout=out, stderr=err)
    assert rc == 0, out.getvalue() + err.getvalue()
    assert out.getvalue().startswith("VERIFIED")

    cert_path.write_text(json.dumps({**cert, "reason": "edited after issuance"}))
    rc = certify_cli.main(["verify", str(cert_path), "--pubkey", str(pub_file)],
                          stdout=io.StringIO(), stderr=io.StringIO())
    assert rc == 1
