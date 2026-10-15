"""Persisted feature and setting defaults — ``.mini-ork/config/features.yaml``.

The registry (:mod:`mini_ork.features.registry`) declares what is *controllable*;
this module remembers what the user *chose*. :func:`apply_defaults` is the one
way it reaches ``os.environ`` — at startup by
:func:`mini_ork.cli.main._load_feature_defaults`, and again per read by
:func:`mini_ork.cli.board_cmd.build_payload`, which knows the home being read.
Precedence is, first writer wins:

    real environment  >  features.yaml  >  secrets.local.sh  >  engine default

The file has two sections. ``features:`` maps a registry id to the user's chosen
value and is materialized by :func:`resolve_defaults` (which, unlike the per-run
:func:`resolve_selection`, emits explicit *off* knobs so a default-ON feature can
actually be turned off). ``env:`` is a raw ``NAME -> value`` escape hatch for the
dozens of knobs that are settings rather than cost features; it is applied
verbatim, except that a name a registered feature owns always loses to
``features:`` so the two sections cannot fight.

Not secret. This file is world-readable (0644) and git-tracked; the credential
store it sits beside is neither. :func:`set_env` therefore refuses
credential-shaped names — the hub must not grow into a second secret store.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

import yaml

from .registry import (
    CONTROLLABLE_FEATURES,
    KIND_BOOL,
    KIND_COUNT,
    KIND_TEXT,
    PREMIUM_THRESHOLD,
    Feature,
    get_feature,
    is_off,
    resolve_defaults,
)

#: Written to the ``env:`` section so the premium knobs a user opted into are
#: not stripped at a run boundary that consults ``gate_env``.
ACCEPT_PREMIUM_ENV = "MO_ACCEPT_PREMIUM"

STORE_NAME = "features.yaml"
_SECTION_FEATURES = "features"
_SECTION_ENV = "env"

#: Refuse to parse anything larger than this — the file holds a handful of lines.
_MAX_BYTES = 256 * 1024

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CREDENTIAL = re.compile(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD)(?:$|_)", re.IGNORECASE)
_FORBIDDEN_IN_VALUE = ("\n", "\r", "\x00")


class StoreError(ValueError):
    """A user-facing refusal: bad id, bad value, or an unsafe name."""


# ── path ────────────────────────────────────────────────────────────────────


def store_path(home: str | Path | None = None) -> Path:
    """Resolve the store path without reading it.

    Mirrors :func:`mini_ork.dispatch.secrets.secret_store_path`: an explicit
    ``$MINI_ORK_FEATURES`` wins, else ``<home>/config/features.yaml`` with
    ``<home>`` defaulting to ``$MINI_ORK_HOME`` then ``.mini-ork``.
    """
    explicit = str(os.environ.get("MINI_ORK_FEATURES") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if home is None:
        home = os.environ.get("MINI_ORK_HOME") or ".mini-ork"
    return Path(home) / "config" / STORE_NAME


# ── read ────────────────────────────────────────────────────────────────────


def _empty() -> dict[str, dict[str, str]]:
    return {_SECTION_FEATURES: {}, _SECTION_ENV: {}}


def _coerce_section(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for name, raw in value.items():
        if raw is None:
            continue
        out[str(name)] = str(raw)
    return out


def load(path: str | Path | None = None) -> dict[str, dict[str, str]]:
    """Read the store. Never raises — a missing or malformed file is empty.

    A malformed file must not take down the CLI (``mini-ork help`` loads this),
    and an empty section is indistinguishable from an absent one by design.
    """
    target = Path(path) if path is not None else store_path()
    try:
        if target.stat().st_size > _MAX_BYTES:
            return _empty()
        text = target.read_text(encoding="utf-8")
    except OSError:
        return _empty()
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        return _empty()
    if not isinstance(raw, Mapping):
        return _empty()
    return {
        _SECTION_FEATURES: _coerce_section(raw.get(_SECTION_FEATURES)),
        _SECTION_ENV: _coerce_section(raw.get(_SECTION_ENV)),
    }


def _feature_owned_names() -> frozenset[str]:
    owned: set[str] = set()
    for feat in CONTROLLABLE_FEATURES.values():
        owned.update(feat.knobs)
        if feat.count_knob:
            owned.add(feat.count_knob)
    return frozenset(owned)


def effective_env(path: str | Path | None = None) -> dict[str, str]:
    """The env overrides this store contributes, ready for ``setdefault``.

    ``features:`` is materialized with ``accept_premium=True`` — a persisted
    premium feature *is* the opt-in (the store writes ``MO_ACCEPT_PREMIUM`` next
    to it), so dropping it here would make the toggle a lie. ``env:`` is applied
    verbatim, minus any name a registered feature owns.
    """
    data = load(path)
    env = resolve_defaults(data[_SECTION_FEATURES], accept_premium=True)
    owned = _feature_owned_names()
    for name, value in data[_SECTION_ENV].items():
        if name in owned:
            continue
        env[name] = value
    return env


# ── apply ───────────────────────────────────────────────────────────────────


#: The names this module has put into ``os.environ`` from a defaults file. A
#: name in here is *this module's*, not the operator's — which is what lets a
#: read of another home replace it (see :func:`apply_defaults`).
_applied: set[str] = set()


def apply_defaults(home: str | Path | None = None) -> set[str]:
    """Apply the store's overrides to ``os.environ``; return the names applied.

    The file belongs to one *home*, but a process can serve several: the IDE
    spawns every read as ``board page --home <project home>`` from the app's own
    working directory, and ``mini-ork serve`` answers for whichever home a
    request names. Applying the defaults once, for the process's own home, would
    answer a read of home B with home A's flags — so the home being read applies
    its own file, and the knobs an earlier home applied are retracted when the
    new one does not mention them.

    Precedence is unchanged — env > features.yaml > secrets.local.sh > engine
    default — because only names *this module* applied are ever replaced; a
    variable that was already in the environment is left alone.
    """
    overrides = effective_env(store_path(home))
    for name, value in overrides.items():
        if name in os.environ and name not in _applied:
            continue
        os.environ[name] = value
    retracted = _applied - set(overrides)
    for name in retracted:
        os.environ.pop(name, None)
    _applied.difference_update(retracted)
    _applied.update(overrides)
    return set(overrides)


# ── write ───────────────────────────────────────────────────────────────────


def save(data: Mapping[str, Any], path: str | Path | None = None) -> Path:
    """Atomically write the store, 0644, creating parent dirs as needed.

    Deliberately does NOT chmod the parent ``config/`` dir the way the secrets
    writer does — that directory's mode is owned by the credential file's
    fail-closed permission check.
    """
    target = Path(path) if path is not None else store_path()
    payload = {
        _SECTION_FEATURES: _coerce_section(data.get(_SECTION_FEATURES)),
        _SECTION_ENV: _coerce_section(data.get(_SECTION_ENV)),
    }
    for fid, value in payload[_SECTION_FEATURES].items():
        if get_feature(fid) is None:
            raise StoreError(f"unknown feature {fid!r}")
        _validate_value(fid, value)
    for name, value in payload[_SECTION_ENV].items():
        _validate_name(name)
        _validate_value(name, value)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(payload, sort_keys=True, default_flow_style=False)
    handle, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".features-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def _validate_name(name: str) -> None:
    if not _NAME.match(name):
        raise StoreError(f"not a valid env name: {name!r}")
    if _CREDENTIAL.search(name):
        raise StoreError(
            f"{name} looks like a credential; store it with `mini-ork providers configure`, "
            "not in features.yaml"
        )
    if name in _feature_owned_names():
        owner = next(
            (f.id for f in CONTROLLABLE_FEATURES.values() if name in f.knobs or name == f.count_knob),
            "?",
        )
        raise StoreError(f"{name} is controlled by the {owner!r} feature; use `features set {owner}`")


def _validate_value(name: str, value: str) -> None:
    for bad in _FORBIDDEN_IN_VALUE:
        if bad in value:
            raise StoreError(f"value for {name} must not contain a newline or NUL")


def _normalize_value(feat: Feature, value: str) -> str:
    """Canonicalize a user-chosen value for ``feat``, or refuse it."""
    if is_off(value):
        return "0"  # resolve_defaults maps this to the feature's explicit off knobs
    if feat.kind == KIND_BOOL:
        return "1"
    if feat.kind == KIND_COUNT:
        try:
            count = int(str(value).strip())
        except ValueError as exc:
            raise StoreError(f"{feat.id}: expected a whole number, got {value!r}") from exc
        if count < 0:
            raise StoreError(f"{feat.id}: count must be >= 0, got {count}")
        return str(count)
    if feat.kind == KIND_TEXT:
        if not str(value).split():
            return "0"
        return str(value)
    raise StoreError(f"{feat.id}: unknown kind {feat.kind!r}")  # pragma: no cover


def _is_premium(feat: Feature, value: str) -> bool:
    return feat.multiplier(value) > PREMIUM_THRESHOLD


def set_feature(
    feature_id: str,
    value: str,
    *,
    accept_premium: bool = False,
    path: str | Path | None = None,
) -> dict[str, dict[str, str]]:
    """Set one feature's persisted value. Returns the new store contents.

    A value that turns on a *premium* feature requires ``accept_premium`` and
    writes :data:`ACCEPT_PREMIUM_ENV` alongside it — without that opt-in a run
    boundary would strip the very knob being turned on.
    """
    feat = get_feature(feature_id)
    if feat is None:
        raise StoreError(f"unknown feature {feature_id!r}")
    normalized = _normalize_value(feat, value)
    data = load(path)
    if not is_off(normalized) and _is_premium(feat, normalized):
        if not accept_premium:
            multiplier = feat.multiplier(normalized)
            raise StoreError(
                f"{feature_id} is premium (×{multiplier:g}); re-run with --accept-premium to opt in "
                "project-wide"
            )
        data[_SECTION_ENV][ACCEPT_PREMIUM_ENV] = "1"
    data[_SECTION_FEATURES][feature_id] = normalized
    save(data, path)
    return data


def unset_feature(feature_id: str, *, path: str | Path | None = None) -> bool:
    """Remove one feature from the store. Returns whether it was present.

    Drops :data:`ACCEPT_PREMIUM_ENV` once no premium feature remains enabled, so
    the opt-in cannot outlive the reason for it.
    """
    data = load(path)
    if feature_id not in data[_SECTION_FEATURES]:
        return False
    del data[_SECTION_FEATURES][feature_id]
    if not enabled_premium(data):
        data[_SECTION_ENV].pop(ACCEPT_PREMIUM_ENV, None)
    save(data, path)
    return True


def enabled_premium(data: Mapping[str, Any]) -> list[str]:
    """Ids in ``data`` that are on and premium at their chosen value."""
    out: list[str] = []
    for fid, value in _coerce_section(data.get(_SECTION_FEATURES)).items():
        feat = get_feature(fid)
        if feat is not None and not is_off(value) and _is_premium(feat, value):
            out.append(fid)
    return out


def set_env(name: str, value: str, *, path: str | Path | None = None) -> dict[str, dict[str, str]]:
    """Set a raw ``env:`` override. Refuses credentials and feature-owned names."""
    _validate_name(name)
    _validate_value(name, value)
    data = load(path)
    data[_SECTION_ENV][name] = str(value)
    save(data, path)
    return data


def unset_env(name: str, *, path: str | Path | None = None) -> bool:
    """Remove a raw ``env:`` override. Returns whether it was present."""
    data = load(path)
    if name not in data[_SECTION_ENV]:
        return False
    del data[_SECTION_ENV][name]
    save(data, path)
    return True


# ── provenance ──────────────────────────────────────────────────────────────


def _primary_knob(feat: Feature) -> str:
    if feat.count_knob:
        return feat.count_knob
    if feat.knobs:
        return next(iter(feat.knobs))
    return f"MO_{feat.id.upper()}"


def current(
    feat: Feature,
    *,
    environ: Mapping[str, str] | None = None,
    data: Mapping[str, Any] | None = None,
    secrets: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Where a feature's value comes from right now, for the page to render.

    Attribution must be read from the *files*, never from the merged
    ``os.environ`` (which by page-build time already contains the store's own
    values). Source is one of ``env`` / ``secrets`` / ``project`` / ``default``.
    """
    environ = os.environ if environ is None else environ
    data = {} if data is None else data
    knob = _primary_knob(feat)

    if environ.get(knob) not in (None, ""):
        value = str(environ[knob])
        source = "env"
    elif data.get(_SECTION_FEATURES, {}).get(feat.id) is not None:
        value = str(data[_SECTION_FEATURES][feat.id])
        source = "project"
    elif data.get(_SECTION_ENV, {}).get(knob) is not None:
        value = str(data[_SECTION_ENV][knob])
        source = "project"
    elif secrets and secrets.get(knob) not in (None, ""):
        value = str(secrets[knob])
        source = "secrets"
    else:
        # Not the registry's ``default``: that is what the wizard suggests, and
        # for two features the engine's own fallback differs (see
        # ``Feature.effective_default``). Reporting the suggestion here would
        # claim a cost the engine is not paying.
        value = feat.effective_default
        source = "default"

    off = is_off(value)
    return {
        "value": "off" if off else value,
        "source": source,
        "knob": knob,
        "off": off,
        # multiplier on the canonical off token, not the display string
        "multiplier": feat.multiplier("0" if off else value),
        "wizard_default": feat.default,
        "engine_default": feat.effective_default,
    }
