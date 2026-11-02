"""The laptop tier as one file: ``config/profile.simple.yaml``.

mini-ork has to be usable on one machine with no daemon and no cloud account,
AND safely in a hostile deployment. Those are different configs, not different
products, so the choice is one environment variable — ``MINI_ORK_PROFILE`` —
and one file per tier. This module loads the chosen profile, fills the knobs the
tier sets, and refuses to guess when the requested file is missing or broken.

**Fill-if-absent only.** A profile default never overwrites a value the operator
set. That is what lets a user start on ``simple`` and move to a vault-backed
production config without rewriting call sites: the environment still wins, the
profile only supplies what would otherwise be an unset knob.

**Fail closed.** ``MINI_ORK_PROFILE=simple`` with no readable
``config/profile.<name>.yaml`` raises :class:`ProfileError` — a clear error, not
a silent fall-through to built-in defaults that happen to look similar. With
``MINI_ORK_PROFILE`` unset (the default) this module does nothing at all.
"""

from __future__ import annotations

import os
from typing import Mapping, MutableMapping

PROFILE_ENV = "MINI_ORK_PROFILE"

#: path -> (mtime, parsed document). The file is tiny; the cache exists because
#: ``RunContext.apply`` runs on nearly every command.
_CACHE: dict[str, tuple[float, dict]] = {}


class ProfileError(RuntimeError):
    """The requested profile is missing or malformed."""


def profile_path(name: str, root: str) -> str:
    return os.path.join(root, "config", f"profile.{name}.yaml")


def load(name: str, root: str) -> dict:
    """The parsed profile, or :class:`ProfileError` naming the file it wanted."""
    path = profile_path(name, root)
    try:
        mtime = os.path.getmtime(path)
    except OSError as exc:
        raise ProfileError(
            f"profile {name!r} requested but {path} is not readable: {exc}") from None
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        import yaml  # noqa: PLC0415 — only a profile-using command needs it

        with open(path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle) or {}
    except Exception as exc:  # noqa: BLE001 — any parse failure is the same refusal
        raise ProfileError(f"profile {path} is unreadable: {exc}") from None
    if not isinstance(document, dict):
        raise ProfileError(f"profile {path} must be a mapping, got {type(document).__name__}")
    _CACHE[path] = (mtime, document)
    return document


def defaults(name: str, root: str) -> dict[str, str]:
    """``defaults:`` as a string→string map. A non-mapping is a ProfileError."""
    document = load(name, root)
    raw = document.get("defaults") or {}
    if not isinstance(raw, dict):
        raise ProfileError(f"profile {profile_path(name, root)}: defaults must be a mapping")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, bool):
            out[str(key)] = "1" if value else "0"
        elif value is None:
            continue
        else:
            out[str(key)] = str(value)
    return out


def required(name: str, root: str) -> tuple[list[tuple[str, str]], list[list[str]]]:
    """``(single knobs, any-of groups)`` the tier demands be set explicitly.

    Each single knob carries the reason it is required; a group is satisfied by
    any one member being non-empty.
    """
    document = load(name, root)
    block = document.get("required") or {}
    if not isinstance(block, dict):
        raise ProfileError(
            f"profile {profile_path(name, root)}: required must be a mapping")
    raw_all = block.get("all") or {}
    single: list[tuple[str, str]] = []
    if isinstance(raw_all, dict):          # `all: {KEY: why it is required}`
        single.extend((str(key), str(reason or "")) for key, reason in raw_all.items())
    elif isinstance(raw_all, (list, tuple)):   # `all: [KEY, …]`
        single.extend((str(key), "") for key in raw_all)
    else:
        raise ProfileError(
            f"profile {profile_path(name, root)}: required.all must be a mapping or list")
    groups: list[list[str]] = []
    for group in block.get("any_of") or []:
        if isinstance(group, (list, tuple)):
            groups.append([str(name_) for name_ in group])
    return single, groups


def fill(env: MutableMapping[str, str], *, name: str, root: str) -> dict[str, str]:
    """Set each profile default whose knob is unset. Returns what was applied."""
    applied: dict[str, str] = {}
    for key, value in defaults(name, root).items():
        if str(env.get(key) or "").strip():
            continue
        env[key] = value
        applied[key] = value
    return applied


def apply_if_requested(env: MutableMapping[str, str] | None = None,
                       root: str | None = None) -> dict[str, str]:
    """Fill the active profile's defaults. No-op when ``MINI_ORK_PROFILE`` is unset."""
    target = os.environ if env is None else env
    name = str(target.get(PROFILE_ENV) or "").strip()
    if not name:
        return {}
    base = root or str(target.get("MINI_ORK_ROOT") or "").strip()
    if not base:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return fill(target, name=name, root=base)


def active(env: Mapping[str, str] | None = None) -> str:
    return str((os.environ if env is None else env).get(PROFILE_ENV) or "").strip()
