"""Seal a certificate so an outside party can check it without trusting the runtime.

A certificate's ``digest`` proves only that its JSON is self-consistent: anyone
who edits the file can recompute it. The seal adds the two things an outsider
needs, in a detached ``<certificate>.seal.json`` so the certificate schema
(``mini-ork.certificate/v1``) and every consumer of it stay unchanged:

* **audit** — the ``audit_log`` row (``mini_ork.stores.audit_chain``) that
  recorded the issuance: its id and row hash. The row commits to the digest and
  to every row before it, so removing or rewriting the issuance breaks the chain.
* **signature** — Ed25519 over ``canonical({certificate_id, digest, audit})``,
  with a key kept under ``$MINI_ORK_HOME/keys/``. Verified against a public key
  the verifier obtained independently (``--pubkey``), it shows the certificate
  came from the holder of that key and was not changed since.

Signing needs the optional ``cryptography`` package (the ``integrations``
extra). Without it the seal is written with ``signature: null`` and an
``unsigned_reason``, and ``verify`` reports UNSIGNED rather than pretending.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from mini_ork.stores import audit_chain

SEAL_SCHEMA = "mini-ork.certificate-seal/v1"
_KEY_NAME = "certify-ed25519"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except ImportError:
        return None
    return serialization, Ed25519PrivateKey, Ed25519PublicKey


def key_paths(home: str | os.PathLike) -> tuple[Path, Path]:
    keys = Path(home) / "keys"
    return keys / f"{_KEY_NAME}.pem", keys / f"{_KEY_NAME}.pub"


def _public_b64(public_key, serialization) -> str:
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def load_or_create_signing_key(home: str | os.PathLike):
    """``(private_key, public_key_b64)``, minting the key pair on first use.

    ``None`` when ``cryptography`` is not installed. The private key is written
    0600; the ``.pub`` file is what an operator publishes for verifiers.
    """
    crypto = _crypto()
    if crypto is None:
        return None
    serialization, Ed25519PrivateKey, _ = crypto
    private_path, public_path = key_paths(home)
    if private_path.is_file():
        private_key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    else:
        private_path.parent.mkdir(parents=True, exist_ok=True)
        private_key = Ed25519PrivateKey.generate()
        pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(pem)
    public_b64 = _public_b64(private_key.public_key(), serialization)
    if not public_path.is_file():
        public_path.write_text(public_b64 + "\n", encoding="utf-8")
    return private_key, public_b64


def _signed_message(certificate_id: str, digest: str, audit: dict | None) -> bytes:
    return _canonical({"certificate_id": certificate_id, "digest": digest, "audit": audit}).encode("utf-8")


def seal_certificate(cert: dict, *, home: str | os.PathLike, db: str | None) -> dict:
    """Record the issuance in the audit chain (when ``db`` exists) and sign it."""
    audit = None
    if db and os.path.isfile(db):
        audit = audit_chain.record(
            db, "certificate_issued", "certify", f"certificate:{cert['id']}",
            {"certificate_id": cert["id"], "digest": cert["digest"],
             "verdict": cert.get("verdict"), "repo_head": (cert.get("repo") or {}).get("head")},
        )
    seal: dict = {
        "schema": SEAL_SCHEMA,
        "certificate_id": cert["id"],
        "digest": cert["digest"],
        "audit": audit,
        "signature": None,
    }
    signer = load_or_create_signing_key(home)
    if signer is None:
        seal["unsigned_reason"] = "cryptography is not installed (pip install 'mini-ork[integrations]')"
        return seal
    private_key, public_b64 = signer
    value = private_key.sign(_signed_message(cert["id"], cert["digest"], audit))
    seal["signature"] = {
        "alg": "ed25519",
        "public_key": public_b64,
        "value": base64.b64encode(value).decode("ascii"),
    }
    return seal


def certificate_digest(cert: dict) -> str:
    """Recompute a certificate's digest the way ``build_certificate`` sealed it."""
    base = {k: v for k, v in cert.items() if k != "digest"}
    return hashlib.sha256(_canonical(base).encode("utf-8")).hexdigest()


def verify_certificate(cert: dict, seal: dict, *, pubkey_b64: str | None = None,
                       db: str | None = None) -> dict:
    """Check a certificate and its seal; nothing here trusts the issuing runtime.

    ``verdict`` is ``VERIFIED`` (digest, seal, signature against the pinned key,
    and — when ``db`` is given — the audit chain all hold), ``UNPINNED`` (all
    hold but no ``pubkey_b64`` was supplied, so the signature only proves
    self-consistency), ``UNSIGNED`` (no signature in the seal) or ``FAILED``.
    """
    checks: dict[str, Any] = {}
    failures: list[str] = []

    digest_ok = certificate_digest(cert) == cert.get("digest")
    checks["digest"] = digest_ok
    if not digest_ok:
        failures.append("certificate digest does not match its contents")

    seal_ok = (seal.get("schema") == SEAL_SCHEMA and seal.get("certificate_id") == cert.get("id")
               and seal.get("digest") == cert.get("digest"))
    checks["seal_matches_certificate"] = seal_ok
    if not seal_ok:
        failures.append("seal does not name this certificate's id and digest")

    signature = seal.get("signature")
    pinned = None
    if signature:
        crypto = _crypto()
        if crypto is None:
            failures.append("cryptography is not installed; cannot check the signature")
            checks["signature"] = None
        else:
            serialization, _, Ed25519PublicKey = crypto
            embedded = signature.get("public_key") or ""
            key_b64 = pubkey_b64 or embedded
            pinned = pubkey_b64 is not None
            if pinned and pubkey_b64.strip() != embedded.strip():
                failures.append("seal was signed by a different key than the pinned one")
            try:
                public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(key_b64))
                public_key.verify(
                    base64.b64decode(signature.get("value") or ""),
                    _signed_message(seal.get("certificate_id", ""), seal.get("digest", ""),
                                    seal.get("audit")),
                )
                checks["signature"] = True
            except Exception:  # noqa: BLE001 — any parse/verify error is a failed signature
                checks["signature"] = False
                failures.append("signature does not verify")
    else:
        checks["signature"] = None

    if db is not None:
        chain = audit_chain.verify_chain(db)
        checks["audit_chain"] = chain["ok"]
        if not chain["ok"]:
            failures.append(f"audit chain broken at row {chain['first_break']}: {chain['reason']}")
        audit = seal.get("audit") or {}
        row = audit_chain.find_row(db, int(audit["audit_id"])) if audit.get("audit_id") else None
        row_ok = (row is not None and row.get("row_hash") == audit.get("row_hash")
                  and (row.get("event") or {}).get("digest") == cert.get("digest"))
        checks["issuance_row"] = row_ok
        if not row_ok:
            failures.append("no audit row records this certificate's issuance")

    if failures:
        verdict = "FAILED"
    elif not signature:
        verdict = "UNSIGNED"
    elif not pinned:
        verdict = "UNPINNED"
    else:
        verdict = "VERIFIED"
    return {"verdict": verdict, "checks": checks, "failures": failures}


__all__ = [
    "SEAL_SCHEMA", "certificate_digest", "key_paths", "load_or_create_signing_key",
    "seal_certificate", "verify_certificate",
]
