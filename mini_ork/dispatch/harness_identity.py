"""Which harness served a lane, and which release of it.

The same model behind a different harness — or behind a different release of
the same CLI — is a different system: swapping the harness alone moves cost and
token counts by multiples. A ledger row that names only the lane cannot say
which one ran, so neither a result nor a regression it backs can be reproduced.

``identity(lane)`` names it::

    {"harness": "claude-code", "kind": "anthropic-native",
     "binary": "claude", "version": "2.1.3 (Claude Code)"}

* ``harness`` — what runs the agent loop: a vendor CLI (``claude-code``,
  ``codex``, ``opencode``), an ``executable`` script, or one of mini-ork's own
  transports (``mini-ork/openai-compat``, ``mini-ork/uhp``, …);
* ``kind``    — the lane's ``providers.yaml`` kind;
* ``binary``  — the CLI that harness spawns (the script path for
  ``executable``), ``""`` for a transport mini-ork runs itself;
* ``version`` — the CLI's ``--version`` line, ``sha256:<12 hex>`` of an
  ``executable`` script, ``mini-ork <version>`` for an in-process transport,
  or ``""`` when the binary is missing or will not say.

A ``--version`` call costs a process spawn, so answers are cached in
``$MINI_ORK_HOME/harness-versions.json`` keyed by the binary's resolved path,
size and mtime: an upgrade invalidates its own entry, and the steady state is
one ``stat`` per call. Without ``MINI_ORK_HOME`` the answer is kept in memory
only — nothing is ever written relative to the cwd.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

from ..context import context_env
from ..version import __version__

_CACHE_NAME = "harness-versions.json"
_VERSION_TIMEOUT_S = 10.0
_MEMO: dict[str, str] = {}

# providers.yaml kind -> (harness, binary). An empty binary is a transport
# mini-ork runs itself, so its version is mini-ork's.
_VENDOR_CLI: dict[str, tuple[str, str]] = {
    "anthropic-native": ("claude-code", "claude"),
    "anthropic-compat": ("claude-code", "claude"),
    "codex-native": ("codex", "codex"),
    "opencode-native": ("opencode", "opencode"),
}


def _read_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(path: Path, cache: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _ask_version(path: str, timeout_s: float) -> str:
    try:
        proc = subprocess.run(
            [path, "--version"], capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=timeout_s, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in (proc.stdout or proc.stderr or "").splitlines():
        if line.strip():
            return line.strip()[:120]
    return ""


def binary_version(path: str, *, home: str | os.PathLike | None = None,
                   timeout_s: float = _VERSION_TIMEOUT_S) -> str:
    """``path --version``, cached until the binary behind ``path`` changes."""
    try:
        real = os.path.realpath(path)
        st = os.stat(real)
    except OSError:
        return ""
    key = f"{real}|{st.st_size}|{st.st_mtime_ns}"
    if key in _MEMO:
        return _MEMO[key]
    # No configured home means no disk cache: a relative ".mini-ork" would land
    # in whatever the cwd is — a lane's target repo, or a test's checkout.
    home = str(home or context_env("MINI_ORK_HOME", "") or "")
    cache_path = Path(home) / _CACHE_NAME if home else None
    cache = _read_cache(cache_path) if cache_path else {}
    hit = cache.get(real)
    if isinstance(hit, dict) and hit.get("key") == key:
        version = str(hit.get("version") or "")
    else:
        # A binary that will not answer is cached too, so it costs one
        # timeout per release rather than one per dispatch.
        version = _ask_version(path, timeout_s)
        if cache_path:
            cache[real] = {"key": key, "version": version, "at": int(time.time())}
            _write_cache(cache_path, cache)
    _MEMO[key] = version
    return version


def _file_digest(path: Path) -> str:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def identity(lane: str, root: str | os.PathLike | None = None, *,
             environment: Mapping[str, str] | None = None) -> dict | None:
    """The harness serving ``lane``, or ``None`` when the registry has no such lane."""
    from .providers import _load_providers_registry, mini_ork_root

    try:
        entry = _load_providers_registry(root).get(lane)
    except ValueError:
        return None
    if not isinstance(entry, Mapping):
        return None
    env = os.environ if environment is None else environment
    kind = str(entry.get("kind") or "").strip()
    if kind in _VENDOR_CLI:
        harness, binary = _VENDOR_CLI[kind]
        path = shutil.which(binary, path=env.get("PATH"))
        version = binary_version(path, home=env.get("MINI_ORK_HOME")) if path else ""
    elif kind == "executable":
        script = Path(str(entry.get("script") or ""))
        if not script.is_absolute():
            script = mini_ork_root(root) / script
        harness, binary, version = "executable", str(script), _file_digest(script)
    else:
        harness, binary, version = f"mini-ork/{kind or 'unknown'}", "", f"mini-ork {__version__}"
    return {"harness": harness, "kind": kind, "binary": binary, "version": version}


__all__ = ["binary_version", "identity"]
