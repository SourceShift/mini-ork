"""Certificate construction for ``mini-ork certify``.

The certificate is the durable output of a certify run. Schema
``mini-ork.certificate/v1`` — every key is required and load-bearing; downstream
tools (the dashboard, the audit trail) parse by name.

The digest is computed over the canonical JSON of every other key with
``sort_keys=True`` and ``separators=(",", ":")``. Re-deriving the digest
elsewhere must yield the same bytes — do NOT insert floats with non-stable
representations, do NOT round-trip through Python's repr.

The ``files`` list is parsed from the ``+++ b/...`` lines of the patch. Empty
list is acceptable for an empty patch (but the CLI never reaches
``build_certificate`` with one — it exits UNVERIFIED first).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone


def _canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _sha256_hex(s: str | bytes) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8")
    return hashlib.sha256(s).hexdigest()


def _parse_patch_files(patch: str) -> list[str]:
    out: list[str] = []
    for line in patch.splitlines():
        # `git diff` writes the new-side path on the `+++ b/<path>` line. We
        # skip the `/dev/null` sentinel (deletions) and the synthetic `/dev/null`
        # of a pure-empty patch.
        if line.startswith("+++ b/"):
            p = line[6:]
            if p and p != "/dev/null":
                out.append(p)
    # Deduplicate, preserving order — the same path can appear in rename
    # detection headers multiple times.
    seen: set[str] = set()
    unique: list[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def build_certificate(
    *,
    verdict: str,
    reason: str,
    repo_path: str,
    remote: str | None,
    base_sha: str,
    head_sha: str | None,
    patch_text: str,
    issue_text: str,
    oracle_method: str,
    mr_n: int,
    proven_bar: float,
    model: str,
    image: str,
    poc_plus: str | None,
    mr_pass_rate: float | None,
    invariants: list,
    cost_usd: float | None,
    llm_calls: int,
    duration_s: float,
    context_files: list | None = None,
) -> dict:
    """Assemble the certificate dict. Caller writes it to ``--out``.

    The digest is recomputed by hashing the canonical JSON of every key
    EXCEPT ``digest`` itself, in the exact schema order. Adding or removing
    a key silently changes the digest — that's the point.

    ``context_files`` (optional): repo-relative paths of the .py files whose
    BASE source was shown to the oracle's probe / invariant generators. When
    supplied, it lands inside ``method.context_files`` so the audit trail can
    confirm which files the oracle saw. Default ``None`` omits the key —
    callers that don't pass a CodeContext (legacy / SWE-bench-style) keep the
    prior certificate shape byte-for-byte (modulo the absent key).
    """
    files = _parse_patch_files(patch_text)
    method: dict = {
        "oracle": oracle_method,
        "mr_n": mr_n,
        "proven_bar": proven_bar,
        "model": model,
        "image": image,
    }
    if context_files is not None:
        method["context_files"] = list(context_files)
    base: dict = {
        "schema": "mini-ork.certificate/v1",
        "id": str(uuid.uuid4()),
        # ``+00:00`` -> ``Z`` so JSON consumers don't see a non-UTC offset.
        "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "verdict": verdict,
        "reason": reason,
        "repo": {
            "path": repo_path,
            "remote": remote,
            "base": base_sha,
            "head": head_sha,
        },
        "change": {
            "sha256": _sha256_hex(patch_text),
            "files": files,
        },
        "claim": {
            "sha256": _sha256_hex(issue_text),
            "summary": issue_text[:200],
        },
        "method": method,
        "evidence": {
            "probe": poc_plus,
            "mr_pass_rate": mr_pass_rate,
            "invariants": invariants,
        },
        "cost": {
            "usd": cost_usd,
            "llm_calls": llm_calls,
        },
        "duration_s": duration_s,
    }
    digest = hashlib.sha256(_canonical(base).encode("utf-8")).hexdigest()
    return {**base, "digest": digest}


__all__ = ["build_certificate"]
