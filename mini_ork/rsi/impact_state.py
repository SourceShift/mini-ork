"""The sealed acceptor's persistent state: secret, epochs, looks, canary.

Gate G12 tests a candidate on a *sealed* benchmark split, so two things must
survive a cycle: the secret that keys the dev/sealed split, and how much of the
error budget has been spent. The counter is the point — each sealed look costs
budget, and a corrupt or silently-reset counter would let an unattended loop
keep looking until something passes. Every load or save path therefore fails
closed, naming the file rather than guessing a safe default.

Two tiers, one file format: the simple tier keeps a ``secret`` under
``<home>/rsi/impact/`` at mode ``0600``; the secure tier reads the same value
from the vault and never falls back to the file. A missing secure secret is an
error, never an empty string.

No callers yet — RRL-4b wires this into the gate.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "DEFAULT_SECRET_NAME",
    "ImpactStateError",
    "STATE_VERSION",
    "bench_state",
    "canary_scores",
    "corpus_hash",
    "impact_dir",
    "load_secret",
    "load_state",
    "new_state",
    "record_canary",
    "record_look",
    "rotate",
    "save_state",
    "state_path",
]

STATE_VERSION = 1
DEFAULT_SECRET_NAME = "MO_RSI_IMPACT_SECRET"


class ImpactStateError(RuntimeError):
    """A value the acceptor cannot run with. Never carries a secret value."""


def impact_dir(home: str | Path) -> Path:
    """Where the secret and the look counter live."""
    return Path(home) / "rsi" / "impact"


def state_path(home: str | Path) -> Path:
    return impact_dir(home) / "state.json"


# --------------------------------------------------------------------------
# The secret
# --------------------------------------------------------------------------


def _secure_secret(
    secure_store: str, secret_name: str, environ: Mapping[str, str] | None,
) -> bytes:
    """Read ``secret_name`` from the vault; never touch the filesystem.

    The vault import is lazy and resolved at call time: the simple tier must
    not pay for ``yaml``/``urllib``, and a test (or an operator) that patches
    ``mini_ork.dispatch.vault.vault_environment`` must be honoured.
    """
    from mini_ork.dispatch import vault as _vault

    try:
        value = _vault.vault_environment(environ).get(secret_name)
    except Exception:  # noqa: BLE001 — any vault failure is "not available"
        value = None
    if not isinstance(value, str) or not value.strip():
        # The value itself is never interpolated: only the store and the name.
        raise ImpactStateError(
            f"secure tier: secret {secret_name} is not available from {secure_store}")
    return value.strip().encode()


def _read_secret_file(path: Path) -> bytes:
    if path.stat().st_mode & 0o077:
        raise ImpactStateError(f"{path}: permissions too open (expected 0600)")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ImpactStateError(f"{path}: empty secret")
    return text.encode()


def load_secret(
    home: str | Path,
    *,
    secure_store: str = "",
    secret_name: str = DEFAULT_SECRET_NAME,
    environ: Mapping[str, str] | None = None,
) -> bytes:
    """The split key. Secure tier if ``secure_store`` names a store, else local file."""
    if secure_store.strip():
        return _secure_secret(secure_store, secret_name, environ)

    path = impact_dir(home) / "secret"
    if not path.exists():
        directory = impact_dir(home)
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        token = secrets.token_hex(32).encode()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Another process won the creation race; its content is the secret.
            pass
        else:
            try:
                os.write(fd, token)
            finally:
                os.close(fd)
    return _read_secret_file(path)


# --------------------------------------------------------------------------
# The corpus fingerprint
# --------------------------------------------------------------------------


def corpus_hash(items: Mapping[str, str]) -> str:
    """A stable, order-independent fingerprint of the dev/sealed split."""
    payload = json.dumps(sorted(items.items()), separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


# --------------------------------------------------------------------------
# The state file
# --------------------------------------------------------------------------


def new_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "benchmarks": {}}


def load_state(home: str | Path) -> dict[str, Any]:
    """Read the counter. A corrupt file is an error, never a fresh start."""
    path = state_path(home)
    if not path.exists():
        return new_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImpactStateError(
            f"{path}: unreadable impact state ({type(exc).__name__})") from None
    if not isinstance(raw, dict):
        raise ImpactStateError(f"{path}: impact state is not an object")
    version = raw.get("version")
    if type(version) is not int or version != STATE_VERSION:
        raise ImpactStateError(
            f"{path}: unsupported impact state version {version!r}")
    if not isinstance(raw.get("benchmarks"), dict):
        raise ImpactStateError(f"{path}: impact state benchmarks is not an object")
    return raw


def save_state(home: str | Path, state: Mapping[str, Any]) -> Path:
    """Atomically replace ``state.json``; the temp file is mode ``0600`` too."""
    directory = impact_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    path = state_path(home)
    text = json.dumps(state, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# --------------------------------------------------------------------------
# Per-benchmark entries
# --------------------------------------------------------------------------


def _entry(state: dict[str, Any], bench: str) -> dict[str, Any]:
    entry = state.get("benchmarks", {}).get(bench)
    if entry is None:
        raise ImpactStateError(f"no impact state for {bench}")
    return entry


def bench_state(state: dict[str, Any], bench: str, corpus: str) -> dict[str, Any]:
    """The benchmark's entry, rotating the epoch when the corpus changed.

    A rotation keeps ``canary``: main's history is not the candidate's, and
    throwing it away on every corpus refresh would blind the gate.
    """
    benchmarks = state.setdefault("benchmarks", {})
    entry = benchmarks.get(bench)
    if entry is None:
        entry = {"epoch": 1, "corpus": corpus, "looks": 0, "accepted": 0,
                 "rejected": 0, "canary": []}
        benchmarks[bench] = entry
    elif entry.get("corpus") != corpus:
        entry["epoch"] = int(entry.get("epoch", 0)) + 1
        entry["corpus"] = corpus
        entry["looks"] = 0
        entry["accepted"] = 0
        entry["rejected"] = 0
    return entry


def rotate(state: dict[str, Any], bench: str) -> dict[str, Any]:
    entry = _entry(state, bench)
    entry["epoch"] = int(entry.get("epoch", 0)) + 1
    entry["looks"] = 0
    entry["accepted"] = 0
    entry["rejected"] = 0
    return entry


def record_look(state: dict[str, Any], bench: str, *, accepted: bool) -> int:
    """Spend one sealed look and return the epoch's new look count."""
    entry = _entry(state, bench)
    entry["looks"] = int(entry.get("looks", 0)) + 1
    if accepted:
        entry["accepted"] = int(entry.get("accepted", 0)) + 1
    else:
        entry["rejected"] = int(entry.get("rejected", 0)) + 1
    return entry["looks"]


def record_canary(
    state: dict[str, Any],
    bench: str,
    *,
    sha: str,
    score: float,
    at: int,
    keep: int = 200,
) -> None:
    """Record main's canary score; the same ``sha`` updates in place."""
    if keep < 1:
        raise ImpactStateError("record_canary: keep must be a positive integer")
    entry = _entry(state, bench)
    canary = entry.setdefault("canary", [])
    item = {"sha": sha, "score": float(score), "at": int(at)}
    if canary and canary[-1].get("sha") == sha:
        canary[-1] = item
    else:
        canary.append(item)
    if len(canary) > keep:
        del canary[: len(canary) - keep]


def canary_scores(state: dict[str, Any], bench: str) -> list[float]:
    entry = state.get("benchmarks", {}).get(bench)
    if entry is None:
        return []
    return [float(item["score"]) for item in entry.get("canary", [])]
